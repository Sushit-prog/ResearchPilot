from __future__ import annotations

from datetime import datetime, timezone

from app.agent.verifier import CLAIM_OVERLAP_THRESHOLD, deduplicate_evidence
from app.models.events import EventKind, ExecutionEvent
from app.models.evidence import Evidence
from app.observability import EventEmitter, InMemoryEventSink

FIXED_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def make_evidence(
    evidence_id: str,
    claim: str,
    *,
    url: str | None = "https://example.com/a",
    title: str | None = "Example",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        claim=claim,
        source_url=url,
        source_title=title,
        extracted_text=claim,
        relevance_score=0.9,
        confidence=0.8,
        retrieved_at=FIXED_TIME,
        step_id="r1",
    )


def build_emitter(events: list[ExecutionEvent]) -> EventEmitter:
    return EventEmitter(lambda: FIXED_TIME, sinks=[InMemoryEventSink(events)])


def events_of_kind(events: list[ExecutionEvent], kind: EventKind) -> list[ExecutionEvent]:
    return [event for event in events if event.event is kind]


def test_duplicate_urls_after_normalization_are_dropped() -> None:
    events: list[ExecutionEvent] = []
    kept = deduplicate_evidence(
        [
            make_evidence("ev-a", "First distinct claim about launch windows.", url="https://example.com/page"),
            make_evidence(
                "ev-b",
                "A totally different second claim!",
                url="https://EXAMPLE.com/page/?utm_source=news",
            ),
        ],
        emitter=build_emitter(events),
    )
    assert [item.evidence_id for item in kept] == ["ev-a"]
    drops = events_of_kind(events, EventKind.EVIDENCE_DEDUPLICATED)
    assert len(drops) == 1
    assert drops[0].data == {"evidence_id": "ev-b", "kept": "ev-a", "reason": "duplicate_url"}


def test_identical_claims_across_different_urls_are_dropped() -> None:
    events: list[ExecutionEvent] = []
    kept = deduplicate_evidence(
        [
            make_evidence("ev-a", "Revenue grew 12% last quarter.", url="https://example.com/a"),
            make_evidence(
                "ev-b",
                "  revenue   grew 12% LAST QUARTER! ",
                url="https://example.org/b",
            ),
        ],
        emitter=build_emitter(events),
    )
    assert [item.evidence_id for item in kept] == ["ev-a"]
    drops = events_of_kind(events, EventKind.EVIDENCE_DEDUPLICATED)
    assert len(drops) == 1
    assert drops[0].data["reason"] == "duplicate_claim"


def test_near_identical_distinct_claims_are_preserved() -> None:
    events: list[ExecutionEvent] = []
    kept = deduplicate_evidence(
        [
            make_evidence("ev-a", "Revenue grew 12% last quarter.", url="https://example.com/a"),
            make_evidence("ev-b", "Revenue grew 15% last quarter.", url="https://example.org/b"),
        ],
        emitter=build_emitter(events),
    )
    assert [item.evidence_id for item in kept] == ["ev-a", "ev-b"]
    assert events_of_kind(events, EventKind.EVIDENCE_DEDUPLICATED) == []


def test_source_titles_are_collapsed_on_kept_evidence() -> None:
    kept = deduplicate_evidence(
        [make_evidence("ev-a", "A single claim.", title="  Annual   Report ")],
        emitter=build_emitter([]),
    )
    assert kept[0].source_title == "Annual Report"


def test_derived_evidence_without_urls_is_claim_deduplicated() -> None:
    events: list[ExecutionEvent] = []
    kept = deduplicate_evidence(
        [
            make_evidence("ev-a", "2 + 2 = 4", url=None),
            make_evidence("ev-b", "2 + 2 = 4", url=None),
        ],
        emitter=build_emitter(events),
    )
    assert [item.evidence_id for item in kept] == ["ev-a"]
    assert events_of_kind(events, EventKind.EVIDENCE_DEDUPLICATED)[0].data["reason"] == (
        "duplicate_claim"
    )


def test_multiple_duplicates_each_emit_one_event() -> None:
    events: list[ExecutionEvent] = []
    kept = deduplicate_evidence(
        [
            make_evidence("ev-a", "First unique claim alpha.", url="https://example.com/a"),
            make_evidence("ev-b", "First unique claim alpha.", url="https://example.org/b"),
            make_evidence("ev-c", "First unique claim alpha.", url="https://example.net/c"),
        ],
        emitter=build_emitter(events),
    )
    assert [item.evidence_id for item in kept] == ["ev-a"]
    drops = events_of_kind(events, EventKind.EVIDENCE_DEDUPLICATED)
    assert [drop.data["evidence_id"] for drop in drops] == ["ev-b", "ev-c"]
    assert all(drop.data["kept"] == "ev-a" for drop in drops)


def test_claim_overlap_threshold_is_doc_value() -> None:
    assert CLAIM_OVERLAP_THRESHOLD == 0.9
