from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class FailureKind(str, Enum):              # §12 taxonomy — maps 1:1 from errors.py
    TRANSIENT = "transient"                # timeout, 5xx, connection reset → retriable
    PERMANENT = "permanent"                # 404, DNS failure, blocked → not retriable
    MALFORMED_OUTPUT = "malformed_output"   # tool output failed its output schema
    PLANNER_ERROR = "planner_error"        # plan invalid after bounded correction
    VALIDATION_ERROR = "validation_error"  # structural failure (plan or evidence)


class FailureMode(str, Enum):              # failure_simulator input (§7)
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    TEMPORARY_ERROR = "temporary_error"


class ToolResult(BaseModel):
    result_id: str
    tool: str
    step_id: str
    call_ordinal: int                  # nth tool call within the step (the research
                                       #  unit makes several: search + k fetches)
    success: bool
    output: dict[str, Any] | None = None  # schema-validated output, serialized
    attempts: int = 1                  # runner attempts consumed
    failure_kind: FailureKind | None = None
    error: str | None = None
    duration_ms: int = 0
    started_at: datetime


class EventKind(str, Enum):
    # §13 floor
    PLAN_CREATED = "plan_created"
    TOOL_SELECTED = "tool_selected"
    TOOL_STARTED = "tool_started"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    RETRY_STARTED = "retry_started"
    EVIDENCE_ADDED = "evidence_added"
    EVIDENCE_DEDUPLICATED = "evidence_deduplicated"
    SYNTHESIS_STARTED = "synthesis_started"
    REPORT_GENERATED = "report_generated"
    # declared extensions (deviation D9)
    GOAL_NORMALIZED = "goal_normalized"
    PLAN_INVALID = "plan_invalid"
    STEP_FAILED = "step_failed"
    SOURCE_UNAVAILABLE = "source_unavailable"
    CANDIDATE_ADVANCED = "candidate_advanced"
    EVIDENCE_REJECTED = "evidence_rejected"
    EVIDENCE_CONFLICT = "evidence_conflict"
    RUN_COMPLETED = "run_completed"


class ExecutionEvent(BaseModel):
    timestamp: datetime                # injected clock, not time.time()
    event: EventKind
    tool: str | None = None
    step_id: str | None = None
    status: str | None = None          # free-form detail (e.g. "timeout")
    attempt: int | None = None
    message: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)  # NEVER secrets (§26)
