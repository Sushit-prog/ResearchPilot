from __future__ import annotations

import pytest

from app.errors import (
    ConfigurationError,
    EvidenceValidationError,
    MalformedToolOutputError,
    PermanentToolError,
    PlannerError,
    PlanValidationError,
    ResearchPilotError,
    ToolError,
    ToolInputValidationError,
    ToolNotFoundError,
    TransientToolError,
)
from app.models.events import FailureKind

ALL_ERRORS = [
    TransientToolError,
    PermanentToolError,
    MalformedToolOutputError,
    ToolInputValidationError,
    PlanValidationError,
    PlannerError,
    EvidenceValidationError,
    ConfigurationError,
    ToolNotFoundError,
]


@pytest.mark.parametrize(
    ("error_type", "expected_kind"),
    [
        (TransientToolError, FailureKind.TRANSIENT),
        (PermanentToolError, FailureKind.PERMANENT),
        (MalformedToolOutputError, FailureKind.MALFORMED_OUTPUT),
        (ToolInputValidationError, FailureKind.VALIDATION_ERROR),
        (PlanValidationError, FailureKind.VALIDATION_ERROR),
        (PlannerError, FailureKind.PLANNER_ERROR),
        (EvidenceValidationError, FailureKind.VALIDATION_ERROR),
        (ConfigurationError, None),
        (ToolNotFoundError, None),
    ],
)
def test_failure_kind_mapping_table(
    error_type: type[ResearchPilotError], expected_kind: FailureKind | None
) -> None:
    assert error_type.failure_kind is expected_kind


def test_all_errors_inherit_base() -> None:
    for error_type in ALL_ERRORS:
        assert issubclass(error_type, ResearchPilotError)


def test_tool_errors_inherit_tool_error() -> None:
    for error_type in (TransientToolError, PermanentToolError, MalformedToolOutputError):
        assert issubclass(error_type, ToolError)
    for error_type in (ToolInputValidationError, PlanValidationError, PlannerError):
        assert not issubclass(error_type, ToolError)


def test_plan_validation_error_is_not_value_error() -> None:
    assert not issubclass(PlanValidationError, (ValueError, AssertionError))


def test_tool_error_carries_tool_name() -> None:
    error = TransientToolError("timed out", tool="webpage_fetch")
    assert error.tool == "webpage_fetch"
    assert str(error) == "timed out"
    assert PermanentToolError("blocked").tool is None


def test_error_messages_preserved() -> None:
    error = PlanValidationError("plan rejected: duplicate step ids: ['r1']")
    assert "duplicate step ids" in str(error)
