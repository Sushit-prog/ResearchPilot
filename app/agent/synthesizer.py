from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from app.agent.state import AgentState
from app.config import Config
from app.errors import ResearchPilotError, SynthesisError
from app.llm.base import LLMProvider
from app.models.events import EventKind, FailureKind
from app.models.evidence import Evidence
from app.models.report import (
    ExecutionSummary,
    FailureRecord,
    Finding,
    ImportantEvidence,
    Report,
    SourceRef,
)
from app.observability import EventEmitter
from app.reliability.validation import normalize_url

SYNTHESIS_CONTRACT = (
    'Return ONLY a JSON object: {"research_question": string, "executive_summary": string, '
    '"key_findings": [{"index": int, "claim": string, "evidence_id": string, '
    '"source_url": string|null, "source_title": string|null, "confidence": number}], '
    '"important_evidence": [{"evidence_id": string, "excerpt": string}], '
    '"contradictions": [string], "actionable_insights": [string], '
    '"sources": [{"index": int, "url": string, "title": string|null, "domain": string}], '
    '"execution_summary": {"started_at": string(ISO-8601), '
    '"finished_at": string(ISO-8601), "duration_s": number, "steps_total": int, '
    '"steps_succeeded": int, "steps_failed": int, "sources_searched": int, '
    '"sources_used": int, "sources_rejected": int, "tool_calls": int, "retries": int, '
    '"failures": [object]}}'
)

_EXCERPT_LIMIT = 240
_SLUG_LIMIT = 60


def build_synthesis_prompt(question: str, evidence: list[Evidence]) -> tuple[str, str]:
    serialized = [
        {
            "evidence_id": item.evidence_id,
            "claim": item.claim,
            "source_url": item.source_url,
            "source_title": item.source_title,
            "relevance_score": item.relevance_score,
            "confidence": item.confidence,
            "numeric": item.numeric.model_dump() if item.numeric else None,
            "verification": item.verification.model_dump(),
        }
        for item in evidence
    ]
    system = (
        "You write a cited research report from validated evidence objects.\n"
        f"{SYNTHESIS_CONTRACT}\n\n"
        "Rules:\n"
        "- Every key_findings[i].evidence_id must be one of the evidence_id values in the "
        "evidence list below, and its source_url must exactly match that evidence's "
        "source_url (null only when the evidence's source_url is null).\n"
        "- important_evidence excerpts, research_question, sources, and execution_summary "
        "are placeholders: they are replaced deterministically from the goal, the evidence, "
        "and run state after validation — still include them, correctly shaped.\n"
        "- contradictions must flag disagreements between evidence items (reference "
        "evidence_ids); never invent claims or sources absent from the evidence list.\n"
        "- key_findings and actionable_insights are yours: synthesize the evidence."
    )
    user = f"Research question:\n{question}\n\nEvidence:\n{json.dumps(serialized, indent=2)}"
    return system, user


def parse_report(raw: str) -> Report:
    data = _load_report_json(raw)
    try:
        return Report.model_validate(data)
    except ValidationError as exc:
        raise SynthesisError(f"report failed schema validation: {exc}") from exc


def _load_report_json(raw: str) -> dict[str, Any]:
    candidates = [raw.strip()]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    candidates.extend(match.strip() for match in fenced)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
        raise SynthesisError("synthesis output must be a JSON object")
    raise SynthesisError("synthesis output is not valid JSON")


def _finding_violation(finding: Finding, by_id: dict[str, Evidence]) -> str | None:
    item = by_id.get(finding.evidence_id)
    if item is None:
        return f"unknown evidence_id {finding.evidence_id!r}"
    if finding.source_url != item.source_url:
        return (
            f"cites source_url {finding.source_url!r} but evidence "
            f"{finding.evidence_id!r} has {item.source_url!r}"
        )
    return None


def contradictions_from(evidence: list[Evidence]) -> list[str]:
    entries: list[str] = []
    seen_pairs: set[frozenset[str]] = set()
    for item in evidence:
        verification = item.verification
        if verification.conflict_with:
            pair = frozenset([item.evidence_id, *verification.conflict_with])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            others = ", ".join(sorted(verification.conflict_with))
            detail = verification.note or "cross-source disagreement"
            entries.append(f"{item.evidence_id} conflicts with {others}: {detail}")
        elif verification.consistent is False and verification.note:
            entries.append(f"{item.evidence_id}: {verification.note}")
    return entries


