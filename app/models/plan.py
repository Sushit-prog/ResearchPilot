from __future__ import annotations

from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from app.errors import PlanValidationError


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"       # a depends_on prerequisite failed


class PlanStep(BaseModel):
    id: str                            # unique within the plan, e.g. "r1", "q1"
    objective: str                     # human-readable; shown in the visible trace
    tool: str                          # MUST be a ToolRegistry name — validated (§6)
    arguments: dict[str, Any] = Field(default_factory=dict)  # static per plan-time invariant (§4.3)
    expected_output: str               # what this step must produce, LLM-declared
    # reserved — no populated v1 case (§4.5)
    depends_on: list[str] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING   # mutated by the executor during the run


class Plan(BaseModel):
    goal: str
    steps: list[PlanStep]              # structural checks below; context checks (registry,
                                       # tool input schemas, config cap) in verifier (§4.4)

    @model_validator(mode="after")
    def _check_structure(self) -> Self:
        if not self.steps:
            raise PlanValidationError("plan must contain at least one step")

        seen: set[str] = set()
        duplicates: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                duplicates.add(step.id)
            seen.add(step.id)
        if duplicates:
            raise PlanValidationError(f"duplicate step ids: {sorted(duplicates)}")

        for step in self.steps:
            dangling = [dep for dep in step.depends_on if dep not in seen]
            if dangling:
                raise PlanValidationError(
                    f"dangling depends_on in step {step.id!r}: {sorted(dangling)}"
                )

        in_degree = {step.id: len(step.depends_on) for step in self.steps}
        ready = [step_id for step_id, degree in in_degree.items() if degree == 0]
        processed = 0
        while ready:
            current = ready.pop()
            processed += 1
            for step in self.steps:
                if current in step.depends_on:
                    in_degree[step.id] -= 1
                    if in_degree[step.id] == 0:
                        ready.append(step.id)
        if processed != len(self.steps):
            cyclic = sorted(step_id for step_id, degree in in_degree.items() if degree > 0)
            raise PlanValidationError(f"cyclic depends_on involving: {cyclic}")

        return self
