"""Decision model: the contract between the LLM layer and the simulation engine.

Nemotron Super produces a ``DecisionModel`` as structured JSON; the Monte Carlo
engine samples it; cuOpt optimizes over the resulting scenarios. The schema is
deliberately flat (no discriminated unions) so it stays easy for an LLM to fill
via ``response_format={"type": "json_schema"}``.
"""

from __future__ import annotations

import ast
import keyword
import re
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from variator.domain.criteria import Criterion

IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Functions a metric formula may call. The simulation engine must implement
# exactly this set (vectorized over scenarios).
FORMULA_FUNCTIONS = frozenset(
    {"min", "max", "abs", "exp", "log", "sqrt", "clip", "where", "minimum", "maximum"}
)


class DistributionKind(StrEnum):
    FIXED = "fixed"  # value
    NORMAL = "normal"  # mean, std
    LOGNORMAL = "lognormal"  # mean, std of the variable itself (not of log)
    UNIFORM = "uniform"  # low, high
    TRIANGULAR = "triangular"  # low, mode, high
    PERT = "pert"  # low, mode, high (beta-PERT, lambda=4)
    BERNOULLI = "bernoulli"  # p -> 0/1


_REQUIRED_PARAMS: dict[DistributionKind, tuple[str, ...]] = {
    DistributionKind.FIXED: ("value",),
    DistributionKind.NORMAL: ("mean", "std"),
    DistributionKind.LOGNORMAL: ("mean", "std"),
    DistributionKind.UNIFORM: ("low", "high"),
    DistributionKind.TRIANGULAR: ("low", "mode", "high"),
    DistributionKind.PERT: ("low", "mode", "high"),
    DistributionKind.BERNOULLI: ("p",),
}


class Distribution(BaseModel):
    """A univariate distribution. Only the parameters required by ``kind`` are used."""

    kind: DistributionKind
    value: float | None = None
    mean: float | None = None
    std: float | None = Field(default=None, ge=0)
    low: float | None = None
    mode: float | None = None
    high: float | None = None
    p: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _check_params(self) -> Distribution:
        missing = [name for name in _REQUIRED_PARAMS[self.kind] if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.kind} distribution requires {', '.join(missing)}")
        if self.kind in (DistributionKind.UNIFORM, DistributionKind.TRIANGULAR,
                         DistributionKind.PERT) and self.low > self.high:
            raise ValueError("low must be <= high")
        if self.kind in (DistributionKind.TRIANGULAR, DistributionKind.PERT) and not (
            self.low <= self.mode <= self.high
        ):
            raise ValueError("mode must be within [low, high]")
        if self.kind == DistributionKind.LOGNORMAL and self.mean <= 0:
            raise ValueError("lognormal mean must be > 0")
        return self

    @classmethod
    def fixed(cls, value: float) -> Distribution:
        return cls(kind=DistributionKind.FIXED, value=value)


class Variable(BaseModel):
    """An uncertain driver of the outcome (price, volume, churn, capex...)."""

    name: str = Field(description="snake_case identifier used in formulas")
    description: str
    unit: str = ""
    distribution: Distribution
    source: str | None = Field(
        default=None, description="Where the estimate comes from: data, assumption or a URL"
    )

    @field_validator("name")
    @classmethod
    def _identifier(cls, v: str) -> str:
        return _check_identifier(v)


class Correlation(BaseModel):
    """Rank correlation between two variables (Gaussian copula in the engine)."""

    a: str
    b: str
    rho: float = Field(ge=-0.99, le=0.99)


class RiskEffect(StrEnum):
    MULTIPLY = "multiply"  # variable *= (1 + magnitude)
    ADD = "add"  # variable += magnitude


class Risk(BaseModel):
    """A discrete risk event that shifts one variable when it fires."""

    name: str
    description: str
    probability: float = Field(ge=0, le=1)
    variable: str = Field(description="Name of the variable the event affects")
    effect: RiskEffect = RiskEffect.MULTIPLY
    magnitude: Distribution = Field(description="Size of the shift when the event fires")
    options: list[str] | None = Field(
        default=None, description="Options exposed to the risk; null means all options"
    )

    @field_validator("name")
    @classmethod
    def _identifier(cls, v: str) -> str:
        return _check_identifier(v)


