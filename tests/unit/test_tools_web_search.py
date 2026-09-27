from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.errors import (
    ConfigurationError,
    MalformedToolOutputError,
    PermanentToolError,
    ToolInputValidationError,
    TransientToolError,
)
from app.models.tool_io import SearchInput, SearchResultItem
from app.tools.web_search import WebSearchTool

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TAVILY_OK = json.loads(FIXTURES.joinpath("tavily_search_ok.json").read_text(encoding="utf-8"))
TEST_KEY = "tvly-test-key"


def make_tool(
    handler: Any, *, api_key: str | None = TEST_KEY
) -> tuple[WebSearchTool, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recording_handler))
    return WebSearchTool(api_key=api_key, client=client), requests


def ok_handler(body: dict[str, Any] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body if body is not None else TAVILY_OK)

    return handler


def status_handler(status_code: int) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": {"error": "induced"}})

    return handler


async def test_maps_tavily_results_to_contract() -> None:
    tool, _ = make_tool(ok_handler())
    output = await tool.execute(SearchInput(query="python 3.14 release", max_results=8))
    assert output.provider == "tavily"
    assert output.query == "python 3.14 release"
    assert isinstance(output.retrieved_at, datetime)
    assert output.retrieved_at.tzinfo is not None
    assert len(output.results) == 3
    first, second, third = output.results
    assert first.title == "Python 3.14 Release Notes"
    assert first.url == "https://docs.python.org/3.14/whatsnew.html"
    assert first.domain == "docs.python.org"
    assert first.published_at == datetime(2025, 10, 7)
    assert second.title == "Python Insider Blog"
    assert second.published_at is None
    assert third.title == "Unparsable Date"
    assert third.published_at is None


async def test_drops_results_with_invalid_urls_or_missing_fields() -> None:
    tool, _ = make_tool(ok_handler())
    output = await tool.execute(SearchInput(query="anything"))
    urls = [item.url for item in output.results]
    assert "javascript:alert(1)" not in urls
    assert "https://user:pass@example.net/x" not in urls
    assert "https://example.com/missing-title" not in urls


async def test_sends_bearer_auth_and_expected_body() -> None:
    tool, requests = make_tool(ok_handler())
    await tool.execute(SearchInput(query="tavily auth probe", max_results=3))
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.tavily.com/search"
    assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert json.loads(request.content) == {"query": "tavily auth probe", "max_results": 3}


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
async def test_client_errors_are_permanent(status_code: int) -> None:
    tool, _ = make_tool(status_handler(status_code))
    with pytest.raises(PermanentToolError) as exc_info:
        await tool.execute(SearchInput(query="q"))
    assert type(exc_info.value) is PermanentToolError


@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_server_and_rate_limit_errors_are_transient(status_code: int) -> None:
    tool, _ = make_tool(status_handler(status_code))
    with pytest.raises(TransientToolError) as exc_info:
        await tool.execute(SearchInput(query="q"))
    assert type(exc_info.value) is TransientToolError


async def test_non_json_body_is_malformed_output() -> None:
    tool, _ = make_tool(lambda request: httpx.Response(200, text="not json at all"))
    with pytest.raises(MalformedToolOutputError) as exc_info:
        await tool.execute(SearchInput(query="q"))
    assert type(exc_info.value) is MalformedToolOutputError
    assert exc_info.value.tool == "web_search"


@pytest.mark.parametrize("body", [{"query": "x"}, {"results": {}}, ["not", "a", "dict"]])
async def test_shapeless_response_is_malformed_output(body: Any) -> None:
    tool, _ = make_tool(lambda request: httpx.Response(200, json=body))
    with pytest.raises(MalformedToolOutputError):
        await tool.execute(SearchInput(query="q"))


async def test_missing_api_key_is_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        WebSearchTool(api_key=None)
    assert type(exc_info.value) is ConfigurationError


async def test_empty_api_key_is_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        WebSearchTool(api_key="")


async def test_connect_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    tool, _ = make_tool(handler)
    with pytest.raises(TransientToolError) as exc_info:
        await tool.execute(SearchInput(query="q"))
    assert type(exc_info.value) is TransientToolError


async def test_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    tool, _ = make_tool(handler)
    with pytest.raises(TransientToolError) as exc_info:
        await tool.execute(SearchInput(query="q"))
    assert type(exc_info.value) is TransientToolError


async def test_results_are_capped_at_max_results() -> None:
    body = {
        "query": "q",
        "results": [
            {"title": f"R{i}", "url": f"https://example.com/{i}", "content": "c", "score": 0.5}
            for i in range(10)
        ],
    }
    tool, _ = make_tool(ok_handler(body))
    output = await tool.execute(SearchInput(query="q", max_results=2))
    assert len(output.results) == 2


async def test_empty_results_is_valid_output() -> None:
    tool, _ = make_tool(ok_handler({"query": "q", "results": []}))
    output = await tool.execute(SearchInput(query="q"))
    assert output.results == []


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "https://u:p@x.com/"])
def test_search_result_item_rejects_bad_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        SearchResultItem(title="t", url=url, snippet="s", domain="x")


@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""},
        {"query": "q", "max_results": 0},
        {"query": "q", "max_results": 21},
        {"query": "x" * 401},
    ],
)
def test_search_input_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ToolInputValidationError):
        WebSearchTool(api_key=TEST_KEY).parse_input(payload)


def test_tool_metadata() -> None:
    tool = WebSearchTool(api_key=TEST_KEY)
    assert tool.name == "web_search"
    assert tool.is_test_component is False
    assert tool.default_timeout_s == 10.0