def build_execution_summary(
    state: AgentState, *, started_at: datetime, finished_at: datetime
) -> ExecutionSummary:
    """Aggregate run counters and assemble every failure the run produced.

    A failure is listed if it failed a step (one record per failed step) or if
    it was a failed tool call whose step nevertheless succeeded (candidate
    exhaustion inside a research step — the run continued). Calls that failed
    and then recovered on retry are not listed: their retries are already
    counted in ``retries``.
    """
    searched: set[str] = set()
    for source in state.sources:
        try:
            searched.add(normalize_url(source.url))
        except ValueError:
            continue
    used: set[str] = set()
    for item in state.evidence:
        if item.source_url:
            try:
                used.add(normalize_url(item.source_url))
            except ValueError:
                continue
    failures: list[FailureRecord] = []
    for step in state.failed_steps:
        result_ids = set(step.result_ids)
        unclassified = any(
            result.unclassified and result.result_id in result_ids
            for result in state.tool_results
        )
        failures.append(
            FailureRecord(
                step_id=step.step_id,
                tool=step.tool,
                failure_kind=step.failure_kind or FailureKind.PERMANENT,
                message=step.error or "",
                unclassified=unclassified,
            )
        )
    covered = {
        result_id for step in state.failed_steps for result_id in step.result_ids
    }
    for result in state.tool_results:
        if result.success or result.result_id in covered:
            continue
        if result.tool == "webpage_fetch":
            suffix = " — source marked unavailable, run continued"
        else:
            suffix = " — run continued"
        failures.append(
            FailureRecord(
                step_id=result.step_id,
                tool=result.tool,
                failure_kind=result.failure_kind or FailureKind.PERMANENT,
                message=f"{result.error or 'tool call failed'}{suffix}",
                unclassified=result.unclassified,
            )
        )
    return ExecutionSummary(
        started_at=started_at,
        finished_at=finished_at,
        duration_s=max((finished_at - started_at).total_seconds(), 0.0),
        steps_total=len(state.plan.steps) if state.plan else 0,
        steps_succeeded=len(state.completed_steps),
        steps_failed=len(state.failed_steps),
        sources_searched=len(searched),
        sources_used=len(used),
        sources_rejected=max(len(searched) - len(used), 0),
        tool_calls=len(state.tool_results),
        retries=sum(state.retry_counts.values()),
        failures=failures,
    )


async def synthesize(
    llm: LLMProvider,
    state: AgentState,
    *,
    config: Config,
    emitter: EventEmitter,
    clock: Callable[[], datetime],
    started_at: datetime,
) -> Report:
    question = state.normalized_goal
    evidence = state.evidence
    by_id = {item.evidence_id: item for item in evidence}
    system, user = build_synthesis_prompt(question, evidence)
    emitter.emit(
        EventKind.SYNTHESIS_STARTED, message=question, data={"evidence": len(evidence)}
    )
    feedback = ""
    for attempt in range(1, config.max_synthesis_attempts + 1):
        try:
            raw = await llm.complete(system=system, user=user + feedback)
        except ResearchPilotError as exc:
            raise SynthesisError(f"synthesis LLM call failed: {exc}") from exc
        try:
            report = parse_report(raw)
        except SynthesisError as exc:
            if attempt >= config.max_synthesis_attempts:
                raise SynthesisError(
                    f"synthesis still invalid after {config.max_synthesis_attempts} "
                    f"attempts: {exc}"
                ) from exc
            feedback = (
                "\n\nYour previous output was rejected.\n"
                f"Previous output:\n{raw}\n"
                f"Error:\n{exc}\n"
                "Return a corrected report as JSON only."
            )
            continue
        violations = [
            f"finding {finding.index}: {reason}"
            for finding in report.key_findings
            if (reason := _finding_violation(finding, by_id)) is not None
        ]
        if violations and attempt < config.max_synthesis_attempts:
            feedback = (
                "\n\nYour previous output was rejected by the citation gate.\n"
                "Violations:\n"
                + "\n".join(f"- {entry}" for entry in violations)
                + "\nEvery evidence_id must resolve in the evidence list and source_url "
                "must exactly match that evidence's source_url. Return a corrected "
                "report as JSON only."
            )
            continue
        return _finalize(report, state, by_id, started_at=started_at, clock=clock)
    raise SynthesisError("synthesis exhausted")


