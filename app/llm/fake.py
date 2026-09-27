from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.errors import ResearchPilotError


class FakeLLMExhaustedError(ResearchPilotError):
    pass


class FakeLLMProvider:
    name = "fake"

    def __init__(self, responses: Sequence[str] | None = None) -> None:
        self._responses: list[str] = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        if not self._responses:
            raise FakeLLMExhaustedError("FakeLLMProvider has no scripted responses left")
        return self._responses.pop(0)
