from __future__ import annotations

from datetime import datetime, timezone

import pydantic
import pytest

from app.models.evidence import (
    Evidence,
    EvidenceVerification,
    NumericFact,
    Source,
    SourceQuality,
    SourceStatus,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _evidence_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "evidence_id": "e1",
        "claim": "X grew 12% to $8.9B",
        "source_url": "https://example.com/report",
        "source_title": "Example report",
        "extracted_text": "X grew 12% to $8.9B in 2024.",
        "relevance_score": 0.8,
        "confidence": 0.9,
        "retrieved_at": NOW,
    }
    data.update(overrides)
    return data


def test_valid_evidence_accepted() -> None:
    evidence = Evidence.model_validate(_evidence_data())
    assert evidence.evidence_id == "e1"
    assert evidence.relevance_score == 0.8
    assert evidence.step_id is None
    assert evidence.numeric is None


def test_evidence_field_set_matches_spec() -> None:
    assert list(Evidence.model_fields) == [
        "evidence_id",
        "claim",
        "source_url",
        "source_title",
        "extracted_text",
        "relevance_score",
        "confidence",
        "retrieved_at",
        "step_id",
        "numeric",
        "verification",
    ]


def test_evidence_verification_defaults() -> None:
    verification = EvidenceVerification()
    assert verification.recomputed is False
    assert verification.consistent is None
    assert verification.conflict_with == []
    assert verification.note is None


def test_evidence_verification_instance_is_fresh_per_evidence() -> None:
    first = Evidence.model_validate(_evidence_data())
    second = Evidence.model_validate(_evidence_data())
    first.verification.conflict_with.append("e2")
    assert second.verification.conflict_with == []


@pytest.mark.parametrize("field", ["relevance_score", "confidence"])
@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_scores_out_of_range_rejected(field: str, value: float) -> None:
    with pytest.raises(pydantic.ValidationError):
        Evidence.model_validate(_evidence_data(**{field: value}))


@pytest.mark.parametrize(
    "field",
    [
        "evidence_id",
        "claim",
        "source_url",
        "source_title",
        "extracted_text",
        "relevance_score",
        "confidence",
        "retrieved_at",
    ],
)
def test_missing_required_field_rejected(field: str) -> None:
    data = _evidence_data()
    del data[field]
    with pytest.raises(pydantic.ValidationError):
        Evidence.model_validate(data)


def test_source_url_none_allowed_for_derived_evidence() -> None:
    evidence = Evidence.model_validate(_evidence_data(source_url=None))
    assert evidence.source_url is None


def test_numeric_fact_schema() -> None:
    fact = NumericFact.model_validate({"value": 8.9})
    assert fact.unit is None
    assert fact.metric_label is None
    with pytest.raises(pydantic.ValidationError):
        NumericFact.model_validate({})


def test_source_status_members() -> None:
    assert {member.value for member in SourceStatus} == {"available", "unavailable", "rejected"}


def test_source_quality_members() -> None:
    assert {member.value for member in SourceQuality} == {"primary", "reputable", "unknown"}


def test_source_defaults() -> None:
    source = Source.model_validate({"url": "https://example.com", "domain": "example.com"})
    assert source.quality is SourceQuality.UNKNOWN
    assert source.status is SourceStatus.AVAILABLE
    assert source.title is None
    assert source.retrieved_at is None
    assert source.unavailable_reason is None


def test_evidence_json_round_trip() -> None:
    evidence = Evidence.model_validate(
        _evidence_data(numeric={"value": 8.9, "unit": "USD_B", "metric_label": "revenue"})
    )
    restored = Evidence.model_validate_json(evidence.model_dump_json())
    assert restored == evidence
