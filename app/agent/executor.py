from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, Field

from app.agent.state import StepRecord
from app.errors import ConfigurationError, ResearchPilotError
from app.models.events import EventKind, FailureKind, ToolResult
from app.models.evidence import Evidence, Source
from app.models.plan import Plan, PlanStep, StepStatus
from app.observability import EventEmitter

OnStepStart = Callable[[PlanStep, int, int], None]

_TERMINAL_FAILED = (StepStatus.FAILED, StepStatus.SKIPPED)


class StepOutcome(BaseModel):
    tool_results: list[ToolResult] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    error: str | None = None
    failure_kind: FailureKind | None = None


class StepHandler(Protocol):
    async def handle(self, step: PlanStep) -> StepOutcome: ...


class StepBundle(BaseModel):
    record: StepRecord
    tool_results: list[ToolResult] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)


async def execute_plan(
    plan: Plan,
    handler: StepHandler,
    *,
    emitter: EventEmitter,
    on_step_start: OnStepStart | None = None,
) -> list[StepBundle]:
    total = len(plan.steps)
    index_of = {step.id: position for position, step in enumerate(plan.steps, start=1)}
    bundles: dict[str, StepBundle] = {}
    remaining = {step.id for step in plan.steps}

    while remaining:
        wave: list[PlanStep] = []
        skipped_any = False
        for step in plan.steps:
            if step.id not in remaining:
                continue
            if any(dep in remaining for dep in step.depends_on):
                continue
            failed_dep = next(
                (
                    dep
                    for dep in step.depends_on
                    if bundles[dep].record.status in _TERMINAL_FAILED
                ),
                None,
            )
            if failed_dep is not None:
                dep_status = bundles[failed_dep].record.status
                record = StepRecord(
                    step_id=step.id,
                    objective=step.objective,
                    tool=step.tool,
                    status=StepStatus.SKIPPED,
                    error=f"prerequisite {failed_dep!r} {dep_status.value}",
                )
                step.status = StepStatus.SKIPPED
                bundles[step.id] = StepBundle(record=record)
                remaining.discard(step.id)
                skipped_any = True
                continue
            wave.append(step)

        if not wave:
            if skipped_any:
                continue
            if remaining:
                raise ConfigurationError(
                    f"execution stalled with unresolved steps: {sorted(remaining)}"
                )
            break

        results = await asyncio.gather(
            *(
                _run_step(
                    step,
                    handler,
                    emitter,
                    on_step_start,
                    index_of[step.id],
                    total,
                )
                for step in wave
            )
        )
        for step, bundle in results:
            bundles[step.id] = bundle
            remaining.discard(step.id)

    return [bundles[step.id] for step in plan.steps]


async def _run_step(
    step: PlanStep,
    handler: StepHandler,
    emitter: EventEmitter,
    on_step_start: OnStepStart | None,
    index: int,
    total: int,
) -> tuple[PlanStep, StepBundle]:
    step.status = StepStatus.RUNNING
    if on_step_start is not None:
        on_step_start(step, index, total)
    try:
        outcome = await handler.handle(step)
    except ResearchPilotError as exc:
        outcome = StepOutcome(error=str(exc), failure_kind=exc.failure_kind)
    except Exception as exc:
        outcome = StepOutcome(error=str(exc))
    result_ids = [result.result_id for result in outcome.tool_results]
    if outcome.error is not None:
        step.status = StepStatus.FAILED
        record = StepRecord(
            step_id=step.id,
            objective=step.objective,
            tool=step.tool,
            status=StepStatus.FAILED,
            result_ids=result_ids,
            error=outcome.error,
            failure_kind=outcome.failure_kind,
        )
        emitter.emit(
            EventKind.STEP_FAILED,
            step_id=step.id,
            message=outcome.error,
            status=outcome.failure_kind.value if outcome.failure_kind else None,
        )
    else:
        step.status = StepStatus.SUCCEEDED
        record = StepRecord(
            step_id=step.id,
            objective=step.objective,
            tool=step.tool,
            status=StepStatus.SUCCEEDED,
            result_ids=result_ids,
        )
    bundle = StepBundle(
        record=record,
        tool_results=outcome.tool_results,
        evidence=outcome.evidence,
        sources=outcome.sources,
    )
    return step, bundle
