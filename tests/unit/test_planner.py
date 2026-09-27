from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.agent.planner import generate_plan, normalize_goal, parse_plan
from app.config import Config
from app.errors import PlannerError, PlanValidationError, ToolInputValidationError
from app.models.events import EventKind


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


def test_normalize_goal_trims_collapses_and_nfc() -> None:
    assert normalize_goal("  What   is\nreally\tX?  ") == "What is really X?"
    assert normalize_goal("café") == "café"
    assert normalize_goal("   \t\n ") == ""


def test_normalize_goal_preserves_case() -> None:
    assert normalize_goal("GPT-4o vs CLAUDE") == "GPT-4o vs CLAUDE"


async def test_prompt_contains_goal_contract_tools_and_invariant(
    fake_llm, registry, event_log, valid_plan_json
) -> None:
    llm = fake_llm(valid_plan_json)
    emitter, events = event_log
    plan = await generate_plan(llm, registry, Config(), emitter, "What is X?")
    call = llm.calls[0]
    assert "What is X?" in call["user"]
    assert "web_search" in call["system"]
    assert "calculator" in call["system"]
    assert "Plan-time invariant" in call["system"]
    assert '"expected_output"' in call["system"]
    assert [event.event for event in events] == [EventKind.PLAN_CREATED]
    assert len(plan.steps) == 2


async def test_invalid_json_feeds_error_back_then_succeeds(
    fake_llm, registry, event_log, valid_plan_json
) -> None:
    llm = fake_llm("totally not json", valid_plan_json)
    emitter, events = event_log
    plan = await generate_plan(llm, registry, Config(), emitter, "goal")
    assert len(llm.calls) == 2
    assert "not valid JSON" in llm.calls[1]["user"]
    assert "Previous output" in llm.calls[1]["user"]
    assert "totally not json" in llm.calls[1]["user"]
    assert plan.steps[0].id == "r1"
    assert [event.event for event in events] == [
        EventKind.PLAN_INVALID,
        EventKind.PLAN_CREATED,
    ]
    assert events[0].attempt == 1


async def test_fenced_json_accepted(
    fake_llm, registry, event_log, valid_plan_json
) -> None:
    llm = fake_llm(f"```json\n{valid_plan_json}\n```")
    emitter, events = event_log
    plan = await generate_plan(llm, registry, Config(), emitter, "goal")
    assert len(plan.steps) == 2
    assert len(llm.calls) == 1
    assert [event.event for event in events] == [EventKind.PLAN_CREATED]


async def test_exhausted_correction_raises_planner_error(
    fake_llm, registry, event_log
) -> None:
    llm = fake_llm("bad one", "bad two", "bad three")
    emitter, events = event_log
    with pytest.raises(PlannerError) as exc_info:
        await generate_plan(llm, registry, Config(), emitter, "goal")
    assert type(exc_info.value) is PlannerError
    assert len(llm.calls) == 3
    assert [event.event for event in events] == [EventKind.PLAN_INVALID] * 3
    assert [event.attempt for event in events] == [1, 2, 3]


async def test_max_plan_attempts_budget_respected(fake_llm, registry, event_log) -> None:
    llm = fake_llm("bad one", "bad two", "never used")
    emitter, events = event_log
    config = Config(max_plan_attempts=2)
    with pytest.raises(PlannerError):
        await generate_plan(llm, registry, config, emitter, "goal")
    assert len(llm.calls) == 2
    assert len(events) == 2


async def test_unknown_tool_triggers_correction_then_success(
    fake_llm, registry, event_log, valid_plan_json
) -> None:
    bad_plan = json.dumps(
        {"goal": "g", "steps": [_step("r1", tool="shell_exec", arguments={})]}
    )
    llm = fake_llm(bad_plan, valid_plan_json)
    emitter, events = event_log
    plan = await generate_plan(llm, registry, Config(), emitter, "goal")
    assert len(llm.calls) == 2
    assert "unknown tool" in llm.calls[1]["user"]
    assert plan.steps[0].id == "r1"
    assert [event.event for event in events] == [
        EventKind.PLAN_INVALID,
        EventKind.PLAN_CREATED,
    ]


async def test_empty_goal_rejected_before_any_llm_call(
    fake_llm, registry, event_log, valid_plan_json
) -> None:
    llm = fake_llm(valid_plan_json)
    emitter, events = event_log
    with pytest.raises(PlanValidationError) as exc_info:
        await generate_plan(llm, registry, Config(), emitter, "")
    assert type(exc_info.value) is PlanValidationError
    assert llm.calls == []
    assert events == []


def test_parse_plan_valid(registry, valid_plan_json) -> None:
    plan = parse_plan(valid_plan_json, registry, Config())
    assert [step.id for step in plan.steps] == ["r1", "q1"]


def test_parse_plan_structural_error_propagates_unwrapped(registry) -> None:
    data = {
        "goal": "g",
        "steps": [_step("r1"), _step("r1")],
    }
    with pytest.raises(PlanValidationError) as exc_info:
        parse_plan(json.dumps(data), registry, Config())
    assert type(exc_info.value) is PlanValidationError
    assert "duplicate" in str(exc_info.value)


def test_parse_plan_field_level_error_is_wrapped(registry) -> None:
    data = {"goal": "g", "steps": [{"id": "r1", "objective": "o"}]}
    with pytest.raises(PlanValidationError) as exc_info:
        parse_plan(json.dumps(data), registry, Config())
    assert type(exc_info.value) is PlanValidationError
    assert isinstance(exc_info.value.__cause__, ValidationError)


def test_parse_plan_empty_steps_rejected(registry) -> None:
    with pytest.raises(PlanValidationError) as exc_info:
        parse_plan(json.dumps({"goal": "g", "steps": []}), registry, Config())
    assert type(exc_info.value) is PlanValidationError


def test_parse_plan_unknown_tool_rejected(registry) -> None:
    data = {"goal": "g", "steps": [_step("r1", tool="shell_exec", arguments={})]}
    with pytest.raises(PlanValidationError) as exc_info:
        parse_plan(json.dumps(data), registry, Config())
    assert type(exc_info.value) is PlanValidationError
    assert "shell_exec" in str(exc_info.value)


def test_parse_plan_arguments_failing_schema_rejected(registry) -> None:
    data = {"goal": "g", "steps": [_step("r1", arguments={})]}
    with pytest.raises(PlanValidationError) as exc_info:
        parse_plan(json.dumps(data), registry, Config())
    assert type(exc_info.value) is PlanValidationError
    assert isinstance(exc_info.value.__cause__, ToolInputValidationError)


def test_parse_plan_empty_expected_output_rejected(registry) -> None:
    data = {"goal": "g", "steps": [_step("r1", expected_output="  ")]}
    with pytest.raises(PlanValidationError) as exc_info:
        parse_plan(json.dumps(data), registry, Config())
    assert "expected_output" in str(exc_info.value)


def test_parse_plan_over_cap_rejected(registry) -> None:
    steps = [_step(f"s{i:02d}") for i in range(13)]
    with pytest.raises(PlanValidationError) as exc_info:
        parse_plan(json.dumps({"goal": "g", "steps": steps}), registry, Config())
    assert "maximum" in str(exc_info.value)
