from __future__ import annotations

import pydantic
import pytest

from app.errors import PlanValidationError
from app.models.plan import Plan, StepStatus


def _step(step_id: str = "r1", **overrides: object) -> dict[str, object]:
    step: dict[str, object] = {
        "id": step_id,
        "objective": f"objective for {step_id}",
        "tool": "web_search",
        "expected_output": "findings",
    }
    step.update(overrides)
    return step


def _valid_plan_data() -> dict[str, object]:
    return {
        "goal": "Research X",
        "steps": [
            _step("r1", arguments={"query": "x"}),
            _step("r2", tool="calculator", arguments={"expression": "1+1"}),
        ],
    }


def test_valid_plan_accepted() -> None:
    plan = Plan.model_validate(_valid_plan_data())
    assert plan.goal == "Research X"
    assert len(plan.steps) == 2
    assert plan.steps[0].id == "r1"
    assert plan.steps[0].arguments == {"query": "x"}
    assert plan.steps[1].tool == "calculator"


def test_plan_step_field_defaults() -> None:
    plan = Plan.model_validate(_valid_plan_data())
    first = plan.steps[0]
    assert first.depends_on == []
    assert first.status is StepStatus.PENDING
    assert Plan.model_validate({"goal": "g", "steps": [_step("only")]}).steps[0].arguments == {}


def test_empty_steps_rejected() -> None:
    with pytest.raises(PlanValidationError) as exc_info:
        Plan.model_validate({"goal": "g", "steps": []})
    assert type(exc_info.value) is PlanValidationError
    assert "at least one step" in str(exc_info.value)


def test_duplicate_step_ids_rejected() -> None:
    data = _valid_plan_data()
    data["steps"] = [_step("r1"), _step("r1")]
    with pytest.raises(PlanValidationError) as exc_info:
        Plan.model_validate(data)
    assert type(exc_info.value) is PlanValidationError
    assert "duplicate step ids" in str(exc_info.value)
    assert "r1" in str(exc_info.value)


def test_dangling_depends_on_rejected() -> None:
    data = _valid_plan_data()
    data["steps"] = [_step("r1"), _step("r2", depends_on=["ghost"])]
    with pytest.raises(PlanValidationError) as exc_info:
        Plan.model_validate(data)
    assert type(exc_info.value) is PlanValidationError
    assert "dangling depends_on" in str(exc_info.value)
    assert "ghost" in str(exc_info.value)


def test_cyclic_depends_on_rejected() -> None:
    data = _valid_plan_data()
    data["steps"] = [_step("r1", depends_on=["r2"]), _step("r2", depends_on=["r1"])]
    with pytest.raises(PlanValidationError) as exc_info:
        Plan.model_validate(data)
    assert type(exc_info.value) is PlanValidationError
    assert "cyclic depends_on" in str(exc_info.value)


def test_self_loop_depends_on_rejected() -> None:
    with pytest.raises(PlanValidationError) as exc_info:
        Plan.model_validate({"goal": "g", "steps": [_step("r1", depends_on=["r1"])]})
    assert type(exc_info.value) is PlanValidationError
    assert "cyclic depends_on" in str(exc_info.value)


@pytest.mark.parametrize("field", ["id", "objective", "tool", "expected_output"])
def test_missing_required_step_fields_raise_pydantic_error(field: str) -> None:
    step = _step()
    del step[field]
    with pytest.raises(pydantic.ValidationError) as exc_info:
        Plan.model_validate({"goal": "g", "steps": [step]})
    assert not isinstance(exc_info.value, PlanValidationError)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("steps", "not-a-list-of-steps"),
        ("goal", 42),
        ("steps", [42]),
    ],
)
def test_wrong_types_raise_pydantic_error(field: str, value: object) -> None:
    data: dict[str, object] = {"goal": "g", "steps": [_step()]}
    data[field] = value
    with pytest.raises(pydantic.ValidationError) as exc_info:
        Plan.model_validate(data)
    assert not isinstance(exc_info.value, PlanValidationError)


def test_plan_validation_error_is_not_value_error() -> None:
    assert not issubclass(PlanValidationError, (ValueError, AssertionError))


def test_plan_json_round_trip() -> None:
    plan = Plan.model_validate(_valid_plan_data())
    restored = Plan.model_validate_json(plan.model_dump_json())
    assert restored == plan
