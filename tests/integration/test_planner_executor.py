from __future__ import annotations

import pytest

from app.agent.orchestrator import Orchestrator
from app.config import Config
from app.errors import PlannerError, PlanValidationError
from app.models.events import EventKind
from app.models.plan import StepStatus


async def test_planner_to_executor_full_phase_run(
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
        config=Config(report_dir=str(tmp_path)),
        clock=fixed_clock,
    )
    state = await orchestrator.run()

    assert state is orchestrator.state
    assert state.status.value == "completed"
    assert state.normalized_goal == "What is X?"
    assert [record.step_id for record in state.completed_steps] == ["r1", "q1"]
    assert state.failed_steps == []
    assert [step.status for step in state.plan.steps] == [
        StepStatus.SUCCEEDED,
        StepStatus.SUCCEEDED,
    ]
    kinds = [event.event for event in state.execution_events]
    assert kinds == [
        EventKind.GOAL_NORMALIZED,
        EventKind.PLAN_CREATED,
        EventKind.SYNTHESIS_STARTED,
        EventKind.REPORT_GENERATED,
        EventKind.RUN_COMPLETED,
    ]
    assert all(event.timestamp == fixed_clock() for event in state.execution_events)
    assert state.retry_counts == {"r1:web_search:1": 0, "q1:calculator:1": 2}
    assert [result.tool for result in state.tool_results] == [
        "web_search",
        "calculator",
    ]
    assert len(state.evidence) == 3
    assert state.warnings == []
    assert state.current_step is not None
    assert state.final_report is not None
    assert state.final_report.research_question == "What is X?"
    assert state.report_path is not None
    assert state.report_path.endswith("what-is-x.md")

    console_output = capsys.readouterr().out
    assert "[GOAL] What is X?" in console_output
    assert "[PLAN] 2 steps:" in console_output
    assert "[1/2] doing search for X..." in console_output
    assert "[2/2] doing compute 2+2..." in console_output
    assert "[SYNTHESIS] composing report from 3 evidence items" in console_output
    assert "[DONE] completed — 2 steps, 0 failures, 2 retries, 0.0s" in console_output


async def test_planner_exhaustion_marks_state_failed(
    fake_llm, registry, fixed_clock, evidence_handler
) -> None:
    llm = fake_llm("bad one", "bad two", "bad three")
    orchestrator = Orchestrator(
        "What is X?",
        llm=llm,
        registry=registry,
        handler=evidence_handler,
        config=Config(),
        clock=fixed_clock,
    )
    with pytest.raises(PlannerError):
        await orchestrator.run()
    state = orchestrator.state
    assert state.status.value == "failed"
    assert state.plan is None
    kinds = [event.event for event in state.execution_events]
    assert kinds == [
        EventKind.GOAL_NORMALIZED,
        EventKind.PLAN_INVALID,
        EventKind.PLAN_INVALID,
        EventKind.PLAN_INVALID,
    ]


async def test_zero_evidence_marks_run_failed(
    fake_llm, registry, fixed_clock, valid_plan_json, empty_handler
) -> None:
    llm = fake_llm(valid_plan_json)
    orchestrator = Orchestrator(
        "What is X?",
        llm=llm,
        registry=registry,
        handler=empty_handler,
        config=Config(),
        clock=fixed_clock,
    )
    state = await orchestrator.run()
    assert state.status.value == "failed"
    assert [record.status for record in state.completed_steps] == [
        StepStatus.SUCCEEDED,
        StepStatus.SUCCEEDED,
    ]
    assert any("no evidence" in warning for warning in state.warnings)


async def test_empty_goal_fails_before_any_llm_call(
    fake_llm, registry, fixed_clock, valid_plan_json, evidence_handler
) -> None:
    llm = fake_llm(valid_plan_json)
    orchestrator = Orchestrator(
        "   \t  ",
        llm=llm,
        registry=registry,
        handler=evidence_handler,
        config=Config(),
        clock=fixed_clock,
    )
    with pytest.raises(PlanValidationError):
        await orchestrator.run()
    assert orchestrator.state.status.value == "failed"
    assert llm.calls == []


async def test_failed_step_with_remaining_evidence_completes_run(
    fake_llm,
    registry,
    fixed_clock,
    valid_plan_json,
    synthesis_json,
    evidence_factory,
    tool_result_factory,
    tmp_path,
) -> None:
    class PartialHandler:
        async def handle(self, step):
            from app.agent.executor import StepOutcome

            if step.id == "r1":
                return StepOutcome(
                    error="all candidates failed",
                    failure_kind=None,
                    tool_results=[tool_result_factory("r1", "web_search", success=False)],
                )
            return StepOutcome(
                tool_results=[tool_result_factory(step.id, "calculator", attempts=3)],
                evidence=[
                    evidence_factory("ev-a", "claim", url="https://example.com/a"),
                    evidence_factory("ev-b", "claim", url="https://example.com/b"),
                    evidence_factory("ev-c", "2+2=4", url=None),
                ],
            )

    llm = fake_llm(
        valid_plan_json,
        synthesis_json([("ev-c", None)]),
    )
    orchestrator = Orchestrator(
        "What is X?",
        llm=llm,
        registry=registry,
        handler=PartialHandler(),
        config=Config(report_dir=str(tmp_path)),
        clock=fixed_clock,
    )
    state = await orchestrator.run()
    assert state.status.value == "completed"
    assert [record.step_id for record in state.failed_steps] == ["r1"]
    assert [record.step_id for record in state.completed_steps] == ["q1"]
    assert any("step r1 failed" in warning for warning in state.warnings)
    assert state.retry_counts == {"r1:web_search:1": 0, "q1:calculator:1": 2}
    assert state.final_report is not None
    assert state.final_report.execution_summary.steps_failed == 1
    assert [record.step_id for record in state.final_report.execution_summary.failures] == [
        "r1"
    ]
