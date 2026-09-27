from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from app.config import Config
from app.errors import (
    ConfigurationError,
    MalformedToolOutputError,
    PermanentToolError,
    PlannerError,
)
from app.models.events import EventKind, FailureKind
from app.observability import EventEmitter, InMemoryEventSink
from app.reliability.runner import Runner, classify
from app.tools.base import Tool
from app.tools.calculator import CalculatorTool
from app.tools.failure_simulator import FailureSimulatorTool

FIXED_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def zero(low: float, high: float) -> float:
    return 0.0


class _NeverInput(BaseModel):
    note: str = "x"


class _NeverOutput(BaseModel):
    ok: bool = True


class _NeverTool(Tool[_NeverInput, _NeverOutput]):
    name = "never_tool"
    description = "always fails with a permanent error"
    input_model = _NeverInput
    output_model = _NeverOutput
    default_timeout_s = 1.0

    async def execute(self, params: _NeverInput) -> _NeverOutput:
        raise PermanentToolError("HTTP 404", tool=self.name)


def build_runner(
    events: list[Any], *, config: Config | None = None
) -> tuple[Runner, list[float]]:
    delays: list[float] = []
    emitter = EventEmitter(lambda: FIXED_TIME, sinks=[InMemoryEventSink(events)])
    runner = Runner(
        config if config is not None else Config(),
        emitter,
        clock=lambda: FIXED_TIME,
        sleep=delays.append,
        rng=zero,
    )
    return runner, delays


def _kinds(events: list[Any]) -> list[EventKind]:
    return [event.event for event in events]


async def test_happy_path_calculator_produces_serialized_output() -> None:
    events: list[Any] = []
    runner, delays = build_runner(events)
    result = await runner.call(
        CalculatorTool(),
        step_id="q1",
        call_ordinal=1,
        arguments={"expression": "2 + 2"},
    )
    assert result.success is True
    assert result.attempts == 1
    assert result.output == {"expression": "2 + 2", "value": 4.0, "unit": None}
    assert result.result_id == "q1:calculator:1"
    assert result.duration_ms >= 0
    assert result.started_at == FIXED_TIME
    assert delays == []
    assert _kinds(events) == [EventKind.TOOL_STARTED, EventKind.TOOL_SUCCEEDED]
    assert events[1].attempt == 1


async def test_validation_failure_consumes_zero_attempts() -> None:
    events: list[Any] = []
    runner, delays = build_runner(events)
    result = await runner.call(
        CalculatorTool(),
        step_id="q1",
        call_ordinal=1,
        arguments={},
    )
    assert result.success is False
    assert result.attempts == 0
    assert result.failure_kind is FailureKind.VALIDATION_ERROR
    assert "invalid input" in (result.error or "")
    assert delays == []
    assert _kinds(events) == [EventKind.TOOL_FAILED]
    assert events[0].attempt is None
    assert events[0].status == "validation_error"


async def test_malformed_output_exhausts_retries() -> None:
    events: list[Any] = []
    runner, delays = build_runner(events)
    result = await runner.call(
        FailureSimulatorTool(),
        step_id="s1",
        call_ordinal=1,
        arguments={"failure_mode": "invalid_response", "fail_times": 10},
    )
    assert result.success is False
    assert result.attempts == 3
    assert result.failure_kind is FailureKind.MALFORMED_OUTPUT
    assert "malformed output" in (result.error or "")
    assert delays == [0.0, 0.0]
    assert _kinds(events) == [
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FAILED,
        EventKind.RETRY_STARTED,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FAILED,
        EventKind.RETRY_STARTED,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FAILED,
    ]
    assert [event.attempt for event in events if event.event is EventKind.TOOL_FAILED] == [
        1,
        2,
        3,
    ]
    assert [event.attempt for event in events if event.event is EventKind.RETRY_STARTED] == [
        2,
        3,
    ]


async def test_recovery_emits_started_failed_retry_succeeded() -> None:
    events: list[Any] = []
    runner, delays = build_runner(events)
    result = await runner.call(
        FailureSimulatorTool(),
        step_id="s1",
        call_ordinal=1,
        arguments={"failure_mode": "temporary_error", "fail_times": 1},
    )
    assert result.success is True
    assert result.attempts == 2
    assert _kinds(events) == [
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FAILED,
        EventKind.RETRY_STARTED,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_SUCCEEDED,
    ]
    assert events[1].status == "transient"
    assert events[2].attempt == 2
    assert events[4].attempt == 2
    assert delays == [0.0]


async def test_per_call_timeout_override_fires_transient_then_recovers() -> None:
    events: list[Any] = []
    runner, delays = build_runner(events)
    result = await runner.call(
        FailureSimulatorTool(),
        step_id="s1",
        call_ordinal=1,
        arguments={"failure_mode": "timeout", "fail_times": 1},
        timeout_s=0.05,
    )
    assert result.success is True
    assert result.attempts == 2
    failures = [event for event in events if event.event is EventKind.TOOL_FAILED]
    assert failures[0].status == "transient"
    assert "exceeded 0.05s" in (failures[0].message or "")
    assert len(delays) == 1


async def test_permanent_failure_is_not_retried() -> None:
    events: list[Any] = []
    runner, delays = build_runner(events)
    result = await runner.call(
        _NeverTool(),
        step_id="f1",
        call_ordinal=1,
        arguments={},
    )
    assert result.success is False
    assert result.attempts == 1
    assert result.failure_kind is FailureKind.PERMANENT
    assert delays == []
    assert _kinds(events) == [EventKind.TOOL_STARTED, EventKind.TOOL_FAILED]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (PermanentToolError("x"), FailureKind.PERMANENT),
        (MalformedToolOutputError("x"), FailureKind.MALFORMED_OUTPUT),
        (PlannerError("x"), FailureKind.PLANNER_ERROR),
        (ConfigurationError("x"), FailureKind.PERMANENT),
        (TimeoutError("slow"), FailureKind.TRANSIENT),
        (httpx.ConnectError("refused"), FailureKind.TRANSIENT),
        (ConnectionError("reset"), FailureKind.TRANSIENT),
        (ValueError("unknown bug"), FailureKind.PERMANENT),
    ],
)
def test_classify_table(exc: BaseException, expected: FailureKind) -> None:
    assert classify(exc) is expected
