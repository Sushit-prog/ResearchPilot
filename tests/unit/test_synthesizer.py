from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.agent.state import AgentState, StepRecord
from app.agent.synthesizer import (
    build_execution_summary,
    build_synthesis_prompt,
    contradictions_from,
    parse_report,
    render_markdown,
    slugify_goal,
    synthesize,
    write_report,
)
from app.config import Config
from app.errors import SynthesisError
from app.models.events import EventKind, FailureKind, ToolResult
from app.models.evidence import Evidence, EvidenceVerification, Source
from app.models.plan import Plan, StepStatus
from app.observability import EventEmitter, InMemoryEventSink
from app.reliability.validation import normalize_url

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

PLAN_DATA = {
    "goal": "What is X?",
    "steps": [
        {
            "id": "r1",
            "objective": "search for X",
            "tool": "web_search",
            "arguments": {"query": "X"},
            "expected_output": "results",
        }
    ],
}


def make_evidence(
    evidence_id: str,
    claim: str,
    *,
    url: str | None,
    extracted: str | None = None,
    confidence: float = 0.8,
    verification: EvidenceVerification | None = None,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        claim=claim,
        source_url=url,
        source_title="Example",
        extracted_text=extracted or claim,
        relevance_score=0.9,
        confidence=confidence,
        retrieved_at=FIXED_TIME,
        step_id="r1",
        verification=verification or EvidenceVerification(),
    )


def make_state(evidence: list[Evidence], *, goal: str = "What is X?") -> AgentState:
    state = AgentState(user_goal=goal)
    state.normalized_goal = " ".join(goal.split())
    state.plan = Plan.model_validate(PLAN_DATA)
    state.evidence = evidence
    state.completed_steps = [
        StepRecord(
            step_id="r1",
            objective="search for X",
            tool="web_search",
            status=StepStatus.SUCCEEDED,
            result_ids=["r1-web_search-1"],
        )
    ]
    return state


async def synthesize_with(
    llm, state: AgentState, *, config: Config | None = None
) -> tuple:
    events: list = []
    emitter = EventEmitter(lambda: FIXED_TIME, sinks=[InMemoryEventSink(events)])
    report = await synthesize(
        llm,
        state,
        config=config or Config(),
        emitter=emitter,
        clock=lambda: FIXED_TIME,
        started_at=FIXED_TIME,
    )
    return report, events, state


def test_prompt_serializes_provenance_but_never_extracted_text() -> None:
    evidence = [
        make_evidence(
            "ev-a",
            "Revenue grew 12%",
            url="https://example.com/a",
            extracted="RAW_WEB_TEXT_SENTINEL must never reach the LLM",
        )
    ]
    system, user = build_synthesis_prompt("What is X?", evidence)
    combined = system + user
    assert "RAW_WEB_TEXT_SENTINEL" not in combined
    assert "ev-a" in combined
    assert "relevance_score" in combined
    assert "source_url" in combined
    assert "verification" in combined


def test_parse_report_tolerates_fenced_json(synthesis_json) -> None:
    payload = synthesis_json([("ev-a", "https://example.com/a")])
    report = parse_report(f"```json\n{payload}\n```")
    assert report.key_findings[0].evidence_id == "ev-a"


def test_parse_report_rejects_non_json() -> None:
    with pytest.raises(SynthesisError, match="not valid JSON"):
        parse_report("certainly not json")


async def test_structural_exhaustion_raises_synthesis_error(fake_llm) -> None:
    state = make_state([make_evidence("ev-a", "claim", url="https://example.com/a")])
    llm = fake_llm("definitely not json", '{"missing_sections": true}')
    with pytest.raises(SynthesisError) as exc_info:
        await synthesize_with(llm, state)
    assert type(exc_info.value) is SynthesisError
    assert "synthesis still invalid after 2 attempts" in str(exc_info.value)
    assert state.warnings == []


async def test_structural_then_valid_second_attempt_completes(
    fake_llm, synthesis_json
) -> None:
    payload = synthesis_json([("ev-a", "https://example.com/a")])
    state = make_state([make_evidence("ev-a", "claim", url="https://example.com/a")])
    llm = fake_llm("oops not json", payload)
    report, _events, _state = await synthesize_with(llm, state)
    assert len(llm.calls) == 2
    assert "Your previous output was rejected" in llm.calls[1]["user"]
    assert report.key_findings[0].evidence_id == "ev-a"


