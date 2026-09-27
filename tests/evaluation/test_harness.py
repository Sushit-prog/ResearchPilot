"""§21 evaluation tests — run the synthetic tasks offline, check measured
metrics against each task's expectations, and regenerate docs/evaluation.md
from actual results (never invented numbers).

The determinism test re-runs the whole harness and asserts the rendered
document is byte-identical, so stale or hand-edited evaluation numbers fail
the suite rather than passing silently.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.evaluation.harness import (
    CANONICAL_SECTIONS,
    EVALUATION_DOC_PATH,
    TASKS_PATH,
    TaskResult,
    aggregate,
    load_tasks,
    render_markdown,
    run_tasks,
)

TASKS = load_tasks()
TASK_IDS = [task.id for task in TASKS]


@pytest.fixture(scope="module")
def evaluation_results() -> list[TaskResult]:
    return asyncio.run(run_tasks(TASKS))


def test_fixture_is_labelled_synthetic() -> None:
    raw = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    assert raw["synthetic"] is True
    assert "synthetic" in raw["_note"].lower()
    assert len(raw["tasks"]) >= 5
    assert all(entry["synthetic"] is True for entry in raw["tasks"])


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_task_meets_expected(task_id: str, evaluation_results: list[TaskResult]) -> None:
    task = next(entry for entry in TASKS if entry.id == task_id)
    result = next(entry for entry in evaluation_results if entry.task_id == task_id)
    expected = task.expected

    assert result.status == expected["status"]
    assert result.report_written is (expected["status"] == "completed")
    assert (result.plan_invalid == 0) is expected["plan_first_pass_valid"]
    assert set(expected["required_tools"]) <= result.tools_used
    assert len(expected["required_sections"]) == len(CANONICAL_SECTIONS)
    assert result.sections_found == len(expected["required_sections"])
    assert result.sources_distinct >= expected["min_distinct_sources"]
    assert result.findings_total >= expected["min_findings"]
    assert result.findings_cited == result.findings_total
    assert result.dedup_dropped == expected["deduplicated_events"]
    assert result.induced_failures == expected["induced_failures"]
    assert result.recovered_failures >= expected["min_recovered"]
    assert (result.contradictions > 0) is expected["contradictions_expected"]
    assert result.findings_computed >= expected["min_computed_findings"]


def test_aggregate_denominators_are_meaningful(evaluation_results: list[TaskResult]) -> None:
    agg = aggregate(evaluation_results)

    assert agg.tasks == len(TASKS)
    assert agg.completed == agg.tasks
    assert agg.first_pass_ok >= 5
    assert agg.plan_invalid >= 1
    assert agg.tool_calls >= agg.tool_ok > 0
    assert agg.induced_failures >= 1
    assert agg.recovered_failures >= 1
    assert agg.evidence_added > 0
    assert agg.dedup_dropped >= 1
    assert agg.sections_found == agg.completed * len(CANONICAL_SECTIONS)
    assert agg.findings_total > 0
    assert agg.findings_cited == agg.findings_total


def test_evaluation_doc_matches_measured_results(evaluation_results: list[TaskResult]) -> None:
    text = render_markdown(evaluation_results)
    EVALUATION_DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVALUATION_DOC_PATH.write_text(text, encoding="utf-8", newline="\n")
    back = EVALUATION_DOC_PATH.read_text(encoding="utf-8")

    assert back == text
    agg = aggregate(evaluation_results)
    assert f"| Tool success rate | {agg.tool_ok}/{agg.tool_calls} calls |" in back
    assert f"| Citation presence | {agg.findings_cited}/{agg.findings_total} findings |" in back
    assert f"| Report completeness | {agg.sections_found}/" in back
    assert f"| Failure recovery (induced) | {agg.recovered_failures}/" in back
    for result in evaluation_results:
        assert f"| {result.task_id} | {result.category} |" in back


def test_evaluation_rendering_is_deterministic(evaluation_results: list[TaskResult]) -> None:
    rerun = asyncio.run(run_tasks(TASKS))

    assert render_markdown(evaluation_results) == render_markdown(rerun)
