from __future__ import annotations

import httpx

from app.config import Config
from app.errors import ConfigurationError
from app.tools.base import ToolRegistry
from app.tools.calculator import CalculatorTool
from app.tools.failure_simulator import FailureSimulatorTool
from app.tools.web_search import WebSearchTool
from app.tools.webpage_fetch import WebpageFetchTool

_HTTP_TIMEOUT_S = 10.0


def build_tool_registry(
    config: Config, *, client: httpx.AsyncClient | None = None
) -> ToolRegistry:
    if config.search.provider != "tavily":
        raise ConfigurationError(
            f"unsupported search provider: {config.search.provider!r} (supported: 'tavily')"
        )
    api_key = config.search.api_key.get_secret_value() if config.search.api_key else None
    shared = client or httpx.AsyncClient(
        timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
        follow_redirects=True,
        max_redirects=20,
    )
    registry = ToolRegistry()
    registry.register(WebSearchTool(api_key=api_key, client=shared))
    registry.register(WebpageFetchTool(client=shared))
    registry.register(CalculatorTool())
    registry.register(FailureSimulatorTool())
    return registry
