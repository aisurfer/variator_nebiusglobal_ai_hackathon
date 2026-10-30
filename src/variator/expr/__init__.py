"""Safe formula compiler: formula text -> vectorized closures over NumPy/CuPy (no eval).

A base layer: ``rules`` and ``simulation`` both use it, and it uses only ``domain``.
``compile_with_derived`` adds named formulas (derived values), calculated in dependency order.
"""

from variator.expr.derived import DerivedFormula, compile_with_derived
from variator.expr.formula import CompiledFormula, FormulaError, compile_formula

__all__ = ["CompiledFormula", "DerivedFormula", "FormulaError", "compile_formula", "compile_with_derived"]
