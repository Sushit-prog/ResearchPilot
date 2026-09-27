from __future__ import annotations

import pytest

from app.errors import PermanentToolError, ToolInputValidationError
from app.models.tool_io import CalculatorInput
from app.tools.calculator import CalculatorTool

tool = CalculatorTool()


async def value(expression: str) -> float:
    output = await tool.execute(CalculatorInput(expression=expression))
    return output.value


async def test_basic_arithmetic() -> None:
    assert await value("2 + 3 * 4") == 14.0
    assert await value("(2 + 3) * 4") == 20.0
    assert await value("10 - 3 - 2") == 5.0
    assert await value("100 / 10 / 2") == 5.0


async def test_integer_division_modulo_power() -> None:
    assert await value("7 // 2") == 3.0
    assert await value("7 % 3") == 1.0
    assert await value("2 ** 10") == 1024.0


async def test_unary_operators() -> None:
    assert await value("-5 + 3") == -2.0
    assert await value("+4.5") == 4.5
    assert await value("-(2 + 3)") == -5.0


async def test_allowed_functions() -> None:
    assert await value("sqrt(16)") == 4.0
    assert await value("abs(-7)") == 7.0
    assert await value("min(3, 1, 2)") == 1.0
    assert await value("max(3, 1, 2)") == 3.0
    assert await value("sqrt(abs(-16))") == 4.0


async def test_float_precision_approx() -> None:
    assert await value("0.1 + 0.2") == pytest.approx(0.3)


async def test_unit_and_expression_passthrough() -> None:
    output = await tool.execute(CalculatorInput(expression="2 + 2", unit="USD_B"))
    assert output.expression == "2 + 2"
    assert output.value == 4.0
    assert output.unit == "USD_B"


async def test_output_without_unit() -> None:
    output = await tool.execute(CalculatorInput(expression="1 + 1"))
    assert output.unit is None


EVAL_PAYLOADS = [
    "__import__('os').system('ls')",
    "().__class__",
    "'hello'",
    '"hello"',
    "x",
    "os.system('ls')",
    "open('x')",
    "(lambda: 1)()",
    "lambda: 1",
    "[x for x in range(3)]",
    "a[0]",
    "(y := 5)",
    "1 if True else 2",
    "1 < 2",
    "1 and 2",
    "f'{1}'",
    "{1: 2}",
    "1, 2",
    "sqrt(16, 2)",
    "min()",
    "sqrt(x=16)",
    "True",
    "None",
]


@pytest.mark.parametrize("expression", EVAL_PAYLOADS)
async def test_eval_payloads_rejected(expression: str) -> None:
    with pytest.raises(PermanentToolError) as exc_info:
        await value(expression)
    assert type(exc_info.value) is PermanentToolError
    assert exc_info.value.tool == "calculator"


@pytest.mark.parametrize("expression", ["1 / 0", "1 // 0", "1 % 0", "0 ** -1"])
async def test_division_by_zero_is_permanent(expression: str) -> None:
    with pytest.raises(PermanentToolError) as exc_info:
        await value(expression)
    assert type(exc_info.value) is PermanentToolError
    assert "zero" in str(exc_info.value).lower() or "division" in str(exc_info.value).lower()


async def test_math_domain_error_is_permanent() -> None:
    with pytest.raises(PermanentToolError) as exc_info:
        await value("sqrt(-1)")
    assert type(exc_info.value) is PermanentToolError


async def test_overflow_is_permanent() -> None:
    with pytest.raises(PermanentToolError) as exc_info:
        await value("10.0 ** 400")
    assert type(exc_info.value) is PermanentToolError


async def test_non_finite_intermediate_is_permanent() -> None:
    with pytest.raises(PermanentToolError):
        await value("1e308 * 10")


async def test_float_conversion_of_huge_int_is_permanent() -> None:
    with pytest.raises(PermanentToolError) as exc_info:
        await value("9 ** 999")
    assert type(exc_info.value) is PermanentToolError


async def test_pow_exponent_bound_rejects_memory_bomb() -> None:
    with pytest.raises(PermanentToolError) as exc_info:
        await value("2 ** 1000000")
    assert type(exc_info.value) is PermanentToolError


async def test_nested_pow_bomb_rejected_before_evaluation() -> None:
    with pytest.raises(PermanentToolError) as exc_info:
        await value("10 ** 10 ** 9")
    assert type(exc_info.value) is PermanentToolError


async def test_pow_base_bound() -> None:
    with pytest.raises(PermanentToolError):
        await value("(10 ** 200) ** (10 ** 200)")


async def test_expression_length_limit() -> None:
    with pytest.raises(PermanentToolError) as exc_info:
        await value("1" * 501)
    assert type(exc_info.value) is PermanentToolError


async def test_node_count_limit() -> None:
    expression = "+".join(["1"] * 101)
    with pytest.raises(PermanentToolError) as exc_info:
        await value(expression)
    assert type(exc_info.value) is PermanentToolError
    assert "nodes" in str(exc_info.value)


async def test_depth_limit() -> None:
    expression = "1"
    for _ in range(25):
        expression = f"({expression}+1)"
    with pytest.raises(PermanentToolError) as exc_info:
        await value(expression)
    assert type(exc_info.value) is PermanentToolError
    assert "nesting" in str(exc_info.value)


async def test_empty_expression_is_permanent() -> None:
    with pytest.raises(PermanentToolError) as exc_info:
        await value("")
    assert type(exc_info.value) is PermanentToolError


async def test_non_string_expression_rejected_by_input_validation() -> None:
    with pytest.raises(ToolInputValidationError):
        tool.parse_input({"expression": 5})


def test_tool_metadata() -> None:
    assert tool.name == "calculator"
    assert tool.is_test_component is False
    assert tool.input_model is CalculatorInput
