from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Config
from app.errors import ResearchPilotError
from app.models.events import EventKind, FailureKind, ToolResult
from app.observability import EventEmitter
from app.reliability.retry import RetryPolicy, run_with_retry
from app.reliability.timeout import with_timeout
from app.tools.base import Tool

_RETRIABLE_KINDS = frozenset({FailureKind.TRANSIENT, FailureKind.MALFORMED_OUTPUT})
_TRANSIENT_TYPES = (
    TimeoutError,
    httpx.TimeoutException,
    httpx.TransportError,
    ConnectionError,
    OSError,
)

SleepFn = Callable[[float], "None | Any"]
RngFn = Callable[[float, float], float]
ClockFn = Callable[[], datetime]


def classify(exc: BaseException) -> FailureKind:
    """§12 failure taxonomy — single table; kind decides retriable or not."""
    if isinstance(exc, ResearchPilotError):
        if exc.failure_kind is not None:
            return exc.failure_kind
        return FailureKind.PERMANENT
    if isinstance(exc, _TRANSIENT_TYPES):
        return FailureKind.TRANSIENT
    return FailureKind.PERMANENT


def is_unclassified(exc: BaseException) -> bool:
    """True when the exception type matched no row of classify()'s table.

    Such failures still classify as PERMANENT (never retry-loop on a bug),
    but the runner tags them so a code bug stays distinguishable from a
    known permanent failure like a 404 (D11).
    """
    return not isinstance(exc, (ResearchPilotError, *_TRANSIENT_TYPES))


def is_retriable(exc: BaseException) -> bool:
    return classify(exc) in _RETRIABLE_KINDS


class Runner:
    """§4.7 execution seam: validate → per-attempt timeout → bounded retry →
    parse_output → ToolResult. Returns the ToolResult even on exhaustion —
    the executor, not the runner, decides what a failure means for the step.
    """

    def __init__(
        self,
        config: Config,
        emitter: EventEmitter,
        *,
        clock: ClockFn | None = None,
        sleep: SleepFn = asyncio.sleep,
        rng: RngFn = random.uniform,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._emitter = emitter
        self._clock: ClockFn = clock if clock is not None else _utcnow
        self._sleep = sleep
        self._rng = rng
        self._monotonic = monotonic

    async def call(
        self,
        tool: Tool[Any, Any],
        *,
        step_id: str,
        call_ordinal: int,
        arguments: Mapping[str, Any],
        timeout_s: float | None = None,
    ) -> ToolResult:
        started_at = self._clock()
        origin = self._monotonic()
        result_id = f"{step_id}:{tool.name}:{call_ordinal}"

        try:
            params = tool.parse_input(arguments)
        except ResearchPilotError as exc:
            kind = classify(exc)
            self._emitter.emit(
                EventKind.TOOL_FAILED,
                tool=tool.name,
                step_id=step_id,
                status=kind.value,
                message=str(exc),
            )
            return ToolResult(
                result_id=result_id,
                tool=tool.name,
                step_id=step_id,
                call_ordinal=call_ordinal,
                success=False,
                attempts=0,
                failure_kind=kind,
                error=str(exc),
                duration_ms=self._elapsed_ms(origin),
                started_at=started_at,
            )

        effective_timeout = timeout_s if timeout_s is not None else tool.default_timeout_s
        attempts_seen = 0

        async def attempt() -> dict[str, Any]:
            nonlocal attempts_seen
            attempts_seen += 1
            self._emitter.emit(
                EventKind.TOOL_STARTED,
                tool=tool.name,
                step_id=step_id,
                attempt=attempts_seen,
            )
            raw = await with_timeout(tool.execute(params), effective_timeout)
            if hasattr(raw, "model_dump"):
                raw = raw.model_dump()
            output = tool.parse_output(raw)
            return output.model_dump(mode="json")

        def on_failure(attempt_no: int, exc: BaseException) -> None:
            kind = classify(exc)
            data: dict[str, Any] | None = None
            if is_unclassified(exc):
                data = {"unclassified": True, "error_type": type(exc).__name__}
            self._emitter.emit(
                EventKind.TOOL_FAILED,
                tool=tool.name,
                step_id=step_id,
                attempt=attempt_no,
                status=kind.value,
                message=str(exc),
                data=data,
            )

        def before_retry(next_attempt: int, exc: BaseException) -> None:
            self._emitter.emit(
                EventKind.RETRY_STARTED,
                tool=tool.name,
                step_id=step_id,
                attempt=next_attempt,
                status=classify(exc).value,
                message=str(exc),
            )

        outcome = await run_with_retry(
            attempt,
            policy=RetryPolicy.from_config(self._config.retry),
            is_retriable=is_retriable,
            sleep=self._sleep,
            rng=self._rng,
            on_failure=on_failure,
            before_retry=before_retry,
        )
        duration_ms = self._elapsed_ms(origin)

        if outcome.error is None:
            self._emitter.emit(
                EventKind.TOOL_SUCCEEDED,
                tool=tool.name,
                step_id=step_id,
                attempt=outcome.attempts,
            )
            return ToolResult(
                result_id=result_id,
                tool=tool.name,
                step_id=step_id,
                call_ordinal=call_ordinal,
                success=True,
                output=outcome.value,
                attempts=outcome.attempts,
                duration_ms=duration_ms,
                started_at=started_at,
            )

        kind = classify(outcome.error)
        return ToolResult(
            result_id=result_id,
            tool=tool.name,
            step_id=step_id,
            call_ordinal=call_ordinal,
            success=False,
            attempts=outcome.attempts,
            failure_kind=kind,
            unclassified=is_unclassified(outcome.error),
            error=str(outcome.error),
            duration_ms=duration_ms,
            started_at=started_at,
        )

    def _elapsed_ms(self, origin: float) -> int:
        return max(int((self._monotonic() - origin) * 1000), 0)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
