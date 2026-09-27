from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.agent.executor import execute_plan
from app.agent.orchestrator import Orchestrator
from app.agent.researcher import Researcher
from app.agent.state import AgentStatus
from app.config import Config
from app.models.events import EventKind
from app.models.plan import Plan
from app.observability import EventEmitter, InMemoryEventSink
from app.reliability.runner import Runner
from app.tools.base import ToolRegistry
from app.tools.calculator import CalculatorTool
from app.tools.failure_simulator import FailureSimulatorTool
from app.tools.web_search import WebSearchTool
from app.tools.webpage_fetch import WebpageFetchTool

FIXED_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

PLAN_DATA = {
    "goal": "explain the mission in numbers",
    "steps": [
        {
            "id": "r1",
            "objective": "search for mission facts",
            "tool": "web_search",
            "arguments": {"query": "orbital mechanics"},
            "expected_output": "search results with sources",
        },
        {
            "id": "q1",
            "objective": "compute the sum",
            "tool": "calculator",
            "arguments": {"expression": "2 + 2"},
            "expected_output": "the sum",
        },
    ],
}


def zero(low: float, high: float) -> float:
    return 0.0


def page(title: str, *paragraphs: str) -> str:
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"


def html(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def make_handler(*, flaky_search: bool = False) -> Any:
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        if request.url.host == "api.tavily.com":
            search_calls += 1
            if flaky_search and search_calls == 1:
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Mission fact one",
                            "url": "https://example.com/one",
                            "content": "orbital mechanics fact",
                        },
                        {
                            "title": "Mission fact two",
                            "url": "https://example.org/two",
                            "content": "orbital mechanics fact",
                        },
                    ]
                },
            )
        if request.url.path == "/one":
            return html(page("One", "Fact one about orbital mechanics."))
        if request.url.path == "/two":
            return html(page("Two", "Fact two about orbital mechanics."))
        return httpx.Response(404)

    return handler


def build_world(
    handler: Any, emitter: EventEmitter
) -> tuple[Researcher, list[float], ToolRegistry]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    registry = ToolRegistry()
    registry.register(WebSearchTool(api_key="test-key", client=client))
    registry.register(WebpageFetchTool(client=client))
    registry.register(CalculatorTool())
    registry.register(FailureSimulatorTool())
    delays: list[float] = []
    runner = Runner(Config(), emitter, clock=lambda: FIXED_TIME, sleep=delays.append, rng=zero)
    researcher = Researcher(
        registry=registry, config=Config(), emitter=emitter, runner=runner
    )
    return researcher, delays, registry


async def test_execute_plan_runs_researcher_through_untouched_executor() -> None:
    emitter = EventEmitter(lambda: FIXED_TIME, sinks=[InMemoryEventSink([])])
    researcher, delays, _ = build_world(make_handler(), emitter)
    plan = Plan.model_validate(PLAN_DATA)

    bundles = await execute_plan(plan, researcher, emitter=emitter)

    assert [bundle.record.status.value for bundle in bundles] == ["succeeded", "succeeded"]
    first, second = bundles
    assert [result.result_id for result in first.tool_results] == [
        "r1:web_search:1",
        "r1:webpage_fetch:2",
        "r1:webpage_fetch:3",
    ]
    assert [result.result_id for result in second.tool_results] == ["q1:calculator:1"]
    assert len(first.evidence) == 2
    assert len(second.evidence) == 1
    assert len(first.sources) == 2
    assert delays == []


async def test_orchestrator_end_to_end_books_retries_and_shares_emitter(
    fake_llm,
    synthesis_json,
    tmp_path,
) -> None:
    events: list[Any] = []
    emitter = EventEmitter(lambda: FIXED_TIME, sinks=[InMemoryEventSink(events)])
    researcher, delays, registry = build_world(make_handler(flaky_search=True), emitter)

    orchestrator = Orchestrator(
        "explain the mission in numbers",
        llm=fake_llm(
            json.dumps(PLAN_DATA),
            synthesis_json(
                [
                    ("r1:2", "https://example.com/one"),
                    ("r1:3", "https://example.org/two"),
                    ("q1:1", None),
                ],
                question="explain the mission in numbers",
            ),
        ),
        registry=registry,
        handler=researcher,
        config=Config(report_dir=str(tmp_path)),
        clock=lambda: FIXED_TIME,
        emitter=emitter,
    )
    # production wiring: the same emitter also feeds state.execution_events
    emitter.add(InMemoryEventSink(orchestrator.state.execution_events))

    state = await orchestrator.run()

    assert state.status is AgentStatus.COMPLETED
    assert [record.step_id for record in state.completed_steps] == ["r1", "q1"]
    assert state.failed_steps == []
    assert state.warnings == []
    assert len(state.evidence) == 3
    assert len(state.sources) == 2
    assert state.final_report is not None
    assert state.report_path is not None
    assert Path(state.report_path).parent == tmp_path
    assert delays == [0.0]
    assert state.retry_counts == {
        "r1:web_search:1": 1,
        "r1:webpage_fetch:2": 0,
        "r1:webpage_fetch:3": 0,
        "q1:calculator:1": 0,
    }

    kinds = [event.event for event in state.execution_events]
    assert kinds.count(EventKind.RETRY_STARTED) == 1
    recovered = [
        event
        for event in state.execution_events
        if event.event is EventKind.TOOL_SUCCEEDED and event.attempt == 2
    ]
    assert len(recovered) == 1
    assert recovered[0].tool == "web_search"
    assert kinds.count(EventKind.EVIDENCE_ADDED) == 3
    assert kinds.count(EventKind.PLAN_CREATED) == 1

    external = [event.event for event in events]
    assert EventKind.RETRY_STARTED in external
    assert EventKind.EVIDENCE_ADDED in external
