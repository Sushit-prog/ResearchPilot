from __future__ import annotations

from datetime import datetime, timezone

from app.agent.executor import StepOutcome
from app.agent.orchestrator import Orchestrator
from app.config import Config
from app.models.events import EventKind, ExecutionEvent, ToolResult
from app.models.evidence import Evidence, Source, SourceStatus
from app.observability import EventEmitter, InMemoryEventSink

FIXED_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def make_evidence(
    evidence_id: str,
    claim: str,
    *,
    url: str | None,
    relevance: float = 0.9,
    step_id: str,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        claim=claim,
        source_url=url,
        source_title="Example",
        extracted_text=claim,
        relevance_score=relevance,
        confidence=0.8,
        retrieved_at=FIXED_TIME,
        step_id=step_id,
    )


class MessyEvidenceHandler:
    """Returns duplicates + irrelevant evidence so Stage 8/9 must act."""

    def __init__(self, *, include_conflict: bool = False) -> None:
        self._include_conflict = include_conflict

    async def handle(self, step) -> StepOutcome:
        if step.tool == "web_search":
            claim = (
                "Revenue grew 10% from $100 to $130 in the period."
                if self._include_conflict
                else "Alpha base claim about launch windows."
            )
            return StepOutcome(
                tool_results=[_tool_result(step.id, "web_search")],
                evidence=[
                    make_evidence("ev-a", claim, url="https://example.com/a", step_id=step.id),
                    make_evidence(
                        "ev-b",
                        "An unrelated duplicate URL claim.",
                        url="https://EXAMPLE.com/a/?utm_source=x",
                        step_id=step.id,
                    ),
                    make_evidence(
                        "ev-c",
                        "Barely related filler text.",
                        url="https://example.org/c",
                        relevance=0.05,
                        step_id=step.id,
                    ),
                    make_evidence(
                        "ev-d",
                        "A second distinct claim about testing procedures.",
                        url="https://example.net/d",
                        step_id=step.id,
                    ),
                ],
                sources=[
                    _source("https://example.com/a"),
                    _source("https://example.org/c"),
                    _source("https://example.net/d"),
                ],
            )
        return StepOutcome(
            tool_results=[_tool_result(step.id, "calculator")],
            evidence=[
                make_evidence("ev-x", "2 + 2 = 4", url=None, step_id=step.id),
            ],
        )


def _tool_result(step_id: str, tool: str) -> ToolResult:
    return ToolResult(
        result_id=f"{step_id}:{tool}:1",
        tool=tool,
        step_id=step_id,
        call_ordinal=1,
        success=True,
        attempts=1,
        started_at=FIXED_TIME,
    )


def _source(url: str) -> Source:
    return Source(url=url, domain=url.split("/")[2], retrieved_at=FIXED_TIME)


def _make_orchestrator(
    handler: object,
    fake_llm,
    registry,
    fixed_clock,
    valid_plan_json,
    synthesis_json,
    citations: list[tuple[str, str | None]],
    report_dir: str,
) -> tuple[Orchestrator, list[ExecutionEvent]]:
    events: list[ExecutionEvent] = []
    emitter = EventEmitter(fixed_clock, sinks=[InMemoryEventSink(events)])
    orchestrator = Orchestrator(
        "What is X?",
        llm=fake_llm(valid_plan_json, synthesis_json(citations)),
        registry=registry,
        handler=handler,  # type: ignore[arg-type]
        config=Config(report_dir=report_dir, memory_enabled=False),
        clock=fixed_clock,
        emitter=emitter,
    )
    return orchestrator, events


def _kinds(events: list[ExecutionEvent]) -> list[EventKind]:
    return [event.event for event in events]


async def test_pipeline_dedups_and_filters_before_sufficiency(
    fake_llm, registry, fixed_clock, valid_plan_json, synthesis_json, tmp_path
) -> None:
    orchestrator, events = _make_orchestrator(
        MessyEvidenceHandler(),
        fake_llm,
        registry,
        fixed_clock,
        valid_plan_json,
        synthesis_json,
        [("ev-a", "https://example.com/a"), ("ev-d", "https://example.net/d"), ("ev-x", None)],
        str(tmp_path),
    )
    state = await orchestrator.run()

    assert state.status.value == "completed"
    assert [item.evidence_id for item in state.evidence] == ["ev-a", "ev-d", "ev-x"]
    dedup_events = [event for event in events if event.event is EventKind.EVIDENCE_DEDUPLICATED]
    reject_events = [event for event in events if event.event is EventKind.EVIDENCE_REJECTED]
    assert len(dedup_events) == 1
    assert dedup_events[0].data == {
        "evidence_id": "ev-b",
        "kept": "ev-a",
        "reason": "duplicate_url",
    }
    assert len(reject_events) == 1
    assert reject_events[0].data["evidence_id"] == "ev-c"
    assert len(state.warnings) == 1
    assert state.warnings[0].startswith("evidence ev-c rejected: relevance")
    assert not any("thresholds" in warning for warning in state.warnings)
    assert state.retry_counts == {"r1:web_search:1": 0, "q1:calculator:1": 0}
    # filtering drops Evidence, never Source rows: REJECTED has no setter (docs §D-note)
    assert all(source.status is not SourceStatus.REJECTED for source in state.sources)
    assert state.final_report is not None
    summary = state.final_report.execution_summary
    assert summary.sources_searched == 3
    assert summary.sources_used == 2
    assert summary.sources_rejected == summary.sources_searched - summary.sources_used


