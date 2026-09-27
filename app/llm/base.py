from __future__ import annotations

from typing import Protocol


class LLMProvider(Protocol):
    name: str

    async def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str: ...