async def test_citation_violation_corrected_on_second_attempt(
    fake_llm, synthesis_json
) -> None:
    evidence = [
        make_evidence("ev-a", "claim A", url="https://example.com/a"),
        make_evidence("ev-b", "claim B", url="https://example.org/b"),
    ]
    state = make_state(evidence)
    bad = synthesis_json(
        [("ev-a", "https://example.com/a"), ("ev-ghost", "https://ghost.example")]
    )
    good = synthesis_json(
        [
            ("ev-a", "https://example.com/a"),
            ("ev-b", "https://example.org/b"),
        ]
    )
    llm = fake_llm(bad, good)
    report, _events, _state = await synthesize_with(llm, state)
    assert len(llm.calls) == 2
    assert "citation gate" in llm.calls[1]["user"]
    assert "ev-ghost" in llm.calls[1]["user"]
    assert [f.evidence_id for f in report.key_findings] == ["ev-a", "ev-b"]
    assert not any("dropped" in warning for warning in state.warnings)


async def test_citation_violation_dropped_on_final_attempt(
    fake_llm, synthesis_json
) -> None:
    evidence = [
        make_evidence("ev-a", "claim A", url="https://example.com/a"),
        make_evidence("ev-b", "claim B", url="https://example.org/b"),
    ]
    state = make_state(evidence)
    payload = synthesis_json(
        [("ev-a", "https://example.com/a"), ("ev-ghost", "https://ghost.example")]
    )
    llm = fake_llm(payload, payload)
    report, _events, _state = await synthesize_with(llm, state)
    assert [f.evidence_id for f in report.key_findings] == ["ev-a"]
    assert any(
        "synthesis dropped 1 finding(s) with invalid citations" in warning
        and "ev-ghost" in warning
        for warning in state.warnings
    )


async def test_url_mismatch_dropped_on_final_attempt(fake_llm, synthesis_json) -> None:
    state = make_state([make_evidence("ev-a", "claim", url="https://example.com/a")])
    payload = synthesis_json([("ev-a", "https://wrong.example/a")])
    llm = fake_llm(payload, payload)
    report, _events, _state = await synthesize_with(llm, state)
    assert report.key_findings == []
    assert any("does not match" in warning or "has" in warning for warning in state.warnings)


async def test_calculator_none_url_citation_allowed(fake_llm, synthesis_json) -> None:
    state = make_state(
        [
            make_evidence("ev-a", "2 + 2 = 4", url=None),
            make_evidence("ev-b", "claim", url="https://example.com/b"),
        ]
    )
    payload = synthesis_json([("ev-a", None), ("ev-b", "https://example.com/b")])
    llm = fake_llm(payload)
    report, _events, _state = await synthesize_with(llm, state)
    assert [f.evidence_id for f in report.key_findings] == ["ev-a", "ev-b"]
    assert report.key_findings[0].source_url is None
    assert state.warnings == []


async def test_findings_reindexed_without_gap_after_drop(
    fake_llm, synthesis_json
) -> None:
    evidence = [
        make_evidence("ev-a", "claim A", url="https://example.com/a"),
        make_evidence("ev-b", "claim B", url="https://example.org/b"),
        make_evidence("ev-c", "claim C", url="https://example.net/c"),
    ]
    state = make_state(evidence)
    payload = synthesis_json(
        [
            ("ev-a", "https://example.com/a"),
            ("ev-ghost", "https://ghost.example"),
            ("ev-b", "https://example.org/b"),
        ]
    )
    llm = fake_llm(payload, payload)
    report, _events, _state = await synthesize_with(llm, state)

    assert [f.evidence_id for f in report.key_findings] == ["ev-a", "ev-b"]
    assert [f.index for f in report.key_findings] == [1, 2]

    markdown = render_markdown(report)
    section = markdown.split("## Key Findings\n", 1)[1].split("\n## ", 1)[0]
    numbers = [
        line.split(".", 1)[0]
        for line in section.splitlines()
        if line and line[0].isdigit() and line[1:2] == "."
    ]
    assert numbers == ["1", "2"]
    assert "3. " not in section


async def test_contradictions_take_verification_conflicts_first(
    fake_llm, synthesis_json
) -> None:
    evidence = [
        make_evidence(
            "ev-a",
            "yoy growth 12%",
            url="https://example.com/a",
            verification=EvidenceVerification(
                recomputed=True,
                consistent=False,
                conflict_with=["ev-b"],
                note="cross-source disagreement on 'yoy_growth': values [12.0, 15.0]",
            ),
        ),
        make_evidence("ev-b", "yoy growth 15%", url="https://example.org/b"),
    ]
    state = make_state(evidence)
    payload = synthesis_json(
        [("ev-a", "https://example.com/a"), ("ev-b", "https://example.org/b")],
        contradictions=["An extra LLM-observed point."],
    )
    llm = fake_llm(payload)
    report, _events, _state = await synthesize_with(llm, state)
    assert report.contradictions[0].startswith("ev-a conflicts with ev-b:")
    assert "cross-source disagreement on 'yoy_growth'" in report.contradictions[0]
    assert "An extra LLM-observed point." in report.contradictions


