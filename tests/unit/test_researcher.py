from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.agent.researcher import Rejection, Researcher, _select_passages, _web_evidence
from app.config import Config
from app.models.events import EventKind, FailureKind
from app.models.plan import PlanStep
from app.observability import EventEmitter, InMemoryEventSink
from app.reliability.runner import Runner
from app.reliability.validation import term_set
from app.tools.base import ToolRegistry
from app.tools.calculator import CalculatorTool
from app.tools.failure_simulator import FailureSimulatorTool
from app.tools.web_search import WebSearchTool
from app.tools.webpage_fetch import WebpageFetchTool

FIXED_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def zero(low: float, high: float) -> float:
    return 0.0


def sr(title: str, url: str, content: str) -> dict[str, str]:
    return {"title": title, "url": url, "content": content}


def page(title: str, *paragraphs: str) -> str:
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"


def html(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def make_researcher(
    handler: Any, events: list[Any], *, config: Config | None = None
) -> tuple[Researcher, list[float]]:
    cfg = config if config is not None else Config()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    registry = ToolRegistry()
    registry.register(WebSearchTool(api_key="test-key", client=client))
    registry.register(WebpageFetchTool(client=client))
    registry.register(CalculatorTool())
    registry.register(FailureSimulatorTool())
    delays: list[float] = []
    emitter = EventEmitter(lambda: FIXED_TIME, sinks=[InMemoryEventSink(events)])
    runner = Runner(cfg, emitter, clock=lambda: FIXED_TIME, sleep=delays.append, rng=zero)
    return Researcher(registry=registry, config=cfg, emitter=emitter, runner=runner), delays


def research_step(query: str = "orbital mechanics alpha") -> PlanStep:
    return PlanStep(
        id="r1",
        objective=f"research {query}",
        tool="web_search",
        arguments={"query": query},
        expected_output="findings with sources",
    )


def kinds(events: list[Any]) -> list[EventKind]:
    return [event.event for event in events]


async def test_research_step_ranks_tiers_dedups_urls_and_extracts_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        sr("Example A", "https://example.com/a", "alpha beta gamma"),
                        sr("Dup A", "https://example.com/a/", "duplicate copy"),
                        sr("NASA mission", "https://www.nasa.gov/mission", "orbital mechanics"),
                    ]
                },
            )
        if request.url.host == "www.nasa.gov":
            return httpx.Response(
                200,
                text=page("NASA", "NASA's mission explores orbital mechanics daily."),
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if request.url.host == "example.com":
            return html(page("A", "Alpha beta content explains the orbital mechanics workflow."))
        return httpx.Response(404)

    events: list[Any] = []
    researcher, delays = make_researcher(handler, events)
    outcome = await researcher.handle(research_step())

    assert outcome.error is None
    assert delays == []
    assert len(outcome.tool_results) == 3
    assert [result.call_ordinal for result in outcome.tool_results] == [1, 2, 3]
    first_fetch = outcome.tool_results[1]
    assert first_fetch.output is not None
    # nasa.gov is PRIMARY-tier, so it outranks example.com despite search order;
    # the duplicate URL collapsed to a single candidate (only two fetches ran)
    assert first_fetch.output["requested_url"] == "https://www.nasa.gov/mission"

    assert len(outcome.evidence) == 2
    assert {item.evidence_id for item in outcome.evidence} == {"r1:2", "r1:3"}
    nasa = next(item for item in outcome.evidence if item.evidence_id == "r1:2")
    assert nasa.claim == "NASA's mission explores orbital mechanics daily."
    assert nasa.source_url == "https://www.nasa.gov/mission"
    assert nasa.source_title == "NASA"
    assert nasa.step_id == "r1"
    assert nasa.retrieved_at == FIXED_TIME
    assert nasa.confidence == 0.7
    assert 0.0 <= nasa.relevance_score <= 1.0

    assert len(outcome.sources) == 2
    by_url = {source.url: source for source in outcome.sources}
    assert by_url["https://www.nasa.gov/mission"].quality.value == "primary"
    assert by_url["https://example.com/a"].quality.value == "unknown"
    assert all(source.status.value == "available" for source in outcome.sources)

    seen = kinds(events)
    assert seen[0] is EventKind.TOOL_SELECTED
    assert seen.count(EventKind.EVIDENCE_ADDED) == 2


async def test_failed_candidate_marks_source_unavailable_and_advances() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        sr("Dead", "https://example.com/dead", "orbital mechanics"),
                        sr("Live", "https://example.org/live", "orbital mechanics"),
                    ]
                },
            )
        if request.url.host == "example.com":
            return httpx.Response(404)
        if request.url.host == "example.org":
            return html(page("Live", "Recovered content about orbital mechanics research."))
        return httpx.Response(404)

    events: list[Any] = []
    researcher, delays = make_researcher(handler, events)
    outcome = await researcher.handle(research_step())

    assert outcome.error is None
    assert delays == []
    assert len(outcome.evidence) == 1
    assert outcome.evidence[0].source_url == "https://example.org/live"

    assert [source.status.value for source in outcome.sources] == [
        "unavailable",
        "available",
    ]
    unavailable = outcome.sources[0]
    assert unavailable.url == "https://example.com/dead"
    assert "HTTP 404" in (unavailable.unavailable_reason or "")

    dead_fetch = outcome.tool_results[1]
    assert dead_fetch.attempts == 1
    assert dead_fetch.failure_kind is FailureKind.PERMANENT

    advanced = [
        event
        for event in events
        if event.event is EventKind.CANDIDATE_ADVANCED
    ]
    assert len(advanced) == 1
    assert advanced[0].data == {
        "from": "https://example.com/dead",
        "to": "https://example.org/live",
    }
    assert kinds(events).count(EventKind.SOURCE_UNAVAILABLE) == 1


