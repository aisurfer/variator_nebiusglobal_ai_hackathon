"""Compile a metric formula string into a vectorized function over arrays.

The formula is validated with ``variator.domain.formula_names`` (which rejects any
non-whitelisted syntax) and then translated node by node into closures that call the
array module. Nothing is ever passed to ``eval``/``exec``.

Semantics (elementwise over scenarios):

* ``+ - * / ** % //`` and unary ``+``/``-`` follow NumPy float64 rules (division by zero
  gives inf/nan instead of raising; the engine reports non-finite results).
* Comparisons return boolean arrays; chained comparisons ``a < b < c`` mean
  ``(a < b) & (b < c)``. Booleans used in arithmetic count as 0.0/1.0.
* ``and``/``or`` are logical (boolean) operations, not Python's value-returning ones.
* ``a if c else b`` is ``where(c, a, b)``; both branches are evaluated.
* Functions: exactly ``variator.domain.FORMULA_FUNCTIONS``.
  ``min``/``max`` take 2+ arguments and work elementwise; ``minimum``/``maximum`` take 2;
  ``abs``/``exp``/``log``/``sqrt`` take 1; ``clip(x, lo, hi)`` and ``where(c, a, b)`` take 3.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import numpy as np

from variator.domain import FORMULA_FUNCTIONS, formula_names

MAX_FORMULA_LENGTH = 5_000
MAX_FORMULA_DEPTH = 100  # compile and evaluation recurse once per AST level

Env = Mapping[str, Any]
Node = Callable[[Env, ModuleType], Any]


class FormulaError(ValueError):
    """The formula is unsafe, malformed or calls a function with the wrong arity."""


@dataclass(frozen=True)
class CompiledFormula:
    """A formula compiled to a closure tree. Call ``evaluate`` with an env of arrays."""

    source: str
    names: frozenset[str]
    _fn: Node

    def evaluate(self, env: Env, xp: ModuleType = np, size: int | None = None) -> Any:
        """Evaluate over ``env`` (variable name -> array or scalar) using array module ``xp``.

        Returns a float64 array; with ``size`` the result is broadcast to shape ``(size,)``
        (useful when the formula is constant). Floating-point warnings are suppressed:
        callers should check the result for NaN/inf.
        """
        missing = self.names - set(env)
        if missing:
            raise FormulaError(f"formula {self.source!r} needs values for {sorted(missing)}")
        with np.errstate(all="ignore"):
            result = xp.asarray(_as_number(self._fn(env, xp), xp), dtype=xp.float64)
        if size is not None:
            result = xp.broadcast_to(result, (size,))
        return result


@functools.lru_cache(maxsize=512)
def compile_formula(formula: str) -> CompiledFormula:
    """Validate and compile ``formula``; raise ``FormulaError`` if it is not allowed."""
    if len(formula) > MAX_FORMULA_LENGTH:
        raise FormulaError(f"formula is longer than {MAX_FORMULA_LENGTH} characters")
    try:
        names = formula_names(formula)
        tree = ast.parse(formula, mode="eval")
    except (ValueError, RecursionError, MemoryError) as exc:
        raise FormulaError(str(exc)) from exc
    if _depth(tree.body) > MAX_FORMULA_DEPTH:
        raise FormulaError(f"formula is nested deeper than {MAX_FORMULA_DEPTH} levels")
    return CompiledFormula(formula, frozenset(names), _compile(tree.body, formula))


def _depth(root: ast.AST) -> int:
    """Nesting depth of an AST, computed iteratively (no recursion limit issues)."""
    deepest, stack = 0, [(root, 1)]
    while stack:
        node, level = stack.pop()
        deepest = max(deepest, level)
        stack.extend((child, level + 1) for child in ast.iter_child_nodes(node))
    return deepest


def _as_number(value: Any, xp: ModuleType) -> Any:
    """Booleans count as 0.0/1.0 in arithmetic (NumPy would treat bool + bool as OR)."""
    if isinstance(value, bool | np.bool_):
        return np.float64(value)
    dtype = getattr(value, "dtype", None)
    if dtype is not None and dtype.kind == "b":
        return value.astype(xp.float64)
    return value


def _truth(value: Any, xp: ModuleType) -> Any:
    """Truthiness as a boolean array or scalar: nonzero means true."""
    dtype = getattr(value, "dtype", None)
    if isinstance(value, bool | np.bool_) or (dtype is not None and dtype.kind == "b"):
        return value
    return value != 0


_BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a**b,
    ast.Mod: lambda a, b: a % b,
    ast.FloorDiv: lambda a, b: a // b,
}

_COMPARE: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}

# name -> (min args, max args or None for unbounded)
_ARITY: dict[str, tuple[int, int | None]] = {
    "min": (2, None),
    "max": (2, None),
    "minimum": (2, 2),
    "maximum": (2, 2),
    "abs": (1, 1),
    "exp": (1, 1),
    "log": (1, 1),
    "sqrt": (1, 1),
    "clip": (3, 3),
    "where": (3, 3),
}


def _reduce(op: str) -> Callable[[ModuleType, list[Any]], Any]:
    def call(xp: ModuleType, args: list[Any]) -> Any:
        result = args[0]
        for arg in args[1:]:
            result = getattr(xp, op)(result, arg)
        return result

    return call


_FUNCTIONS: dict[str, Callable[[ModuleType, list[Any]], Any]] = {
    "min": _reduce("minimum"),
    "max": _reduce("maximum"),
    "minimum": _reduce("minimum"),
    "maximum": _reduce("maximum"),
    "abs": lambda xp, a: xp.abs(a[0]),
    "exp": lambda xp, a: xp.exp(a[0]),
    "log": lambda xp, a: xp.log(a[0]),
    "sqrt": lambda xp, a: xp.sqrt(a[0]),
    # minimum/maximum instead of xp.clip: array bounds behave the same on NumPy and CuPy
    "clip": lambda xp, a: xp.minimum(xp.maximum(a[0], a[1]), a[2]),
    "where": lambda xp, a: xp.where(_truth(a[0], xp), a[1], a[2]),
}


def _compile(node: ast.expr, source: str) -> Node:
    match node:
        case ast.Constant(value=value):
            # np.float64 so that constant-only overflow (e.g. 9 ** 9 ** 9) becomes inf
            # instead of a Python OverflowError or a huge exact integer.
            constant = np.float64(value)
            return lambda env, xp: constant
        case ast.Name(id=name):
            if name in FORMULA_FUNCTIONS:
                raise FormulaError(f"function {name!r} used as a value in formula {source!r}")
            return lambda env, xp: env[name]
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            inner = _compile(operand, source)
            return lambda env, xp: -_as_number(inner(env, xp), xp)
        case ast.UnaryOp(op=ast.UAdd(), operand=operand):
            inner = _compile(operand, source)
            return lambda env, xp: _as_number(inner(env, xp), xp)
        case ast.BinOp(left=left, op=op, right=right) if type(op) in _BINOPS:
            fn, lhs, rhs = _BINOPS[type(op)], _compile(left, source), _compile(right, source)
            return lambda env, xp: fn(_as_number(lhs(env, xp), xp), _as_number(rhs(env, xp), xp))
        case ast.Compare(left=left, ops=ops, comparators=comparators):
            return _compile_compare(left, ops, comparators, source)
        case ast.BoolOp(op=op, values=values):
            parts = [_compile(v, source) for v in values]
            combine = "logical_and" if isinstance(op, ast.And) else "logical_or"

            def boolop(env: Env, xp: ModuleType) -> Any:
                result = _truth(parts[0](env, xp), xp)
                for part in parts[1:]:
                    result = getattr(xp, combine)(result, _truth(part(env, xp), xp))
                return result

            return boolop
        case ast.IfExp(test=test, body=body, orelse=orelse):
            cond, then, other = (_compile(n, source) for n in (test, body, orelse))
            return lambda env, xp: xp.where(
                _truth(cond(env, xp), xp), _as_number(then(env, xp), xp), _as_number(other(env, xp), xp)
            )
        case ast.Call(func=ast.Name(id=name), args=args, keywords=[]) if name in _FUNCTIONS:
            low, high = _ARITY[name]
            if len(args) < low or (high is not None and len(args) > high):
                expected = f"{low}+" if high is None else str(low)
                raise FormulaError(f"{name}() takes {expected} arguments, got {len(args)} in {source!r}")
            fn, compiled = _FUNCTIONS[name], [_compile(a, source) for a in args]
            return lambda env, xp: fn(xp, [_as_number(c(env, xp), xp) for c in compiled])
    raise FormulaError(f"unsupported syntax {type(node).__name__} in formula {source!r}")


def _compile_compare(left: ast.expr, ops: list[ast.cmpop], comparators: list[ast.expr], source: str) -> Node:
    operands = [_compile(n, source) for n in (left, *comparators)]
    fns = [_COMPARE[type(op)] for op in ops]

    def compare(env: Env, xp: ModuleType) -> Any:
        values = [_as_number(o(env, xp), xp) for o in operands]
        result = fns[0](values[0], values[1])
        for i, fn in enumerate(fns[1:], start=1):
            result = xp.logical_and(result, fn(values[i], values[i + 1]))
        return result

    return compare
