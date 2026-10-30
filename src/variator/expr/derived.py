"""Formulas that use derived values (named formulas), calculated one time each, in dependency order.

Text expansion (``DecisionModel.expand``) copies each derived formula into each place that
uses it, so a deep chain (cash -> revenue -> churn -> nps -> sla -> incidents ...) grows
exponentially. ``compile_with_derived`` calculates each needed derived value one time per
evaluation, as an array, from the current env. So derived values always follow the
variables (also after effect rules), and the cost grows linearly with the chain.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import numpy as np

from variator.expr.formula import CompiledFormula, compile_formula


@dataclass(frozen=True)
class DerivedFormula:
    """Same interface as ``CompiledFormula``: ``names`` are the base variables it needs."""

    source: str
    names: frozenset[str]
    steps: tuple[tuple[str, CompiledFormula], ...]  # derived values in dependency order
    main: CompiledFormula

    def evaluate(self, env: Mapping[str, Any], xp: ModuleType = np, size: int | None = None) -> Any:
        local = dict(env)
        for name, formula in self.steps:
            local[name] = formula.evaluate(local, xp, size=size)
        return self.main.evaluate(local, xp, size=size)


def compile_with_derived(formula: str, derived: Mapping[str, str] | None) -> CompiledFormula | DerivedFormula:
    """Compile ``formula``; derived names in it are calculated from ``derived`` (name -> formula)."""
    main = compile_formula(formula)
    if not derived or not (main.names & derived.keys()):
        return main
    compiled: dict[str, CompiledFormula] = {}
    order: list[str] = []
    state: dict[str, int] = {}  # 1 = on the stack, 2 = done

    def visit(name: str) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            raise ValueError(f"derived values form a cycle at {name!r}")
        state[name] = 1
        compiled[name] = compile_formula(derived[name])
        for dep in sorted(compiled[name].names & derived.keys()):
            visit(dep)
        state[name] = 2
        order.append(name)

    for name in sorted(main.names & derived.keys()):
        visit(name)
    steps = tuple((n, compiled[n]) for n in order)
    base = set(main.names)
    for _, f in steps:
        base |= f.names
    return DerivedFormula(formula, frozenset(base - derived.keys()), steps, main)
