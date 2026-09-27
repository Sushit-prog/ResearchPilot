from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.models.events import FailureMode


def validate_http_url(url: str) -> str:
    """§26 URL guard: http/https only, no embedded credentials, host required.

    Raises ValueError so pydantic validators can adopt it directly;
    tool execute() paths wrap the same ValueError as ToolInputValidationError.
    """
    raw = url.strip()
    if not raw:
        raise ValueError("url must not be empty")
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"unsupported url scheme {parsed.scheme!r} (http/https only)")
    if not parsed.hostname:
        raise ValueError(f"url has no host: {raw!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not embed credentials")
    return raw


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=400)
    max_results: int = Field(default=8, ge=1, le=20)


class SearchResultItem(BaseModel):
    title: str
    url: str                            # validated http/https, no embedded credentials
    snippet: str
    domain: str
    published_at: datetime | None = None

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, value: str) -> str:
        return validate_http_url(value)


class SearchOutput(BaseModel):
    query: str
    results: list[SearchResultItem]
    provider: str                       # which backend answered — tradeoff noted in README
    retrieved_at: datetime


class FetchInput(BaseModel):
    url: str                            # http/https only, validated (§26)
    max_chars: int = Field(default=20_000, ge=500, le=100_000)   # size cap (§26)

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, value: str) -> str:
        return validate_http_url(value)


class FetchOutput(BaseModel):
    requested_url: str
    final_url: str                      # after redirects
    status_code: int
    title: str | None
    text: str                           # extracted main text, whitespace-normalized
    truncated: bool
    content_type: str | None


class CalculatorInput(BaseModel):
    expression: str                     # goal-intrinsic expression, OR an expression
                                        # composed in code by researcher/verifier
    unit: str | None = None


class CalculatorOutput(BaseModel):
    expression: str
    value: float
    unit: str | None = None


class SimulatorInput(BaseModel):
    failure_mode: FailureMode           # timeout | invalid_response | temporary_error
    fail_times: int = Field(default=1, ge=0, le=10)  # fail first N invocations, then
                                                     # succeed → recovery is
                                                     # reproducible by construction
    payload: dict[str, Any] | None = None  # body returned in invalid_response mode


class SimulatorOutput(BaseModel):
    status: str                         # "ok" on post-failure success
    invocation: int
