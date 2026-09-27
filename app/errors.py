from __future__ import annotations

from typing import ClassVar

from app.models.events import FailureKind


class ResearchPilotError(Exception):
    failure_kind: ClassVar[FailureKind | None] = None


class ToolError(ResearchPilotError):
    def __init__(self, message: str, *, tool: str | None = None) -> None:
        super().__init__(message)
        self.tool = tool


class TransientToolError(ToolError):
    failure_kind = FailureKind.TRANSIENT


class PermanentToolError(ToolError):
    failure_kind = FailureKind.PERMANENT


class MalformedToolOutputError(ToolError):
    failure_kind = FailureKind.MALFORMED_OUTPUT


class ToolInputValidationError(ResearchPilotError):
    failure_kind = FailureKind.VALIDATION_ERROR


class PlanValidationError(ResearchPilotError):
    failure_kind = FailureKind.VALIDATION_ERROR


class PlannerError(ResearchPilotError):
    failure_kind = FailureKind.PLANNER_ERROR


class EvidenceValidationError(ResearchPilotError):
    failure_kind = FailureKind.VALIDATION_ERROR


class SynthesisError(ResearchPilotError):
    failure_kind = FailureKind.VALIDATION_ERROR


class ConfigurationError(ResearchPilotError):
    failure_kind = None


class ToolNotFoundError(ResearchPilotError):
    failure_kind = None
