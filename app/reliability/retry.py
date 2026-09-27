from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt

from app.config import RetryConfig

T = TypeVar("T")

SleepFn = Callable[[float], "None | Awaitable[None]"]
RngFn = Callable[[float, float], float]
RetriableFn = Callable[[], "Awaitable[T]"]
IsRetriable = Callable[[BaseException], bool]


class RetryOutcome(Generic[T]):
    __slots__ = ("attempts", "value", "error")

    def __init__(
        self,
        *,
        attempts: int,
        value: T | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.attempts = attempts
        self.value = value
        self.error = error


class RetryPolicy:
    """Hard-bounded retry policy: full-jitter exponential backoff (§4.7)."""

    def __init__(
        self,
        *,
        max_attempts: int,
        base_delay_s: float,
        max_delay_s: float,
        jitter: bool,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.jitter = jitter

    @classmethod
    def from_config(cls, config: RetryConfig) -> RetryPolicy:
        return cls(
            max_attempts=config.max_attempts,
            base_delay_s=config.base_delay_s,
            max_delay_s=config.max_delay_s,
            jitter=config.jitter,
        )

    def cap_for(self, attempt_number: int) -> float:
        exponential = self.base_delay_s * (2 ** (attempt_number - 1))
        return min(self.max_delay_s, exponential)

    def delay_for(self, attempt_number: int, rng: RngFn) -> float:
        cap = self.cap_for(attempt_number)
        return rng(0.0, cap) if self.jitter else cap


async def run_with_retry(
    fn: RetriableFn[T],
    *,
    policy: RetryPolicy,
    is_retriable: IsRetriable,
    sleep: SleepFn = asyncio.sleep,
    rng: RngFn = random.uniform,
    on_failure: Callable[[int, BaseException], None] | None = None,
    before_retry: Callable[[int, BaseException], None] | None = None,
) -> RetryOutcome[T]:
    """Run fn under the policy; never raises — the outcome carries the error.

    Retriability is kind-driven and supplied by the caller's is_retriable
    predicate; attempts are counted by wrapped invocation, and the injected
    sleep receives the full-jitter delay so tests never actually wait.
    """
    attempts = 0

    async def wrapped() -> T:
        nonlocal attempts
        attempts += 1
        try:
            return await fn()
        except BaseException as exc:
            if on_failure is not None:
                on_failure(attempts, exc)
            raise

    def wait(retry_state: RetryCallState) -> float:
        return policy.delay_for(retry_state.attempt_number, rng)

    def before_sleep(retry_state: RetryCallState) -> None:
        if before_retry is not None:
            failed = retry_state.outcome
            if failed is not None and failed.failed:
                before_retry(retry_state.attempt_number + 1, failed.exception())

    async def sleep_adapter(delay: float) -> None:
        # tenacity awaits sleep unconditionally; accept sync recorders too
        result = sleep(delay)
        if inspect.isawaitable(result):
            await result

    retrying = AsyncRetrying(
        stop=stop_after_attempt(policy.max_attempts),
        wait=wait,
        retry=retry_if_exception(is_retriable),
        sleep=sleep_adapter,
        reraise=True,
        before_sleep=before_sleep,
    )
    try:
        value = await retrying(wrapped)
    except BaseException as exc:
        return RetryOutcome(attempts=attempts, error=exc)
    return RetryOutcome(attempts=attempts, value=value)
