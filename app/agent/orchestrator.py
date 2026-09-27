from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.agent.executor import StepHandler, execute_plan
from app.agent.planner import generate_plan, normalize_goal
from app.agent.state import AgentState, AgentStatus
from app.agent.verifier import Sufficiency, evidence_sufficiency
from app.config import Config
from app.errors import PlannerError, PlanValidationError
from app.llm.base import LLMProvider
from app.models.events import EventKind
from app.models.plan import PlanStep, StepStatus
from app.observability import ConsoleEventSink, EventEmitter, InMemoryEventSink
from app.tools.base import ToolRegistry


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class Orchestrator:
    def __init__(
        self,
        goal: str,
        *,
        llm: LLMProvider,
        registry: ToolRegistry,
        handler: StepHandler,
        config: Config | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config if config is not None else Config()
        self._llm = llm
        self._registry = registry
        self._handler = handler
        self._clock = clock if clock is not None else _default_clock
        self.state = AgentState(user_goal=goal)

    async def run(self) -> AgentState:
        state = self.state
        emitter = EventEmitter(
            self._clock,
            sinks=[
                InMemoryEventSink(state.execution_events),
                ConsoleEventSink(verbose=self.config.verbose),
            ],
        )

        normalized = normalize_goal(state.user_goal)
        state.normalized_goal = normalized
        emitter.emit(EventKind.GOAL_NORMALIZED, message=normalized)

        state.status = AgentStatus.PLANNING
        try:
            state.plan = await generate_plan(
                self._llm, self._registry, self.config, emitter, normalized
            )
        except (PlannerError, PlanValidationError):
            state.status = AgentStatus.FAILED
            raise

        state.status = AgentStatus.EXECUTING

        def on_step_start(step: PlanStep, index: int, count: int) -> None:
            state.current_step = step
            print(f"[{index}/{count}] doing {step.objective}...")

        bundles = await execute_plan(
            state.plan, self._handler, emitter=emitter, on_step_start=on_step_start
        )
        for bundle in bundles:
            state.tool_results.extend(bundle.tool_results)
            state.evidence.extend(bundle.evidence)
            state.sources.extend(bundle.sources)
            record = bundle.record
            if record.status is StepStatus.SUCCEEDED:
                state.completed_steps.append(record)
            elif record.status is StepStatus.SKIPPED:
                state.completed_steps.append(record)
                state.warnings.append(f"step {record.step_id} skipped: {record.error}")
            else:
                state.failed_steps.append(record)
                state.warnings.append(f"step {record.step_id} failed: {record.error}")
            for result in bundle.tool_results:
                key = f"{result.step_id}:{result.tool}:{result.call_ordinal}"
                retries = max(result.attempts - 1, 0)
                state.retry_counts[key] = max(state.retry_counts.get(key, 0), retries)

        sufficiency = evidence_sufficiency(
            state.evidence,
            min_evidence=self.config.min_evidence,
            min_distinct_sources=self.config.min_distinct_sources,
        )
        if sufficiency is Sufficiency.EMPTY:
            state.status = AgentStatus.FAILED
            state.warnings.append("no evidence collected — no report possible")
        elif sufficiency is Sufficiency.INSUFFICIENT:
            state.warnings.append(
                "evidence below thresholds — synthesis must degrade, not claim completeness"
            )
        return state
