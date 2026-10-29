"""Exact arithmetic so the model stops guessing prices and totals."""

import ast

from . import as_args

_BIN = {ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow}
_ALLOWED = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub,
)

TOOL = {
    "type": "function",
    "function": {
        "name": "calc",
        "description": (
            "Evaluate one arithmetic expression and return the exact number. "
            "Use for totals, unit conversions, percentages, and any math on "
            "searched figures. Only + - * / // % ** and parentheses."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "e.g. 2*1000000 + 6*3000000",
                },
            },
            "required": ["expression"],
        },
    },
}


def _check(node):
    if not isinstance(node, _ALLOWED):
        raise ValueError("unsupported")
    if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
        raise ValueError("numbers only")
    if isinstance(node, ast.BinOp) and type(node.op) not in _BIN:
        raise ValueError("unsupported operator")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        r = node.right
        big_const = isinstance(r, ast.Constant) and isinstance(r.value, (int, float)) and abs(r.value) > 1000
        pow_in_exp = any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow) for n in ast.walk(r))
        if big_const or pow_in_exp:
            raise ValueError("exponent too large")
    for child in ast.iter_child_nodes(node):
        _check(child)


def evaluate(expression):
    tree = ast.parse(expression, mode="eval")
    _check(tree)
    return eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})


def compute(args):
    """The expression and its value, or `(False, reason)`."""
    args = as_args(args)
    expr = str(args.get("expression", "") or "").strip()
    if not expr:
        return False, "calc: no expression given."
    try:
        value = evaluate(expr)
    except Exception:
        return False, f"calc: cannot evaluate {expr!r}. Use only numbers and + - * / // % **."
    return True, f"{expr} = {value}"


async def run(args):
    return compute(args)[1]
