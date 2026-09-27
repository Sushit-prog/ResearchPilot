from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Config
from app.errors import ConfigurationError

ENV = {
    "RESEARCHPILOT_MAX_STEPS": "7",
    "RESEARCHPILOT_VERBOSE": "true",
    "RESEARCHPILOT_MEMORY": "off",
    "RESEARCHPILOT_RETRY_MAX_ATTEMPTS": "5",
    "RESEARCHPILOT_RETRY_BASE_DELAY": "0.25",
    "RESEARCHPILOT_LLM_TIMEOUT": "12.5",
    "RESEARCHPILOT_LLM_MODEL": "gpt-x",
    "RESEARCHPILOT_LLM_API_KEY": "sk-secret-42",
    "RESEARCHPILOT_REPORT_DIR": "out",
}


def test_defaults_match_doc_section_6() -> None:
    config = Config()
    assert config.max_plan_attempts == 3
    assert config.max_synthesis_attempts == 2
    assert config.max_steps == 12
    assert config.candidate_cap == 3
    assert config.min_evidence == 3
    assert config.min_distinct_sources == 2
    assert config.retry.max_attempts == 3
    assert config.retry.base_delay_s == 0.5
    assert config.retry.max_delay_s == 8.0
    assert config.report_dir == "reports"
    assert config.memory_path == "data/researchpilot.db"
    assert config.verbose is False
    assert config.memory_enabled is True


def test_from_env_overrides_top_level_and_nested() -> None:
    config = Config.from_env(ENV)
    assert config.max_steps == 7
    assert config.verbose is True
    assert config.memory_enabled is False
    assert config.retry.max_attempts == 5
    assert config.retry.base_delay_s == 0.25
    assert config.llm.timeout_s == 12.5
    assert config.llm.model == "gpt-x"
    assert config.report_dir == "out"
    assert config.candidate_cap == 3


def test_max_synthesis_attempts_env_override() -> None:
    config = Config.from_env({"RESEARCHPILOT_MAX_SYNTHESIS_ATTEMPTS": "4"})
    assert config.max_synthesis_attempts == 4


def test_api_key_is_secret_and_absent_from_repr() -> None:
    config = Config.from_env(ENV)
    assert config.llm.api_key is not None
    assert config.llm.api_key.get_secret_value() == "sk-secret-42"
    assert "sk-secret-42" not in repr(config)


def test_invalid_integer_is_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        Config.from_env({"RESEARCHPILOT_MAX_STEPS": "many"})
    assert type(exc_info.value) is ConfigurationError


def test_invalid_float_is_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        Config.from_env({"RESEARCHPILOT_RETRY_BASE_DELAY": "soon"})


def test_invalid_boolean_is_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        Config.from_env({"RESEARCHPILOT_VERBOSE": "perhaps"})


def test_config_is_frozen() -> None:
    with pytest.raises(ValidationError):
        Config().max_steps = 99  # type: ignore[misc]


def test_unknown_env_keys_ignored() -> None:
    config = Config.from_env({"RESEARCHPILOT_NOT_A_SETTING": "x"})
    assert config == Config()


def test_search_defaults_to_tavily() -> None:
    config = Config()
    assert config.search.provider == "tavily"
    assert config.search.api_key is None


def test_tavily_api_key_env_populates_search_key() -> None:
    config = Config.from_env({"TAVILY_API_KEY": "tvly-abc"})
    assert config.search.api_key is not None
    assert config.search.api_key.get_secret_value() == "tvly-abc"
    assert "tvly-abc" not in repr(config)


def test_generic_search_key_takes_precedence_over_tavily() -> None:
    config = Config.from_env(
        {"TAVILY_API_KEY": "tvly-generic", "RESEARCHPILOT_SEARCH_API_KEY": "generic-override"}
    )
    assert config.search.api_key is not None
    assert config.search.api_key.get_secret_value() == "generic-override"


def test_search_provider_env_override() -> None:
    config = Config.from_env({"RESEARCHPILOT_SEARCH_PROVIDER": "custom"})
    assert config.search.provider == "custom"