def test_contradictions_from_dedupes_symmetric_conflict_pair() -> None:
    note = "cross-source disagreement on 'yoy_growth': values [12.0, 15.0]"
    ev_a = make_evidence(
        "ev-a",
        "yoy growth 12%",
        url="https://example.com/a",
        verification=EvidenceVerification(
            recomputed=True,
            consistent=False,
            conflict_with=["ev-b"],
            note=note,
        ),
    )
    ev_b = make_evidence(
        "ev-b",
        "yoy growth 15%",
        url="https://example.org/b",
        verification=EvidenceVerification(
            recomputed=True,
            consistent=False,
            conflict_with=["ev-a"],
            note=note,
        ),
    )
    entries = contradictions_from([ev_a, ev_b])
    assert entries == [f"ev-a conflicts with ev-b: {note}"]


async def test_report_contradictions_single_line_for_symmetric_conflict(
    fake_llm, synthesis_json
) -> None:
    note = "cross-source disagreement on 'yoy_growth': values [12.0, 15.0]"
    evidence = [
        make_evidence(
            "ev-a",
            "yoy growth 12%",
            url="https://example.com/a",
            verification=EvidenceVerification(
                recomputed=True,
                consistent=False,
                conflict_with=["ev-b"],
                note=note,
            ),
        ),
        make_evidence(
            "ev-b",
            "yoy growth 15%",
            url="https://example.org/b",
            verification=EvidenceVerification(
                recomputed=True,
                consistent=False,
                conflict_with=["ev-a"],
                note=note,
            ),
        ),
    ]
    state = make_state(evidence)
    payload = synthesis_json(
        [("ev-a", "https://example.com/a"), ("ev-b", "https://example.org/b")],
        contradictions=[],
    )
    llm = fake_llm(payload)
    report, _events, _state = await synthesize_with(llm, state)
    conflict_lines = [line for line in report.contradictions if "conflicts with" in line]
    assert conflict_lines == [f"ev-a conflicts with ev-b: {note}"]


async def test_source_url_display_parity_and_normalized_dedup(
    fake_llm, synthesis_json
) -> None:
    raw_utm = "https://example.com/a/?utm_source=x"
    raw_slash = "https://example.com/a/"
    state = make_state(
        [
            make_evidence("ev-a", "claim A", url=raw_utm),
            make_evidence("ev-b", "claim B", url=raw_slash),
        ]
    )
    payload = synthesis_json([("ev-a", raw_utm), ("ev-b", raw_slash)])
    llm = fake_llm(payload)
    report, _events, _state = await synthesize_with(llm, state)
    assert raw_utm != raw_slash
    assert normalize_url(raw_utm) == normalize_url(raw_slash)

    rendered = render_markdown(report)
    finding_section = rendered.split("## Key Findings\n", 1)[1].split("\n## ", 1)[0]
    finding_urls = [
        line.split(" — ", 1)[1].rsplit(" (confidence", 1)[0]
        for line in finding_section.splitlines()
        if " Source: " in line
    ]
    assert finding_urls == [raw_utm, raw_slash]

    sources_section = rendered.split("## Sources\n", 1)[1].split("\n## ", 1)[0]
    numbered = [
        line
        for line in sources_section.splitlines()
        if len(line) > 2 and line[1] == "." and line[0].isdigit()
    ]
    assert len(numbered) == 1
    sources_url = numbered[0].split(" — ", 1)[1].rsplit(" (", 1)[0]
    assert sources_url == raw_utm
    assert sources_url == finding_urls[0]


async def test_research_question_and_sources_rebuilt_deterministically(
    fake_llm, synthesis_json
) -> None:
    evidence = [
        make_evidence("ev-a", "claim A", url="https://example.com/a"),
        make_evidence("ev-b", "claim B", url="https://example.org/b"),
    ]
    state = make_state(evidence, goal="  What   is X? ")
    payload = synthesis_json(
        [("ev-a", "https://example.com/a"), ("ev-b", "https://example.org/b")],
        question="LLM invented question",
    )
    llm = fake_llm(payload)
    report, _events, _state = await synthesize_with(llm, state)
    assert report.research_question == "What is X?"
    assert [ref.url for ref in report.sources] == [
        "https://example.com/a",
        "https://example.org/b",
    ]
    assert [ref.index for ref in report.sources] == [1, 2]
    assert all(ref.domain for ref in report.sources)


async def test_important_evidence_excerpt_comes_from_evidence_not_llm(
    fake_llm, synthesis_json
) -> None:
    evidence = [
        make_evidence(
            "ev-a",
            "claim",
            url="https://example.com/a",
            extracted="Stored excerpt that the LLM never saw.",
        )
    ]
    state = make_state(evidence)
    payload = synthesis_json([("ev-a", "https://example.com/a")])
    llm = fake_llm(payload)
    report, _events, _state = await synthesize_with(llm, state)
    assert report.important_evidence[0].excerpt == "Stored excerpt that the LLM never saw."
    assert report.important_evidence[0].evidence_id == "ev-a"


