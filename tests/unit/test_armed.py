from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import BaseModel

from app.config import Config
from app.errors import ConfigurationError, MalformedToolOutputError, TransientToolError
from app.models.events import EventKind
from app.observability import EventEmitter, InMemoryEventSink
from app.reliability.runner import Runner
from app.tools.armed import FAILURE_MODES, ArmedTool, FailureArm
from app.tools.base import Tool


class EchoParams(BaseModel):
    pass


class EchoResult(BaseModel):
    echo: str


class EchoTool(Tool[EchoParams, EchoResult]):
    name = "echo"
    description = "returns what it got"
    input_model = EchoParams
    output_model = EchoResult
    default_timeout_s = 5.0

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, params: EchoParams) -> EchoResult:
        self.calls += 1
        return EchoResult(echo="ok")


def test_failure_modes_constant() -> None:
    assert FAILURE_MODES == ("timeout", "invalid_response", "temporary_error")


def test_unknown_mode_rejected() -> None:
    with pytest.raises(ConfigurationError, match="unsupported failure mode"):
        ArmedTool(EchoTool(), "explode", FailureArm())


def test_armed_tool_copies_inner_metadata() -> None:
    inner = EchoTool()
    tool = ArmedTool(inner, "timeout", FailureArm())
    assert tool.name == "echo"
    assert tool.description == inner.description
    assert tool.default_timeout_s == inner.default_timeout_s
    assert tool.is_test_component is False


async def test_invalid_response_injection_only_fires_once() -> None:
    inner = EchoTool()
    tool = ArmedTool(inner, "invalid_response", FailureArm())
    injected = await tool.execute({})
    with pytest.raises(MalformedToolOutputError):
        tool.parse_output(injected)
    assert inner.calls == 0
    healthy = await tool.execute({})
    assert inner.calls == 1
    assert tool.parse_output(healthy).echo == "ok"


async def test_temporary_error_injection_only_fires_once() -> None:
    inner = EchoTool()
    tool = ArmedTool(inner, "temporary_error", FailureArm())
    with pytest.raises(TransientToolError, match="induced temporary error"):
        await tool.execute({})
    assert inner.calls == 0
    assert tool.parse_output(await tool.execute({})).echo == "ok"
    assert inner.calls == 1


async def test_shared_arm_fires_on_first_wrapped_tool_only() -> None:
    arm = FailureArm()
    first = ArmedTool(EchoTool(), "temporary_error", arm)
    second = ArmedTool(EchoTool(), "temporary_error", arm)
    with pytest.raises(TransientToolError):
        await first.execute({})
    assert second.parse_output(await second.execute({})).echo == "ok"


async def test_timeout_injection_is_enforced_by_runner_and_recovers() -> None:
    events: list[Any] = []
    emitter = EventEmitter(
        lambda: datetime.now(timezone.utc),
        sinks=[InMemoryEventSink(events)],
    )
    runner = Runner(Config(), emitter, sleep=lambda _s: None, rng=lambda _a, _b: 0.0)
    tool = ArmedTool(EchoTool(), "timeout", FailureArm())
    result = await runner.call(
        tool,
        step_id="s1",
        call_ordinal=1,
        arguments={},
        timeout_s=0.05,
    )
    assert result.success is True
    assert result.attempts == 2
    kinds = [event.event for event in events]
    assert EventKind.RETRY_STARTED in kinds
    assert kinds[-1] is EventKind.TOOL_SUCCEEDED