async def test_all_candidates_failed_still_returns_gathered_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        sr("One", "https://example.com/one", "orbital mechanics"),
                        sr("Two", "https://example.com/two", "orbital mechanics"),
                    ]
                },
            )
        return httpx.Response(500)

    events: list[Any] = []
    researcher, delays = make_researcher(handler, events)
    outcome = await researcher.handle(research_step())

    assert outcome.error is None
    assert outcome.evidence == []
    assert len(outcome.sources) == 2
    assert all(source.status.value == "unavailable" for source in outcome.sources)
    assert kinds(events).count(EventKind.SOURCE_UNAVAILABLE) == 2
    assert kinds(events).count(EventKind.CANDIDATE_ADVANCED) == 1
    # each 5xx candidate ran the full 3-attempt budget with two backoffs
    fetch_results = outcome.tool_results[1:]
    assert [result.attempts for result in fetch_results] == [3, 3]
    assert len(delays) == 4


async def test_search_failure_fails_the_step_with_retry_budget_consumed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    events: list[Any] = []
    researcher, delays = make_researcher(handler, events)
    outcome = await researcher.handle(research_step())

    assert outcome.error is not None
    assert outcome.failure_kind is FailureKind.TRANSIENT
    assert outcome.evidence == []
    assert outcome.sources == []
    assert len(outcome.tool_results) == 1
    assert outcome.tool_results[0].attempts == 3
    assert kinds(events).count(EventKind.RETRY_STARTED) == 2


async def test_direct_fetch_step_collects_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return html(page("Doc", "Official documentation sentence covers the release process."))

    events: list[Any] = []
    researcher, _ = make_researcher(handler, events)
    step = PlanStep(
        id="f1",
        objective="read the release documentation",
        tool="webpage_fetch",
        arguments={"url": "https://example.org/doc"},
        expected_output="release facts",
    )
    outcome = await researcher.handle(step)

    assert outcome.error is None
    assert len(outcome.evidence) == 1
    evidence = outcome.evidence[0]
    assert evidence.evidence_id == "f1:1"
    assert evidence.claim == "Official documentation sentence covers the release process."
    assert evidence.source_url == "https://example.org/doc"
    assert len(outcome.sources) == 1
    assert outcome.sources[0].status.value == "available"


async def test_direct_calculator_step_produces_derived_evidence() -> None:
    events: list[Any] = []
    researcher, _ = make_researcher(lambda request: httpx.Response(404), events)
    step = PlanStep(
        id="q1",
        objective="compute the total",
        tool="calculator",
        arguments={"expression": "2 + 2"},
        expected_output="the sum",
    )
    outcome = await researcher.handle(step)

    assert outcome.error is None
    assert outcome.sources == []
    assert len(outcome.evidence) == 1
    evidence = outcome.evidence[0]
    assert evidence.claim == "2 + 2 = 4"
    assert evidence.source_url is None
    assert evidence.confidence == 0.9
    assert evidence.relevance_score == 1.0
    added = [event for event in events if event.event is EventKind.EVIDENCE_ADDED]
    assert added[0].data == {"evidence_id": "q1:1", "derived": True}


