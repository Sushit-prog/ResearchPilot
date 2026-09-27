from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.errors import PermanentToolError, ToolInputValidationError, TransientToolError
from app.models.tool_io import FetchInput
from app.tools.webpage_fetch import WebpageFetchTool

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE_HTML = FIXTURES.joinpath("sample_page.html").read_text(encoding="utf-8")
EXPECTED_TITLE = "Sample Page"
EXPECTED_TEXT = (
    "Heading First paragraph with bold text. Second & final paragraph."
)


def make_tool(handler: Any, *, follow_redirects: bool = True) -> WebpageFetchTool:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=follow_redirects,
    )
    return WebpageFetchTool(client=client)


def html_handler(
    body: str | bytes = SAMPLE_HTML,
    content_type: str | None = "text/html",
    status_code: int = 200,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {}
        if content_type is not None:
            headers["content-type"] = content_type
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        return httpx.Response(status_code, headers=headers, content=payload)

    return handler


BAD_URLS = [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ftp://example.com/resource",
    "data:text/html,<h1>x</h1>",
    "https://user:pass@example.com/",
    "https://admin@example.com/",
    "https://",
    "",
    "not a url",
    "example.com/no-scheme",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_parse_input_rejects_bad_urls(url: str) -> None:
    with pytest.raises(ToolInputValidationError) as exc_info:
        WebpageFetchTool().parse_input({"url": url})
    assert type(exc_info.value) is ToolInputValidationError


@pytest.mark.parametrize("url", ["https://example.com/page", "http://example.com:8080/x?a=b"])
def test_parse_input_accepts_http_urls(url: str) -> None:
    parsed = WebpageFetchTool().parse_input({"url": url})
    assert parsed.url == url


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:pass@example.com/"])
async def test_execute_enforces_url_rules_independently_of_input_model(url: str) -> None:
    tool = make_tool(html_handler())
    params = FetchInput.model_construct(url=url, max_chars=500)
    with pytest.raises(ToolInputValidationError) as exc_info:
        await tool.execute(params)
    assert type(exc_info.value) is ToolInputValidationError


async def test_fetch_extracts_title_and_text() -> None:
    tool = make_tool(html_handler())
    output = await tool.execute(FetchInput(url="https://example.com/page"))
    assert output.status_code == 200
    assert output.title == EXPECTED_TITLE
    assert output.text == EXPECTED_TEXT
    assert output.truncated is False
    assert output.requested_url == "https://example.com/page"
    assert output.final_url == "https://example.com/page"
    assert output.content_type == "text/html"
    assert "alert" not in output.text
    assert "color" not in output.text


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html; charset=utf-8",
        "text/html; charset=UTF-8",
        "text/html",
        "text/html;Charset=utf-8",
    ],
)
async def test_content_type_parameters_are_ignored(content_type: str) -> None:
    tool = make_tool(html_handler(content_type=content_type))
    output = await tool.execute(FetchInput(url="https://example.com/page"))
    assert output.title == EXPECTED_TITLE
    assert output.content_type == content_type


async def test_missing_content_type_is_treated_as_html() -> None:
    tool = make_tool(html_handler(content_type=None))
    output = await tool.execute(FetchInput(url="https://example.com/page"))
    assert output.title == EXPECTED_TITLE
    assert output.content_type is None


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf; charset=binary", "application/octet-stream", "text/css", "image/png"],
)
async def test_unsupported_content_type_is_permanent(content_type: str) -> None:
    tool = make_tool(html_handler(content_type=content_type))
    with pytest.raises(PermanentToolError) as exc_info:
        await tool.execute(FetchInput(url="https://example.com/file"))
    assert type(exc_info.value) is PermanentToolError
    assert exc_info.value.tool == "webpage_fetch"


async def test_plain_text_content() -> None:
    tool = make_tool(html_handler(body="line one\nline two", content_type="text/plain"))
    output = await tool.execute(FetchInput(url="https://example.com/file.txt"))
    assert output.text == "line one line two"
    assert output.title is None
    assert output.truncated is False


async def test_oversized_body_is_truncated_at_max_chars() -> None:
    body = "<p>" + "a" * 10_000 + "</p>"
    tool = make_tool(html_handler(body=body))
    output = await tool.execute(FetchInput(url="https://example.com/big", max_chars=500))
    assert output.truncated is True
    assert len(output.text) == 500


async def test_http_404_is_permanent() -> None:
    tool = make_tool(html_handler(body=b"missing", status_code=404))
    with pytest.raises(PermanentToolError) as exc_info:
        await tool.execute(FetchInput(url="https://example.com/missing"))
    assert type(exc_info.value) is PermanentToolError


async def test_http_500_is_transient() -> None:
    tool = make_tool(html_handler(body=b"boom", status_code=500))
    with pytest.raises(TransientToolError) as exc_info:
        await tool.execute(FetchInput(url="https://example.com/boom"))
    assert type(exc_info.value) is TransientToolError


async def test_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    tool = make_tool(handler)
    with pytest.raises(TransientToolError) as exc_info:
        await tool.execute(FetchInput(url="https://example.com/slow"))
    assert type(exc_info.value) is TransientToolError


async def test_connect_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    tool = make_tool(handler)
    with pytest.raises(TransientToolError) as exc_info:
        await tool.execute(FetchInput(url="https://unreachable.example"))
    assert type(exc_info.value) is TransientToolError


async def test_redirect_records_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://example.com/final"})
        body = b"<html><head><title>Final</title></head><body>done</body></html>"
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body)

    tool = make_tool(handler)
    output = await tool.execute(FetchInput(url="https://example.com/start"))
    assert output.status_code == 200
    assert output.requested_url == "https://example.com/start"
    assert output.final_url == "https://example.com/final"
    assert output.title == "Final"


def test_tool_metadata() -> None:
    tool = WebpageFetchTool()
    assert tool.name == "webpage_fetch"
    assert tool.is_test_component is False
    assert tool.default_timeout_s == 10.0
