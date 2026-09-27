"""§21 evaluation harness — offline runner for the synthetic research tasks.

Every component here is deterministic and fully offline: canned HTTP comes from
`tests/fixtures/research_tasks.json` via `httpx.MockTransport`, plans are
scripted per task, and the LLM stand-in derives synthesis responses from the
evidence list the production prompt itself carries. No live network calls, no
wall-clock timing, no filesystem paths leak into rendered output, so two runs
of the same tasks produce byte-identical `docs/evaluation.md` content.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.agent.orchestrator import Orchestrator
from app.agent.researcher import Researcher
from app.agent.synthesizer import render_markdown as render_report
from app.config import Config
from app.llm.fake import FakeLLMExhaustedError
from app.models.events import EventKind
from app.observability import EventEmitter, InMemoryEventSink
from app.reliability.runner import Runner
from app.tools.base import ToolRegistry
from app.tools.calculator import CalculatorTool
from app.tools.failure_simulator import FailureSimulatorTool
from app.tools.web_search import WebSearchTool
from app.tools.webpage_fetch import WebpageFetchTool

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS_PATH = REPO_ROOT / "tests" / "fixtures" / "research_tasks.json"
EVALUATION_DOC_PATH = REPO_ROOT / "docs" / "evaluation.md"

CANONICAL_SECTIONS = [
    "## Research Question",
    "## Executive Summary",
    "## Key Findings",
    "## Important Evidence",
    "## Contradictions / Uncertainty",
    "## Actionable Insights",
    "## Sources",
    "## Execution Summary",
]

_PLAN_SYSTEM_MARKER = "decompose a high-level research goal"
_SYNTHESIS_SYSTEM_MARKER = "cited research report"
_EVIDENCE_MARKER = "\nEvidence:\n"
_QUESTION_MARKER = "Research question:\n"


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    synthetic: bool
    query: str
    plan_first_attempt: dict[str, Any] | None
    plan: dict[str, Any]
    canned_searches: dict[str, list[dict[str, Any]]]
    canned_pages: dict[str, str]
    expected: dict[str, Any]

    @property
    def plan_responses(self) -> list[str]:
        responses: list[str] = []
        if self.plan_first_attempt is not None:
            responses.append(json.dumps(self.plan_first_attempt))
        responses.append(json.dumps(self.plan))
        return responses


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    category: str
    status: str
    report_written: bool
    plan_invalid: int
    tools_used: frozenset[str]
    tool_calls: int
    tool_ok: int
    induced_failures: int
    recovered_failures: int
    evidence_added: int
    dedup_dropped: int
    evidence_rejected: int
    evidence_final: int
    sources_distinct: int
    findings_total: int
    findings_cited: int
    findings_computed: int
    sections_found: int
    contradictions: int


@dataclass(frozen=True)
class Aggregate:
    tasks: int
    first_pass_ok: int
    completed: int
    plan_invalid: int
    tool_calls: int
    tool_ok: int
    induced_failures: int
    recovered_failures: int
    evidence_added: int
    dedup_dropped: int
    sections_found: int
    sources_distinct: int
    findings_total: int
    findings_cited: int
    findings_computed: int


def load_tasks(path: Path = TASKS_PATH) -> list[Task]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Task(**entry) for entry in raw["tasks"]]


def _build_handler(task: Task) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            query = json.loads(request.content).get("query", "")
            results = task.canned_searches.get(query)
            if results is None:
                return httpx.Response(500)
            return httpx.Response(200, json={"results": results})
        body = task.canned_pages.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(
            200, text=body, headers={"content-type": "text/html; charset=utf-8"}
        )

    return handler


def _synthesis_from_prompt(user: str) -> str:
    head, marker, tail = user.partition(_EVIDENCE_MARKER)
    if not marker:
        raise FakeLLMExhaustedError("synthesis prompt missing evidence list")
    question = head.removeprefix(_QUESTION_MARKER).strip()
    evidence = json.loads(tail)
    findings = [
        {
            "index": index,
            "claim": item["claim"],
            "evidence_id": item["evidence_id"],
            "source_url": item["source_url"],
            "source_title": item["source_title"],
            "confidence": item["confidence"],
        }
        for index, item in enumerate(evidence, start=1)
    ]
    payload = {
        "research_question": question,
        "executive_summary": (
            f"Deterministic harness summary covering {len(evidence)} validated "
            "evidence items."
        ),
        "key_findings": findings,
        "important_evidence": [
            {"evidence_id": item["evidence_id"], "excerpt": item["claim"]}
            for item in evidence
        ],
        "contradictions": [],
        "actionable_insights": ["Re-check the cited sources for follow-up data."],
        "sources": [
            {
                "index": 1,
                "url": "https://placeholder.example",
                "title": None,
                "domain": "placeholder.example",
            }
        ],
        "execution_summary": {
            "started_at": "2026-01-01T12:00:00Z",
            "finished_at": "2026-01-01T12:00:00Z",
            "duration_s": 0.0,
            "steps_total": 0,
            "steps_succeeded": 0,
            "steps_failed": 0,
            "sources_searched": 0,
            "sources_used": 0,
            "sources_rejected": 0,
            "tool_calls": 0,
            "retries": 0,
            "failures": [],
        },
    }
    return json.dumps(payload)


class HarnessLLM:
    """Deterministic LLM stand-in: scripted plans, evidence-derived synthesis."""

    name = "harness-fake"

    def __init__(self, plan_responses: Sequence[str]) -> None:
        self._plans = list(plan_responses)
        self.plan_calls = 0
        self.synthesis_calls = 0

    async def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        del temperature
        if _PLAN_SYSTEM_MARKER in system:
            self.plan_calls += 1
            if not self._plans:
                raise FakeLLMExhaustedError("no scripted plan responses left")
            return self._plans.pop(0)
        if _SYNTHESIS_SYSTEM_MARKER in system:
            self.synthesis_calls += 1
            return _synthesis_from_prompt(user)
        raise FakeLLMExhaustedError(f"unexpected prompt: {system[:80]!r}")


def _measure(task: Task, state: Any) -> TaskResult:
    kinds = [event.event for event in state.execution_events]
    report = state.final_report
    markdown = render_report(report) if report is not None else ""
    section_lines = {line for line in markdown.splitlines() if line.startswith("## ")}
    evidence_ids = {item.evidence_id for item in state.evidence}
    evidence_urls = {item.source_url for item in state.evidence if item.source_url}
    findings = report.key_findings if report is not None else []
    cited = sum(
        1
        for finding in findings
        if finding.evidence_id in evidence_ids
        and (finding.source_url is None or finding.source_url in evidence_urls)
    )
    computed = sum(1 for finding in findings if finding.source_url is None)
    return TaskResult(
        task_id=task.id,
        category=task.category,
        status=state.status.value,
        report_written=state.report_path is not None,
        plan_invalid=kinds.count(EventKind.PLAN_INVALID),
        tools_used=frozenset(result.tool for result in state.tool_results),
        tool_calls=len(state.tool_results),
        tool_ok=sum(1 for result in state.tool_results if result.success),
        induced_failures=sum(1 for result in state.tool_results if result.attempts > 1),
        recovered_failures=sum(
            1 for result in state.tool_results if result.attempts > 1 and result.success
        ),
        evidence_added=kinds.count(EventKind.EVIDENCE_ADDED),
        dedup_dropped=kinds.count(EventKind.EVIDENCE_DEDUPLICATED),
        evidence_rejected=kinds.count(EventKind.EVIDENCE_REJECTED),
        evidence_final=len(state.evidence),
        sources_distinct=len(report.sources) if report is not None else 0,
        findings_total=len(findings),
        findings_cited=cited,
        findings_computed=computed,
        sections_found=sum(1 for header in CANONICAL_SECTIONS if header in section_lines),
        contradictions=len(report.contradictions) if report is not None else 0,
    )


async def run_task(task: Task, *, report_dir: Path) -> TaskResult:
    events: list[Any] = []
    emitter = EventEmitter(lambda: FIXED_TIME, sinks=[InMemoryEventSink(events)])
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_build_handler(task)), follow_redirects=True
    )
    registry = ToolRegistry()
    registry.register(WebSearchTool(api_key="test-key", client=client))
    registry.register(WebpageFetchTool(client=client))
    registry.register(CalculatorTool())
    registry.register(FailureSimulatorTool())
    config = Config(report_dir=str(report_dir), memory_enabled=False)
    runner = Runner(
        config,
        emitter,
        clock=lambda: FIXED_TIME,
        sleep=lambda _seconds: None,
        rng=lambda _low, _high: 0.0,
    )
    researcher = Researcher(
        registry=registry, config=config, emitter=emitter, runner=runner
    )
    orchestrator = Orchestrator(
        task.query,
        llm=HarnessLLM(task.plan_responses),
        registry=registry,
        handler=researcher,
        config=config,
        clock=lambda: FIXED_TIME,
        emitter=emitter,
    )
    emitter.add(InMemoryEventSink(orchestrator.state.execution_events))
    try:
        state = await orchestrator.run()
    finally:
        await client.aclose()
    return _measure(task, state)


async def run_tasks(tasks: Sequence[Task]) -> list[TaskResult]:
    results: list[TaskResult] = []
    with tempfile.TemporaryDirectory(prefix="researchpilot-eval-") as tmp:
        for task in tasks:
            report_dir = Path(tmp) / task.id / "reports"
            results.append(await run_task(task, report_dir=report_dir))
    return results


def aggregate(results: Sequence[TaskResult]) -> Aggregate:
    return Aggregate(
        tasks=len(results),
        first_pass_ok=sum(1 for result in results if result.plan_invalid == 0),
        completed=sum(1 for result in results if result.status == "completed"),
        plan_invalid=sum(result.plan_invalid for result in results),
        tool_calls=sum(result.tool_calls for result in results),
        tool_ok=sum(result.tool_ok for result in results),
        induced_failures=sum(result.induced_failures for result in results),
        recovered_failures=sum(result.recovered_failures for result in results),
        evidence_added=sum(result.evidence_added for result in results),
        dedup_dropped=sum(result.dedup_dropped for result in results),
        sections_found=sum(result.sections_found for result in results),
        sources_distinct=sum(result.sources_distinct for result in results),
        findings_total=sum(result.findings_total for result in results),
        findings_cited=sum(result.findings_cited for result in results),
        findings_computed=sum(result.findings_computed for result in results),
    )


def _rate(part: int, whole: int) -> str:
    if whole == 0:
        return "n/a"
    return f"{100.0 * part / whole:.1f}%"


def render_markdown(results: Sequence[TaskResult]) -> str:
    agg = aggregate(results)
    lines = [
        "# ResearchPilot — Evaluation Results (synthetic)",
        "",
        "> Auto-generated by `tests/evaluation/test_harness.py` as part of the test",
        "> suite. Regenerate with `uv run pytest tests -q`. All tasks are synthetic",
        "> (`tests/fixtures/research_tasks.json`) and run fully offline: a",
        "> deterministic LLM stand-in (scripted plans, synthesis derived from the",
        "> prompt's own evidence list) plus `httpx.MockTransport` for all HTTP — no",
        "> live network calls. Every rate is shown next to the raw counts it comes",
        "> from. The file contains no timing or path data, so unchanged behavior",
        "> regenerates it byte-for-byte (enforced by",
        "> `test_evaluation_rendering_is_deterministic`).",
        "",
        "## Per-task results",
        "",
        "| Task | Category | Status | Invalid plans | Tool calls ok | Sources | "
        "Evidence added→final (dedup) | Findings cited (computed) | Sections | "
        "Recovered/induced |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        lines.append(
            f"| {result.task_id} | {result.category} | {result.status} "
            f"| {result.plan_invalid} | {result.tool_ok}/{result.tool_calls} "
            f"| {result.sources_distinct} "
            f"| {result.evidence_added}→{result.evidence_final} ({result.dedup_dropped}) "
            f"| {result.findings_cited}/{result.findings_total} "
            f"({result.findings_computed}) "
            f"| {result.sections_found}/{len(CANONICAL_SECTIONS)} "
            f"| {result.recovered_failures}/{result.induced_failures} |"
        )
    lines.extend(
        [
            "",
            "## Aggregate metrics",
            "",
            "| Metric | Raw counts | Rate |",
            "|---|---|---|",
            f"| Plan validity (first pass, no correction needed) "
            f"| {agg.first_pass_ok}/{agg.tasks} tasks | {_rate(agg.first_pass_ok, agg.tasks)} |",
            f"| Plan validity (after bounded correction) "
            f"| {agg.completed}/{agg.tasks} tasks | {_rate(agg.completed, agg.tasks)} |",
            f"| Tool success rate "
            f"| {agg.tool_ok}/{agg.tool_calls} calls | {_rate(agg.tool_ok, agg.tool_calls)} |",
            f"| Failure recovery (induced) "
            f"| {agg.recovered_failures}/{agg.induced_failures} recovered "
            f"| {_rate(agg.recovered_failures, agg.induced_failures)} |",
            f"| Source coverage "
            f"| {agg.sources_distinct} distinct sources across {agg.tasks} tasks "
            f"| per task above |",
            f"| Duplicate rate (evidence) "
            f"| {agg.dedup_dropped}/{agg.evidence_added} dropped "
            f"| {_rate(agg.dedup_dropped, agg.evidence_added)} |",
            f"| Report completeness "
            f"| {agg.sections_found}/{agg.completed * len(CANONICAL_SECTIONS)} sections "
            f"| {_rate(agg.sections_found, agg.completed * len(CANONICAL_SECTIONS))} |",
            f"| Citation presence "
            f"| {agg.findings_cited}/{agg.findings_total} findings "
            f"| {_rate(agg.findings_cited, agg.findings_total)} "
            f"(incl. {agg.findings_computed} computed) |",
            "",
            "## Metric definitions",
            "",
            "- **Plan validity (first pass, no correction needed)** — tasks whose run",
            "  recorded zero `plan_invalid` events; counted from the event log.",
            "- **Plan validity (after bounded correction)** — tasks that reached",
            "  `status == completed` (a final report) after at most the configured",
            "  plan attempts; raw counts show how many needed correction.",
            "- **Tool success rate** — successful tool results ÷ total tool results in",
            "  `state.tool_results` (retries collapse into one result, attempts kept).",
            "- **Failure recovery (induced)** — tool results with `attempts > 1`",
            "  (an induced failure consumed a retry) and `success == True`, over all",
            "  induced failures; the raw counts show the induced denominator.",
            "- **Source coverage** — distinct normalized URLs listed in each report's",
            "  Sources section (`len(report.sources)`); per-task values are in the",
            "  table above.",
            "- **Duplicate rate (evidence)** — `evidence_deduplicated` events ÷",
            "  `evidence_added` events across all runs (relevance-floor rejections are",
            "  tracked separately as `evidence_rejected`).",
            "- **Report completeness** — expected `##` sections present in the",
            "  rendered report ÷ 8 canonical sections per completed task.",
            "- **Citation presence** — findings whose `evidence_id` resolves in the",
            "  final evidence list and whose `source_url` is either null",
            "  (calculator-derived, counted as cited) or present among evidence URLs.",
            "  Caveat: citation_presence here measures whether the deterministic",
            "  pipeline (dedup, filter, synthesis gates) correctly carries valid",
            "  evidence through to cited findings when given a scripted LLM that only",
            "  ever cites real evidence_ids; it does not measure a real LLM's tendency",
            "  to hallucinate citations — that failure mode is separately covered by",
            "  Phase 6's citation-gate correction loop",
            "  (`tests/unit/test_synthesizer.py`), which is unit-tested against a",
            "  deliberately fabricated bad citation.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "Aggregate",
    "CANONICAL_SECTIONS",
    "EVALUATION_DOC_PATH",
    "TASKS_PATH",
    "Task",
    "TaskResult",
    "aggregate",
    "load_tasks",
    "render_markdown",
    "run_task",
    "run_tasks",
]
