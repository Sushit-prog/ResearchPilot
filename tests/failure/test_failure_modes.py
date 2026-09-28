from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.agent.researcher import Researcher
from app.config import Config
from app.models.events import EventKind, FailureKind
from app.models.plan import PlanStep
from app.observability import EventEmitter, InMemoryEventSink
from app.reliability.runner import Runner
from app.tools.base import ToolRegistry
from app.tools.calculator import CalculatorTool
from app.tools.failure_simulator import FailureSimulatorTool
from app.tools.web_search import WebSearchTool
from app.tools.webpage_fetch import WebpageFetchTool

FIXED_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def zero(low: float, high: float) -> float:
    return 0.0


def page(title: str, *paragraphs: str) -> str:
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"


def html(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def build() -> tuple[Runner, list[float], list[Any]]:
    events: list[Any] = []
    delays: list[float] = []
    emitter = EventEmitter(lambda: FIXED_TIME, sinks=[InMemoryEventSink(events)])
    runner = Runner(
        Config(),
        emitter,
        clock=lambda: FIXED_TIME,
        sleep=delays.append,
        rng=zero,
    )
    return runner, delays, events


def fetch_tool(client: httpx.AsyncClient) -> WebpageFetchTool:
    return WebpageFetchTool(client=client)


def client_for(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def kinds(events: list[Any]) -> list[EventKind]:
    return [event.event for event in events]


async def test_timeout_becomes_transient_backoff_and_recovery() -> None:
    runner, delays, events = build()
    result = await runner.call(
        FailureSimulatorTool(),
        step_id="s1",
        call_ordinal=1,
        arguments={"failure_mode": "timeout", "fail_times": 1},
        timeout_s=0.05,
    )

    assert result.success is True
    assert result.attempts == 2
    assert delays == [0.0]
    assert kinds(events) == [
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FAILED,
        EventKind.RETRY_STARTED,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_SUCCEEDED,
    ]
    failure = events[1]
    assert failure.status == "transient"
    assert "exceeded 0.05s" in (failure.message or "")
    assert events[2].attempt == 2
    assert events[4].attempt == 2


async def test_malformed_tool_output_is_detected_retried_and_recovered() -> None:
    runner, delays, events = build()
    result = await runner.call(
        FailureSimulatorTool(),
        step_id="s1",
        call_ordinal=1,
        arguments={"failure_mode": "invalid_response", "fail_times": 1},
    )

    assert result.success is True
    assert result.attempts == 2
    assert delays == [0.0]
    failure = events[1]
    assert failure.event is EventKind.TOOL_FAILED
    assert failure.status == "malformed_output"
    assert "malformed output" in (failure.message or "")
    assert events[2].event is EventKind.RETRY_STARTED
    assert events[2].attempt == 2
    assert kinds(events)[-1] is EventKind.TOOL_SUCCEEDED


async def test_http_5xx_retries_then_exhausts_while_404_fails_immediately() -> None:
    runner, delays, server_events = build()
    client = client_for(lambda request: httpx.Response(500))
    transient = await runner.call(
        fetch_tool(client),
        step_id="r1",
        call_ordinal=2,
        arguments={"url": "https://example.com/flaky"},
    )

    assert transient.success is False
    assert transient.attempts == 3
    assert transient.failure_kind is FailureKind.TRANSIENT
    assert delays == [0.0, 0.0]
    assert kinds(server_events) == [
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FAILED,
        EventKind.RETRY_STARTED,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FAILED,
        EventKind.RETRY_STARTED,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FAILED,
    ]

    runner2, delays2, missing_events = build()
    client2 = client_for(lambda request: httpx.Response(404))
    permanent = await runner2.call(
        fetch_tool(client2),
        step_id="r1",
        call_ordinal=3,
        arguments={"url": "https://example.com/missing"},
    )

    assert permanent.success is False
    assert permanent.attempts == 1
    assert permanent.failure_kind is FailureKind.PERMANENT
    assert delays2 == []
    assert kinds(missing_events) == [EventKind.TOOL_STARTED, EventKind.TOOL_FAILED]


async def test_exhausted_retries_advance_to_next_ranked_candidate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Dead 500",
                            "url": "https://example.com/flaky",
                            "content": "orbital mechanics",
                        },
                        {
                            "title": "Dead 404",
                            "url": "https://example.com/missing",
                            "content": "orbital mechanics",
                        },
                        {
                            "title": "Live",
                            "url": "https://example.org/live",
                            "content": "orbital mechanics",
                        },
                    ]
                },
            )
        if request.url.path == "/flaky":
            return httpx.Response(500)
        if request.url.path == "/missing":
            return httpx.Response(404)
        return html(
            page("Live", "The live candidate finally answered with orbital mechanics content.")
        )

    events: list[Any] = []
    delays: list[float] = []
    emitter = EventEmitter(lambda: FIXED_TIME, sinks=[InMemoryEventSink(events)])
    client = client_for(handler)
    registry = ToolRegistry()
    registry.register(WebSearchTool(api_key="test-key", client=client))
    registry.register(WebpageFetchTool(client=client))
    registry.register(CalculatorTool())
    registry.register(FailureSimulatorTool())
    runner = Runner(
        Config(),
        emitter,
        clock=lambda: FIXED_TIME,
        sleep=delays.append,
        rng=zero,
    )
    researcher = Researcher(
        registry=registry, config=Config(), emitter=emitter, runner=runner
    )

    outcome = await researcher.handle(
        PlanStep(
            id="r1",
            objective="research orbital mechanics",
            tool="web_search",
            arguments={"query": "orbital mechanics"},
            expected_output="findings",
        )
    )

    assert outcome.error is None
    search_result, dead500, missing, live = outcome.tool_results
    assert search_result.success is True
    assert dead500.attempts == 3
    assert dead500.failure_kind is FailureKind.TRANSIENT
    assert missing.attempts == 1
    assert missing.failure_kind is FailureKind.PERMANENT
    assert live.success is True
    assert delays == [0.0, 0.0]

    assert [source.status.value for source in outcome.sources] == [
        "unavailable",
        "unavailable",
        "available",
    ]
    assert outcome.sources[2].url == "https://example.org/live"

    assert len(outcome.evidence) == 1
    assert outcome.evidence[0].source_url == "https://example.org/live"
    assert (
        outcome.evidence[0].claim
        == "The live candidate finally answered with orbital mechanics content."
    )

    assert kinds(events).count(EventKind.SOURCE_UNAVAILABLE) == 2
    assert kinds(events).count(EventKind.CANDIDATE_ADVANCED) == 2
    assert kinds(events).count(EventKind.EVIDENCE_ADDED) == 1
    assert [event.attempt for event in events if event.event is EventKind.RETRY_STARTED] == [
        2,
        3,
    ]
