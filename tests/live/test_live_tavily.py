from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.models.tool_io import SearchInput
from app.tools.web_search import TAVILY_ENDPOINT, WebSearchTool

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _key() -> str:
    raw = os.environ.get("TAVILY_API_KEY", "").strip()
    return raw.strip('"').strip("'")


async def test_live_tavily_bearer_auth_and_response_shape() -> None:
    key = _key()
    if not key:
        pytest.skip("TAVILY_API_KEY not set")
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            TAVILY_ENDPOINT,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"query": "python 3.14 release", "max_results": 3},
        )
    print(f"raw HTTP status: {response.status_code}")
    body: Any = response.json()
    shape: dict[str, Any] = {
        "top_level_keys": sorted(body) if isinstance(body, dict) else f"non-dict: {type(body)}",
    }
    if isinstance(body, dict) and isinstance(body.get("results"), list) and body["results"]:
        first = body["results"][0]
        shape["n_results"] = len(body["results"])
        shape["first_result_keys"] = sorted(first)
        shape["first_result_value_types"] = {
            k: type(v).__name__ for k, v in sorted(first.items())
        }
        shape["results_missing_published_date"] = sum(
            1 for r in body["results"] if "published_date" not in r
        )
    print(f"raw response shape: {json.dumps(shape, indent=2)}")
    assert response.status_code == 200
    assert isinstance(body, dict)
    assert isinstance(body.get("results"), list)

    tool = WebSearchTool(api_key=key)
    output = await tool.execute(SearchInput(query="python 3.14 release", max_results=3))
    assert output.provider == "tavily"
    assert output.results
    assert all(item.url.startswith(("http://", "https://")) for item in output.results)
    print(
        "tool-mapped shape: "
        + json.dumps(
            {
                "n_results": len(output.results),
                "published_at": [str(item.published_at) for item in output.results],
                "domains": [item.domain for item in output.results],
            },
            indent=2,
        )
    )
