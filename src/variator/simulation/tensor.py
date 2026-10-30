"""Multi-criteria Monte Carlo: the scenario tensor ``X[s, o, k]``.

``simulate_tensor`` samples a ``DecisionModel`` one time, runs the rule set on each
option and evaluates every criterion formula. All options and criteria share the
same random numbers (see ``engine``), so a difference between two cells comes from
the decision, not from noise. Ranking, RAG bands and tunnel maps use this tensor
without new sampling or LLM calls.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from variator.domain import Criterion, DecisionModel, RagThresholds
from variator.expr import compile_with_derived
from variator.rules import RuleSet
from variator.rules.engine import summarize_masks
from variator.simulation.backend import BackendChoice, get_backend, to_host
from variator.simulation.copula import build_copula
from variator.simulation.distributions import quantile
from variator.simulation.engine import NonFiniteMetricError, _Inputs
from variator.simulation.sampling import Sampling, draw_uniforms

NO_FLAG = -1


def resolved_criteria(model: DecisionModel) -> list[Criterion]:
    """The criteria with ``rag_from`` limits and ``tie_from`` meaningful differences as numbers: the median of
    each variable.

    The pages (``model-simulator.html``, the TS case engine) resolve them the same way.
    """
    out = []
    for criterion in model.effective_criteria():
        data = criterion.model_dump()
        if criterion.rag_from is not None:
            green, red = (
                quantile(model.variable(name).distribution, 0.5)
                for name in (criterion.rag_from.green, criterion.rag_from.red)
            )
            data["rag"] = RagThresholds(green=green, red=red).model_dump()
        if criterion.tie_from is not None:
            data["tie_within"] = max(quantile(model.variable(criterion.tie_from).distribution, 0.5), 0.0)
        if criterion.rag_from is not None or criterion.tie_from is not None:
            criterion = Criterion.model_validate(data)
        out.append(criterion)
    return out


@dataclass
class TensorRun:
    """Per-scenario values of every option on every criterion (host NumPy arrays)."""

    criteria: list[Criterion]
    option_names: list[str]
    values: np.ndarray  # (n_scenarios, n_options, n_criteria), float64
    flags: np.ndarray  # same shape, int8 RAG color set by flag rules, NO_FLAG where none
    rule_hits: dict[str, list[float]]  # rule id -> share of scenarios where it fired, per option
    check_failures: dict[str, list[float]]  # check rule id -> failure share, per option
    seed: int | None
    backend: str
    elapsed_s: float
    notes: list[str] = field(default_factory=list)
    infeasible: np.ndarray | None = None  # (n_scenarios, n_options) bool: a check rule failed
    probes: dict[str, np.ndarray] = field(default_factory=dict)  # name -> (n_scenarios, n_options)

    @property
    def n_scenarios(self) -> int:
        return int(self.values.shape[0])

    def infeasible_mask(self) -> np.ndarray:
        """(S, O) bool; all False for runs without masks (stored before 2026-10-26)."""
        if self.infeasible is None:
            return np.zeros(self.values.shape[:2], dtype=bool)
        return self.infeasible

    def feasible_share(self) -> np.ndarray:
        """Share of scenarios per option where no check rule fails (O,)."""
        return 1.0 - self.infeasible_mask().mean(axis=0)

    @property
    def higher_is_better(self) -> np.ndarray:
        return np.array([c.higher_is_better for c in self.criteria], dtype=bool)

    def criterion_index(self, name: str) -> int:
        return next(i for i, c in enumerate(self.criteria) if c.name == name)

    def option_index(self, name: str) -> int:
        return self.option_names.index(name)


def simulate_tensor(
    model: DecisionModel,
    *,
    rules: RuleSet | None = None,
    n_scenarios: int = 10_000,
    seed: int | None = None,
    backend: BackendChoice = "auto",
    tile: int = 1,
    pins: Mapping[str, Any] | None = None,
    sampling: Sampling = "random",
    probes: Mapping[str, str] | None = None,
) -> TensorRun:
    """Sample ``model`` and evaluate all criteria of all options.

    ``probes`` (name -> formula over inputs, derived values and criterion names) gives
    per-scenario values in ``TensorRun.probes``; the timeline uses them for milestones.

    ``sampling="sobol"`` uses scrambled Sobol points: the same accuracy with 4 to 8
    times fewer scenarios (use a power of 2 for ``n_scenarios``).

    ``tile`` repeats the same ``n_scenarios`` random rows ``tile`` times; with ``pins``
    (variable -> array of length ``n_scenarios * tile``, values before risk events) this
    gives a grid where each cell sees the same samples of the other variables. The
    tunnel map uses this. Raises ``NonFiniteMetricError`` for NaN/inf results.
    """
    if n_scenarios < 1 or tile < 1:
        raise ValueError("n_scenarios and tile must be >= 1")
    start = time.perf_counter()
    rules = rules or RuleSet()
    total = n_scenarios * tile
    be = get_backend(backend, n_scenarios=total)
    xp = be.xp
    criteria = resolved_criteria(model)
    derived = model.derived_map()
    copula = build_copula([v.name for v in model.variables], model.correlations)
    notes = list(copula.notes) if copula else []

    uniforms, risk_uniforms, sampling_notes = draw_uniforms(
        be, seed, n_scenarios, len(model.variables), 2 * len(model.risks), sampling
    )
    notes.extend(sampling_notes)
    if copula is not None:
        uniforms = copula.apply(uniforms, be)
    if tile > 1:
        uniforms = xp.tile(uniforms, (tile, 1))
        risk_uniforms = xp.tile(risk_uniforms, (tile, 1))
    inputs = _Inputs(model, uniforms, risk_uniforms, be)
    pinned = {name: be.asarray(np.asarray(values, dtype=np.float64)) for name, values in (pins or {}).items()}
    unknown = set(pinned) - set(inputs.column)
    if unknown:
        raise ValueError(f"pins reference unknown variables {sorted(unknown)}")

    values = xp.empty((total, len(model.options), len(criteria)), dtype=xp.float64)
    flags = xp.full(values.shape, NO_FLAG, dtype=xp.int8)
    infeasible = xp.zeros((total, len(model.options)), dtype=bool)
    rule_hits: dict[str, list[float]] = {r.id: [0.0] * len(model.options) for r in rules.rules}
    check_failures: dict[str, list[float]] = {}
    bad: dict[str, int] = {}
    probe_values: dict[str, Any] = {}
    everything = slice(None)
    for j, option in enumerate(model.options):
        env = {}
        for name in inputs.column:
            base = pinned[name] if name in pinned else inputs.base_values(option, name)
            env[name] = inputs.apply_risks(option, name, base, everything)
        outcome = rules.apply(env, option.name, xp, total)
        for rule_id, share in summarize_masks(outcome.fired).items():
            rule_hits[rule_id][j] = share
        for rule_id, share in summarize_masks(outcome.failed).items():
            check_failures.setdefault(rule_id, [0.0] * len(model.options))[j] = share
        for mask in outcome.failed.values():  # a scalar mask (fixed inputs) applies to every scenario
            infeasible[:, j] |= xp.broadcast_to(xp.asarray(mask, dtype=bool), (total,))
        for k, (criterion, text) in enumerate(model.option_formulas(option).items()):
            column = compile_with_derived(text, derived).evaluate(outcome.env, xp, size=total)
            values[:, j, k] = column
            invalid = int(to_host((~xp.isfinite(column)).sum()))
            if invalid:
                bad[f"{option.name}/{criterion}"] = invalid
            for target, colors in outcome.flags.items():
                if target is None or target == criterion:
                    flags[:, j, k] = xp.maximum(flags[:, j, k], colors)
        if probes:
            env = {**outcome.env, **{c.name: values[:, j, k] for k, c in enumerate(criteria)}}
            for name, text in probes.items():
                column = compile_with_derived(text, derived).evaluate(env, xp, size=total)
                probe_values.setdefault(name, xp.zeros((total, len(model.options)), dtype=xp.float64))
                probe_values[name][:, j] = xp.broadcast_to(xp.asarray(column, dtype=xp.float64), (total,))
    if bad:
        raise NonFiniteMetricError(
            "criterion values are NaN/inf in some scenarios; guard the formulas: "
            + ", ".join(f"{key}: {count}/{total}" for key, count in bad.items()),
            bad,
        )
    return TensorRun(
        criteria=criteria,
        option_names=[o.name for o in model.options],
        values=to_host(values),
        flags=to_host(flags),
        rule_hits=rule_hits,
        check_failures=check_failures,
        seed=seed,
        backend=be.name,
        elapsed_s=time.perf_counter() - start,
        notes=notes,
        infeasible=to_host(infeasible),
        probes={name: to_host(v) for name, v in probe_values.items()},
    )
