from __future__ import annotations

import pydantic
import pytest

from app.agent.state import AgentState, AgentStatus, StepRecord
from app.models.events import FailureKind
from app.models.plan import Plan, StepStatus


def test_agent_state_field_list_matches_spec() -> None:
    assert list(AgentState.model_fields) == [
        "user_goal",
        "normalized_goal",
        "plan",
        "current_step",
        "completed_steps",
        "failed_steps",
        "evidence",
        "sources",
        "tool_results",
        "retry_counts",
        "warnings",
        "execution_events",
        "final_report",
        "status",
    ]


def test_agent_status_exact_members() -> None:
    assert {member.value for member in AgentStatus} == {
        "init",
        "planning",
        "executing",
        "synthesizing",
        "completed",
        "failed",
    }


def test_agent_state_defaults() -> None:
    state = AgentState(user_goal="Research X")
    assert state.normalized_goal == ""
    assert state.plan is None
    assert state.current_step is None
    assert state.completed_steps == []
    assert state.failed_steps == []
    assert state.evidence == []
    assert state.sources == []
    assert state.tool_results == []
    assert state.retry_counts == {}
    assert state.warnings == []
    assert state.execution_events == []
    assert state.final_report is None
    assert state.status is AgentStatus.INIT


def test_agent_state_missing_user_goal_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        AgentState.model_validate({})


def test_step_record_required_fields_and_defaults() -> None:
    record = StepRecord.model_validate(
        {
            "step_id": "r1",
            "objective": "search",
            "tool": "web_search",
            "status": StepStatus.SUCCEEDED,
        }
    )
    assert record.result_ids == []
    assert record.error is None
    assert record.failure_kind is None
    assert record.status is StepStatus.SUCCEEDED
    incomplete = {"step_id": "r1", "objective": "o", "tool": "web_search"}
    with pytest.raises(pydantic.ValidationError):
        StepRecord.model_validate(incomplete)


def test_step_record_failure_kind_typing() -> None:
    record = StepRecord.model_validate(
        {
            "step_id": "r1",
            "objective": "fetch",
            "tool": "webpage_fetch",
            "status": StepStatus.FAILED,
            "error": "timed out",
            "failure_kind": FailureKind.TRANSIENT,
        }
    )
    assert record.failure_kind is FailureKind.TRANSIENT


def test_agent_state_json_round_trip_with_nested_plan() -> None:
    state = AgentState(
        user_goal="Research X",
        normalized_goal="research x",
        plan=Plan.model_validate(
            {
                "goal": "Research X",
                "steps": [
                    {
                        "id": "r1",
                        "objective": "search",
                        "tool": "web_search",
                        "arguments": {"query": "x"},
                        "expected_output": "findings",
                    }
                ],
            }
        ),
        status=AgentStatus.PLANNING,
    )
    restored = AgentState.model_validate_json(state.model_dump_json())
    assert restored == state
    assert restored.status is AgentStatus.PLANNING
    assert restored.plan is not None
    assert restored.plan.steps[0].id == "r1"
