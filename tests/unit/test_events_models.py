from __future__ import annotations

from datetime import datetime, timezone

import pydantic
import pytest

from app.models.events import EventKind, ExecutionEvent, FailureKind, FailureMode, ToolResult

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

FLOOR = {
    "plan_created",
    "tool_selected",
    "tool_started",
    "tool_succeeded",
    "tool_failed",
    "retry_started",
    "evidence_added",
    "evidence_deduplicated",
    "synthesis_started",
    "report_generated",
}

EXTENSIONS = {
    "goal_normalized",
    "plan_invalid",
    "step_failed",
    "source_unavailable",
    "candidate_advanced",
    "evidence_rejected",
    "evidence_conflict",
    "run_completed",
    "memory_lookup",
}


def test_event_kind_covers_section_13_floor() -> None:
    values = {member.value for member in EventKind}
    assert FLOOR <= values


def test_event_kind_exact_members() -> None:
    assert {member.value for member in EventKind} == FLOOR | EXTENSIONS


def test_failure_kind_exact_members() -> None:
    assert {member.value for member in FailureKind} == {
        "transient",
        "permanent",
        "malformed_output",
        "planner_error",
        "validation_error",
    }


def test_failure_mode_exact_members() -> None:
    assert {member.value for member in FailureMode} == {
        "timeout",
        "invalid_response",
        "temporary_error",
    }


def _tool_result_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "result_id": "tr1",
        "tool": "web_search",
        "step_id": "r1",
        "call_ordinal": 0,
        "success": True,
        "started_at": NOW,
    }
    data.update(overrides)
    return data


def test_tool_result_defaults() -> None:
    result = ToolResult.model_validate(_tool_result_data())
    assert result.output is None
    assert result.attempts == 1
    assert result.failure_kind is None
    assert result.error is None
    assert result.duration_ms == 0


@pytest.mark.parametrize(
    "field", ["result_id", "tool", "step_id", "call_ordinal", "success", "started_at"]
)
def test_tool_result_missing_required_field_rejected(field: str) -> None:
    data = _tool_result_data()
    del data[field]
    with pytest.raises(pydantic.ValidationError):
        ToolResult.model_validate(data)


def test_execution_event_required_fields_and_defaults() -> None:
    event = ExecutionEvent.model_validate({"timestamp": NOW, "event": EventKind.TOOL_STARTED})
    assert event.tool is None
    assert event.step_id is None
    assert event.status is None
    assert event.attempt is None
    assert event.message is None
    assert event.data == {}
    with pytest.raises(pydantic.ValidationError):
        ExecutionEvent.model_validate({"timestamp": NOW})
    with pytest.raises(pydantic.ValidationError):
        ExecutionEvent.model_validate({"event": EventKind.TOOL_STARTED})


def test_execution_event_data_isolated_per_instance() -> None:
    first = ExecutionEvent.model_validate({"timestamp": NOW, "event": EventKind.RETRY_STARTED})
    second = ExecutionEvent.model_validate({"timestamp": NOW, "event": EventKind.RETRY_STARTED})
    first.data["attempt"] = 1
    assert second.data == {}


def test_execution_event_json_round_trip() -> None:
    event = ExecutionEvent.model_validate(
        {
            "timestamp": NOW,
            "event": EventKind.TOOL_FAILED,
            "tool": "webpage_fetch",
            "step_id": "r1",
            "status": "timeout",
            "attempt": 3,
            "message": "timed out",
            "data": {"url": "https://example.com"},
        }
    )
    restored = ExecutionEvent.model_validate_json(event.model_dump_json())
    assert restored == event
