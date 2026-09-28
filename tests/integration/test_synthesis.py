from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.agent.orchestrator import Orchestrator
from app.agent.state import AgentState, StepRecord
from app.agent.synthesizer import synthesize, write_report
from app.config import Config
from app.models.events import EventKind
from app.models.evidence import Evidence, Source
from app.models.plan import Plan, StepStatus
from app.observability import EventEmitter, InMemoryEventSink

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

SECTIONS = [
    "## Research Question",
    "## Executive Summary",
    "## Key Findings",
    "## Important Evidence",
    "## Contradictions / Uncertainty",
    "## Actionable Insights",
    "## Sources",
    "## Execution Summary",
]


def _kinds(state: AgentState) -> list[EventKind]:
    return [event.event for event in state.execution_events]


async def test_full_run_reaches_completed_with_report_file(
    capsys,
    fake_llm,
    registry,
    fixed_clock,
    valid_plan_json,
    synthesis_json,
    evidence_handler,
    tmp_path,
) -> None:
    citations = [
        ("ev-a", "https://example.com/a"),
        ("ev-b", "https://example.org/b"),
        ("ev-c", None),
    ]
    llm = fake_llm(valid_plan_json, synthesis_json(citations))
    orchestrator = Orchestrator(
        "  What   is X?  ",
        llm=llm,
        registry=registry,
        handler=evidence_handler,
        config=Config(report_dir=str(tmp_path), memory_enabled=False),
        clock=fixed_clock,
    )
    state = await orchestrator.run()

    assert state.status.value == "completed"
    assert state.final_report is not None
    assert state.report_path is not None
    path = Path(state.report_path)
    assert path.parent == tmp_path
    content = path.read_text(encoding="utf-8")
    for section in SECTIONS:
        assert section in content
    assert state.final_report.research_question == "What is X?"
    assert [f.evidence_id for f in state.final_report.key_findings] == [
        "ev-a",
        "ev-b",
        "ev-c",
    ]
    assert _kinds(state)[-3:] == [
        EventKind.SYNTHESIS_STARTED,
        EventKind.REPORT_GENERATED,
        EventKind.RUN_COMPLETED,
    ]
    console = capsys.readouterr().out
    assert "[SYNTHESIS] composing report from 3 evidence items" in console
    assert f"[REPORT] {state.report_path} — 3 findings from 3 evidence items" in console
    assert "  - Sources: 2 searched, 2 used, 0 rejected" in console
    assert "  - Failures: none" in console
    assert "[DONE] completed — 2 steps, 0 failed steps, 2 retries, 0.0s" in console


async def test_citation_degradation_ships_report_with_warning(
    fake_llm, registry, fixed_clock, valid_plan_json, synthesis_json, evidence_handler, tmp_path
) -> None:
    ghost_citations = [
        ("ev-a", "https://example.com/a"),
        ("ev-ghost", "https://ghost.example"),
    ]
    llm = fake_llm(
        valid_plan_json,
        synthesis_json(ghost_citations),
        synthesis_json(ghost_citations),
    )
    orchestrator = Orchestrator(
        "What is X?",
        llm=llm,
        registry=registry,
        handler=evidence_handler,
        config=Config(report_dir=str(tmp_path), memory_enabled=False),
        clock=fixed_clock,
    )
    state = await orchestrator.run()

    assert state.status.value == "completed"
    assert state.final_report is not None
    assert [f.evidence_id for f in state.final_report.key_findings] == ["ev-a"]
    assert any(
        "synthesis dropped 1 finding(s) with invalid citations" in warning
        and "ev-ghost" in warning
        for warning in state.warnings
    )
    assert Path(state.report_path).exists()


