from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.config import Config
from app.errors import ConfigurationError, LLMProviderError
from app.llm import build_llm
from app.llm.openai_compatible import OpenAICompatibleProvider

OK_BODY = {"choices": [{"message": {"role": "assistant", "content": "planned json"}}]}


def make_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_provider(client: httpx.AsyncClient) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="https://llm.example/v1/",
        model="flash-lite",
        api_key="sk-test-key",
        client=client,
    )


async def test_success_posts_chat_completions_and_returns_content() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    provider = make_provider(make_client(handler))
    text = await provider.complete(system="sys", user="usr", temperature=0.0)

    assert text == "planned json"
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test-key"
    assert seen["body"]["model"] == "flash-lite"
    assert seen["body"]["temperature"] == 0.0
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]


async def test_http_error_becomes_provider_error_without_leaking_key() -> None:
    provider = make_provider(make_client(lambda request: httpx.Response(503)))
    with pytest.raises(LLMProviderError) as exc_info:
        await provider.complete(system="s", user="u")
    assert "HTTP 503" in str(exc_info.value)
    assert "sk-test-key" not in str(exc_info.value)


async def test_non_json_body_becomes_provider_error() -> None:
    provider = make_provider(
        make_client(lambda request: httpx.Response(200, text="<html>oops</html>"))
    )
    with pytest.raises(LLMProviderError, match="non-JSON"):
        await provider.complete(system="s", user="u")


async def test_missing_choices_becomes_provider_error() -> None:
    provider = make_provider(
        make_client(lambda request: httpx.Response(200, json={"choices": []}))
    )
    with pytest.raises(LLMProviderError, match="missing choices"):
        await provider.complete(system="s", user="u")


async def test_missing_content_becomes_provider_error() -> None:
    body = {"choices": [{"message": {"role": "assistant", "content": "  "}}]}
    provider = make_provider(make_client(lambda request: httpx.Response(200, json=body)))
    with pytest.raises(LLMProviderError, match="message content"):
        await provider.complete(system="s", user="u")


async def test_transport_error_becomes_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = make_provider(make_client(handler))
    with pytest.raises(LLMProviderError, match="ConnectError"):
        await provider.complete(system="s", user="u")


async def test_injected_client_is_not_closed() -> None:
    client = make_client(lambda request: httpx.Response(200, json=OK_BODY))
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example/v1",
        model="m",
        api_key="k",
        client=client,
    )
    await provider.complete(system="s", user="u")
    assert not client.is_closed


def test_build_llm_rejects_fake_provider() -> None:
    config = Config.from_env({"RESEARCHPILOT_LLM_PROVIDER": "fake"})
    with pytest.raises(ConfigurationError, match="test-only"):
        build_llm(config)


def test_build_llm_requires_api_key_for_openai_compatible() -> None:
    config = Config.from_env(
        {
            "RESEARCHPILOT_LLM_PROVIDER": "openai_compatible",
            "RESEARCHPILOT_LLM_BASE_URL": "https://llm.example/v1",
        }
    )
    with pytest.raises(ConfigurationError, match="RESEARCHPILOT_LLM_API_KEY"):
        build_llm(config)


def test_build_llm_requires_base_url_for_openai_compatible() -> None:
    config = Config.from_env(
        {
            "RESEARCHPILOT_LLM_PROVIDER": "openai_compatible",
            "RESEARCHPILOT_LLM_API_KEY": "sk-test",
        }
    )
    with pytest.raises(ConfigurationError, match="RESEARCHPILOT_LLM_BASE_URL"):
        build_llm(config)


def test_build_llm_rejects_unknown_provider() -> None:
    config = Config.from_env({"RESEARCHPILOT_LLM_PROVIDER": "mystery"})
    with pytest.raises(ConfigurationError, match="unsupported LLM provider"):
        build_llm(config)


def test_build_llm_builds_openai_compatible_provider() -> None:
    config = Config.from_env(
        {
            "RESEARCHPILOT_LLM_PROVIDER": "openai_compatible",
            "RESEARCHPILOT_LLM_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "RESEARCHPILOT_LLM_MODEL": "gemini-2.5-flash-lite",
            "RESEARCHPILOT_LLM_API_KEY": "sk-test",
        }
    )
    provider = build_llm(config)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "openai-compatible"
