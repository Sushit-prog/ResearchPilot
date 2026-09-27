from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel

from app.agent.verifier import MIN_RELEVANCE_SCORE, filter_evidence, numeric_pass
from app.config import Config
from app.errors import PermanentToolError
from app.models.events import EventKind, ExecutionEvent, FailureKind, ToolResult
from app.models.evidence import Evidence, NumericFact
from app.models.tool_io import CalculatorInput, CalculatorOutput
from app.observability import EventEmitter, InMemoryEventSink
from app.reliability.runner import Runner
from app.reliability.validation import normalize_url
from app.tools.base import Tool
from app.tools.calculator import CalculatorTool

FIXED_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def zero(low: float, high: float) -> float:
    return 0.0


def make_evidence(
    evidence_id: str,
    claim: str,
    *,
    url: str | None = "https://example.com/a",
    relevance: float = 0.9,
    confidence: float = 0.8,
    numeric: NumericFact | None = None,
    extracted: str | None = None,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        claim=claim,
        source_url=url,
        source_title="Example",
        extracted_text=claim if extracted is None else extracted,
        relevance_score=relevance,
        confidence=confidence,
        retrieved_at=FIXED_TIME,
        step_id="r1",
        numeric=numeric,
    )


def build_emitter(events: list[ExecutionEvent]) -> EventEmitter:
    return EventEmitter(lambda: FIXED_TIME, sinks=[InMemoryEventSink(events)])


def build_runner(emitter: EventEmitter, delays: list[float]) -> Runner:
    return Runner(
        Config(),
        emitter,
        clock=lambda: FIXED_TIME,
        sleep=delays.append,
        rng=zero,
    )


def events_of_kind(events: list[ExecutionEvent], kind: EventKind) -> list[ExecutionEvent]:
    return [event for event in events if event.event is kind]


# --- filter_evidence -------------------------------------------------------


def test_valid_evidence_passes_untouched() -> None:
    events: list[ExecutionEvent] = []
    evidence = make_evidence("ev-a", "A solid claim.")
    keys = {normalize_url("https://example.com/a")}
    kept, warnings = filter_evidence([evidence], emitter=build_emitter(events), source_keys=keys)
    assert kept == [evidence]
    assert warnings == []
    assert events == []


def test_empty_claim_is_rejected() -> None:
    events: list[ExecutionEvent] = []
    evidence = make_evidence("ev-a", "   ", url=None, extracted="Some excerpt.")
    kept, warnings = filter_evidence([evidence], emitter=build_emitter(events), source_keys=set())
    assert kept == []
    assert warnings == ["evidence ev-a rejected: empty claim"]
    rejected = events_of_kind(events, EventKind.EVIDENCE_REJECTED)
    assert len(rejected) == 1
    assert rejected[0].data == {"evidence_id": "ev-a", "reason": "empty claim"}


def test_source_not_among_collected_sources_is_rejected() -> None:
    events: list[ExecutionEvent] = []
    evidence = make_evidence("ev-a", "A claim from elsewhere.", url="https://other.example/p")
    keys = {normalize_url("https://example.com/a")}
    kept, warnings = filter_evidence([evidence], emitter=build_emitter(events), source_keys=keys)
    assert kept == []
    assert warnings == [
        "evidence ev-a rejected: source not among collected sources"
    ]
    assert events_of_kind(events, EventKind.EVIDENCE_REJECTED)[0].data["evidence_id"] == "ev-a"


def test_irrelevant_evidence_below_floor_is_rejected() -> None:
    events: list[ExecutionEvent] = []
    evidence = make_evidence("ev-a", "Barely related text.", relevance=0.05)
    keys = {normalize_url("https://example.com/a")}
    kept, warnings = filter_evidence([evidence], emitter=build_emitter(events), source_keys=keys)
    assert kept == []
    assert warnings == [
        f"evidence ev-a rejected: relevance 0.05 below threshold {MIN_RELEVANCE_SCORE}"
    ]
    assert events_of_kind(events, EventKind.EVIDENCE_REJECTED)[0].data["evidence_id"] == "ev-a"


def test_derived_evidence_exempt_from_source_check() -> None:
    events: list[ExecutionEvent] = []
    evidence = make_evidence("ev-a", "2 + 2 = 4", url=None)
    kept, warnings = filter_evidence([evidence], emitter=build_emitter(events), source_keys=set())
    assert kept == [evidence]
    assert warnings == []


def test_structural_violation_takes_precedence_over_relevance() -> None:
    events: list[ExecutionEvent] = []
    evidence = make_evidence("ev-a", "   ", url=None, relevance=0.05, extracted="text")
    kept, warnings = filter_evidence([evidence], emitter=build_emitter(events), source_keys=set())
    assert kept == []
    assert warnings == ["evidence ev-a rejected: empty claim"]


def test_min_relevance_threshold_is_doc_value() -> None:
    assert MIN_RELEVANCE_SCORE == 0.1


# --- numeric_pass: recompute -----------------------------------------------