class DecisionOption(BaseModel):
    """One alternative the leader can choose."""

    name: str = Field(description="snake_case identifier")
    description: str
    formula: str = Field(
        description="Python-like arithmetic expression over variable names giving the metric"
    )
    cost: float | None = Field(default=None, description="Upfront cost, used for budget limits")
    overrides: dict[str, Distribution] = Field(
        default_factory=dict, description="Option-specific distributions for some variables"
    )
    metrics: dict[str, str] = Field(
        default_factory=dict,
        description="Formulas of the secondary criteria (criterion name -> formula); "
        "`formula` gives the primary criterion",
    )

    @field_validator("name")
    @classmethod
    def _identifier(cls, v: str) -> str:
        return _check_identifier(v)


class MetricSpec(BaseModel):
    name: str = Field(description="e.g. NPV, EBITDA, profit over 12 months")
    unit: str = "USD"
    higher_is_better: bool = True


class Derived(BaseModel):
    """A named formula (for example ``duration``) that other formulas and rules can use.

    The engine calculates each derived value one time per evaluation, in dependency order
    (``expr.compile_with_derived``). Effect rules change variables; a derived value always
    follows them.
    """

    name: str = Field(description="snake_case identifier")
    formula: str = Field(description="Formula over variables and earlier derived values")
    description: str = ""
    unit: str = ""

    @field_validator("name")
    @classmethod
    def _identifier(cls, v: str) -> str:
        return _check_identifier(v)


class DecisionModel(BaseModel):
    """Everything the simulation engine needs to evaluate a decision."""

    question: str
    metric: MetricSpec
    variables: list[Variable]
    options: list[DecisionOption] = Field(min_length=1)
    correlations: list[Correlation] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    criteria: list[Criterion] = Field(
        default_factory=list,
        description="Axes of the analysis; the first one is the primary criterion (option.formula). "
        "Empty means one criterion built from `metric`",
    )
    exclusive_groups: list[list[str]] = Field(
        default_factory=list,
        description="Groups of options of which at most one can be chosen together",
    )
    derived: list[Derived] = Field(
        default_factory=list, description="Named formulas; use them in option formulas, metrics and rules"
    )
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> DecisionModel:
        names = [v.name for v in self.variables]
        if len(names) != len(set(names)):
            raise ValueError("variable names must be unique")
        variables = set(names)
        _check_derived(self.derived, variables)
        known = variables | {d.name for d in self.derived}
        option_names = [o.name for o in self.options]
        if len(option_names) != len(set(option_names)):
            raise ValueError("option names must be unique")
        for option in self.options:
            unknown = formula_names(option.formula) - known
            if unknown:
                raise ValueError(f"option {option.name!r} uses unknown variables: {sorted(unknown)}")
            bad = set(option.overrides) - variables
            if bad:
                raise ValueError(f"option {option.name!r} overrides unknown variables: {sorted(bad)}")
        criterion_names = [c.name for c in self.criteria]
        if len(criterion_names) != len(set(criterion_names)):
            raise ValueError("criterion names must be unique")
        for criterion in self.criteria:
            if criterion.rag_from is not None:
                bad = {criterion.rag_from.green, criterion.rag_from.red} - variables
                if bad:
                    raise ValueError(f"criterion {criterion.name!r}: RAG limits from unknown {sorted(bad)}")
            if criterion.tie_from is not None and criterion.tie_from not in variables:
                raise ValueError(f"criterion {criterion.name!r}: tie from unknown {criterion.tie_from!r}")
        secondary = set(criterion_names[1:])
        for option in self.options:
            extra = set(option.metrics) - secondary
            if extra:
                raise ValueError(
                    f"option {option.name!r} defines metrics for unknown or primary criteria: {sorted(extra)}"
                )
            missing = secondary - set(option.metrics)
            if missing:
                raise ValueError(f"option {option.name!r} has no formula for criteria {sorted(missing)}")
            for criterion, formula in option.metrics.items():
                unknown = formula_names(formula) - known
                if unknown:
                    raise ValueError(
                        f"option {option.name!r}, criterion {criterion!r} uses unknown variables: "
                        f"{sorted(unknown)}"
                    )
        for corr in self.correlations:
            if corr.a not in variables or corr.b not in variables or corr.a == corr.b:
                raise ValueError(f"bad correlation pair {corr.a!r}/{corr.b!r}")
        for risk in self.risks:
            if risk.variable not in variables:
                raise ValueError(f"risk {risk.name!r} targets unknown variable {risk.variable!r}")
            if risk.options and set(risk.options) - set(option_names):
                raise ValueError(f"risk {risk.name!r} references unknown options")
        for group in self.exclusive_groups:
            if set(group) - set(option_names):
                raise ValueError(f"exclusive group {group} references unknown options")
        return self

    def variable(self, name: str) -> Variable:
        return next(v for v in self.variables if v.name == name)

    def option(self, name: str) -> DecisionOption:
        return next(o for o in self.options if o.name == name)

    def effective_criteria(self) -> list[Criterion]:
        """The criteria of the analysis; one criterion from ``metric`` if none are set."""
        if self.criteria:
            return list(self.criteria)
        return [
            Criterion(
                name="value",
                description=self.metric.name,
                unit=self.metric.unit,
                higher_is_better=self.metric.higher_is_better,
            )
        ]

    def option_formulas(self, option: DecisionOption, *, expand: bool = False) -> dict[str, str]:
        """Criterion name -> formula for one option, in the order of ``effective_criteria``.

        The formulas keep derived names; compile them with ``expr.compile_with_derived``.
        ``expand=True`` gives text expansion (for display only; it grows exponentially in deep chains).
        """
        criteria = self.effective_criteria()
        formulas = {criteria[0].name: option.formula}
        formulas.update({c.name: option.metrics[c.name] for c in criteria[1:]})
        return {k: self.expand(v) for k, v in formulas.items()} if expand else formulas

    def derived_map(self) -> dict[str, str]:
        """Derived name -> formula."""
        return {d.name: d.formula for d in self.derived}

    def expand(self, formula: str) -> str:
        """Replace derived names in ``formula`` by their formulas (display only; can grow very large)."""
        if not self.derived:
            return formula
        return expand_formula(formula, {d.name: d.formula for d in self.derived})


