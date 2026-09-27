from __future__ import annotations

from datetime import datetime, timezone

import pydantic
import pytest

from app.models.events import FailureKind
from app.models.report import ExecutionSummary, FailureRecord, Report

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _summary_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "started_at": NOW,
        "finished_at": NOW,
        "duration_s": 1.5,
        "steps_total": 2,
        "steps_succeeded": 2,
        "steps_failed": 0,
        "sources_searched": 3,
        "sources_used": 2,
        "sources_rejected": 1,
        "tool_calls": 5,
        "retries": 1,
        "failures": [],
    }
    data.update(overrides)
    return data


def _report_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "research_question": "What is X?",
        "executive_summary": "X is Y.",
        "key_findings": [
            {
                "index": 1,
                "claim": "X is Y",
                "evidence_id": "e1",
                "source_url": "https://example.com/a",
                "source_title": "Example A",
                "confidence": 0.9,
            }
        ],
        "important_evidence": [{"evidence_id": "e1", "excerpt": "X is Y."}],
        "contradictions": ["Source B says X is Z."],
        "actionable_insights": ["Monitor X."],
        "sources": [
            {
                "index": 1,
                "url": "https://example.com/a",
                "title": "Example A",
                "domain": "example.com",
            }
        ],
        "execution_summary": _summary_data(),
    }
    data.update(overrides)
    return data


def test_report_section_fields_match_spec() -> None:
    assert list(Report.model_fields) == [
        "research_question",
        "executive_summary",
        "key_findings",
        "important_evidence",
        "contradictions",
        "actionable_insights",
        "sources",
        "execution_summary",
    ]


def test_valid_report_accepted() -> None:
    report = Report.model_validate(_report_data())
    assert report.research_question == "What is X?"
    assert report.key_findings[0].evidence_id == "e1"
    assert report.execution_summary.retries == 1


@pytest.mark.parametrize(
    "section",
    [
        "research_question",
        "executive_summary",
        "key_findings",
        "important_evidence",
        "contradictions",
        "actionable_insights",
        "sources",
        "execution_summary",
    ],
)
def test_missing_section_rejected(section: str) -> None:
    data = _report_data()
    del data[section]
    with pytest.raises(pydantic.ValidationError):
        Report.model_validate(data)


def test_finding_computed_source_url_none_allowed() -> None:
    data = _report_data()
    findings = list(data["key_findings"])  # type: ignore[arg-type]
    findings[0] = {**findings[0], "source_url": None}
    data["key_findings"] = findings
    report = Report.model_validate(data)
    assert report.key_findings[0].source_url is None


@pytest.mark.parametrize("field", ["evidence_id", "source_url", "confidence", "index", "claim"])
def test_finding_missing_required_field_rejected(field: str) -> None:
    finding = {
        "index": 1,
        "claim": "X is Y",
        "evidence_id": "e1",
        "source_url": "https://example.com/a",
        "confidence": 0.9,
    }
    del finding[field]
    data = _report_data()
    data["key_findings"] = [finding]
    with pytest.raises(pydantic.ValidationError):
        Report.model_validate(data)


def test_execution_summary_requires_all_fields() -> None:
    summary = ExecutionSummary.model_validate(_summary_data())
    assert summary.failures == []
    assert summary.steps_failed == 0
    incomplete = _summary_data()
    del incomplete["retries"]
    with pytest.raises(pydantic.ValidationError):
        ExecutionSummary.model_validate(incomplete)


def test_failure_record_requires_kind() -> None:
    record = FailureRecord.model_validate(
        {
            "step_id": "r1",
            "tool": "webpage_fetch",
            "failure_kind": FailureKind.TRANSIENT,
            "message": "timed out",
        }
    )
    assert record.failure_kind is FailureKind.TRANSIENT
    assert record.unclassified is False
    with pytest.raises(pydantic.ValidationError):
        FailureRecord.model_validate({"step_id": "r1", "tool": "t", "message": "m"})


def test_report_json_round_trip() -> None:
    report = Report.model_validate(_report_data())
    restored = Report.model_validate_json(report.model_dump_json())
    assert restored == report
