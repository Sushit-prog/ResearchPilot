from __future__ import annotations

import asyncio
from typing import Any

from app.errors import ConfigurationError, TransientToolError
from app.tools.base import Tool

FAILURE_MODES = ("timeout", "invalid_response", "temporary_error")


class FailureArm:
    """One-shot state shared by the wrapped network tools.

    Whichever wrapped tool executes first consumes the arm, so
    --simulate-failure reliably hits the run's first network call.
    """

    def __init__(self) -> None:
        self.armed = True

    def fire(self) -> bool:
        if self.armed:
            self.armed = False
            return True
        return False


class ArmedTool(Tool[Any, Any]):
    """--simulate-failure wrapper (§12 demo) for a real network tool.

    The first execute() injects the configured failure — a transient error, a
    malformed response, or slowness that trips the runner's timeout — and every
    later call passes straight through. Only the failure is injected;
    classification, timeout enforcement, backoff, retry, and recovery all run
    through the runner's normal path, identical to a genuine incident.
    """

    def __init__(self, inner: Tool[Any, Any], mode: str, arm: FailureArm) -> None:
        if mode not in FAILURE_MODES:
            raise ConfigurationError(
                f"unsupported failure mode: {mode!r} (supported: {', '.join(FAILURE_MODES)})"
            )
        self._inner = inner
        self._mode = mode
        self._arm = arm
        self.name = inner.name
        self.description = inner.description
        self.input_model = inner.input_model
        self.output_model = inner.output_model
        self.default_timeout_s = inner.default_timeout_s
        self.is_test_component = inner.is_test_component

    async def execute(self, params: Any) -> Any:
        if self._arm.fire():
            if self._mode == "temporary_error":
                raise TransientToolError(
                    f"induced temporary error (--simulate-failure) on {self._inner.name}",
                    tool=self._inner.name,
                )
            if self._mode == "invalid_response":
                return {"induced": "invalid_response", "tool": self._inner.name}
            await asyncio.sleep(self._inner.default_timeout_s + 1.0)
        return await self._inner.execute(params)
