from __future__ import annotations

from enum import Enum

from app.errors import PlanValidationError, ToolInputValidationError
from app.models.evidence import Evidence
from app.models.plan import Plan
from app.tools.base import ToolRegistry


def validate_plan(plan: Plan, registry: ToolRegistry, max_steps: int) -> None:
    if len(plan.steps) > max_steps:
        raise PlanValidationError(
            f"plan has {len(plan.steps)} steps; maximum is {max_steps}"
        )
    for step in plan.steps:
        if not step.expected_output.strip():
            raise PlanValidationError(f"step {step.id!r} has an empty expected_output")
        if step.tool not in registry.names():
            raise PlanValidationError(
                f"step {step.id!r} references unknown tool {step.tool!r}; "
                f"registered tools: {registry.names()}"
            )
        tool = registry.get(step.tool)
        try:
            tool.parse_input(step.arguments)
        except ToolInputValidationError as exc:
            raise PlanValidationError(f"step {step.id!r} arguments rejected: {exc}") from exc


class Sufficiency(str, Enum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    EMPTY = "empty"


def evidence_sufficiency(
    evidence: list[Evidence],
    *,
    min_evidence: int,
    min_distinct_sources: int,
) -> Sufficiency:
    if not evidence:
        return Sufficiency.EMPTY
    real_sources = {item.source_url for item in evidence if item.source_url is not None}
    if len(evidence) < min_evidence or len(real_sources) < min_distinct_sources:
        return Sufficiency.INSUFFICIENT
    return Sufficiency.SUFFICIENT
