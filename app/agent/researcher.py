from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlparse

from app.agent.executor import StepOutcome
from app.config import Config
from app.models.events import EventKind, FailureKind, ToolResult
from app.models.evidence import Evidence, NumericFact, Source, SourceQuality, SourceStatus
from app.models.plan import PlanStep
from app.models.tool_io import CalculatorOutput, FetchOutput, SearchOutput, SearchResultItem
from app.observability import EventEmitter
from app.reliability.runner import Runner
from app.reliability.validation import normalize_url, significant_tokens, term_overlap, term_set
from app.tools.base import ToolRegistry

_PRIMARY_SUFFIXES = (".gov", ".edu", ".mil")
_PRIMARY_HOSTS = frozenset(
    {"arxiv.org", "w3.org", "ietf.org", "rfc-editor.org", "iana.org"}
)
_REPUTABLE_HOSTS = frozenset(
    {
        "en.wikipedia.org",
        "developer.mozilla.org",
        "stackoverflow.com",
        "github.com",
        "pypi.org",
        "docs.python.org",
        "docs.rs",
        "npmjs.com",
    }
)
_TIER_RANK = {
    SourceQuality.PRIMARY: 2,
    SourceQuality.REPUTABLE: 1,
    SourceQuality.UNKNOWN: 0,
}

_WEB_CONFIDENCE = 0.7
_DERIVED_CONFIDENCE = 0.9
_CLAIM_LIMIT = 240
_EXCERPT_LIMIT = 500