async def test_dedup_before_sufficiency_triggers_insufficient_warning(
    fake_llm, registry, fixed_clock, valid_plan_json, synthesis_json, tmp_path
) -> None:
    class ThinHandler(MessyEvidenceHandler):
        async def handle(self, step) -> StepOutcome:
            if step.tool == "web_search":
                return StepOutcome(
                    tool_results=[_tool_result(step.id, "web_search")],
                    evidence=[
                        make_evidence(
                            "ev-a",
                            "Alpha base claim about launch windows.",
                            url="https://example.com/a",
                            step_id=step.id,
                        ),
                        make_evidence(
                            "ev-b",
                            "Duplicate of the base claim URL.",
                            url="https://example.com/a",
                            step_id=step.id,
                        ),
                    ],
                    sources=[_source("https://example.com/a")],
                )
            return StepOutcome(
                tool_results=[_tool_result(step.id, "calculator")],
                evidence=[
                    make_evidence("ev-x", "2 + 2 = 4", url=None, step_id=step.id)
                ],
            )

    orchestrator, events = _make_orchestrator(
        ThinHandler(),
        fake_llm,
        registry,
        fixed_clock,
        valid_plan_json,
        synthesis_json,
        [("ev-a", "https://example.com/a"), ("ev-x", None)],
        str(tmp_path),
    )
    state = await orchestrator.run()

    assert [item.evidence_id for item in state.evidence] == ["ev-a", "ev-x"]
    assert any("evidence below thresholds" in warning for warning in state.warnings)
    dedup_events = [event for event in events if event.event is EventKind.EVIDENCE_DEDUPLICATED]
    assert len(dedup_events) == 1


async def test_numeric_conflict_flows_into_state_warnings_and_retry_counts(
    fake_llm, fixed_clock, valid_plan_json, synthesis_json, tmp_path
) -> None:
    from app.tools.base import ToolRegistry
    from app.tools.calculator import CalculatorTool
    from app.tools.web_search import WebSearchTool

    real_registry = ToolRegistry()
    real_registry.register(WebSearchTool(api_key="test-key"))
    real_registry.register(CalculatorTool())
    orchestrator, events = _make_orchestrator(
        MessyEvidenceHandler(include_conflict=True),
        fake_llm,
        real_registry,
        fixed_clock,
        valid_plan_json,
        synthesis_json,
        [("ev-a", "https://example.com/a"), ("ev-d", "https://example.net/d"), ("ev-x", None)],
        str(tmp_path),
    )
    state = await orchestrator.run()

    conflicting = [item for item in state.evidence if item.evidence_id == "ev-a"]
    assert len(conflicting) == 1
    assert conflicting[0].verification.recomputed is True
    assert conflicting[0].verification.consistent is False
    assert conflicting[0].confidence == 0.6
    assert any("arithmetic inconsistent" in warning for warning in state.warnings)
    assert state.retry_counts == {
        "r1:web_search:1": 0,
        "q1:calculator:1": 0,
        "ev-a:calculator:1": 0,
    }
    conflict_events = [event for event in events if event.event is EventKind.EVIDENCE_CONFLICT]
    assert len(conflict_events) == 1
    assert conflict_events[0].data["evidence_id"] == "ev-a"
    assert state.tool_results[-1].tool == "calculator"
    assert state.tool_results[-1].step_id == "ev-a"


async def test_clean_run_emits_no_evidence_pipeline_events(
    fake_llm,
    registry,
    fixed_clock,
    valid_plan_json,
    synthesis_json,
    evidence_handler,
    tmp_path,
) -> None:
    orchestrator, events = _make_orchestrator(
        evidence_handler,
        fake_llm,
        registry,
        fixed_clock,
        valid_plan_json,
        synthesis_json,
        [
            ("ev-a", "https://example.com/a"),
            ("ev-b", "https://example.org/b"),
            ("ev-c", None),
        ],
        str(tmp_path),
    )
    state = await orchestrator.run()

    assert [item.evidence_id for item in state.evidence] == ["ev-a", "ev-b", "ev-c"]
    assert state.warnings == []
    pipeline_kinds = {
        EventKind.EVIDENCE_DEDUPLICATED,
        EventKind.EVIDENCE_REJECTED,
        EventKind.EVIDENCE_CONFLICT,
    }
    assert not any(event.event in pipeline_kinds for event in events)
    assert _kinds(events) == [
        EventKind.GOAL_NORMALIZED,
        EventKind.PLAN_CREATED,
        EventKind.SYNTHESIS_STARTED,
        EventKind.REPORT_GENERATED,
        EventKind.RUN_COMPLETED,
    ]