def _finalize(
    report: Report,
    state: AgentState,
    by_id: dict[str, Evidence],
    *,
    started_at: datetime,
    clock: Callable[[], datetime],
) -> Report:
    warnings: list[str] = []
    findings: list[Finding] = []
    dropped: list[str] = []
    for finding in report.key_findings:
        reason = _finding_violation(finding, by_id)
        if reason is not None:
            dropped.append(f"{finding.evidence_id}: {reason}")
            continue
        findings.append(finding)
    if dropped:
        warnings.append(
            f"synthesis dropped {len(dropped)} finding(s) with invalid citations: "
            + "; ".join(dropped)
        )
    for position, finding in enumerate(findings, start=1):
        item = by_id[finding.evidence_id]
        finding.index = position
        finding.source_url = item.source_url
        finding.source_title = item.source_title
        finding.confidence = item.confidence

    important: list[ImportantEvidence] = []
    unknown: list[str] = []
    for entry in report.important_evidence:
        item = by_id.get(entry.evidence_id)
        if item is None:
            unknown.append(entry.evidence_id)
            continue
        excerpt = item.extracted_text.strip()
        if len(excerpt) > _EXCERPT_LIMIT:
            excerpt = excerpt[: _EXCERPT_LIMIT - 1].rstrip() + "…"
        important.append(ImportantEvidence(evidence_id=entry.evidence_id, excerpt=excerpt))
    if unknown:
        warnings.append(
            "synthesis dropped important-evidence entries with unknown evidence_id: "
            + ", ".join(unknown)
        )

    contradictions = list(
        dict.fromkeys(contradictions_from(state.evidence) + report.contradictions)
    )
    sources = _build_sources(state)
    summary = build_execution_summary(
        state, started_at=started_at, finished_at=clock()
    )
    final = Report(
        research_question=state.normalized_goal,
        executive_summary=report.executive_summary,
        key_findings=findings,
        important_evidence=important,
        contradictions=contradictions,
        actionable_insights=report.actionable_insights,
        sources=sources,
        execution_summary=summary,
    )
    state.warnings.extend(warnings)
    return final


def _build_sources(state: AgentState) -> list[SourceRef]:
    titles: dict[str, str | None] = {}
    for source in state.sources:
        try:
            titles.setdefault(normalize_url(source.url), source.title)
        except ValueError:
            continue
    refs: list[SourceRef] = []
    seen: set[str] = set()
    for item in state.evidence:
        if not item.source_url:
            continue
        try:
            key = normalize_url(item.source_url)
        except ValueError:
            continue
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            SourceRef(
                index=0,
                url=item.source_url,
                title=titles.get(key, item.source_title),
                domain=urlparse(key).netloc,
            )
        )
    for position, ref in enumerate(refs, start=1):
        ref.index = position
    return refs


def render_markdown(report: Report) -> str:
    lines = [
        "# ResearchPilot Report",
        "",
        "## Research Question",
        report.research_question,
        "",
        "## Executive Summary",
        report.executive_summary,
        "",
        "## Key Findings",
    ]
    if report.key_findings:
        for finding in report.key_findings:
            lines.append(f"{finding.index}. {finding.claim}")
            lines.append(f"   Source: {_source_line(finding)}")
    else:
        lines.append("- None identified.")
    lines += ["", "## Important Evidence"]
    if report.important_evidence:
        for entry in report.important_evidence:
            lines.append(f"- [{entry.evidence_id}] {entry.excerpt}")
    else:
        lines.append("- None identified.")
    lines += ["", "## Contradictions / Uncertainty"]
    if report.contradictions:
        lines.extend(f"- {entry}" for entry in report.contradictions)
    else:
        lines.append("- None identified.")
    lines += ["", "## Actionable Insights"]
    if report.actionable_insights:
        lines.extend(f"- {insight}" for insight in report.actionable_insights)
    else:
        lines.append("- None identified.")
    lines += ["", "## Sources"]
    if report.sources:
        for source in report.sources:
            title = source.title or "Untitled source"
            lines.append(f"{source.index}. {title} — {source.url} ({source.domain})")
    else:
        lines.append("- None identified.")
    lines += ["", "## Execution Summary"]
    lines.extend(execution_summary_lines(report.execution_summary))
    return "\n".join(lines) + "\n"


def _source_line(finding: Finding) -> str:
    if finding.source_url is None:
        origin = "computed — calculator-derived, no external source"
    else:
        origin = f"{finding.source_title or 'Untitled source'} — {finding.source_url}"
    return f"{origin} (confidence {finding.confidence:.2f})"


def execution_summary_lines(summary: ExecutionSummary) -> list[str]:
    lines = [
        f"- Duration: {summary.duration_s:.1f}s",
        (
            f"- Steps: {summary.steps_total} total, {summary.steps_succeeded} succeeded, "
            f"{summary.steps_failed} failed"
        ),
        (
            f"- Sources: {summary.sources_searched} searched, {summary.sources_used} used, "
            f"{summary.sources_rejected} rejected"
        ),
        f"- Tool calls: {summary.tool_calls} | Retries: {summary.retries}",
    ]
    if summary.failures:
        lines.append("- Failures:")
        for failure in summary.failures:
            marker = ", UNCLASSIFIED" if failure.unclassified else ""
            lines.append(
                f"  - {failure.step_id} via {failure.tool} "
                f"({failure.failure_kind.value}{marker}): {failure.message}"
            )
    else:
        lines.append("- Failures: none")
    return lines


def slugify_goal(goal: str) -> str:
    text = (
        unicodedata.normalize("NFKD", goal).encode("ascii", "ignore").decode("ascii").lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    slug = slug[:_SLUG_LIMIT].strip("-")
    return slug or "research-report"


def write_report(report: Report, *, report_dir: str, goal: str) -> str:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slugify_goal(goal)}.md"
    path.write_text(render_markdown(report), encoding="utf-8")
    return str(path)
