from __future__ import annotations

import httpx

from app.config import Config
from app.errors import ConfigurationError
from app.llm.base import LLMProvider
from app.llm.openai_compatible import OpenAICompatibleProvider

OPENAI_COMPATIBLE_NAMES = ("openai_compatible", "openai-compatible")


def build_llm(config: Config, *, client: httpx.AsyncClient | None = None) -> LLMProvider:
    """Config-driven provider factory; validation errors are ConfigurationError.

    'fake' is intentionally rejected here: it exists for tests only, so a real
    CLI run without a configured provider fails fast with actionable guidance
    instead of running one step and dying on an empty response queue.
    """
    llm = config.llm
    if llm.provider == "fake":
        raise ConfigurationError(
            "LLM provider 'fake' is test-only; set RESEARCHPILOT_LLM_PROVIDER "
            "(supported: 'openai_compatible') for real runs"
        )
    if llm.provider in OPENAI_COMPATIBLE_NAMES:
        if llm.api_key is None:
            raise ConfigurationError(
                "LLM provider 'openai_compatible' requires an API key "
                "(set RESEARCHPILOT_LLM_API_KEY)"
            )
        if not llm.base_url:
            raise ConfigurationError(
                "LLM provider 'openai_compatible' requires a base URL "
                "(set RESEARCHPILOT_LLM_BASE_URL)"
            )
        return OpenAICompatibleProvider(
            base_url=llm.base_url,
            model=llm.model,
            api_key=llm.api_key.get_secret_value(),
            timeout_s=llm.timeout_s,
            client=client,
        )
    raise ConfigurationError(
        f"unsupported LLM provider: {llm.provider!r} (supported: 'openai_compatible')"
    )
