from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.evidence import Evidence
from app.reliability.validation import (
    claim_overlap,
    evidence_violations,
    normalize_claim,
    normalize_title,
    normalize_url,
    term_overlap,
    term_set,
)

FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXED_URL = "https://example.com/a"


def make_evidence(
    evidence_id: str = "ev-1",
    claim: str = "A real sentence.",
    *,
    url: str | None = FIXED_URL,
    extracted: str = "A real sentence.",
    relevance: float = 0.9,
    confidence: float = 0.8,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        claim=claim,
        source_url=url,
        source_title="Example",
        extracted_text=extracted,
        relevance_score=relevance,
        confidence=confidence,
        retrieved_at=FIXED_TIME,
    )


def test_scheme_and_host_case_folded_path_preserved() -> None:
    assert normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"


def test_trailing_slash_stripped_including_root() -> None:
    assert normalize_url("https://example.com/a/") == "https://example.com/a"
    assert normalize_url("https://example.com/") == "https://example.com"


def test_query_sorted() -> None:
    assert normalize_url("https://example.com/a?b=2&a=1") == "https://example.com/a?a=1&b=2"


def test_utm_params_stripped() -> None:
    normalized = normalize_url("https://example.com/a?utm_source=x&utm_medium=y&a=1")
    assert normalized == "https://example.com/a?a=1"


def test_fragment_dropped() -> None:
    assert normalize_url("https://example.com/a#section-2") == "https://example.com/a"


def test_default_ports_dropped_custom_port_kept() -> None:
    assert normalize_url("http://example.com:80/a") == "http://example.com/a"
    assert normalize_url("https://example.com:443/a") == "https://example.com/a"
    assert normalize_url("https://example.com:8443/a") == "https://example.com:8443/a"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "https://user:pass@example.com/x",
        "",
        "not a url",
    ],
)
def test_non_http_urls_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(url)


def test_title_whitespace_collapsed() -> None:
    assert normalize_title("  A   Source \n Here ") == "A Source Here"


def test_claim_normalization_casefolds_and_collapses_punctuation() -> None:
    assert normalize_claim("Revenue grew 12%!  ") == "revenue grew 12"


def test_claim_overlap_identical_and_near_distinct() -> None:
    assert claim_overlap("Revenue grew 12%.", "  revenue grew  12 ") == 1.0
    near = claim_overlap(
        "Revenue grew 12% last quarter",
        "Revenue grew 15% last quarter",
    )
    assert near < 0.9


def test_term_set_drops_stopwords_and_short_tokens() -> None:
    assert term_set("the Python 3.14 release") == frozenset({"python", "14", "release"})


def test_term_overlap_fraction_of_query_terms_found() -> None:
    terms = term_set("python release date")
    assert term_overlap(terms, "the python release date is here") == 1.0
    assert term_overlap(terms, "unrelated orbital mechanics text") == 0.0
    assert term_overlap(frozenset(), "anything") == 0.0


def test_evidence_violations_accepts_structurally_valid_evidence() -> None:
    evidence = make_evidence()
    keys = {normalize_url(FIXED_URL)}
    assert evidence_violations(evidence, source_keys=keys) == []


def test_evidence_violations_flag_empty_and_unknown_sources() -> None:
    evidence = make_evidence(claim="   ", extracted="   ")
    assert "empty claim" in evidence_violations(evidence, source_keys=set())
    assert "empty excerpt" in evidence_violations(evidence, source_keys=set())

    sourced = make_evidence(url="https://other.example/x")
    violations = evidence_violations(sourced, source_keys={normalize_url(FIXED_URL)})
    assert "source not among collected sources" in violations


def test_evidence_violations_flag_invalid_url() -> None:
    evidence = make_evidence(url="javascript:x")
    violations = evidence_violations(evidence, source_keys=set())
    assert any(violation.startswith("invalid source_url") for violation in violations)
