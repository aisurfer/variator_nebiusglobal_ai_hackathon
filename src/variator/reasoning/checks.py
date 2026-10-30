"""Deterministic checks on decision models and results.

These run before or after the LLM and never call it: model lint feeds the
critic, ranking/dominance/flip checks keep the analyst and red team honest.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from variator.domain import (
    DecisionModel,
    Distribution,
    DistributionKind,
    OptimizationResult,
    OptionStats,
    SimulationResult,
)
from variator.expr import compile_with_derived
from variator.reasoning.schemas import CritiqueIssue, IssueKind, SensitivityFlip, Severity

NARROW_RANGE = 0.05  # relative width below which a range looks overconfident


def match_name(raw: str | None, names: Iterable[str]) -> str | None:
    """Map an LLM-written name to a known identifier ("Second Warehouse" -> "second_warehouse")."""
    if raw is None:
        return None
    names = list(names)
    if raw in names:
        return raw
    key = re.sub(r"[\s\-]+", "_", raw.strip().strip("`'\"").casefold())
    return next((n for n in names if n.casefold() == key), None)


# --- model lint ----------------------------------------------------------------------------


def lint_decision_model(model: DecisionModel) -> list[CritiqueIssue]:
    """Structural problems code can detect without judgment (dead variables, no-op risks...)."""
    used_by = {o.name: set(compile_with_derived(o.formula, model.derived_map()).names) for o in model.options}
    used = set().union(*used_by.values())
    issues: list[CritiqueIssue] = []

    def add(severity: Severity, kind: IssueKind, target: str, problem: str, suggestion: str) -> None:
        issues.append(CritiqueIssue(severity=severity, kind=kind, target=target, problem=problem,
                                    suggestion=suggestion))

    for var in model.variables:
        if var.name not in used:
            add(Severity.MEDIUM, IssueKind.STRUCTURE, var.name,
                "Variable is not used in any option formula, so it has no effect.",
                "Use it in the formulas that depend on it or remove it.")
        if not (var.source or "").strip():
            add(Severity.LOW, IssueKind.UNGROUNDED, var.name, "Variable has no source.",
                "Cite the supporting fact or mark it as an assumption.")
        problem = _range_problem(var.distribution)
        if problem:
            add(Severity.MEDIUM if "zero" in problem else Severity.LOW, IssueKind.OVERCONFIDENT_RANGE,
                var.name, problem, "Widen the range to reflect real uncertainty, or make it fixed.")

    for option in model.options:
        for name in option.overrides:
            if name not in used_by[option.name]:
                add(Severity.MEDIUM, IssueKind.STRUCTURE, option.name,
                    f"Override of {name!r} has no effect: the option formula does not use it.",
                    f"Use {name!r} in the formula or drop the override.")

    seen: dict[tuple[str, str], str] = {}
    for option in model.options:
        signature = (re.sub(r"\s+", "", option.formula), repr(sorted(option.overrides.items())))
        if signature in seen:
            add(Severity.HIGH, IssueKind.FORMULA_ERROR, option.name,
                f"Option is indistinguishable from {seen[signature]!r} (same formula and overrides).",
                "Give each option its own formula or overrides.")
        seen.setdefault(signature, option.name)

    for risk in model.risks:
        exposed = risk.options or [o.name for o in model.options]
        if not any(risk.variable in used_by[o] for o in exposed if o in used_by):
            add(Severity.HIGH, IssueKind.STRUCTURE, risk.name,
                f"Risk shifts {risk.variable!r}, which no exposed option formula uses; it has no effect.",
                "Target a variable the exposed options use, or expose the right options.")
        if risk.probability in (0.0, 1.0):
            add(Severity.LOW, IssueKind.IMPLAUSIBLE_RANGE, risk.name,
                f"Probability {risk.probability:g} makes the event impossible or certain.",
                "Use a probability strictly between 0 and 1, or model it as a variable.")
    return issues


def _range_problem(dist: Distribution) -> str | None:
    if dist.kind in (DistributionKind.PERT, DistributionKind.TRIANGULAR, DistributionKind.UNIFORM):
        width = dist.high - dist.low
        center = dist.mode if dist.mode is not None else (dist.low + dist.high) / 2
    elif dist.kind in (DistributionKind.NORMAL, DistributionKind.LOGNORMAL):
        width, center = 2 * dist.std, dist.mean
    else:
        return None
    if width == 0:
        return "Range has zero width but is declared uncertain."
    if center and width / abs(center) < NARROW_RANGE:
        return f"Range is narrower than {NARROW_RANGE:.0%} of the central value; looks overconfident."
    return None


# --- result checks -------------------------------------------------------------------------


def rank_options(sim: SimulationResult) -> list[OptionStats]:
    """Options from best to worst expected value, respecting ``metric.higher_is_better``."""
    return sorted(sim.options, key=lambda o: o.mean, reverse=sim.metric.higher_is_better)


def _better(a: float, b: float, higher_is_better: bool) -> bool:
    return a > b if higher_is_better else a < b


def dominance_warnings(sim: SimulationResult, recommended: str) -> list[str]:
    """Warn when another option beats the recommendation on both mean and downside tail."""
    hib = sim.metric.higher_is_better
    by_name = {o.option: o for o in sim.options}
    rec = by_name.get(recommended)
    if rec is None:
        return []

    def tail(o: OptionStats) -> float:  # downside tail: p5 for gains, p95 for costs
        return o.p5 if hib else o.p95

    return [
        f"{other.option!r} beats the recommended {recommended!r} on both expected value "
        f"and downside tail (mean {other.mean:,.4g} vs {rec.mean:,.4g}; tail {tail(other):,.4g} "
        f"vs {tail(rec):,.4g})."
        for other in sim.options
        if other.option != recommended
        and _better(other.mean, rec.mean, hib)
        and not _better(tail(rec), tail(other), hib)
    ]


def optimizer_warnings(opt: OptimizationResult | None, recommended: str) -> list[str]:
    if opt is None or not opt.selected or recommended in opt.selected:
        return []
    return [
        f"The optimizer ({opt.solver}, status {opt.status}) selected {opt.selected}, "
        f"which does not include the recommended {recommended!r}."
    ]


def sensitivity_flips(sim: SimulationResult, recommended: str) -> list[SensitivityFlip]:
    """Variables whose p10 or p90 alone makes another option beat the recommendation.

    Uses the one-at-a-time tornado data. The engine pins the variable on the same
    scenario rows for every option, so the two options are compared like for like.
    Pairs without a sensitivity entry for both options are skipped rather than guessed.
    """
    hib = sim.metric.higher_is_better
    table = {(s.option, s.variable): s for s in sim.sensitivities}
    flips = []
    for (option, variable), rec in table.items():
        if option != recommended:
            continue
        for other in sim.options:
            challenger = table.get((other.option, variable))
            if other.option == recommended or challenger is None:
                continue
            for side, rec_value, other_value in (
                ("low", rec.metric_at_low, challenger.metric_at_low),
                ("high", rec.metric_at_high, challenger.metric_at_high),
            ):
                if _better(other_value, rec_value, hib):
                    flips.append(SensitivityFlip(
                        variable=variable, at=side, recommended=recommended, challenger=other.option,
                        recommended_metric=rec_value, challenger_metric=other_value,
                    ))
    return flips


def option_names(model: DecisionModel) -> Sequence[str]:
    return [o.name for o in model.options]
