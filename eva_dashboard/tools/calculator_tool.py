"""AST-sandboxed math calculator — never invent arithmetic in the LLM."""

from __future__ import annotations

import ast
import operator
from typing import Any

_ALLOWED_OPERATORS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def calculate_expression(expression: str) -> dict[str, Any]:
    """Safely evaluate a basic mathematical expression string."""
    raw = (expression or "").strip()
    if not raw:
        return {"ok": False, "error": "Empty expression", "markdown": "Calculation Error: empty"}

    try:
        node = ast.parse(raw, mode="eval")

        def _eval(n: ast.AST) -> float | int:
            if isinstance(n, ast.Expression):
                return _eval(n.body)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
                return n.value
            if isinstance(n, ast.UnaryOp) and type(n.op) in _ALLOWED_OPERATORS:
                return _ALLOWED_OPERATORS[type(n.op)](_eval(n.operand))
            if isinstance(n, ast.BinOp) and type(n.op) in _ALLOWED_OPERATORS:
                left = _eval(n.left)
                right = _eval(n.right)
                if isinstance(n.op, (ast.Div, ast.FloorDiv)) and float(right) == 0.0:
                    raise ZeroDivisionError("division by zero")
                return _ALLOWED_OPERATORS[type(n.op)](left, right)
            raise ValueError(f"Unsupported math syntax in: {raw}")

        result = _eval(node)
        text = f"Result: {result}"
        return {
            "ok": True,
            "expression": raw,
            "result": result,
            "markdown": text,
            "answer_markdown": text,
        }
    except Exception as exc:  # noqa: BLE001
        text = f"Calculation Error: {exc}"
        return {"ok": False, "error": str(exc), "markdown": text}
