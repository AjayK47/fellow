"""Safe arithmetic evaluation for the ``calculate`` tool.

Deliberately *not* a miniature ``run_code``. The expression is parsed with
``ast.parse`` and walked against a closed allow-list of node types: there is no
name resolution beyond a fixed constant table, no attribute access, no
subscripting, no comprehension, and no call into anything ``_FUNCTIONS`` does
not name. That closure is the entire security property. ``run_code``'s module
docstring explains why this harness must not grow a second, un-gated Python
execution path (NousResearch/hermes-agent#4146); this tool exists so that
ordinary arithmetic never has to reach for that approval-gated sandbox in the
first place.

Arithmetic runs in ``Decimal`` rather than float because the questions this
answers are company questions -- money, percentages, headcount, runway -- where
``0.1 + 0.2`` must be ``0.3`` and a currency total must be exact rather than
merely close.
"""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal, DecimalException, getcontext, localcontext
from typing import Any, Callable

# Bounds on the *shape* of the expression, checked before any evaluation.
# They exist so a single call cannot become a denial-of-service through
# expression size alone; the exponent bound below covers the other direction
# (a tiny expression whose result is enormous).
MAX_EXPRESSION_CHARS = 500
MAX_NODES = 250
MAX_EXPONENT = 100
# Keep enough precision for ordinary business arithmetic and large integer
# powers without allowing a tiny expression to manufacture an unbounded tool
# result. Results whose integer part exceeds this bound are rejected rather
# than returned with a misleadingly rounded ``exact`` field.
MAX_RESULT_DIGITS = 512

_CONSTANTS: dict[str, Decimal] = {
    "pi": Decimal("3.14159265358979323846264338327950288"),
    "e": Decimal("2.71828182845904523536028747135266250"),
}


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; never meaningful here
        raise ValueError("booleans are not valid numbers")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # str() first so the literal the model wrote is what gets computed,
        # rather than the binary float that happens to be nearest to it.
        return Decimal(str(value))
    raise ValueError(f"unsupported numeric value: {value!r}")


def _round(value: Any, digits: Any = 0) -> Decimal:
    number = _decimal(value)
    places = int(_decimal(digits))
    quantum = Decimal(1).scaleb(-places)
    return number.quantize(quantum)


def _sqrt(value: Any) -> Decimal:
    number = _decimal(value)
    if number < 0:
        raise ValueError("sqrt of a negative number is undefined")
    return number.sqrt()


def _ln(value: Any) -> Decimal:
    number = _decimal(value)
    if number <= 0:
        raise ValueError("ln requires a positive number")
    return number.ln()


def _log10(value: Any) -> Decimal:
    number = _decimal(value)
    if number <= 0:
        raise ValueError("log10 requires a positive number")
    return number.log10()


def _log(value: Any, base: Any = 10) -> Decimal:
    return _ln(value) / _ln(base)


def _floor(value: Any) -> Decimal:
    return _decimal(value).to_integral_value(rounding="ROUND_FLOOR")


def _ceil(value: Any) -> Decimal:
    return _decimal(value).to_integral_value(rounding="ROUND_CEILING")


def _days_between(start: Any, end: Any) -> Decimal:
    """Whole days from ``start`` to ``end``, both ISO ``YYYY-MM-DD``.

    Date arithmetic is where a language model is most confidently wrong, and
    it is the one non-numeric input worth accepting here: the arguments are
    strings but the result is an ordinary number, so the expression stays
    uniformly ``Decimal`` and composes with everything else.
    """
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("days_between requires two ISO date strings")
    return Decimal((date.fromisoformat(end) - date.fromisoformat(start)).days)


def _percent_of(percent: Any, whole: Any) -> Decimal:
    """``percent_of(15, 240)`` -> 36. Reads the way the question is asked."""
    return _decimal(percent) / Decimal(100) * _decimal(whole)


def _percent_change(before: Any, after: Any) -> Decimal:
    start = _decimal(before)
    if start == 0:
        raise ValueError("percent_change is undefined from a base of zero")
    return (_decimal(after) - start) / start * Decimal(100)


_FUNCTIONS: dict[str, Callable[..., Decimal]] = {
    "abs": lambda value: abs(_decimal(value)),
    "round": _round,
    "min": lambda *values: min(_decimal(value) for value in values),
    "max": lambda *values: max(_decimal(value) for value in values),
    "sqrt": _sqrt,
    "ln": _ln,
    "log": _log,
    "log10": _log10,
    "exp": lambda value: _decimal(value).exp(),
    "floor": _floor,
    "ceil": _ceil,
    "days_between": _days_between,
    "percent_of": _percent_of,
    "percent_change": _percent_change,
}

