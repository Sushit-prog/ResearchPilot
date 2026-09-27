from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import ValidationError

from app.errors import PlanValidationError, ToolInputValidationError
from app.models.events import EventKind, ToolResult
from app.models.evidence import Evidence
from app.models.plan import Plan
from app.models.tool_io import CalculatorOutput
from app.observability import EventEmitter
from app.reliability.runner import Runner
from app.reliability.validation import (
    claim_tokens,
    evidence_violations,
    normalize_title,
    normalize_url,
    tokens_overlap,
)
from app.tools.base import Tool, ToolRegistry

CLAIM_OVERLAP_THRESHOLD = 0.9   # §10 token-set overlap ≥ threshold → duplicate
MIN_RELEVANCE_SCORE = 0.1       # §4.11 seed relevance below this is filtered out
CLAIM_TOLERANCE = 1e-3          # rounding tolerance for claim recomputation
_CONFIDENCE_PENALTY = 0.2

_PERCENT_IN_CLAIM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:percent|%)", re.IGNORECASE)
_AMOUNT_RE = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(trillion|billion|million|thousand|[kKmMbBtT])?\b"
)
_MAGNITUDE = {
    "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12,
    "thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12,
}


def validate_plan(plan: Plan, registry: ToolRegistry, max_steps: int) -> None:
    if len(plan.steps) > max_steps:
        raise PlanValidationError(
            f"plan has {len(plan.steps)} steps; maximum is {max_steps}"
        )
    for step in plan.steps:
        if not step.expected_output.strip():
            raise PlanValidationError(f"step {step.id!r} has an empty expected_output")
        if step.tool not in registry.names():
            raise PlanValidationError(
                f"step {step.id!r} references unknown tool {step.tool!r}; "
                f"registered tools: {registry.names()}"
            )
        tool = registry.get(step.tool)
        try:
            tool.parse_input(step.arguments)
        except ToolInputValidationError as exc:
            raise PlanValidationError(f"step {step.id!r} arguments rejected: {exc}") from exc


