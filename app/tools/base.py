from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, Mapping, TypeVar, cast

from pydantic import BaseModel, ValidationError

from app.errors import (
    ConfigurationError,
    MalformedToolOutputError,
    ToolInputValidationError,
    ToolNotFoundError,
)

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class Tool(ABC, Generic[InputT, OutputT]):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]
    default_timeout_s: ClassVar[float]
    is_test_component: ClassVar[bool] = False

    @abstractmethod
    async def execute(self, params: InputT) -> OutputT: ...

    def parse_input(self, raw: Mapping[str, Any]) -> InputT:
        try:
            validated = self.input_model.model_validate(dict(raw))
        except ValidationError as exc:
            raise ToolInputValidationError(f"invalid input for tool {self.name!r}: {exc}") from exc
        return cast(InputT, validated)

    def parse_output(self, raw: Any) -> OutputT:
        try:
            validated = self.output_model.model_validate(raw)
        except ValidationError as exc:
            raise MalformedToolOutputError(
                f"malformed output from tool {self.name!r}: {exc}", tool=self.name
            ) from exc
        return cast(OutputT, validated)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool[Any, Any]] = {}

    def register(self, tool: Tool[Any, Any]) -> None:
        if tool.name in self._tools:
            raise ConfigurationError(f"tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool[Any, Any]:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"unknown tool: {name!r}") from None

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "description": tool.description,
                "input_schema": tool.input_model.model_json_schema(),
            }
            for name, tool in self._tools.items()
        }
