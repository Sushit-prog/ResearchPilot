from __future__ import annotations

from collections.abc import Callable

import pytest

from app.llm.fake import FakeLLMProvider


@pytest.fixture
def fake_llm() -> Callable[..., FakeLLMProvider]:
    def _make(*responses: str) -> FakeLLMProvider:
        return FakeLLMProvider(responses)

    return _make
