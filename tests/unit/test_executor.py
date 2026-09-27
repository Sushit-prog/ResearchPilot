from __future__ import annotations

import asyncio

from app.agent.executor import StepOutcome, execute_plan
from app.errors import TransientToolError
from app.models.events import EventKind
from app.models.plan import Plan, StepStatus
from app.observability import EventEmitter


class ScriptedHandler:
    def __init__(
        self,
        *,
        outcomes: dict[str, StepOutcome] | None = None,
        errors: dict[str, Exception] | None = None,
        delays: dict[str, float] | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.errors = errors or {}
        self.delays = delays or {}
        self.calls: list[str] = []
        self.completion: list[str] = []

    async def handle(self, step) -> StepOutcome:
        self.calls.append(step.id)
        delay = self.delays.get(step.id)
        if delay:
            await asyncio.sleep(delay)
        self.completion.append(step.id)
        if step.id in self.errors:
            raise self.errors[step.id]
        return self.outcomes.get(step.id, StepOutcome())


def _step(step_id: str, *, deps: list[str] | None = None, tool: str = "web_search") -> dict:
    return {
        "id": step_id,
        "objective": f"objective for {step_id}",
        "tool": tool,
        "arguments": {"query": "x"},
        "expected_output": "results",
        "depends_on": deps or [],
    }


def _plan(steps: list[dict]) -> Plan:
    return Plan.model_validate({"goal": "g", "steps": steps})


async def test_single_step_success(
    event_log, tool_result_factory, evidence_factory, source_factory
) -> None:
    emitter, events = event_log
    outcome = StepOutcome(
        tool_results=[tool_result_factory("r1", "web_search")],
        evidence=[evidence_factory("ev-a", "claim", url="https://example.com/a")],
        sources=[source_factory("https://example.com/a")],
    )
    handler = ScriptedHandler(outcomes={"r1": outcome})
    plan = _plan([_step("r1")])
    bundles = await execute_plan(plan, handler, emitter=emitter)
    assert [bundle.record.step_id for bundle in bundles] == ["r1"]
    record = bundles[0].record
    assert record.status is StepStatus.SUCCEEDED
    assert record.result_ids == ["r1-web_search-1"]
    assert record.error is None
    assert plan.steps[0].status is StepStatus.SUCCEEDED
    assert len(bundles[0].evidence) == 1
    assert events == []


async def test_concurrent_dispatch_merges_in_plan_order(event_log) -> None:
    emitter, _ = event_log
    handler = ScriptedHandler(
        delays={"r1": 0.04, "r2": 0.03, "r3": 0.02, "r4": 0.0},
    )
    plan = _plan([_step(f"r{i}") for i in range(1, 5)])
    bundles = await execute_plan(plan, handler, emitter=emitter)
    assert [bundle.record.step_id for bundle in bundles] == ["r1", "r2", "r3", "r4"]
    assert all(
        bundle.record.status is StepStatus.SUCCEEDED for bundle in bundles
    )
    assert handler.completion[0] == "r4"


async def test_failed_outcome_emits_step_failed(event_log) -> None:
    emitter, events = event_log
    outcome = StepOutcome(error="search backend down", failure_kind=None)
    handler = ScriptedHandler(outcomes={"r1": outcome})
    plan = _plan([_step("r1")])
    bundles = await execute_plan(plan, handler, emitter=emitter)
    record = bundles[0].record
    assert record.status is StepStatus.FAILED
    assert record.error == "search backend down"
    assert plan.steps[0].status is StepStatus.FAILED
    assert [event.event for event in events] == [EventKind.STEP_FAILED]
    assert events[0].step_id == "r1"
    assert events[0].message == "search backend down"


async def test_handler_exception_becomes_failed_step(event_log) -> None:
    emitter, events = event_log
    handler = ScriptedHandler(
        errors={"r1": TransientToolError("timed out", tool="web_search")}
    )
    plan = _plan([_step("r1")])
    bundles = await execute_plan(plan, handler, emitter=emitter)
    record = bundles[0].record
    assert record.status is StepStatus.FAILED
    assert record.error == "timed out"
    assert record.failure_kind is not None
    assert record.failure_kind.value == "transient"
    assert [event.event for event in events] == [EventKind.STEP_FAILED]
    assert events[0].status == "transient"


async def test_unexpected_exception_becomes_failed_step(event_log) -> None:
    emitter, _ = event_log
    handler = ScriptedHandler(errors={"r1": RuntimeError("bug in handler")})
    plan = _plan([_step("r1")])
    bundles = await execute_plan(plan, handler, emitter=emitter)
    record = bundles[0].record
    assert record.status is StepStatus.FAILED
    assert record.error == "bug in handler"
    assert record.failure_kind is None


async def test_skipped_when_prerequisite_fails(event_log) -> None:
    emitter, events = event_log
    handler = ScriptedHandler(errors={"s1": TransientToolError("down", tool="web_search")})
    plan = _plan(
        [
            _step("s1"),
            _step("s2", deps=["s1"]),
            _step("s3"),
        ]
    )
    bundles = await execute_plan(plan, handler, emitter=emitter)
    assert [bundle.record.step_id for bundle in bundles] == ["s1", "s2", "s3"]
    assert bundles[0].record.status is StepStatus.FAILED
    assert bundles[1].record.status is StepStatus.SKIPPED
    assert "s1" in bundles[1].record.error
    assert bundles[2].record.status is StepStatus.SUCCEEDED
    assert plan.steps[1].status is StepStatus.SKIPPED
    assert handler.calls == ["s1", "s3"]
    assert [event.event for event in events] == [EventKind.STEP_FAILED]


async def test_skipped_propagates_through_chain(event_log) -> None:
    emitter, _ = event_log
    handler = ScriptedHandler(errors={"s1": TransientToolError("down", tool="web_search")})
    plan = _plan(
        [
            _step("s1"),
            _step("s2", deps=["s1"]),
            _step("s3", deps=["s2"]),
        ]
    )
    bundles = await execute_plan(plan, handler, emitter=emitter)
    assert [bundle.record.status for bundle in bundles] == [
        StepStatus.FAILED,
        StepStatus.SKIPPED,
        StepStatus.SKIPPED,
    ]
    assert handler.calls == ["s1"]


async def test_on_step_start_receives_index_and_total(event_log) -> None:
    emitter, _ = event_log
    started: list[tuple[str, int, int]] = []

    def on_step_start(step, index: int, count: int) -> None:
        started.append((step.id, index, count))

    handler = ScriptedHandler(
        errors={"s1": TransientToolError("down", tool="web_search")}
    )
    plan = _plan([_step("s1"), _step("s2", deps=["s1"]), _step("s3")])
    await execute_plan(plan, handler, emitter=emitter, on_step_start=on_step_start)
    assert started == [("s1", 1, 3), ("s3", 3, 3)]


async def test_emitter_can_be_absent_of_sinks(fixed_clock) -> None:
    emitter = EventEmitter(fixed_clock)
    handler = ScriptedHandler()
    plan = _plan([_step("r1")])
    bundles = await execute_plan(plan, handler, emitter=emitter)
    assert bundles[0].record.status is StepStatus.SUCCEEDED
