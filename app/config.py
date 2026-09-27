from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, SecretStr, ValidationError

from app.errors import ConfigurationError


class RetryConfig(BaseModel, frozen=True):
    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    jitter: bool = True


class LLMConfig(BaseModel, frozen=True):
    provider: str = "fake"
    base_url: str | None = None
    model: str = "fake-model"
    api_key: SecretStr | None = None
    timeout_s: float = 60.0


class SearchConfig(BaseModel, frozen=True):
    provider: str = "tavily"
    api_key: SecretStr | None = None
    max_results: int = 8


class Config(BaseModel, frozen=True):
    llm: LLMConfig = LLMConfig()
    search: SearchConfig = SearchConfig()
    retry: RetryConfig = RetryConfig()
    max_plan_attempts: int = 3
    max_steps: int = 12
    candidate_cap: int = 3
    min_evidence: int = 3
    min_distinct_sources: int = 2
    report_dir: str = "reports"
    memory_path: str = "data/researchpilot.db"
    verbose: bool = False
    memory_enabled: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        source = os.environ if env is None else env
        top: dict[str, Any] = {}
        retry: dict[str, Any] = {}
        llm: dict[str, Any] = {}
        search: dict[str, Any] = {}

        _collect(source, _INT_TOP, top, _parse_int)
        _collect(source, _BOOL_TOP, top, _parse_bool)
        _collect(source, _STR_TOP, top, _parse_str)
        _collect(source, _INT_RETRY, retry, _parse_int)
        _collect(source, _FLOAT_RETRY, retry, _parse_float)
        _collect(source, _INT_SEARCH, search, _parse_int)
        _collect(source, _STR_SEARCH, search, _parse_str)
        _collect(source, _STR_LLM, llm, _parse_str)
        _collect(source, _FLOAT_LLM, llm, _parse_float)
        _collect(source, _SECRET_LLM, llm, _parse_secret)
        _collect(source, _SECRET_SEARCH, search, _parse_secret)

        kwargs: dict[str, Any] = dict(top)
        if retry:
            kwargs["retry"] = retry
        if llm:
            kwargs["llm"] = llm
        if search:
            kwargs["search"] = search
        try:
            return cls(**kwargs)
        except ValidationError as exc:
            raise ConfigurationError(f"invalid configuration: {exc}") from exc


_INT_TOP = {
    "RESEARCHPILOT_MAX_PLAN_ATTEMPTS": "max_plan_attempts",
    "RESEARCHPILOT_MAX_STEPS": "max_steps",
    "RESEARCHPILOT_CANDIDATE_CAP": "candidate_cap",
    "RESEARCHPILOT_MIN_EVIDENCE": "min_evidence",
    "RESEARCHPILOT_MIN_DISTINCT_SOURCES": "min_distinct_sources",
}
_BOOL_TOP = {
    "RESEARCHPILOT_VERBOSE": "verbose",
    "RESEARCHPILOT_MEMORY": "memory_enabled",
}
_STR_TOP = {
    "RESEARCHPILOT_REPORT_DIR": "report_dir",
    "RESEARCHPILOT_MEMORY_PATH": "memory_path",
}
_INT_RETRY = {"RESEARCHPILOT_RETRY_MAX_ATTEMPTS": "max_attempts"}
_FLOAT_RETRY = {
    "RESEARCHPILOT_RETRY_BASE_DELAY": "base_delay_s",
    "RESEARCHPILOT_RETRY_MAX_DELAY": "max_delay_s",
}
_INT_SEARCH = {"RESEARCHPILOT_SEARCH_MAX_RESULTS": "max_results"}
_STR_SEARCH = {"RESEARCHPILOT_SEARCH_PROVIDER": "provider"}
_STR_LLM = {
    "RESEARCHPILOT_LLM_PROVIDER": "provider",
    "RESEARCHPILOT_LLM_BASE_URL": "base_url",
    "RESEARCHPILOT_LLM_MODEL": "model",
}
_FLOAT_LLM = {"RESEARCHPILOT_LLM_TIMEOUT": "timeout_s"}
_SECRET_LLM = {"RESEARCHPILOT_LLM_API_KEY": "api_key"}
_SECRET_SEARCH = {
    "TAVILY_API_KEY": "api_key",
    "RESEARCHPILOT_SEARCH_API_KEY": "api_key",
}


def _collect(
    source: Mapping[str, str],
    table: dict[str, str],
    target: dict[str, Any],
    parse: Callable[[str, str], Any],
) -> None:
    for env_name, field in table.items():
        raw = source.get(env_name)
        if raw is not None:
            target[field] = parse(env_name, raw)


def _parse_int(name: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc


def _parse_float(name: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc


def _parse_bool(name: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean, got {raw!r}")


def _parse_str(name: str, raw: str) -> str:
    return raw


def _parse_secret(name: str, raw: str) -> SecretStr:
    return SecretStr(raw)
