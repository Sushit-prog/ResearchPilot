from __future__ import annotations

import pytest

from app.llm.fake import FakeLLMExhaustedError, FakeLLMProvider


async def test_responses_served_in_queue_order(fake_llm) -> None:
    provider = fake_llm("first", "second")
    assert await provider.complete(system="s", user="u1") == "first"
    assert await provider.complete(system="s", user="u2") == "second"
    assert len(provider.calls) == 2


async def test_calls_recorded_with_content_and_temperature() -> None:
    provider = FakeLLMProvider(["r", "r2"])
    await provider.complete(system="system prompt", user="user prompt")
    await provider.complete(system="s", user="u", temperature=0.7)
    assert provider.calls[0] == {
        "system": "system prompt",
        "user": "user prompt",
        "temperature": 0.0,
    }
    assert provider.calls[1]["temperature"] == 0.7
    assert len(provider.calls) == 2


async def test_exhausted_queue_raises_explicit_error() -> None:
    provider = FakeLLMProvider(["only"])
    await provider.complete(system="s", user="u")
    with pytest.raises(FakeLLMExhaustedError) as exc_info:
        await provider.complete(system="s", user="u")
    assert type(exc_info.value) is FakeLLMExhaustedError


async def test_empty_provider_raises_on_first_call() -> None:
    provider = FakeLLMProvider()
    with pytest.raises(FakeLLMExhaustedError):
        await provider.complete(system="s", user="u")


def test_provider_name() -> None:
    assert FakeLLMProvider().name == "fake"
