from __future__ import annotations

import ast
import math
from collections.abc import Callable

from app.errors import PermanentToolError
from app.models.tool_io import CalculatorInput, CalculatorOutput
from app.tools.base import Tool

_MAX_EXPRESSION_CHARS = 500
_MAX_AST_NODES = 200
_MAX_AST_DEPTH = 20
_MAX_POW_EXPONENT = 1000
_MAX_POW_BASE = 1e100
_MAX_RESULT_BITS = 100_000

_ALLOWED_FUNCTIONS: dict[str, Callable[..., float | int]] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "min": min,
    "max": max,
}

_BINARY_OPS: dict[type, Callable[[float | int, float | int], float | int]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}

_UNARY_OPS: dict[type, Callable[[float | int], float | int]] = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}


class CalculatorTool(Tool[CalculatorInput, CalculatorOutput]):
    name = "calculator"
    description = "Deterministic arithmetic: whitelisted-AST expression evaluator"
    input_model = CalculatorInput
    output_model = CalculatorOutput
    default_timeout_s = 5.0

    async def execute(self, params: CalculatorInput) -> CalculatorOutput:
        expression = params.expression
        if len(expression) > _MAX_EXPRESSION_CHARS:
            raise PermanentToolError(
                f"expression exceeds {_MAX_EXPRESSION_CHARS} characters", tool=self.name
            )
        try:
            tree = ast.parse(expression, mode="eval")
        except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
            raise PermanentToolError(f"invalid expression: {exc}", tool=self.name) from exc
        nodes = list(ast.walk(tree))
        if len(nodes) > _MAX_AST_NODES:
            raise PermanentToolError(
                f"expression has too many nodes ({len(nodes)} > {_MAX_AST_NODES})",
                tool=self.name,
            )
        if _depth(tree.body) > _MAX_AST_DEPTH:
            raise PermanentToolError(
                f"expression nesting exceeds {_MAX_AST_DEPTH}", tool=self.name
            )
        try:
            value = self._evaluate(tree.body)
            result = float(value)
        except ZeroDivisionError as exc:
            raise PermanentToolError("division by zero", tool=self.name) from exc
        except OverflowError as exc:
            raise PermanentToolError(f"arithmetic overflow: {exc}", tool=self.name) from exc
        except ValueError as exc:
            raise PermanentToolError(f"math domain error: {exc}", tool=self.name) from exc
        except TypeError as exc:
            raise PermanentToolError(f"invalid function call: {exc}", tool=self.name) from exc
        except (MemoryError, RecursionError) as exc:
            raise PermanentToolError("expression too complex to evaluate", tool=self.name) from exc
        if not math.isfinite(result):
            raise PermanentToolError("result is not a finite number", tool=self.name)
        return CalculatorOutput(expression=expression, value=result, unit=params.unit)

    def _evaluate(self, node: ast.expr) -> float | int:
        if isinstance(node, ast.Constant):
            if type(node.value) in (int, float):
                return node.value
            raise PermanentToolError(
                f"literal of type {type(node.value).__name__} is not allowed", tool=self.name
            )
        if isinstance(node, ast.UnaryOp):
            op = _UNARY_OPS.get(type(node.op))
            if op is None:
                raise PermanentToolError(
                    f"unary operator {type(node.op).__name__} is not allowed", tool=self.name
                )
            return self._checked(op(self._evaluate(node.operand)))
        if isinstance(node, ast.BinOp):
            left = self._evaluate(node.left)
            right = self._evaluate(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(right) > _MAX_POW_EXPONENT:
                    raise PermanentToolError(
                        f"exponent {right} exceeds bound {_MAX_POW_EXPONENT}", tool=self.name
                    )
                if abs(left) > _MAX_POW_BASE:
                    raise PermanentToolError(
                        f"base exceeds bound {_MAX_POW_BASE:g}", tool=self.name
                    )
            op = _BINARY_OPS.get(type(node.op))
            if op is None:
                raise PermanentToolError(
                    f"operator {type(node.op).__name__} is not allowed", tool=self.name
                )
            return self._checked(op(left, right))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
                raise PermanentToolError("only sqrt/abs/min/max calls are allowed", tool=self.name)
            if node.keywords:
                raise PermanentToolError("keyword arguments are not allowed", tool=self.name)
            args = [self._evaluate(arg) for arg in node.args]
            return self._checked(_ALLOWED_FUNCTIONS[node.func.id](*args))
        raise PermanentToolError(
            f"expression element {type(node).__name__} is not allowed", tool=self.name
        )

    def _checked(self, value: float | int) -> float | int:
        if isinstance(value, int) and value.bit_length() > _MAX_RESULT_BITS:
            raise PermanentToolError(
                f"intermediate result exceeds {_MAX_RESULT_BITS} bits", tool=self.name
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise PermanentToolError("intermediate result is not a finite number", tool=self.name)
        return value


def _depth(node: ast.AST) -> int:
    children = list(ast.iter_child_nodes(node))
    if not children:
        return 1
    return 1 + max(_depth(child) for child in children)