def _check_derived(derived: list[Derived], variables: set[str]) -> None:
    """Unique names, no clash with variables, known references, no cycles."""
    names = [d.name for d in derived]
    if len(names) != len(set(names)):
        raise ValueError("derived names must be unique")
    clash = set(names) & variables
    if clash:
        raise ValueError(f"derived names clash with variables: {sorted(clash)}")
    allowed = variables | set(names)
    graph: dict[str, set[str]] = {}
    for d in derived:
        used = formula_names(d.formula)
        unknown = used - allowed
        if unknown:
            raise ValueError(f"derived {d.name!r} uses unknown names: {sorted(unknown)}")
        graph[d.name] = used & set(names)
    state: dict[str, int] = {}  # 1 = on the stack, 2 = done

    def visit(node: str, path: list[str]) -> None:
        if state.get(node) == 1:
            raise ValueError(f"derived values form a cycle: {' -> '.join([*path, node])}")
        if state.get(node) == 2:
            return
        state[node] = 1
        for nxt in sorted(graph[node]):
            visit(nxt, [*path, node])
        state[node] = 2

    for name in names:
        visit(name, [])


def expand_formula(formula: str, derived: dict[str, str]) -> str:
    """Inline ``derived`` (name -> formula) into ``formula``, recursively. The input must be valid."""

    class _Inline(ast.NodeTransformer):
        def __init__(self, stack: tuple[str, ...] = ()):
            self.stack = stack

        def visit_Name(self, node: ast.Name) -> ast.AST:
            if node.id not in derived:
                return node
            if node.id in self.stack:
                raise ValueError(f"derived values form a cycle at {node.id!r}")
            inner = ast.parse(derived[node.id], mode="eval").body
            return _Inline((*self.stack, node.id)).visit(inner)

    tree = _Inline().visit(ast.parse(formula, mode="eval"))
    return ast.unparse(ast.fix_missing_locations(tree))


def _check_identifier(v: str) -> str:
    if not IDENTIFIER_RE.match(v) or keyword.iskeyword(v) or v in FORMULA_FUNCTIONS:
        raise ValueError(f"{v!r} must be a snake_case identifier (not a keyword or function)")
    return v


_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp, ast.IfExp, ast.Call,
    ast.Name, ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
    ast.FloorDiv, ast.USub, ast.UAdd, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq,
    ast.And, ast.Or,
)


def formula_names(formula: str) -> set[str]:
    """Return variable names referenced by a formula; raise ValueError if it is unsafe.

    Only arithmetic, comparisons, conditionals and ``FORMULA_FUNCTIONS`` calls are allowed.
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid formula {formula!r}: {exc.msg}") from exc
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"formula {formula!r} uses forbidden syntax {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FORMULA_FUNCTIONS:
                raise ValueError(f"formula {formula!r} calls a non-whitelisted function")
            if node.keywords:
                raise ValueError(f"formula {formula!r} uses keyword arguments")
        elif isinstance(node, ast.Name) and node.id not in FORMULA_FUNCTIONS:
            names.add(node.id)
        elif isinstance(node, ast.Constant) and not isinstance(node.value, int | float):
            raise ValueError(f"formula {formula!r} contains a non-numeric constant")
    return names
