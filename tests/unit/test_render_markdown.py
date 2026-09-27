from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.agent.synthesizer import render_markdown
from app.models.events import FailureKind
from app.models.report import (
    ExecutionSummary,
    FailureRecord,
    Finding,
    ImportantEvidence,
    Report,
    SourceRef,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

GOLDEN_REPORT = Report(
    research_question="What is X?",
    executive_summary="X grew 12% to $10B in 2025.",
    key_findings=[
        Finding(
            index=1,
            claim="X reached $10B.",
            evidence_id="ev-a",
            source_url="https://example.com/a",
            source_title="Example A",
            confidence=0.9,
        ),
        Finding(
            index=2,
            claim="2 + 2 = 4",
            evidence_id="q1:1",
            source_url=None,
            source_title=None,
            confidence=0.95,
        ),
    ],
    important_evidence=[
        ImportantEvidence(evidence_id="ev-a", excerpt="Revenue reached $10B.")
    ],
    contradictions=[
        "ev-a conflicts with ev-b: cross-source disagreement on 'yoy_growth': "
        "values [12.0, 15.0]"
    ],
    actionable_insights=["Track X's next quarterly filing."],
    sources=[
        SourceRef(index=1, url="https://example.com/a", title="Example A", domain="example.com")
    ],
    execution_summary=ExecutionSummary(
        started_at=NOW,
        finished_at=NOW,
        duration_s=0.0,
        steps_total=2,
        steps_succeeded=1,
        steps_failed=1,
        sources_searched=3,
        sources_used=2,
        sources_rejected=1,
        tool_calls=5,
        retries=2,
        failures=[
            FailureRecord(
                step_id="r1",
                tool="web_search",
                failure_kind=FailureKind.PERMANENT,
                message="all candidates failed",
                unclassified=True,
            )
        ],
    ),
)


def test_render_markdown_matches_golden_file() -> None:
    golden = (
        Path(__file__).resolve().parent.parent / "fixtures" / "golden_report.md"
    ).read_text(encoding="utf-8")
    assert render_markdown(GOLDEN_REPORT) == golden


def test_render_markdown_empty_sections_say_none_identified() -> None:
    empty = GOLDEN_REPORT.model_copy(
        update={
            "key_findings": [],
            "important_evidence": [],
            "contradictions": [],
            "actionable_insights": [],
            "sources": [],
            "execution_summary": GOLDEN_REPORT.execution_summary.model_copy(
                update={"failures": []}
            ),
        }
    )
    rendered = render_markdown(empty)
    assert rendered.count("- None identified.") == 5
    assert "- Failures: none" in rendered
    assert "UNCLASSIFIED" not in rendered