async def test_synthesis_started_event_emitted(fake_llm, synthesis_json) -> None:
    state = make_state([make_evidence("ev-a", "claim", url="https://example.com/a")])
    payload = synthesis_json([("ev-a", "https://example.com/a")])
    llm = fake_llm(payload)
    _report, events, _state = await synthesize_with(llm, state)
    assert events[0].event is EventKind.SYNTHESIS_STARTED
    assert events[0].data == {"evidence": 1}
    assert events[0].message == "What is X?"


async def test_llm_call_failure_becomes_synthesis_error(fake_llm) -> None:
    state = make_state([make_evidence("ev-a", "claim", url="https://example.com/a")])
    llm = fake_llm()
    with pytest.raises(SynthesisError, match="synthesis LLM call failed"):
        await synthesize_with(llm, state)


def test_execution_summary_counts_use_arithmetic_rejected_semantics() -> None:
    state = make_state(
        [
            make_evidence("ev-a", "claim A", url="https://example.com/a"),
            make_evidence("ev-b", "claim B", url="https://example.org/b"),
        ]
    )
    state.sources = [
        Source(url="https://example.com/a", domain="example.com", retrieved_at=FIXED_TIME),
        Source(url="https://example.org/b", domain="example.org", retrieved_at=FIXED_TIME),
        Source(url="https://example.net/c", domain="example.net", retrieved_at=FIXED_TIME),
    ]
    summary = build_execution_summary(state, started_at=FIXED_TIME, finished_at=FIXED_TIME)
    assert summary.sources_searched == 3
    assert summary.sources_used == 2
    assert summary.sources_rejected == 1
    assert summary.sources_rejected == summary.sources_searched - summary.sources_used
    assert summary.duration_s == 0.0
    assert summary.steps_total == 1
    assert summary.steps_succeeded == 1
    assert summary.failures == []


def test_execution_summary_failures_unclassified_and_kind_fallback() -> None:
    state = make_state([make_evidence("ev-a", "claim", url="https://example.com/a")])
    state.failed_steps = [
        StepRecord(
            step_id="q1",
            objective="compute",
            tool="calculator",
            status=StepStatus.FAILED,
            result_ids=["q1-calculator-1"],
            error="IndexError: list index out of range",
            failure_kind=None,
        )
    ]
    state.completed_steps = []
    state.tool_results = [
        ToolResult(
            result_id="q1-calculator-1",
            tool="calculator",
            step_id="q1",
            call_ordinal=1,
            success=False,
            attempts=1,
            started_at=FIXED_TIME,
            unclassified=True,
        )
    ]
    state.retry_counts = {"q1:calculator:1": 2, "r1:web_search:1": 1}
    summary = build_execution_summary(state, started_at=FIXED_TIME, finished_at=FIXED_TIME)
    assert summary.steps_total == 1
    assert summary.steps_failed == 1
    assert summary.retries == 3
    assert len(summary.failures) == 1
    failure = summary.failures[0]
    assert failure.failure_kind is FailureKind.PERMANENT
    assert failure.unclassified is True
    assert "IndexError" in failure.message


def test_slugify_is_path_traversal_safe() -> None:
    assert slugify_goal("What is X?") == "what-is-x"
    assert slugify_goal("../../etc/passwd") == "etc-passwd"
    assert slugify_goal("   ") == "research-report"
    assert slugify_goal("") == "research-report"
    assert slugify_goal("Résumé of the Élysée") == "resume-of-the-elysee"
    assert len(slugify_goal("word " * 40)) <= 60


def test_write_report_stays_inside_report_dir(tmp_path) -> None:
    report = parse_report(
        json.dumps(
            {
                "research_question": "What is X?",
                "executive_summary": "Summary.",
                "key_findings": [],
                "important_evidence": [],
                "contradictions": [],
                "actionable_insights": [],
                "sources": [],
                "execution_summary": {
                    "started_at": "2026-01-01T12:00:00Z",
                    "finished_at": "2026-01-01T12:00:00Z",
                    "duration_s": 0.0,
                    "steps_total": 0,
                    "steps_succeeded": 0,
                    "steps_failed": 0,
                    "sources_searched": 0,
                    "sources_used": 0,
                    "sources_rejected": 0,
                    "tool_calls": 0,
                    "retries": 0,
                    "failures": [],
                },
            }
        )
    )
    path = Path(
        write_report(report, report_dir=str(tmp_path), goal="../../etc/passwd")
    )
    assert path.parent == tmp_path
    assert path.name == "etc-passwd.md"
    assert path.read_text(encoding="utf-8") == render_markdown(report)