async def test_direct_calculator_failure_fails_the_step() -> None:
    events: list[Any] = []
    researcher, _ = make_researcher(lambda request: httpx.Response(404), events)
    step = PlanStep(
        id="q1",
        objective="divide by zero",
        tool="calculator",
        arguments={"expression": "1 / 0"},
        expected_output="error surfaced",
    )
    outcome = await researcher.handle(step)

    assert outcome.error is not None
    assert outcome.failure_kind is FailureKind.PERMANENT
    assert outcome.evidence == []


async def test_direct_simulator_step_runs_without_evidence() -> None:
    events: list[Any] = []
    researcher, _ = make_researcher(lambda request: httpx.Response(404), events)
    step = PlanStep(
        id="s1",
        objective="exercise the failure simulator",
        tool="failure_simulator",
        arguments={"failure_mode": "temporary_error", "fail_times": 0},
        expected_output="ok after no failure",
    )
    outcome = await researcher.handle(step)

    assert outcome.error is None
    assert outcome.evidence == []
    assert outcome.sources == []
    assert len(outcome.tool_results) == 1
    assert outcome.tool_results[0].success is True


async def test_unknown_tool_returns_validation_failure_outcome() -> None:
    events: list[Any] = []
    researcher, _ = make_researcher(lambda request: httpx.Response(404), events)
    step = PlanStep(
        id="x1",
        objective="mystery",
        tool="mystery_tool",
        arguments={},
        expected_output="nothing",
    )
    outcome = await researcher.handle(step)

    assert outcome.failure_kind is FailureKind.VALIDATION_ERROR
    assert "no handler registered" in (outcome.error or "")


async def test_empty_page_is_rejected_not_reported_as_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={"results": [sr("Empty", "https://example.com/empty", "orbital mechanics")]},
            )
        return html("<html><head><title>Empty</title></head><body></body></html>")

    events: list[Any] = []
    researcher, _ = make_researcher(handler, events)
    outcome = await researcher.handle(research_step())

    assert outcome.error is None
    assert outcome.evidence == []
    rejected = [event for event in events if event.event is EventKind.EVIDENCE_REJECTED]
    assert len(rejected) == 1
    assert rejected[0].data == {"url": "https://example.com/empty"}


async def test_nav_heavy_page_selects_query_relevant_sentences_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={"results": [sr("Nav", "https://example.com/nav", "orbital mechanics")]},
            )
        return html(
            page(
                "Nav",
                "Home. Products. Solutions. Orbital mechanics explains how satellites "
                "stay in orbit. Launch costs keep falling as reusable rockets fly "
                "more often. Alpha transfer trajectories follow orbital mechanics "
                "principles closely.",
            )
        )

    events: list[Any] = []
    researcher, _ = make_researcher(handler, events)
    outcome = await researcher.handle(research_step())

    assert outcome.error is None
    assert len(outcome.evidence) == 1
    claim = outcome.evidence[0].claim
    assert claim == (
        "Orbital mechanics explains how satellites stay in orbit. "
        "Alpha transfer trajectories follow orbital mechanics principles closely."
    )
    assert "Launch costs" not in claim
    assert "Home" not in claim


async def test_no_relevant_passage_is_rejected_without_zero_score_padding() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={"results": [sr("Weather", "https://example.com/wx", "orbital mechanics")]},
            )
        return html(
            page(
                "Weather",
                "Weather patterns shift gently across the region every season. "
                "Tourism boards publish quarterly visitor statistics for coastal towns.",
            )
        )

    events: list[Any] = []
    researcher, _ = make_researcher(handler, events)
    outcome = await researcher.handle(research_step())

    assert outcome.error is None
    assert outcome.evidence == []
    rejected = [event for event in events if event.event is EventKind.EVIDENCE_REJECTED]
    assert len(rejected) == 1
    assert rejected[0].message == "no relevant passage"
    assert rejected[0].data == {"url": "https://example.com/wx"}
    assert kinds(events).count(EventKind.EVIDENCE_ADDED) == 0


