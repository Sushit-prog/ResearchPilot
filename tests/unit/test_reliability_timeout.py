from __future__ import annotations

import asyncio

import pytest

from app.reliability.timeout import with_timeout


async def test_returns_value_when_under_budget() -> None:
    async def quick() -> str:
        return "ok"

    assert await with_timeout(quick(), 1.0) == "ok"


async def test_raises_timeout_error_when_deadline_exceeded() -> None:
    async def slow() -> str:
        await asyncio.sleep(0.5)
        return "late"

    with pytest.raises(TimeoutError, match="exceeded 0.05s"):
        await with_timeout(slow(), 0.05)


async def test_propagates_underlying_failure_untouched() -> None:
    async def broken() -> str:
        raise ValueError("inner boom")

    with pytest.raises(ValueError, match="inner boom"):
        await with_timeout(broken(), 1.0)