_BINARY_OPS: dict[type, Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.FloorDiv: lambda left, right: (left / right).to_integral_value(
        rounding="ROUND_FLOOR"
    ),
    ast.Mod: lambda left, right: left % right,
}

# Every node type the walker will accept. Anything absent -- Attribute,
# Subscript, Lambda, comprehensions, Starred, JoinedStr, NamedExpr, Compare,
# BoolOp -- is rejected by _evaluate's fallthrough rather than by a blocklist,
# so a future Python grammar addition is refused by default instead of
# silently becoming reachable.
_ALLOWED_NODES = (
    ast.Expression,
    ast.Constant,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)


class CalculationError(ValueError):
    """Raised for any rejected or unevaluable expression."""


def _evaluate(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            # Only ever meaningful as a function argument (days_between);
            # a bare string expression is rejected by _result_decimal below.
            return node.value
        return _decimal(node.value)
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise CalculationError(f"unknown name: {node.id!r}")
    if isinstance(node, ast.UnaryOp):
        operand = _decimal(_evaluate(node.operand))
        return -operand if isinstance(node.op, ast.USub) else +operand
    if isinstance(node, ast.BinOp):
        left = _decimal(_evaluate(node.left))
        right = _decimal(_evaluate(node.right))
        if isinstance(node.op, ast.Pow):
            if abs(right) > MAX_EXPONENT:
                raise CalculationError(
                    f"exponent magnitude is limited to {MAX_EXPONENT}"
                )
            return left**right
        operation = _BINARY_OPS.get(type(node.op))
        if operation is None:
            raise CalculationError("unsupported operator")
        if right == 0 and isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)):
            raise CalculationError("division by zero")
        return operation(left, right)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise CalculationError("only direct calls to named functions are allowed")
        function = _FUNCTIONS.get(node.func.id)
        if function is None:
            raise CalculationError(f"unknown function: {node.func.id!r}")
        if node.keywords:
            raise CalculationError("keyword arguments are not supported")
        return function(*[_evaluate(argument) for argument in node.args])
    raise CalculationError(f"unsupported expression element: {type(node).__name__}")


def _format(value: Decimal) -> str:
    """Human-readable rendering: thousands separators, no exponent noise."""
    if value == value.to_integral_value():
        try:
            return f"{int(value):,}"
        except (OverflowError, ValueError):
            return format(value, "f")
    with localcontext() as context:
        context.prec = MAX_RESULT_DIGITS + 32
        quantized = value.quantize(Decimal("0.000000001")).normalize()
        return f"{quantized:,f}"


def _exact(value: Decimal) -> str:
    """Serialize without re-rounding under the process default context."""
    if value == value.to_integral_value():
        return format(value, "f")
    with localcontext() as context:
        context.prec = MAX_RESULT_DIGITS
        return str(value.normalize())


def calculate(expression: str) -> dict[str, Any]:
    """Evaluate ``expression`` and return exact, numeric, and display forms."""
    text = str(expression or "").strip()
    if not text:
        raise CalculationError("expression is empty")
    if len(text) > MAX_EXPRESSION_CHARS:
        raise CalculationError(
            f"expression exceeds {MAX_EXPRESSION_CHARS} characters"
        )
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise CalculationError(f"could not parse expression: {exc.msg}") from exc

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise CalculationError("expression is too complex")
    for node in nodes:
        if not isinstance(node, _ALLOWED_NODES):
            raise CalculationError(
                f"unsupported expression element: {type(node).__name__}"
            )

    # A local context keeps precision generous for intermediate steps without
    # mutating the process-wide Decimal context -- tool handlers can run
    # concurrently in the agent loop's read pool.
    try:
        with localcontext() as context:
            context.prec = max(MAX_RESULT_DIGITS, getcontext().prec)
            result = _evaluate(tree)
    except CalculationError:
        raise
    except DecimalException as exc:
        raise CalculationError(f"arithmetic error: {type(exc).__name__}") from exc
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise CalculationError(str(exc)) from exc

    if not isinstance(result, Decimal):
        raise CalculationError("expression did not evaluate to a number")
    if not result.is_finite():
        raise CalculationError("result is not a finite number")
    if result and result.adjusted() + 1 > MAX_RESULT_DIGITS:
        raise CalculationError(
            f"result exceeds {MAX_RESULT_DIGITS} integer digits"
        )

    return {
        "status": "completed",
        "expression": text,
        # `exact` is the authoritative answer; `value` is a float for
        # convenience and can lose precision on very large or very precise
        # results, which is exactly why both are returned.
        "exact": _exact(result),
        "value": float(result),
        "formatted": _format(result),
    }
