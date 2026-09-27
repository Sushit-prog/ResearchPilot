from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.agent.executor import StepHandler, execute_plan
from app.agent.planner import generate_plan, normalize_goal
from app.agent.state import AgentState, AgentStatus
from app.agent.synthesizer import execution_summary_lines, synthesize, write_report
from app.agent.verifier import (
    Sufficiency,
    deduplicate_evidence,
    evidence_sufficiency,
    filter_evidence,
    numeric_pass,
)
from app.config import Config
from app.errors import PlannerError, PlanValidationError, SynthesisError
from app.llm.base import LLMProvider
from app.models.events import EventKind
from app.models.plan import PlanStep, StepStatus
from app.observability import ConsoleEventSink, EventEmitter, InMemoryEventSink
from app.reliability.runner import Runner
from app.reliability.validation import normalize_url
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
        emitter: EventEmitter | None = None,
    ) -> None:
        self.config = config if config is not None else Config()
        self._llm = llm
        self._registry = registry
        self._handler = handler
        self._clock = clock if clock is not None else _default_clock
        self.state = AgentState(user_goal=goal)
        # built in __init__ so an injected handler (Researcher) can share the
        # SAME emitter — tool/evidence events must land in state.execution_events
        self._emitter = (
            emitter
            if emitter is not None
            else EventEmitter(
                self._clock,
                sinks=[
                    InMemoryEventSink(self.state.execution_events),
                    ConsoleEventSink(verbose=self.config.verbose),
                ],
            )
        )

    async def run(self) -> AgentState:
        state = self.state
        emitter = self._emitter
        started_at = self._clock()

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

        # Stage 8 + 9 (§4.10/§4.11): dedup → filter/validate → numeric pass
        # run before sufficiency, so thresholds count only evidence that
        # survived validation.
        source_keys: set[str] = set()
        for source in state.sources:
            try:
                source_keys.add(normalize_url(source.url))
            except ValueError:
                continue
        deduped = deduplicate_evidence(state.evidence, emitter=emitter)
        filtered, reject_warnings = filter_evidence(
            deduped, emitter=emitter, source_keys=source_keys
        )
        state.warnings.extend(reject_warnings)
        if "calculator" in self._registry.names():
            verifier_runner = Runner(self.config, emitter, clock=self._clock)
            evidence, conflict_warnings, verification_results = await numeric_pass(
                filtered,
                runner=verifier_runner,
                calculator=self._registry.get("calculator"),
                emitter=emitter,
            )
            state.tool_results.extend(verification_results)
            for result in verification_results:
                key = f"{result.step_id}:{result.tool}:{result.call_ordinal}"
                retries = max(result.attempts - 1, 0)
                state.retry_counts[key] = max(state.retry_counts.get(key, 0), retries)
            state.warnings.extend(conflict_warnings)
        else:
            evidence = filtered
        state.evidence = evidence

        sufficiency = evidence_sufficiency(
            state.evidence,
            min_evidence=self.config.min_evidence,
            min_distinct_sources=self.config.min_distinct_sources,
        )
        if sufficiency is Sufficiency.EMPTY:
            state.status = AgentStatus.FAILED
            state.warnings.append("no evidence collected — no report possible")
            _emit_run_completed(emitter, state, started_at=started_at, clock=self._clock)
            return state
        if sufficiency is Sufficiency.INSUFFICIENT:
            state.warnings.append(
                "evidence below thresholds — synthesis must degrade, not claim completeness"
            )

        state.status = AgentStatus.SYNTHESIZING
        try:
            report = await synthesize(
                self._llm,
                state,
                config=self.config,
                emitter=emitter,
                clock=self._clock,
                started_at=started_at,
            )
        except SynthesisError as exc:
            state.status = AgentStatus.FAILED
            state.warnings.append(str(exc))
            _emit_run_completed(emitter, state, started_at=started_at, clock=self._clock)
            return state
        state.final_report = report
        path = write_report(
            report, report_dir=self.config.report_dir, goal=state.user_goal
        )
        state.report_path = path
        emitter.emit(
            EventKind.REPORT_GENERATED,
            message=path,
            data={
                "path": path,
                "findings": len(report.key_findings),
                "evidence": len(state.evidence),
                "summary": execution_summary_lines(report.execution_summary),
            },
        )
        state.status = AgentStatus.COMPLETED
        _emit_run_completed(emitter, state, started_at=started_at, clock=self._clock)
        return state


def _emit_run_completed(
    emitter: EventEmitter,
    state: AgentState,
    *,
    started_at: datetime,
    clock: Callable[[], datetime],
) -> None:
    emitter.emit(
        EventKind.RUN_COMPLETED,
        status=state.status.value,
        data={
            "status": state.status.value,
            "steps": len(state.plan.steps) if state.plan else 0,
            "failures": len(state.failed_steps),
            "retries": sum(state.retry_counts.values()),
            "duration_s": max((clock() - started_at).total_seconds(), 0.0),
        },
    )
