from __future__ import annotations

from app.config import RetryConfig
from app.reliability.retry import RetryPolicy, run_with_retry


def midpoint(low: float, high: float) -> float:
    return (low + high) / 2.0


def zero(low: float, high: float) -> float:
    return 0.0


def _always_fails(counter: list[int], exc: BaseException | None = None):
    async def attempt() -> str:
        counter.append(len(counter) + 1)
        raise exc if exc is not None else RuntimeError("boom")

    return attempt


async def test_full_jitter_schedule_is_exact() -> None:
    delays: list[float] = []
    calls: list[int] = []
    policy = RetryPolicy(max_attempts=7, base_delay_s=0.5, max_delay_s=8.0, jitter=True)
    outcome = await run_with_retry(
        _always_fails(calls),
        policy=policy,
        is_retriable=lambda e: isinstance(e, RuntimeError),
        sleep=delays.append,
        rng=midpoint,
    )
    assert isinstance(outcome.error, RuntimeError)
    assert outcome.attempts == 7
    assert calls == [1, 2, 3, 4, 5, 6, 7]
    # caps are 0.5,1,2,4,8,8 — midpoint halving, then max_delay_s ceiling
    assert delays == [0.25, 0.5, 1.0, 2.0, 4.0, 4.0]


async def test_jitter_disabled_uses_fixed_delays() -> None:
    delays: list[float] = []
    policy = RetryPolicy(max_attempts=3, base_delay_s=0.5, max_delay_s=8.0, jitter=False)
    outcome = await run_with_retry(
        _always_fails([]),
        policy=policy,
        is_retriable=lambda e: True,
        sleep=delays.append,
        rng=midpoint,
    )
    assert outcome.attempts == 3
    assert delays == [0.5, 1.0]


async def test_hard_max_bounds_attempts_and_reraises_original_error() -> None:
    delays: list[float] = []
    policy = RetryPolicy(max_attempts=3, base_delay_s=0.5, max_delay_s=8.0, jitter=True)
    original = ValueError("original failure")
    outcome = await run_with_retry(
        _always_fails([], exc=original),
        policy=policy,
        is_retriable=lambda e: True,
        sleep=delays.append,
        rng=midpoint,
    )
    assert outcome.error is original
    assert outcome.value is None
    assert outcome.attempts == 3
    assert len(delays) == 2


async def test_success_on_first_attempt_never_sleeps() -> None:
    delays: list[float] = []

    async def quick() -> str:
        return "done"

    outcome = await run_with_retry(
        quick,
        policy=RetryPolicy(max_attempts=3, base_delay_s=0.5, max_delay_s=8.0, jitter=True),
        is_retriable=lambda e: True,
        sleep=delays.append,
        rng=midpoint,
    )
    assert outcome.value == "done"
    assert outcome.attempts == 1
    assert delays == []


async def test_success_on_third_attempt_pays_two_backoffs() -> None:
    delays: list[float] = []
    calls: list[int] = []

    async def flaky() -> str:
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise RuntimeError("not yet")
        return "eventually"

    outcome = await run_with_retry(
        flaky,
        policy=RetryPolicy(max_attempts=3, base_delay_s=0.5, max_delay_s=8.0, jitter=True),
        is_retriable=lambda e: isinstance(e, RuntimeError),
        sleep=delays.append,
        rng=midpoint,
    )
    assert outcome.value == "eventually"
    assert outcome.attempts == 3
    assert delays == [0.25, 0.5]


async def test_non_retriable_failure_fails_fast() -> None:
    delays: list[float] = []
    policy = RetryPolicy(max_attempts=3, base_delay_s=0.5, max_delay_s=8.0, jitter=True)
    outcome = await run_with_retry(
        _always_fails([]),
        policy=policy,
        is_retriable=lambda e: False,
        sleep=delays.append,
        rng=midpoint,
    )
    assert isinstance(outcome.error, RuntimeError)
    assert outcome.attempts == 1
    assert delays == []


async def test_callbacks_see_every_failure_but_only_retries_advance() -> None:
    failures: list[int] = []
    retries: list[int] = []
    policy = RetryPolicy(max_attempts=3, base_delay_s=0.5, max_delay_s=8.0, jitter=True)
    await run_with_retry(
        _always_fails([]),
        policy=policy,
        is_retriable=lambda e: True,
        sleep=lambda d: None,
        rng=zero,
        on_failure=lambda attempt, exc: failures.append(attempt),
        before_retry=lambda attempt, exc: retries.append(attempt),
    )
    assert failures == [1, 2, 3]
    assert retries == [2, 3]


def test_policy_from_config_maps_all_fields() -> None:
    policy = RetryPolicy.from_config(
        RetryConfig(max_attempts=5, base_delay_s=0.25, max_delay_s=4.0, jitter=False)
    )
    assert policy.max_attempts == 5
    assert policy.base_delay_s == 0.25
    assert policy.max_delay_s == 4.0
    assert policy.jitter is False
