from __future__ import annotations

from typing import Any

import pydantic
import pytest

from app.errors import (
    ConfigurationError,
    MalformedToolOutputError,
    ToolInputValidationError,
    ToolNotFoundError,
)
from app.tools.base import Tool, ToolRegistry


class DummyInput(pydantic.BaseModel):
    value: int


class DummyOutput(pydantic.BaseModel):
    ok: bool


class DummyTool(Tool[DummyInput, DummyOutput]):
    name = "dummy"
    description = "dummy tool for tests"
    input_model = DummyInput
    output_model = DummyOutput
    default_timeout_s = 1.0

    async def execute(self, params: DummyInput) -> DummyOutput:
        return DummyOutput(ok=params.value > 0)


async def test_execute_returns_typed_output() -> None:
    tool = DummyTool()
    result = await tool.execute(DummyInput(value=1))
    assert result == DummyOutput(ok=True)


def test_parse_input_success() -> None:
    parsed = DummyTool().parse_input({"value": 3})
    assert parsed == DummyInput(value=3)


def test_parse_input_invalid_value_wraps_pydantic_error() -> None:
    with pytest.raises(ToolInputValidationError) as exc_info:
        DummyTool().parse_input({"value": "not-an-int"})
    assert type(exc_info.value) is ToolInputValidationError
    assert isinstance(exc_info.value.__cause__, pydantic.ValidationError)
    assert "dummy" in str(exc_info.value)


def test_parse_input_missing_field_rejected() -> None:
    with pytest.raises(ToolInputValidationError):
        DummyTool().parse_input({})


def test_parse_output_success() -> None:
    parsed = DummyTool().parse_output({"ok": True})
    assert parsed == DummyOutput(ok=True)


def test_parse_output_invalid_wraps_pydantic_error() -> None:
    with pytest.raises(MalformedToolOutputError) as exc_info:
        DummyTool().parse_output({"missing": True})
    assert type(exc_info.value) is MalformedToolOutputError
    assert isinstance(exc_info.value.__cause__, pydantic.ValidationError)
    assert exc_info.value.tool == "dummy"


def test_registry_register_and_get() -> None:
    registry = ToolRegistry()
    tool = DummyTool()
    registry.register(tool)
    assert registry.get("dummy") is tool
    assert registry.names() == ["dummy"]


def test_registry_empty() -> None:
    registry = ToolRegistry()
    assert registry.names() == []
    with pytest.raises(ToolNotFoundError) as exc_info:
        registry.get("web_search")
    assert type(exc_info.value) is ToolNotFoundError


def test_registry_duplicate_registration_rejected() -> None:
    registry = ToolRegistry()
    registry.register(DummyTool())
    with pytest.raises(ConfigurationError) as exc_info:
        registry.register(DummyTool())
    assert type(exc_info.value) is ConfigurationError


def test_registry_schemas_shape() -> None:
    registry = ToolRegistry()
    registry.register(DummyTool())
    schemas = registry.schemas()
    assert set(schemas) == {"dummy"}
    entry: dict[str, Any] = schemas["dummy"]
    assert entry["description"] == "dummy tool for tests"
    assert entry["input_schema"]["type"] == "object"
    assert "value" in entry["input_schema"]["properties"]
