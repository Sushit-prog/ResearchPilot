from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def with_timeout(awaitable: Awaitable[T], timeout_s: float) -> T:
    """Pure asyncio.wait_for wrapper; deadline exceeded → builtin TimeoutError.

    Classifies as FailureKind.TRANSIENT in the runner's table — retrying a
    timed-out call is meaningful.
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise TimeoutError(f"operation exceeded {timeout_s}s") from exc
