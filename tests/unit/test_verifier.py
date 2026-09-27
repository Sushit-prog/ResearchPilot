from __future__ import annotations

import pytest

from app.agent.verifier import Sufficiency, evidence_sufficiency, validate_plan
from app.config import Config
from app.errors import PlanValidationError, ToolInputValidationError
from app.models.plan import Plan


def _plan(steps: list[dict]) -> Plan:
    return Plan.model_validate({"goal": "g", "steps": steps})


def _step(step_id: str, **overrides) -> dict:
    step = {
        "id": step_id,
        "objective": "do a thing",
        "tool": "web_search",
        "arguments": {"query": "x"},
        "expected_output": "results",
    }
    step.update(overrides)
    return step


def test_validate_plan_accepts_valid_plan(registry) -> None:
    validate_plan(_plan([_step("r1"), _step("q1", tool="calculator",
                                            arguments={"expression": "2+2"})]),
                  registry, Config().max_steps)


def test_validate_plan_rejects_over_cap(registry) -> None:
    steps = [_step(f"s{i:02d}") for i in range(13)]
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan(steps), registry, Config().max_steps)
    assert "maximum" in str(exc_info.value)


def test_validate_plan_rejects_unknown_tool(registry) -> None:
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([_step("r1", tool="shell_exec")]), registry, 12)
    assert "shell_exec" in str(exc_info.value)


def test_validate_plan_rejects_empty_expected_output(registry) -> None:
    with pytest.raises(PlanValidationError):
        validate_plan(_plan([_step("r1", expected_output="")]), registry, 12)


def test_validate_plan_rejects_arguments_failing_schema(registry) -> None:
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([_step("r1", arguments={})]), registry, 12)
    assert isinstance(exc_info.value.__cause__, ToolInputValidationError)


def test_sufficiency_empty_when_no_evidence() -> None:
    assert (
        evidence_sufficiency([], min_evidence=3, min_distinct_sources=2)
        is Sufficiency.EMPTY
    )


def test_sufficiency_insufficient_by_count(evidence_factory) -> None:
    evidence = [evidence_factory("ev-a", "claim", url="https://example.com/a")]
    assert (
        evidence_sufficiency(evidence, min_evidence=3, min_distinct_sources=2)
        is Sufficiency.INSUFFICIENT
    )


def test_sufficiency_insufficient_by_distinct_sources(evidence_factory) -> None:
    evidence = [
        evidence_factory("ev-a", "claim", url="https://example.com/a"),
        evidence_factory("ev-b", "claim", url="https://example.com/a"),
        evidence_factory("ev-c", "claim", url="https://example.com/a"),
    ]
    assert (
        evidence_sufficiency(evidence, min_evidence=3, min_distinct_sources=2)
        is Sufficiency.INSUFFICIENT
    )


def test_sufficiency_sufficient_with_two_sources(evidence_factory) -> None:
    evidence = [
        evidence_factory("ev-a", "claim", url="https://example.com/a"),
        evidence_factory("ev-b", "claim", url="https://example.com/a"),
        evidence_factory("ev-c", "claim", url="https://example.org/c"),
    ]
    assert (
        evidence_sufficiency(evidence, min_evidence=3, min_distinct_sources=2)
        is Sufficiency.SUFFICIENT
    )


def test_sufficiency_derived_evidence_never_counts_as_source(evidence_factory) -> None:
    evidence = [
        evidence_factory("ev-a", "2+2=4", url=None),
        evidence_factory("ev-b", "3+3=6", url=None),
        evidence_factory("ev-c", "4+4=8", url=None),
    ]
    assert (
        evidence_sufficiency(evidence, min_evidence=3, min_distinct_sources=2)
        is Sufficiency.INSUFFICIENT
    )


def test_sufficiency_single_real_source_plus_derived_is_insufficient(
    evidence_factory,
) -> None:
    evidence = [
        evidence_factory("ev-a", "claim", url="https://example.com/a"),
        evidence_factory("ev-b", "2+2=4", url=None),
        evidence_factory("ev-c", "3+3=6", url=None),
    ]
    assert (
        evidence_sufficiency(evidence, min_evidence=3, min_distinct_sources=2)
        is Sufficiency.INSUFFICIENT
    )
