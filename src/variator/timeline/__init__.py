"""Timeline analysis: can the milestones be met, and with which resources, how much and when.

* ``evaluate``: one Monte Carlo run with the moved markers and placed events; per option the
  share of scenarios where each condition is true, the p10/p50/p90/p80 of each ``show``
  formula, and the random inputs that correlate most with a miss (the "inputs in conflict").
* ``solve_plan``: for one option, the levers in their order; each takes the grid value with the
  most milestones met (then the highest capped P), and among equal results its preferred end
  (fewer resources, a later month). The result: how much of each resource, and when.
* ``suggest``: proposed milestone months: the month that the forecast meets in 80% of the
  scenarios, rounded up to half a month.

All trials use the same seed, so a change of the result comes from the lever, not from noise.
The delivery page (``it-delivery-simulator.html``) mirrors all three; ``check_playground.py`` compares.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import numpy as np

from variator.domain import DecisionModel, Timeline, TimelineItem, with_values
from variator.rules import RuleSet
from variator.simulation import simulate_tensor

MIN_CORRELATION = 0.1  # |r| below this is noise at n >= 2000 (standard error about 0.02)
TOP_CONFLICTS = 3
SUGGEST_P = 0.8


@dataclass
class ItemResult:
    name: str
    label: str
    kind: str
    month: float
    target: float | None
    p_met: dict[str, float] = field(default_factory=dict)  # option -> share of scenarios
    spread: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)  # p10, p50, p90, p80

    def met(self, option: str) -> bool:
        return self.target is None or self.p_met.get(option, 1.0) >= self.target


@dataclass
class Conflict:
    item: str
    option: str
    variable: str
    r: float  # correlation of the input with "met"; < 0: high values cause the miss

    @property
    def direction(self) -> str:
        return "too high" if self.r < 0 else "too low"


@dataclass
class TimelineResult:
    items: list[ItemResult]
    conflicts: list[Conflict]

    def missed(self, option: str) -> list[str]:
        return [i.name for i in self.items if not i.met(option)]


@dataclass
class PlanStep:
    variable: str
    label: str
    mode: str | None
    current: float
    value: float


@dataclass
class Plan:
    option: str
    steps: list[PlanStep]
    p_met: dict[str, float]  # milestone or goal -> P(met) with the plan
    met: bool  # all milestones and goals reach their target


def _fixed(model: DecisionModel, name: str) -> float:
    var = next(v for v in model.variables if v.name == name)
    dist = var.distribution
    for key in ("value", "mode", "mean"):
        if getattr(dist, key, None) is not None:
            return float(getattr(dist, key))
    return float(dist.low + dist.high) / 2


def apply(
    model: DecisionModel, timeline: Timeline, events: Iterable[str] = (), moves: Mapping[str, float] = ()
) -> DecisionModel:
    """The model with the placed events and the moved markers (month variable -> month)."""
    timeline.check(model)
    by_name = {i.name: i for i in timeline.items}
    values: dict[str, float] = {}
    for name in events:
        item = by_name.get(name)
        if item is None or item.kind != "event":
            known = [i.name for i in timeline.items if i.kind == "event"]
            raise ValueError(f"no event {name!r}; events: {known}")
        values.update(item.place)
    values.update(dict(moves))
    return with_values(model, values)


def _run(model: DecisionModel, timeline: Timeline, rules: RuleSet | None, n: int, seed: int, inputs: bool):
    probes = {f"met:{i.name}": i.condition for i in timeline.goals()}
    probes |= {f"show:{i.name}": i.show for i in timeline.items if i.show}
    if inputs:
        probes |= {f"in:{v.name}": v.name for v in model.variables}
    return simulate_tensor(model, rules=rules, n_scenarios=n, seed=seed, probes=probes)


def _month(model: DecisionModel, item: TimelineItem) -> float:
    return _fixed(model, item.month) if isinstance(item.month, str) else float(item.month)


def _shares(run, goals: list[TimelineItem], j: int) -> list[float]:
    return [float((run.probes[f"met:{g.name}"][:, j] > 0.5).mean()) for g in goals]


def evaluate(
    model: DecisionModel,
    timeline: Timeline,
    rules: RuleSet | None = None,
    *,
    n_scenarios: int = 4000,
    seed: int = 7,
    events: Iterable[str] = (),
    moves: Mapping[str, float] = (),
) -> TimelineResult:
    """P(met), spreads and inputs in conflict for the timeline on ``model``."""
    model = apply(model, timeline, events, moves)
    run = _run(model, timeline, rules, n_scenarios, seed, inputs=True)
    items, conflicts = [], []
    for item in timeline.items:
        res = ItemResult(
            item.name, item.label, item.kind, _month(model, item), item.target if item.condition else None
        )
        for j, option in enumerate(run.option_names):
            if item.condition and item.applies(option):  # a lever milestone counts only for its option
                res.p_met[option] = float((run.probes[f"met:{item.name}"][:, j] > 0.5).mean())
            if item.show:
                p10, p50, p90, p80 = np.quantile(
                    run.probes[f"show:{item.name}"][:, j], [0.1, 0.5, 0.9, SUGGEST_P]
                )
                res.spread[option] = (float(p10), float(p50), float(p90), float(p80))
        items.append(res)
        for j, option in enumerate(run.option_names):
            if res.met(option) or not item.applies(option):
                continue
            met = (run.probes[f"met:{item.name}"][:, j] > 0.5).astype(float)
            if (
                met.std() == 0
            ):  # never met: a later forecast month is worse (the milestone cannot say how much)
                if not item.show:
                    continue
                met = -run.probes[f"show:{item.name}"][:, j]
                if met.std() == 0:
                    continue
            found = []
            for var in model.variables:
                x = run.probes[f"in:{var.name}"][:, j]
                if x.std() == 0:
                    continue
                r = float(np.corrcoef(x, met)[0, 1])
                if abs(r) >= MIN_CORRELATION:
                    found.append(Conflict(item.name, option, var.name, r))
            conflicts += sorted(found, key=lambda c: -abs(c.r))[:TOP_CONFLICTS]
    return TimelineResult(items, conflicts)


def suggest(result: TimelineResult, timeline: Timeline, option: str) -> dict[str, float]:
    """Proposed months (month variable -> month) for movable milestones with a forecast."""
    out = {}
    for item, res in zip(timeline.items, result.items, strict=True):
        if item.kind == "milestone" and isinstance(item.month, str) and option in res.spread:
            out[item.month] = math.ceil(res.spread[option][3] * 2) / 2
    return out


def solve_plan(
    model: DecisionModel,
    timeline: Timeline,
    option: str,
    rules: RuleSet | None = None,
    *,
    n_scenarios: int = 2000,
    seed: int = 7,
    events: Iterable[str] = (),
    moves: Mapping[str, float] = (),
) -> Plan:
    """Resources, amounts and months that meet the milestones of ``option`` (or come closest)."""
    model = apply(model, timeline, events, moves)
    goals = [g for g in timeline.goals() if g.applies(option)]
    j = [o.name for o in model.options].index(option)
    cache: dict[tuple, tuple[int, float, list[float]]] = {}

    def score(values: dict[str, float]) -> tuple[int, float, list[float]]:
        key = tuple(sorted(values.items()))
        if key not in cache:
            p = _shares(_run(with_values(model, values), timeline, rules, n_scenarios, seed, False), goals, j)
            met = sum(x >= g.target for x, g in zip(p, goals, strict=True))
            cache[key] = (met, sum(min(x, g.target) for x, g in zip(p, goals, strict=True)), p)
        return cache[key]

    state: dict[str, float] = {}
    steps = []
    for lever in timeline.levers:
        if lever.options is not None and option not in lever.options:
            continue
        if lever.needs and not state.get(lever.needs, _fixed(model, lever.needs)):
            continue
        grid = lever.grid()[::-1] if lever.prefer == "high" else lever.grid()
        best = None  # (met, soft, value, mode label, mode value)
        for label, mode_value in list(lever.modes.items()) if lever.mode else [(None, None)]:
            for v in grid:
                values = {**state, lever.variable: v} | ({lever.mode: mode_value} if lever.mode else {})
                met, soft, _ = score(values)
                if (
                    best is None
                    or (met, soft) > best[:2]
                    or ((met, soft) == best[:2] and lever.prefer == "low" and v < best[2])
                ):
                    best = (met, soft, v, label, mode_value)
        state[lever.variable] = best[2]
        if lever.mode:
            state[lever.mode] = best[4]
        steps.append(PlanStep(lever.variable, lever.label, best[3], _fixed(model, lever.variable), best[2]))
    met, _, p = score(state)
    return Plan(option, steps, {g.name: x for g, x in zip(goals, p, strict=True)}, met == len(goals))


__all__ = [
    "Conflict",
    "ItemResult",
    "Plan",
    "PlanStep",
    "TimelineResult",
    "apply",
    "evaluate",
    "solve_plan",
    "suggest",
]
