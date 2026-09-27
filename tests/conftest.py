from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel

from app.agent.executor import StepOutcome
from app.llm.fake import FakeLLMProvider
from app.models.events import ExecutionEvent, ToolResult
from app.models.evidence import Evidence, Source
from app.observability import EventEmitter, InMemoryEventSink
from app.tools.base import Tool, ToolRegistry

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class SearchInput(BaseModel):
    query: str
    max_results: int = 5


class SearchOutput(BaseModel):
    results: list[str] = []


class CalculatorInput(BaseModel):
    expression: str


class CalculatorOutput(BaseModel):
    answer: float


class WebSearchTool(Tool[SearchInput, SearchOutput]):
    name = "web_search"
    description = "Search the web and return structured results"
    input_model = SearchInput
    output_model = SearchOutput
    default_timeout_s = 5.0

    async def execute(self, params: SearchInput) -> SearchOutput:
        return SearchOutput()


class CalculatorTool(Tool[CalculatorInput, CalculatorOutput]):
    name = "calculator"
    description = "Evaluate arithmetic expressions"
    input_model = CalculatorInput
    output_model = CalculatorOutput
    default_timeout_s = 2.0

    async def execute(self, params: CalculatorInput) -> CalculatorOutput:
        return CalculatorOutput(answer=0.0)


VALID_PLAN_DATA = {
    "goal": "test goal",
    "steps": [
        {
            "id": "r1",
            "objective": "search for X",
            "tool": "web_search",
            "arguments": {"query": "X"},
            "expected_output": "search results",
        },
        {
            "id": "q1",
            "objective": "compute 2+2",
            "tool": "calculator",
            "arguments": {"expression": "2+2"},
            "expected_output": "the sum",
        },
    ],
}


class EvidenceHandler:
    def __init__(self, evidence_factory, source_factory, tool_result_factory) -> None:
        self._evidence = evidence_factory
        self._source = source_factory
        self._tool_result = tool_result_factory

    async def handle(self, step) -> StepOutcome:
        if step.tool == "web_search":
            return StepOutcome(
                tool_results=[self._tool_result(step.id, "web_search")],
                evidence=[
                    self._evidence(
                        "ev-a", "X reached 10B", url="https://example.com/a",
                        step_id=step.id,
                    ),
                    self._evidence(
                        "ev-b", "X grew 12%", url="https://example.org/b",
                        step_id=step.id,
                    ),
                ],
                sources=[
                    self._source("https://example.com/a", title="Example A"),
                    self._source("https://example.org/b", title="Example B"),
                ],
            )
        return StepOutcome(
            tool_results=[self._tool_result(step.id, "calculator", attempts=3)],
            evidence=[
                self._evidence("ev-c", "2 + 2 = 4", url=None, step_id=step.id)
            ],
        )


class EmptyHandler:
    async def handle(self, step) -> StepOutcome:
        return StepOutcome()


@pytest.fixture
def fake_llm() -> Callable[..., FakeLLMProvider]:
    def _make(*responses: str) -> FakeLLMProvider:
        return FakeLLMProvider(responses)

    return _make


@pytest.fixture
def registry() -> ToolRegistry:
    tool_registry = ToolRegistry()
    tool_registry.register(WebSearchTool())
    tool_registry.register(CalculatorTool())
    return tool_registry


@pytest.fixture
def fixed_clock() -> Callable[[], datetime]:
    def _clock() -> datetime:
        return FIXED_TIME

    return _clock


@pytest.fixture
def event_log(fixed_clock) -> tuple[EventEmitter, list[ExecutionEvent]]:
    events: list[ExecutionEvent] = []
    emitter = EventEmitter(fixed_clock, sinks=[InMemoryEventSink(events)])
    return emitter, events


@pytest.fixture
def valid_plan_json() -> str:
    return json.dumps(VALID_PLAN_DATA)


@pytest.fixture
def evidence_factory() -> Callable[..., Evidence]:
    def _make(
        evidence_id: str,
        claim: str,
        *,
        url: str | None,
        step_id: str | None = None,
    ) -> Evidence:
        return Evidence(
            evidence_id=evidence_id,
            claim=claim,
            source_url=url,
            source_title="Example",
            extracted_text=claim,
            relevance_score=0.9,
            confidence=0.8,
            retrieved_at=FIXED_TIME,
            step_id=step_id,
        )

    return _make


@pytest.fixture
def source_factory() -> Callable[..., Source]:
    def _make(url: str, *, title: str | None = None) -> Source:
        return Source(
            url=url,
            domain=url.split("/")[2],
            title=title,
            retrieved_at=FIXED_TIME,
        )

    return _make


@pytest.fixture
def tool_result_factory() -> Callable[..., ToolResult]:
    def _make(
        step_id: str,
        tool: str,
        *,
        ordinal: int = 1,
        attempts: int = 1,
        success: bool = True,
    ) -> ToolResult:
        return ToolResult(
            result_id=f"{step_id}-{tool}-{ordinal}",
            tool=tool,
            step_id=step_id,
            call_ordinal=ordinal,
            success=success,
            attempts=attempts,
            started_at=FIXED_TIME,
        )

    return _make


@pytest.fixture
def evidence_handler(evidence_factory, source_factory, tool_result_factory):
    return EvidenceHandler(evidence_factory, source_factory, tool_result_factory)


@pytest.fixture
def empty_handler() -> EmptyHandler:
    return EmptyHandler()
