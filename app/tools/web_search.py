from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from app.errors import (
    ConfigurationError,
    MalformedToolOutputError,
    PermanentToolError,
    TransientToolError,
)
from app.models.tool_io import SearchInput, SearchOutput, SearchResultItem, validate_http_url
from app.tools.base import Tool

TAVILY_ENDPOINT = "https://api.tavily.com/search"


class WebSearchTool(Tool[SearchInput, SearchOutput]):
    name = "web_search"
    description = "Web search via the Tavily API; returns structured results only"
    input_model = SearchInput
    output_model = SearchOutput
    default_timeout_s = 10.0

    def __init__(
        self,
        *,
        api_key: str | None,
        client: httpx.AsyncClient | None = None,
        endpoint: str = TAVILY_ENDPOINT,
    ) -> None:
        if not api_key:
            raise ConfigurationError(
                "search provider 'tavily' requires an API key "
                "(set TAVILY_API_KEY or RESEARCHPILOT_SEARCH_API_KEY)"
            )
        self._api_key = api_key
        self._endpoint = endpoint
        self._client = client

    async def execute(self, params: SearchInput) -> SearchOutput:
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(self.default_timeout_s))
        try:
            try:
                response = await client.post(
                    self._endpoint,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"query": params.query, "max_results": params.max_results},
                )
            except httpx.TimeoutException as exc:
                raise TransientToolError("Tavily request timed out", tool=self.name) from exc
            except httpx.HTTPError as exc:
                raise TransientToolError(f"Tavily request failed: {exc}", tool=self.name) from exc
            return self._parse_response(params, response)
        finally:
            if self._client is None:
                await client.aclose()

    def _parse_response(self, params: SearchInput, response: httpx.Response) -> SearchOutput:
        status = response.status_code
        if status == 429:
            raise TransientToolError("Tavily rate limit reached (HTTP 429)", tool=self.name)
        if 400 <= status < 500:
            raise PermanentToolError(f"Tavily rejected request (HTTP {status})", tool=self.name)
        if status >= 500:
            raise TransientToolError(f"Tavily server error (HTTP {status})", tool=self.name)
        try:
            body: Any = response.json()
        except ValueError as exc:
            raise MalformedToolOutputError(
                "Tavily returned a non-JSON body", tool=self.name
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise MalformedToolOutputError(
                "Tavily response is missing a results list", tool=self.name
            )
        results = [
            item
            for raw in body["results"]
            if (item := _map_result(raw)) is not None
        ][: params.max_results]
        return self.parse_output(
            {
                "query": params.query,
                "results": [item.model_dump() for item in results],
                "provider": "tavily",
                "retrieved_at": datetime.now(timezone.utc),
            }
        )


def _map_result(raw: Any) -> SearchResultItem | None:
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    url = raw.get("url")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(url, str):
        return None
    try:
        url = validate_http_url(url)
    except ValueError:
        return None
    snippet = raw.get("content")
    return SearchResultItem(
        title=title.strip(),
        url=url,
        snippet=snippet if isinstance(snippet, str) else "",
        domain=urlparse(url).netloc.lower(),
        published_at=_parse_published(raw.get("published_date")),
    )


def _parse_published(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