async def test_consistent_claim_is_marked_recomputed_and_consistent() -> None:
    events: list[ExecutionEvent] = []
    delays: list[float] = []
    emitter = build_emitter(events)
    evidence = make_evidence(
        "ev-a", "Revenue grew 10% from $100 to $110 in the period.", confidence=0.9
    )
    kept, warnings, tool_results = await numeric_pass(
        [evidence],
        runner=build_runner(emitter, delays),
        calculator=CalculatorTool(),
        emitter=emitter,
    )
    assert kept == [evidence]
    assert warnings == []
    assert delays == []
    assert evidence.verification.recomputed is True
    assert evidence.verification.consistent is True
    assert evidence.confidence == 0.9
    assert len(tool_results) == 1
    result = tool_results[0]
    assert result.success is True
    assert result.tool == "calculator"
    assert result.step_id == "ev-a"
    assert result.output is not None
    assert result.output["value"] == pytest.approx(110.0, abs=1e-6)
    assert events_of_kind(events, EventKind.EVIDENCE_CONFLICT) == []


async def test_downward_consistent_claim_is_consistent() -> None:
    events: list[ExecutionEvent] = []
    emitter = build_emitter(events)
    evidence = make_evidence("ev-a", "Costs fell 10% from $100 to $90 after redesign.")
    kept, warnings, tool_results = await numeric_pass(
        [evidence],
        runner=build_runner(emitter, []),
        calculator=CalculatorTool(),
        emitter=emitter,
    )
    assert kept == [evidence]
    assert warnings == []
    assert evidence.verification.recomputed is True
    assert evidence.verification.consistent is True
    assert tool_results[0].output is not None
    assert tool_results[0].output["value"] == pytest.approx(90.0, abs=1e-6)


async def test_inconsistent_claim_lowers_confidence_and_emits_conflict() -> None:
    events: list[ExecutionEvent] = []
    emitter = build_emitter(events)
    evidence = make_evidence(
        "ev-a", "Revenue grew 10% from $100 to $130 in the period.", confidence=0.8
    )
    kept, warnings, tool_results = await numeric_pass(
        [evidence],
        runner=build_runner(emitter, []),
        calculator=CalculatorTool(),
        emitter=emitter,
    )
    assert kept == [evidence]
    assert evidence.verification.recomputed is True
    assert evidence.verification.consistent is False
    assert evidence.confidence == pytest.approx(0.6)
    assert evidence.verification.note is not None
    assert "calculator recomputed" in evidence.verification.note
    assert len(warnings) == 1
    assert "arithmetic inconsistent" in warnings[0]
    conflicts = events_of_kind(events, EventKind.EVIDENCE_CONFLICT)
    assert len(conflicts) == 1
    assert conflicts[0].data["evidence_id"] == "ev-a"
    assert conflicts[0].data["claim_value"] == pytest.approx(130.0)
    assert conflicts[0].data["recomputed"] == pytest.approx(110.0, abs=1e-6)
    assert tool_results[0].success is True


async def test_claim_without_two_amounts_skips_recompute() -> None:
    events: list[ExecutionEvent] = []
    emitter = build_emitter(events)
    evidence = make_evidence("ev-a", "Revenue grew 12% this quarter.")
    kept, warnings, tool_results = await numeric_pass(
        [evidence],
        runner=build_runner(emitter, []),
        calculator=CalculatorTool(),
        emitter=emitter,
    )
    assert kept == [evidence]
    assert warnings == []
    assert tool_results == []
    assert evidence.verification.recomputed is False
    assert evidence.verification.consistent is None


async def test_claim_without_percent_skips_recompute() -> None:
    emitter = build_emitter([])
    evidence = make_evidence("ev-a", "Revenue reached $500 million this year.")
    kept, warnings, tool_results = await numeric_pass(
        [evidence],
        runner=build_runner(emitter, []),
        calculator=CalculatorTool(),
        emitter=emitter,
    )
    assert kept == [evidence]
    assert warnings == []
    assert tool_results == []
    assert evidence.verification.recomputed is False


class _BrokenCalculatorInput(CalculatorInput):
    pass


class _BrokenCalculator(Tool[_BrokenCalculatorInput, CalculatorOutput]):
    name = "calculator"
    description = "always fails"
    input_model = _BrokenCalculatorInput
    output_model = CalculatorOutput
    default_timeout_s = 1.0

    async def execute(self, params: _BrokenCalculatorInput) -> CalculatorOutput:
        raise PermanentToolError("calculator offline", tool=self.name)


async def test_unavailable_calculator_warns_but_keeps_evidence() -> None:
    events: list[ExecutionEvent] = []
    emitter = build_emitter(events)
    evidence = make_evidence(
        "ev-a", "Revenue grew 10% from $100 to $130 in the period.", confidence=0.8
    )
    kept, warnings, tool_results = await numeric_pass(
        [evidence],
        runner=build_runner(emitter, []),
        calculator=_BrokenCalculator(),
        emitter=emitter,
    )
    assert kept == [evidence]
    assert len(warnings) == 1
    assert "numeric recomputation unavailable for ev-a" in warnings[0]
    assert evidence.verification.recomputed is False
    assert evidence.confidence == 0.8
    assert len(tool_results) == 1
    failed = tool_results[0]
    assert failed.success is False
    assert failed.failure_kind is FailureKind.PERMANENT
    assert failed.attempts == 1
    conflicts = events_of_kind(events, EventKind.EVIDENCE_CONFLICT)
    assert len(conflicts) == 1
    assert conflicts[0].status == "recompute_unavailable"


