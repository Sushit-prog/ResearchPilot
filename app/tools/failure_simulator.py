from __future__ import annotations

import asyncio
from typing import Any, cast

from app.errors import TransientToolError
from app.models.events import FailureMode
from app.models.tool_io import SimulatorInput, SimulatorOutput
from app.tools.base import Tool


class FailureSimulatorTool(Tool[SimulatorInput, SimulatorOutput]):
    name = "failure_simulator"
    description = "Induces reproducible failures for testing recovery (test component)"
    input_model = SimulatorInput
    output_model = SimulatorOutput
    default_timeout_s = 5.0
    is_test_component = True

    def __init__(self) -> None:
        self._invocation = 0

    async def execute(self, params: SimulatorInput) -> SimulatorOutput:
        self._invocation += 1
        invocation = self._invocation
        if invocation > params.fail_times:
            return SimulatorOutput(status="ok", invocation=invocation)
        if params.failure_mode is FailureMode.TIMEOUT:
            await asyncio.sleep(self.default_timeout_s * 2)
            return SimulatorOutput(status="ok", invocation=invocation)
        if params.failure_mode is FailureMode.INVALID_RESPONSE:
            if params.payload is not None:
                return cast(Any, params.payload)
            return cast(Any, {"invocation": invocation})
        raise TransientToolError(
            f"induced temporary error (invocation {invocation})", tool=self.name
        )
