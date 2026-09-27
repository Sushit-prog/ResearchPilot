from __future__ import annotations

import asyncio

import pytest

from app.errors import MalformedToolOutputError, ToolInputValidationError, TransientToolError
from app.models.events import FailureMode
from app.models.tool_io import SimulatorInput, SimulatorOutput
from app.tools.failure_simulator import FailureSimulatorTool


@pytest.fixture
def simulator() -> FailureSimulatorTool:
    return FailureSimulatorTool()


async def test_temporary_error_then_success(simulator: FailureSimulatorTool) -> None:
    params = SimulatorInput(failure_mode=FailureMode.TEMPORARY_ERROR, fail_times=1)
    with pytest.raises(TransientToolError) as exc_info:
        await simulator.execute(params)
    assert type(exc_info.value) is TransientToolError
    output = await simulator.execute(params)
    assert output == SimulatorOutput(status="ok", invocation=2)


async def test_fail_times_zero_succeeds_immediately(simulator: FailureSimulatorTool) -> None:
    params = SimulatorInput(failure_mode=FailureMode.TEMPORARY_ERROR, fail_times=0)
    output = await simulator.execute(params)
    assert output == SimulatorOutput(status="ok", invocation=1)


async def test_fail_times_counts_invocations(simulator: FailureSimulatorTool) -> None:
    params = SimulatorInput(failure_mode=FailureMode.TEMPORARY_ERROR, fail_times=2)
    for expected in (1, 2):
        with pytest.raises(TransientToolError):
            await simulator.execute(params)
    output = await simulator.execute(params)
    assert output == SimulatorOutput(status="ok", invocation=3)


async def test_invalid_response_body_fails_parse_output(simulator: FailureSimulatorTool) -> None:
    params = SimulatorInput(failure_mode=FailureMode.INVALID_RESPONSE, fail_times=1)
    body = await simulator.execute(params)
    with pytest.raises(MalformedToolOutputError) as exc_info:
        simulator.parse_output(body)
    assert type(exc_info.value) is MalformedToolOutputError
    assert exc_info.value.tool == "failure_simulator"


async def test_invalid_response_custom_payload_passthrough(
    simulator: FailureSimulatorTool,
) -> None:
    params = SimulatorInput(
        failure_mode=FailureMode.INVALID_RESPONSE,
        fail_times=1,
        payload={"status": 123, "invocation": "x"},
    )
    body = await simulator.execute(params)
    with pytest.raises(MalformedToolOutputError):
        simulator.parse_output(body)


async def test_invalid_response_valid_payload_passes_validation(
    simulator: FailureSimulatorTool,
) -> None:
    params = SimulatorInput(
        failure_mode=FailureMode.INVALID_RESPONSE,
        fail_times=1,
        payload={"status": "ok", "invocation": 7},
    )
    body = await simulator.execute(params)
    assert simulator.parse_output(body) == SimulatorOutput(status="ok", invocation=7)


async def test_timeout_mode_sleeps_past_deadline(simulator: FailureSimulatorTool) -> None:
    params = SimulatorInput(failure_mode=FailureMode.TIMEOUT, fail_times=1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(simulator.execute(params), timeout=0.05)


async def test_fail_times_bounds_rejected() -> None:
    with pytest.raises(ToolInputValidationError):
        FailureSimulatorTool().parse_input({"failure_mode": "timeout", "fail_times": 11})
    with pytest.raises(ToolInputValidationError):
        FailureSimulatorTool().parse_input({"failure_mode": "timeout", "fail_times": -1})


async def test_unknown_failure_mode_rejected() -> None:
    with pytest.raises(ToolInputValidationError):
        FailureSimulatorTool().parse_input({"failure_mode": "explode"})


def test_tool_metadata(simulator: FailureSimulatorTool) -> None:
    assert simulator.name == "failure_simulator"
    assert simulator.is_test_component is True
    assert simulator.default_timeout_s == 5.0
