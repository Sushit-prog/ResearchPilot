from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.models.evidence import Evidence
from app.models.tool_io import validate_http_url

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CLAIM_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "what", "how", "why", "when", "where", "from",
        "that", "this", "are", "was", "were", "its", "into", "than", "then", "who",
        "which", "about", "over", "under", "between", "during", "after", "before",
    }
)


def significant_tokens(text: str) -> list[str]:
    """Lowercase tokens in order, stopwords and short tokens dropped."""
    return [
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) >= 2 and token not in _STOPWORDS
    ]


def term_set(text: str) -> frozenset[str]:
    """Query/goal term set: lowercase tokens, stopwords and short tokens dropped."""
    return frozenset(significant_tokens(text))


def term_overlap(terms: frozenset[str], text: str) -> float:
    """Fraction of `terms` present in `text`, clamped to [0, 1]."""
    if not terms:
        return 0.0
    overlap = len(terms & term_set(text)) / len(terms)
    return round(min(1.0, max(0.0, overlap)), 3)


def normalize_url(url: str) -> str:
    """§10 canonical URL: scheme/host case folded, default port and fragment
    dropped, trailing slash stripped, utm_* removed, query sorted.

    Raises ValueError for non-http(s) URLs, missing hosts, embedded
    credentials, or malformed ports (§26 — nothing non-http escapes).
    """
    raw = validate_http_url(url)
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    port = parsed.port
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    pairs.sort()
    return urlunparse((scheme, netloc, path, "", urlencode(pairs), ""))


def normalize_title(title: str) -> str:
    return " ".join(title.split())


def normalize_claim(claim: str) -> str:
    """Casefolded, punctuation/whitespace-collapsed claim form (§10)."""
    return " ".join(_CLAIM_TOKEN_RE.findall(claim.casefold()))


def claim_tokens(claim: str) -> frozenset[str]:
    return frozenset(_CLAIM_TOKEN_RE.findall(claim.casefold()))


def tokens_overlap(left: frozenset[str], right: frozenset[str]) -> float:
    """Token-set Jaccard overlap; two empty sets count as identical."""
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def claim_overlap(left: str, right: str) -> float:
    """Token-set Jaccard overlap of two normalized claims (§10)."""
    return tokens_overlap(claim_tokens(left), claim_tokens(right))


def evidence_violations(
    evidence: Evidence, *, source_keys: set[str] | None = None
) -> list[str]:
    """§4.11 structural checks — invalid evidence must never reach synthesis."""
    violations: list[str] = []
    if not evidence.claim.strip():
        violations.append("empty claim")
    if not evidence.extracted_text.strip():
        violations.append("empty excerpt")
    if evidence.source_url is not None:
        try:
            key = normalize_url(evidence.source_url)
        except ValueError:
            violations.append(f"invalid source_url: {evidence.source_url!r}")
        else:
            if source_keys is not None and key not in source_keys:
                violations.append("source not among collected sources")
    if not 0.0 <= evidence.relevance_score <= 1.0:
        violations.append("relevance_score out of range")
    if not 0.0 <= evidence.confidence <= 1.0:
        violations.append("confidence out of range")
    return violations
