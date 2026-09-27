from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.models.events import ExecutionEvent, FailureKind, ToolResult
from app.models.evidence import Evidence, Source
from app.models.plan import Plan, PlanStep, StepStatus
from app.models.report import Report


class AgentStatus(str, Enum):
    INIT = "init"
    PLANNING = "planning"
    EXECUTING = "executing"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"   # report produced — partial step failures surface in
                              # ExecutionSummary + warnings, NOT here
    FAILED = "failed"         # no report possible (unrecoverable planner/validation
                              # failure, or zero evidence)


class StepRecord(BaseModel):               # compact account of a finished step
    step_id: str
    objective: str
    tool: str
    status: StepStatus
    result_ids: list[str] = Field(default_factory=list)  # indexes into AgentState.tool_results
    error: str | None = None
    failure_kind: FailureKind | None = None


class AgentState(BaseModel):
    user_goal: str
    normalized_goal: str = ""
    plan: Plan | None = None
    current_step: PlanStep | None = None
    completed_steps: list[StepRecord] = Field(default_factory=list)
    failed_steps: list[StepRecord] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    # retry_counts key = "{step_id}:{tool}:{call_ordinal}"; hard max enforced by the runner (§12)
    retry_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    execution_events: list[ExecutionEvent] = Field(default_factory=list)
    final_report: Report | None = None
    status: AgentStatus = AgentStatus.INIT