class Researcher:
    """Real StepHandler: plan step in, StepOutcome out.

    Tool dispatch is by step.tool — web_search runs the §4.6 research unit
    (search → rank → fetch → evidence), every other tool runs as a direct
    step through the same Runner seam.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        config: Config,
        emitter: EventEmitter,
        runner: Runner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._config = config
        self._emitter = emitter
        self._runner = runner if runner is not None else Runner(config, emitter, clock=clock)

    async def handle(self, step: PlanStep) -> StepOutcome:
        self._emitter.emit(EventKind.TOOL_SELECTED, tool=step.tool, step_id=step.id)
        if step.tool == "web_search":
            return await self._research(step)
        if step.tool == "webpage_fetch":
            return await self._direct_fetch(step)
        if step.tool == "calculator":
            return await self._direct_calculator(step)
        if step.tool == "failure_simulator":
            return await self._direct_simulator(step)
        return StepOutcome(
            error=f"no handler registered for tool {step.tool!r}",
            failure_kind=FailureKind.VALIDATION_ERROR,
        )

    async def _research(self, step: PlanStep) -> StepOutcome:
        search = await self._runner.call(
            self._registry.get("web_search"),
            step_id=step.id,
            call_ordinal=1,
            arguments=step.arguments,
        )
        if not search.success:
            return StepOutcome(
                tool_results=[search],
                error=search.error,
                failure_kind=search.failure_kind,
            )
        query = str(step.arguments.get("query", ""))
        query_terms = term_set(query)
        results = SearchOutput.model_validate(search.output).results
        candidates = _rank(results, query_terms)[: self._config.candidate_cap]

        tool_results: list[ToolResult] = [search]
        evidence: list[Evidence] = []
        sources: list[Source] = []
        for index, candidate in enumerate(candidates):
            ordinal = index + 2
            fetch = await self._runner.call(
                self._registry.get("webpage_fetch"),
                step_id=step.id,
                call_ordinal=ordinal,
                arguments={"url": candidate.url},
            )
            tool_results.append(fetch)
            if fetch.success:
                fetched = FetchOutput.model_validate(fetch.output)
                sources.append(
                    Source(
                        url=fetched.final_url,
                        domain=_domain(fetched.final_url),
                        title=fetched.title or candidate.title,
                        quality=_quality(_domain(fetched.final_url)),
                        status=SourceStatus.AVAILABLE,
                        retrieved_at=fetch.started_at,
                    )
                )
                found = _web_evidence(
                    evidence_id=f"{step.id}:{ordinal}",
                    step_id=step.id,
                    url=fetched.final_url,
                    title=fetched.title or candidate.title,
                    text=fetched.text,
                    query_terms=query_terms,
                    retrieved_at=fetch.started_at,
                )
                if found is not None:
                    evidence.append(found)
                    self._emitter.emit(
                        EventKind.EVIDENCE_ADDED,
                        tool="webpage_fetch",
                        step_id=step.id,
                        data={"evidence_id": found.evidence_id, "url": found.source_url},
                    )
                else:
                    self._emitter.emit(
                        EventKind.EVIDENCE_REJECTED,
                        tool="webpage_fetch",
                        step_id=step.id,
                        message="no extractable text in fetched page",
                        data={"url": fetched.final_url},
                    )
            else:
                sources.append(
                    Source(
                        url=candidate.url,
                        domain=candidate.domain,
                        title=candidate.title,
                        quality=_quality(candidate.domain),
                        status=SourceStatus.UNAVAILABLE,
                        unavailable_reason=fetch.error,
                    )
                )
                self._emitter.emit(
                    EventKind.SOURCE_UNAVAILABLE,
                    tool="webpage_fetch",
                    step_id=step.id,
                    status=fetch.failure_kind.value if fetch.failure_kind else None,
                    message=fetch.error,
                    data={"url": candidate.url},
                )
                if index < len(candidates) - 1:
                    next_candidate = candidates[index + 1]
                    self._emitter.emit(
                        EventKind.CANDIDATE_ADVANCED,
                        tool="webpage_fetch",
                        step_id=step.id,
                        message=f"{candidate.url} -> {next_candidate.url}",
                        data={"from": candidate.url, "to": next_candidate.url},
                    )
        return StepOutcome(tool_results=tool_results, evidence=evidence, sources=sources)

    async def _direct_fetch(self, step: PlanStep) -> StepOutcome:
        fetch = await self._runner.call(
            self._registry.get("webpage_fetch"),
            step_id=step.id,
            call_ordinal=1,
            arguments=step.arguments,
        )
        if not fetch.success:
            return StepOutcome(
                tool_results=[fetch],
                error=fetch.error,
                failure_kind=fetch.failure_kind,
            )
        fetched = FetchOutput.model_validate(fetch.output)
        domain = _domain(fetched.final_url)
        sources = [
            Source(
                url=fetched.final_url,
                domain=domain,
                title=fetched.title,
                quality=_quality(domain),
                status=SourceStatus.AVAILABLE,
                retrieved_at=fetch.started_at,
            )
        ]
        found = _web_evidence(
            evidence_id=f"{step.id}:1",
            step_id=step.id,
            url=fetched.final_url,
            title=fetched.title,
            text=fetched.text,
            query_terms=term_set(f"{step.objective} {step.expected_output}"),
            retrieved_at=fetch.started_at,
        )
        evidence: list[Evidence] = []
        if found is not None:
            evidence.append(found)
            self._emitter.emit(
                EventKind.EVIDENCE_ADDED,
                tool="webpage_fetch",
                step_id=step.id,
                data={"evidence_id": found.evidence_id, "url": found.source_url},
            )
        else:
            self._emitter.emit(
                EventKind.EVIDENCE_REJECTED,
                tool="webpage_fetch",
                step_id=step.id,
                message="no extractable text in fetched page",
                data={"url": fetched.final_url},
            )
        return StepOutcome(tool_results=[fetch], evidence=evidence, sources=sources)

    async def _direct_calculator(self, step: PlanStep) -> StepOutcome:
        calc = await self._runner.call(
            self._registry.get("calculator"),
            step_id=step.id,
            call_ordinal=1,
            arguments=step.arguments,
        )
        if not calc.success:
            return StepOutcome(
                tool_results=[calc],
                error=calc.error,
                failure_kind=calc.failure_kind,
            )
        output = CalculatorOutput.model_validate(calc.output)
        claim = f"{output.expression} = {output.value:g}"
        if output.unit:
            claim = f"{claim} {output.unit}"
        derived = Evidence(
            evidence_id=f"{step.id}:1",
            claim=claim,
            source_url=None,
            source_title=None,
            extracted_text=claim,
            relevance_score=1.0,
            confidence=_DERIVED_CONFIDENCE,
            retrieved_at=calc.started_at,
            step_id=step.id,
        )
        self._emitter.emit(
            EventKind.EVIDENCE_ADDED,
            tool="calculator",
            step_id=step.id,
            data={"evidence_id": derived.evidence_id, "derived": True},
        )
        return StepOutcome(tool_results=[calc], evidence=[derived])

    async def _direct_simulator(self, step: PlanStep) -> StepOutcome:
        call = await self._runner.call(
            self._registry.get("failure_simulator"),
            step_id=step.id,
            call_ordinal=1,
            arguments=step.arguments,
        )
        if not call.success:
            return StepOutcome(
                tool_results=[call],
                error=call.error,
                failure_kind=call.failure_kind,
            )
        return StepOutcome(tool_results=[call])


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def _quality(domain: str) -> SourceQuality:
    host = domain.lower()
    if host.endswith(_PRIMARY_SUFFIXES) or host in _PRIMARY_HOSTS:
        return SourceQuality.PRIMARY
    if host in _REPUTABLE_HOSTS:
        return SourceQuality.REPUTABLE
    return SourceQuality.UNKNOWN


def _rank(
    results: list[SearchResultItem], query_terms: frozenset[str]
) -> list[SearchResultItem]:
    seen: set[str] = set()
    unique: list[SearchResultItem] = []
    for item in results:
        key = normalize_url(item.url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    def sort_key(pair: tuple[int, SearchResultItem]) -> tuple[int, float, int]:
        position, item = pair
        tier = -_TIER_RANK[_quality(item.domain)]
        relevance = -term_overlap(query_terms, f"{item.title} {item.snippet}")
        return tier, relevance, position

    return [item for _, item in sorted(enumerate(unique), key=sort_key)]


def _first_sentence(text: str, limit: int) -> str:
    cut = -1
    for marker in (". ", "! ", "? "):
        index = text.find(marker)
        if index != -1 and (cut == -1 or index < cut):
            cut = index
    sentence = text[: cut + 1].strip() if cut != -1 else text.strip()
    if len(sentence) <= limit:
        return sentence
    return sentence[: limit - 3].rstrip() + "..."


def _web_evidence(
    *,
    evidence_id: str,
    step_id: str,
    url: str,
    title: str | None,
    text: str,
    query_terms: frozenset[str],
    retrieved_at: datetime,
) -> Evidence | None:
    normalized = " ".join(text.split())
    if not normalized:
        return None
    return Evidence(
        evidence_id=evidence_id,
        claim=_first_sentence(normalized, _CLAIM_LIMIT),
        source_url=url,
        source_title=title,
        extracted_text=normalized[:_EXCERPT_LIMIT],
        relevance_score=term_overlap(query_terms, normalized),
        confidence=_WEB_CONFIDENCE,
        retrieved_at=retrieved_at,
        step_id=step_id,
        numeric=extract_numeric(normalized),
    )


_CURRENCY_RE = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(trillion|billion|million|thousand|[kKmMbBtT])?\b"
)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:percent|%)", re.IGNORECASE)
_MEGA_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(trillion|billion|million)\s+([A-Za-z][A-Za-z-]*)")

_MAGNITUDE = {
    "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12,
    "thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12,
}
_CURRENCY_ABBREV = {
    "k": "K", "m": "M", "b": "B", "t": "T",
    "thousand": "K", "million": "M", "billion": "B", "trillion": "T",
}


def extract_numeric(text: str) -> NumericFact | None:
    """§4.3 item 1 — deterministic numeric extraction from fetched prose.

    Earliest pattern in the text wins: currency ($8.9B → USD_B), percentage
    (12% → percent), magnitude+noun (350 million users → million_users).
    metric_label = up to three significant words immediately before the match;
    returns None for prose with no numeric pattern.
    """
    candidates: list[tuple[int, str, re.Match[str]]] = []
    for kind, pattern in (("currency", _CURRENCY_RE), ("percent", _PERCENT_RE), ("mega", _MEGA_RE)):
        match = pattern.search(text)
        if match is not None:
            candidates.append((match.start(), kind, match))
    if not candidates:
        return None
    _, kind, match = min(candidates, key=lambda item: item[0])
    label = _label_before(text, match.start())
    if kind == "currency":
        raw, suffix = match.group(1), match.group(2)
        value = float(raw.replace(",", "")) * _MAGNITUDE.get(suffix.lower() if suffix else "", 1.0)
        if suffix:
            unit = f"USD_{_CURRENCY_ABBREV[suffix.lower()]}"
        else:
            unit = "USD"
        return NumericFact(value=value, unit=unit, metric_label=label)
    if kind == "percent":
        return NumericFact(value=float(match.group(1)), unit="percent", metric_label=label)
    raw, magnitude, noun = match.group(1), match.group(2), match.group(3)
    return NumericFact(
        value=float(raw.replace(",", "")),
        unit=f"{magnitude}_{noun.casefold()}",
        metric_label=label,
    )


def _label_before(text: str, position: int) -> str | None:
    tokens = significant_tokens(text[:position])
    if not tokens:
        return None
    return " ".join(tokens[-3:])