async def test_synthesis_exhaustion_fails_run_without_report_file(
    fake_llm, registry, fixed_clock, valid_plan_json, evidence_handler, tmp_path
) -> None:
    llm = fake_llm(
        valid_plan_json,
        "definitely not json",
        '{"missing_sections": true}',
    )
    orchestrator = Orchestrator(
        "What is X?",
        llm=llm,
        registry=registry,
        handler=evidence_handler,
        config=Config(report_dir=str(tmp_path), memory_enabled=False),
        clock=fixed_clock,
    )
    state = await orchestrator.run()

    assert state.status.value == "failed"
    assert state.final_report is None
    assert state.report_path is None
    assert list(tmp_path.glob("*.md")) == []
    assert any(
        "synthesis still invalid after 2 attempts" in warning for warning in state.warnings
    )
    kinds = _kinds(state)
    assert EventKind.SYNTHESIS_STARTED in kinds
    assert EventKind.REPORT_GENERATED not in kinds
    assert kinds[-1] is EventKind.RUN_COMPLETED
    assert state.execution_events[-1].data["status"] == "failed"


async def test_llm_exhaustion_during_synthesis_fails_run_gracefully(
    fake_llm, registry, fixed_clock, valid_plan_json, evidence_handler, tmp_path
) -> None:
    llm = fake_llm(valid_plan_json)
    orchestrator = Orchestrator(
        "What is X?",
        llm=llm,
        registry=registry,
        handler=evidence_handler,
        config=Config(report_dir=str(tmp_path), memory_enabled=False),
        clock=fixed_clock,
    )
    state = await orchestrator.run()

    assert state.status.value == "failed"
    assert any(
        "synthesis LLM call failed" in warning for warning in state.warnings
    )
    assert list(tmp_path.glob("*.md")) == []
    assert state.execution_events[-1].event is EventKind.RUN_COMPLETED


async def test_canned_evidence_to_synthesis_writes_markdown_file(
    fake_llm, synthesis_json, tmp_path
) -> None:
    evidence = [
        Evidence(
            evidence_id="ev-a",
            claim="Revenue grew 12%",
            source_url="https://example.com/a",
            source_title="Example A",
            extracted_text="Revenue grew 12% year over year.",
            relevance_score=0.9,
            confidence=0.8,
            retrieved_at=FIXED_TIME,
            step_id="r1",
        ),
        Evidence(
            evidence_id="q1:1",
            claim="2 + 2 = 4",
            source_url=None,
            source_title=None,
            extracted_text="2 + 2 = 4",
            relevance_score=0.9,
            confidence=0.9,
            retrieved_at=FIXED_TIME,
            step_id="q1",
        ),
    ]
    state = AgentState(user_goal="What is X?")
    state.normalized_goal = "What is X?"
    state.plan = Plan.model_validate(
        {
            "goal": "What is X?",
            "steps": [
                {
                    "id": "r1",
                    "objective": "search",
                    "tool": "web_search",
                    "arguments": {"query": "X"},
                    "expected_output": "results",
                }
            ],
        }
    )
    state.evidence = evidence
    state.sources = [
        Source(url="https://example.com/a", domain="example.com", retrieved_at=FIXED_TIME)
    ]
    state.completed_steps = [
        StepRecord(
            step_id="r1",
            objective="search",
            tool="web_search",
            status=StepStatus.SUCCEEDED,
            result_ids=["r1-web_search-1"],
        )
    ]
    events: list = []
    emitter = EventEmitter(lambda: FIXED_TIME, sinks=[InMemoryEventSink(events)])
    llm = fake_llm(synthesis_json([("ev-a", "https://example.com/a"), ("q1:1", None)]))
    report = await synthesize(
        llm,
        state,
        config=Config(),
        emitter=emitter,
        clock=lambda: FIXED_TIME,
        started_at=FIXED_TIME,
    )

    known_ids = {item.evidence_id for item in evidence}
    assert all(f.evidence_id in known_ids for f in report.key_findings)
    path = Path(write_report(report, report_dir=str(tmp_path), goal="What is X?"))
    content = path.read_text(encoding="utf-8")
    for section in SECTIONS:
        assert section in content