# --- numeric_pass: cross-source conflicts ----------------------------------


class _WrongShapeInput(CalculatorInput):
    pass


class _AnswerOnlyOutput(BaseModel):
    answer: float


class _WrongShapeCalculator(Tool[_WrongShapeInput, _AnswerOnlyOutput]):
    name = "calculator"
    description = "returns a differently-shaped payload"
    input_model = _WrongShapeInput
    output_model = _AnswerOnlyOutput
    default_timeout_s = 1.0

    async def execute(self, params: _WrongShapeInput) -> _AnswerOnlyOutput:
        return _AnswerOnlyOutput(answer=42.0)


async def test_malformed_calculator_payload_warns_but_keeps_evidence() -> None:
    events: list[ExecutionEvent] = []
    emitter = build_emitter(events)
    evidence = make_evidence(
        "ev-a", "Revenue grew 10% from $100 to $130 in the period.", confidence=0.8
    )
    kept, warnings, tool_results = await numeric_pass(
        [evidence],
        runner=build_runner(emitter, []),
        calculator=_WrongShapeCalculator(),
        emitter=emitter,
    )
    assert kept == [evidence]
    assert len(warnings) == 1
    assert warnings[0] == (
        "numeric recomputation unavailable for ev-a: malformed output"
    )
    assert evidence.verification.recomputed is False
    assert evidence.confidence == 0.8
    assert len(tool_results) == 1
    assert tool_results[0].success is True
    conflicts = events_of_kind(events, EventKind.EVIDENCE_CONFLICT)
    assert len(conflicts) == 1
    assert conflicts[0].status == "recompute_unavailable"


def _metric_claim(evidence_id: str, value: float, label: str = "yoy growth") -> Evidence:
    return make_evidence(
        evidence_id,
        "Company growth metric reported for the year.",
        url=f"https://example.com/{evidence_id}",
        numeric=NumericFact(value=value, unit="percent", metric_label=label),
    )


async def test_cross_source_disagreement_flags_both_sides_and_keeps_evidence() -> None:
    events: list[ExecutionEvent] = []
    emitter = build_emitter(events)
    first = _metric_claim("ev-a", 12.0)
    second = _metric_claim("ev-b", 15.0)
    kept, warnings, tool_results = await numeric_pass(
        [first, second],
        runner=build_runner(emitter, []),
        calculator=CalculatorTool(),
        emitter=emitter,
    )
    assert [item.evidence_id for item in kept] == ["ev-a", "ev-b"]
    assert tool_results == []
    assert first.verification.conflict_with == ["ev-b"]
    assert second.verification.conflict_with == ["ev-a"]
    assert first.verification.note is not None
    assert "yoy growth" in first.verification.note
    assert len(warnings) == 1
    assert "conflicting values" in warnings[0]
    assert "12" in warnings[0] and "15" in warnings[0]
    conflicts = events_of_kind(events, EventKind.EVIDENCE_CONFLICT)
    assert len(conflicts) == 2
    assert {conflict.data["metric"] for conflict in conflicts} == {"yoy growth"}


async def test_cross_source_same_value_is_not_a_conflict() -> None:
    events: list[ExecutionEvent] = []
    emitter = build_emitter(events)
    first = _metric_claim("ev-a", 12.0)
    second = _metric_claim("ev-b", 12.0)
    kept, warnings, _ = await numeric_pass(
        [first, second],
        runner=build_runner(emitter, []),
        calculator=CalculatorTool(),
        emitter=emitter,
    )
    assert len(kept) == 2
    assert warnings == []
    assert first.verification.conflict_with == []
    assert second.verification.conflict_with == []
    assert events_of_kind(events, EventKind.EVIDENCE_CONFLICT) == []


async def test_different_metric_labels_do_not_conflict() -> None:
    events: list[ExecutionEvent] = []
    emitter = build_emitter(events)
    first = _metric_claim("ev-a", 12.0, label="yoy growth")
    second = _metric_claim("ev-b", 15.0, label="quarterly margin")
    kept, warnings, _ = await numeric_pass(
        [first, second],
        runner=build_runner(emitter, []),
        calculator=CalculatorTool(),
        emitter=emitter,
    )
    assert len(kept) == 2
    assert warnings == []
    assert events_of_kind(events, EventKind.EVIDENCE_CONFLICT) == []


def test_serialized_tool_result_round_trips() -> None:
    result = ToolResult(
        result_id="ev-a:calculator:1",
        tool="calculator",
        step_id="ev-a",
        call_ordinal=1,
        success=True,
        output={"expression": "1+1", "value": 2.0, "unit": None},
        attempts=1,
        started_at=FIXED_TIME,
    )
    parsed = json.loads(result.model_dump_json())
    assert parsed["output"]["value"] == 2.0
    assert parsed["failure_kind"] is None