def test_select_passages_ties_break_by_position_deterministically() -> None:
    terms = term_set("orbital mechanics")
    text = (
        "Orbital mechanics rules the first sentence here. "
        "Something else entirely happens during daytime. "
        "Orbital mechanics rules the second sentence here. "
        "Orbital mechanics rules the third sentence here. "
        "Orbital mechanics rules the fourth sentence here."
    )

    first = _select_passages(text, terms)
    assert first == (
        "Orbital mechanics rules the first sentence here. "
        "Orbital mechanics rules the second sentence here. "
        "Orbital mechanics rules the third sentence here."
    )
    assert "Something else" not in (first or "")
    assert _select_passages(text, terms) == first


def test_select_passages_caps_excerpt_length() -> None:
    terms = term_set("orbital mechanics")

    oversized = "Orbital mechanics " + " ".join(["alpha"] * 120) + " end."
    assert _select_passages(oversized, terms) is None

    good = "Orbital mechanics keeps satellites in stable orbits."
    assert _select_passages(f"{oversized} {good}", terms) == good

    first = "Orbital mechanics " + ", ".join(["unit"] * 40) + "."
    second = "Orbital mechanics " + ", ".join(["block"] * 45) + "."
    third = "Orbital mechanics " + ", ".join(["note"] * 30) + "."
    joined = _select_passages(f"{first} {second} {third}", terms)
    assert joined is not None
    assert len(joined) <= 500
    assert joined == f"{first} {third}"
    assert "block" not in joined


def test_run_on_menu_text_is_rejected() -> None:
    terms = term_set("github repository contribution")
    menu = (
        "GitHub Education Learn how to move into your first professional role "
        "and grow with documentation guides community events developer programs "
        "sponsorship marketplace actions explore navigation footer links today "
        "resources updates hub pages"
    )
    good = "The repository contribution data confirms steady GitHub activity."
    assert _select_passages(f"{menu}. {good}", terms) == good


def test_version_notation_stays_in_one_sentence() -> None:
    terms = term_set("protocol rfc")
    text = (
        "The protocol's first full-featured iteration (v. 1.0) was documented "
        "in RFC 1945 in 1996. As the standard evolved HTTP/2 added multiplexing support."
    )
    selected = _select_passages(text, terms)
    assert selected is not None
    assert "(v. 1.0) was documented in RFC 1945 in 1996." in selected


def test_unbalanced_parenthesis_candidate_is_rejected() -> None:
    terms = term_set("orbital mechanics")
    text = (
        "Orbital mechanics starts this fragment (draft. "
        "Next orbital mechanics sentence finishes properly."
    )
    selected = _select_passages(text, terms)
    assert selected == "Next orbital mechanics sentence finishes properly."


def test_web_evidence_returns_typed_rejections() -> None:
    common = {
        "evidence_id": "e1",
        "step_id": "s1",
        "url": "https://example.com/p",
        "title": None,
        "query_terms": term_set("orbital mechanics"),
        "retrieved_at": FIXED_TIME,
    }
    empty = _web_evidence(text="   ", **common)
    assert isinstance(empty, Rejection)
    assert empty.reason == "no extractable text in fetched page"

    irrelevant = _web_evidence(
        text="Weather patterns shift gently across the region every season.",
        **common,
    )
    assert isinstance(irrelevant, Rejection)
    assert irrelevant.reason == "no relevant passage"


async def test_numeric_fact_extracted_from_selected_passage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={"results": [sr("Budget", "https://example.com/b", "orbital mechanics")]},
            )
        return html(
            page("Budget", "Orbital mechanics funding reached $8.9B in the fiscal year.")
        )

    events: list[Any] = []
    researcher, _ = make_researcher(handler, events)
    outcome = await researcher.handle(research_step())

    assert outcome.error is None
    assert len(outcome.evidence) == 1
    fact = outcome.evidence[0].numeric
    assert fact is not None
    assert fact.unit == "USD_B"
    assert fact.value == 8.9e9


async def test_numeric_not_extracted_from_unselected_sentence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={"results": [sr("Notes", "https://example.com/n", "orbital mechanics")]},
            )
        return html(
            page(
                "Notes",
                "Orbital mechanics funding notes appear in this sentence. "
                "The division produced 12% more output last year.",
            )
        )

    events: list[Any] = []
    researcher, _ = make_researcher(handler, events)
    outcome = await researcher.handle(research_step())

    assert outcome.error is None
    assert len(outcome.evidence) == 1
    evidence = outcome.evidence[0]
    assert evidence.claim == "Orbital mechanics funding notes appear in this sentence."
    assert evidence.numeric is None
