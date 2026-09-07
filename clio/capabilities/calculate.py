"""Arithmetic, spoken. Zero LLM involvement: a model that does mental
arithmetic is slower, costs a call, and is occasionally confidently wrong,
which is the one thing a calculator may never be.

Evaluated by walking the parse tree rather than `eval`, so nothing outside
arithmetic can execute even if the transcriber produces something strange.
Anything that isn't clearly a sum returns None and falls through to plain
conversation instead of being guessed at.
"""

from __future__ import annotations

import ast
import operator
import re

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# Speech has no symbols, so the spoken forms are rewritten before parsing.
# Longest first: "divided by" must win over "by".
_SPOKEN_OPS = [
    (r"\bto the power of\b", "**"),
    (r"\bmultiplied by\b", "*"),
    (r"\bdivided by\b", "/"),
    (r"\bover\b", "/"),
    (r"\btimes\b", "*"),
    (r"\bplus\b", "+"),
    (r"\bminus\b", "-"),
    (r"\bx\b", "*"),
]

_PERCENT_OF = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+(\d+(?:\.\d+)?)")

# A leading "what is" etc. is conversation, not part of the sum.
_PREFIX = re.compile(
    r"^(?:hey\s+)?(?:what(?:'?s| is)|calculate|compute|work out|how much is|tell me)\s+",
    re.IGNORECASE,
)

_ALLOWED_CHARS = re.compile(r"^[\d\s+\-*/%.()]+$")

# 2**999999999 would hang the loop that is also carrying the microphone.
_MAX_EXPONENT = 64
_MAX_BASE = 1e9


def parse_calculation(text: str) -> float | None:
    """Returns the answer if `text` is clearly arithmetic, else None."""
    expression = _PREFIX.sub("", text.strip().lower()).rstrip("?.! ")
    expression = _PERCENT_OF.sub(r"(\2 * \1 / 100)", expression)
    for pattern, symbol in _SPOKEN_OPS:
        expression = re.sub(pattern, symbol, expression)
    expression = expression.replace(",", "")

    if not expression or not _ALLOWED_CHARS.match(expression):
        return None
    # A bare number is not a question, and a lone operator is not a sum.
    if not any(op in expression for op in "+-*/%"):
        return None

    try:
        tree = ast.parse(expression, mode="eval")
        result = _evaluate(tree.body)
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError, MemoryError):
        return None

    if result != result or result in (float("inf"), float("-inf")):
        return None
    return float(result)


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numbers")
        return node.value

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand)
        return -value if isinstance(node.op, ast.USub) else value

    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > _MAX_EXPONENT or abs(left) > _MAX_BASE):
            raise ValueError("exponent too large to be a real question")
        return _OPS[type(node.op)](left, right)

    raise ValueError("unsupported expression")


def format_number(value: float, places: int = 4) -> str:
    """Spoken, so no thousands separators - a comma is read unpredictably."""
    rounded = round(value, places)
    if rounded == int(rounded) and abs(rounded) < 1e15:
        return str(int(rounded))
    return f"{rounded:.{places}f}".rstrip("0").rstrip(".")
