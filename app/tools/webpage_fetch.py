from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from app.errors import PermanentToolError, ToolInputValidationError, TransientToolError
from app.models.tool_io import FetchInput, FetchOutput, validate_http_url
from app.tools.base import Tool

_ALLOWED_MEDIA_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}
)
_CHARSET_RE = re.compile(r"charset\s*=\s*\"?([\w.-]+)\"?", re.IGNORECASE)


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS and self._skip_depth == 0:
            self.text_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS and self._skip_depth == 0:
            self.text_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)


class WebpageFetchTool(Tool[FetchInput, FetchOutput]):
    name = "webpage_fetch"
    description = "Fetch a web page over http(s) and return extracted, size-capped text"
    input_model = FetchInput
    output_model = FetchOutput
    default_timeout_s = 10.0

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def execute(self, params: FetchInput) -> FetchOutput:
        try:
            validate_http_url(params.url)
        except ValueError as exc:
            raise ToolInputValidationError(f"invalid url for {self.name!r}: {exc}") from exc
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.default_timeout_s),
            follow_redirects=True,
            max_redirects=20,
        )
        try:
            return await self._get(params, client)
        except httpx.TimeoutException as exc:
            raise TransientToolError(f"request timed out: {params.url}", tool=self.name) from exc
        except httpx.TooManyRedirects as exc:
            raise PermanentToolError(f"too many redirects: {params.url}", tool=self.name) from exc
        except httpx.HTTPError as exc:
            raise TransientToolError(f"request failed: {exc}", tool=self.name) from exc
        finally:
            if self._client is None:
                await client.aclose()

    async def _get(self, params: FetchInput, client: httpx.AsyncClient) -> FetchOutput:
        async with client.stream(
            "GET", params.url, headers={"User-Agent": "ResearchPilot/0.1"}
        ) as response:
            status = response.status_code
            if 400 <= status < 500:
                raise PermanentToolError(f"HTTP {status} for {params.url}", tool=self.name)
            if status >= 500:
                raise TransientToolError(f"HTTP {status} for {params.url}", tool=self.name)
            raw_content_type = response.headers.get("content-type") or ""
            media_type = raw_content_type.split(";")[0].strip().lower()
            if media_type and media_type not in _ALLOWED_MEDIA_TYPES:
                raise PermanentToolError(
                    f"unsupported content type {media_type!r} for {params.url}",
                    tool=self.name,
                )
            byte_cap = params.max_chars * 4
            buffer = bytearray()
            hit_cap = False
            async for chunk in response.aiter_bytes():
                remaining = byte_cap - len(buffer)
                if len(chunk) >= remaining:
                    buffer.extend(chunk[:remaining])
                    hit_cap = True
                    break
                buffer.extend(chunk)
            final_url = str(response.url)
        body = bytes(buffer).decode(self._charset(raw_content_type), errors="replace")
        if media_type == "text/plain":
            title: str | None = None
            text = body
        else:
            title, text = _extract_html(body)
        text = " ".join(text.split())
        truncated = hit_cap or len(text) > params.max_chars
        if truncated:
            text = text[: params.max_chars]
        return FetchOutput(
            requested_url=params.url,
            final_url=final_url,
            status_code=status,
            title=title,
            text=text,
            truncated=truncated,
            content_type=raw_content_type or None,
        )

    @staticmethod
    def _charset(content_type: str) -> str:
        match = _CHARSET_RE.search(content_type)
        if match:
            encoding = match.group(1).lower()
            if encoding in {"utf-8", "utf8", "latin-1", "iso-8859-1", "ascii"}:
                return "utf-8" if encoding.startswith("utf") else encoding
        return "utf-8"


def _extract_html(body: str) -> tuple[str | None, str]:
    parser = _HtmlTextExtractor()
    parser.feed(body)
    title = " ".join("".join(parser.title_parts).split()) or None
    return title, "".join(parser.text_parts)