class Sufficiency(str, Enum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    EMPTY = "empty"


def evidence_sufficiency(
    evidence: list[Evidence],
    *,
    min_evidence: int,
    min_distinct_sources: int,
) -> Sufficiency:
    if not evidence:
        return Sufficiency.EMPTY
    real_sources = {item.source_url for item in evidence if item.source_url is not None}
    if len(evidence) < min_evidence or len(real_sources) < min_distinct_sources:
        return Sufficiency.INSUFFICIENT
    return Sufficiency.SUFFICIENT


def deduplicate_evidence(evidence: list[Evidence], *, emitter: EventEmitter) -> list[Evidence]:
    """§4.10 Stage 8 — deterministic dedup: normalized URLs, then normalized
    claims (token-set overlap ≥ threshold). Every drop emits
    `evidence_deduplicated`; the first occurrence wins."""
    kept: list[Evidence] = []
    seen_urls: dict[str, str] = {}
    kept_claims: list[tuple[frozenset[str], str]] = []
    for item in evidence:
        if item.source_title is not None:
            item.source_title = normalize_title(item.source_title)
        reason: str | None = None
        kept_id: str | None = None
        url_key: str | None = None
        if item.source_url is not None:
            try:
                url_key = normalize_url(item.source_url)
            except ValueError:
                url_key = None  # structurally invalid — filter stage rejects it
            if url_key is not None and url_key in seen_urls:
                reason, kept_id = "duplicate_url", seen_urls[url_key]
        if reason is None:
            tokens = claim_tokens(item.claim)
            for previous_tokens, previous_id in kept_claims:
                if tokens_overlap(tokens, previous_tokens) >= CLAIM_OVERLAP_THRESHOLD:
                    reason, kept_id = "duplicate_claim", previous_id
                    break
        if reason is not None:
            emitter.emit(
                EventKind.EVIDENCE_DEDUPLICATED,
                step_id=item.step_id,
                message=reason,
                data={
                    "evidence_id": item.evidence_id,
                    "kept": kept_id,
                    "reason": reason,
                },
            )
            continue
        kept.append(item)
        if item.source_url is not None and url_key is not None:
            seen_urls.setdefault(url_key, item.evidence_id)
        kept_claims.append((claim_tokens(item.claim), item.evidence_id))
    return kept


def filter_evidence(
    evidence: list[Evidence],
    *,
    emitter: EventEmitter,
    source_keys: set[str],
) -> tuple[list[Evidence], list[str]]:
    """§4.11 Stage 9a — structural validation + relevance floor. Rejects emit
    `evidence_rejected` and return a warning; invalid evidence never reaches
    synthesis."""
    kept: list[Evidence] = []
    warnings: list[str] = []
    for item in evidence:
        violations = evidence_violations(item, source_keys=source_keys)
        if not violations and item.relevance_score < MIN_RELEVANCE_SCORE:
            violations.append(
                f"relevance {item.relevance_score} below threshold {MIN_RELEVANCE_SCORE}"
            )
        if violations:
            reason = "; ".join(violations)
            emitter.emit(
                EventKind.EVIDENCE_REJECTED,
                step_id=item.step_id,
                message=reason,
                data={"evidence_id": item.evidence_id, "reason": reason},
            )
            warnings.append(f"evidence {item.evidence_id} rejected: {reason}")
            continue
        kept.append(item)
    return kept, warnings


async def numeric_pass(
    evidence: list[Evidence],
    *,
    runner: Runner,
    calculator: Tool[Any, Any],
    emitter: EventEmitter,
) -> tuple[list[Evidence], list[str], list[ToolResult]]:
    """§4.3 items 2–3 — claim-internal recomputation via the calculator tool
    (through the runner) + cross-source consistency. Conflicts are flagged and
    kept (§16), never dropped: `evidence_conflict` events + warnings feed
    Report.contradictions in synthesis."""
    warnings: list[str] = []
    tool_results: list[ToolResult] = []
    for item in evidence:
        warning = await _recompute_claim(
            item, runner=runner, calculator=calculator, emitter=emitter,
            tool_results=tool_results,
        )
        if warning is not None:
            warnings.append(warning)
    warnings.extend(_cross_source_conflicts(evidence, emitter))
    return evidence, warnings, tool_results


async def _recompute_claim(
    item: Evidence,
    *,
    runner: Runner,
    calculator: Tool[Any, Any],
    emitter: EventEmitter,
    tool_results: list[ToolResult],
) -> str | None:
    percent_match = _PERCENT_IN_CLAIM_RE.search(item.claim)
    amounts = [_scaled_amount(match) for match in _AMOUNT_RE.finditer(item.claim)]
    if percent_match is None or len(amounts) < 2:
        return None
    percent = float(percent_match.group(1))

    chosen: tuple[int, int, str] | None = None
    for base_index, base in enumerate(amounts):
        for target_index, target in enumerate(amounts):
            if base_index == target_index:
                continue
            if _close(target, base * (1.0 + percent / 100.0)):
                chosen = (base_index, target_index, "+")
                break
            if _close(target, base * (1.0 - percent / 100.0)):
                chosen = (base_index, target_index, "-")
                break
        if chosen is not None:
            break

    if chosen is None:
        base_index, target_index, sign = 0, 1, "+"
    else:
        base_index, target_index, sign = chosen
    base, target = amounts[base_index], amounts[target_index]
    expression = f"({base!r} * (1 {sign} {percent!r} / 100))"

    result = await runner.call(
        calculator,
        step_id=item.evidence_id,
        call_ordinal=1,
        arguments={"expression": expression},
    )
    tool_results.append(result)
    if not result.success or result.output is None:
        warning = f"numeric recomputation unavailable for {item.evidence_id}: {result.error}"
        emitter.emit(
            EventKind.EVIDENCE_CONFLICT,
            step_id=item.step_id,
            status="recompute_unavailable",
            message=warning,
            data={"evidence_id": item.evidence_id},
        )
        return warning

    try:
        recomputed = CalculatorOutput.model_validate(result.output).value
    except ValidationError:
        warning = f"numeric recomputation unavailable for {item.evidence_id}: malformed output"
        emitter.emit(
            EventKind.EVIDENCE_CONFLICT,
            step_id=item.step_id,
            status="recompute_unavailable",
            message=warning,
            data={"evidence_id": item.evidence_id},
        )
        return warning
    item.verification.recomputed = True
    if chosen is not None and _close(recomputed, target):
        item.verification.consistent = True
        return None

    note = f"calculator recomputed {recomputed:g} but claim implies {target:g}"
    item.verification.consistent = False
    item.verification.note = note
    item.confidence = round(max(0.0, item.confidence - _CONFIDENCE_PENALTY), 3)
    emitter.emit(
        EventKind.EVIDENCE_CONFLICT,
        step_id=item.step_id,
        message=note,
        data={
            "evidence_id": item.evidence_id,
            "claim_value": target,
            "recomputed": recomputed,
        },
    )
    return f"evidence {item.evidence_id} arithmetic inconsistent: {note}"


def _cross_source_conflicts(evidence: list[Evidence], emitter: EventEmitter) -> list[str]:
    """§4.3 item 3 — same metric label, different values across sources →
    flag every member, keep every claim."""
    groups: dict[str, list[Evidence]] = {}
    for item in evidence:
        if item.numeric is None or not item.numeric.metric_label:
            continue
        groups.setdefault(item.numeric.metric_label, []).append(item)

    warnings: list[str] = []
    for label, members in groups.items():
        if len(members) < 2:
            continue
        values = sorted({round(member.numeric.value, 6) for member in members})
        if len(values) < 2:
            continue
        ids = [member.evidence_id for member in members]
        for member in members:
            others = sorted(evidence_id for evidence_id in ids if evidence_id != member.evidence_id)
            member.verification.conflict_with = sorted(
                set(member.verification.conflict_with) | set(others)
            )
            note = f"cross-source disagreement on '{label}': values {values}"
            member.verification.note = (
                note if not member.verification.note else f"{member.verification.note}; {note}"
            )
            emitter.emit(
                EventKind.EVIDENCE_CONFLICT,
                step_id=member.step_id,
                message=note,
                data={"evidence_id": member.evidence_id, "metric": label, "values": values},
            )
        warnings.append(f"metric '{label}' has conflicting values {values} across sources")
    return warnings


def _scaled_amount(match: re.Match[str]) -> float:
    raw, suffix = match.group(1), match.group(2)
    value = float(raw.replace(",", ""))
    if suffix:
        value *= _MAGNITUDE[suffix.lower()]
    return value


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= CLAIM_TOLERANCE * max(abs(left), abs(right), 1.0)
