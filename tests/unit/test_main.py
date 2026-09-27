from __future__ import annotations

import httpx
import pytest

from app.config import Config
from app.errors import ConfigurationError
from app.main import build_tool_registry
from app.tools.armed import ArmedTool
from app.tools.calculator import CalculatorTool
from app.tools.failure_simulator import FailureSimulatorTool
from app.tools.web_search import WebSearchTool
from app.tools.webpage_fetch import WebpageFetchTool

EXPECTED_TOOLS = ["web_search", "webpage_fetch", "calculator", "failure_simulator"]


def config_with_key(**overrides: str) -> Config:
    env = {"TAVILY_API_KEY": "tvly-test-key", **overrides}
    return Config.from_env(env)


def test_registry_registers_all_four_tools() -> None:
    registry = build_tool_registry(config_with_key())
    assert registry.names() == EXPECTED_TOOLS


def test_registry_schemas_have_description_and_input_schema() -> None:
    registry = build_tool_registry(config_with_key())
    schemas = registry.schemas()
    assert set(schemas) == set(EXPECTED_TOOLS)
    for entry in schemas.values():
        assert entry["description"]
        assert entry["input_schema"]["type"] == "object"


def test_registered_tool_types() -> None:
    registry = build_tool_registry(config_with_key())
    assert isinstance(registry.get("web_search"), WebSearchTool)
    assert isinstance(registry.get("webpage_fetch"), WebpageFetchTool)
    assert isinstance(registry.get("calculator"), CalculatorTool)
    assert isinstance(registry.get("failure_simulator"), FailureSimulatorTool)


def test_failure_simulator_flagged_as_test_component() -> None:
    registry = build_tool_registry(config_with_key())
    assert registry.get("failure_simulator").is_test_component is True
    assert registry.get("web_search").is_test_component is False


def test_missing_api_key_aborts_with_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        build_tool_registry(Config.from_env({}))
    assert type(exc_info.value) is ConfigurationError
    assert "TAVILY_API_KEY" in str(exc_info.value)


def test_unknown_provider_aborts_with_configuration_error() -> None:
    config = config_with_key(RESEARCHPILOT_SEARCH_PROVIDER="brave")
    with pytest.raises(ConfigurationError) as exc_info:
        build_tool_registry(config)
    assert type(exc_info.value) is ConfigurationError
    assert "brave" in str(exc_info.value)


def test_injected_http_client_is_shared_and_not_closed() -> None:
    client = httpx.AsyncClient()
    registry = build_tool_registry(config_with_key(), client=client)
    assert registry.names() == EXPECTED_TOOLS
    assert not client.is_closed


def test_default_registry_has_no_armed_tools() -> None:
    registry = build_tool_registry(config_with_key())
    assert not isinstance(registry.get("web_search"), ArmedTool)
    assert not isinstance(registry.get("webpage_fetch"), ArmedTool)


def test_simulate_failure_wraps_both_network_tools_only() -> None:
    registry = build_tool_registry(config_with_key(), simulate_failure="timeout")
    assert isinstance(registry.get("web_search"), ArmedTool)
    assert isinstance(registry.get("webpage_fetch"), ArmedTool)
    assert isinstance(registry.get("calculator"), CalculatorTool)
    assert isinstance(registry.get("failure_simulator"), FailureSimulatorTool)


def test_simulate_failure_rejects_unknown_mode() -> None:
    with pytest.raises(ConfigurationError, match="unsupported failure mode"):
        build_tool_registry(config_with_key(), simulate_failure="explode")
