"""Monte Carlo engine: sample a ``DecisionModel`` and summarize each option.

Common random numbers: every scenario draws one uniform per variable and two per risk
(fires?, magnitude), and all options reuse them. An option's override distribution is
pushed through the same uniform, so differences between options reflect the decision,
not sampling noise.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from variator.domain import (
    DecisionModel,
    DecisionOption,
    Distribution,
    HistogramBin,
    OptionStats,
    RiskEffect,
    Sensitivity,
    SimulationResult,
)
from variator.expr import CompiledFormula, compile_with_derived
from variator.simulation.backend import Backend, BackendChoice, get_backend, to_host
from variator.simulation.copula import build_copula
from variator.simulation.distributions import is_constant, ppf, quantile
from variator.simulation.sampling import Sampling, draw_uniforms

HISTOGRAM_BINS = 30
TAIL_SHARE = 0.05  # VaR/CVaR level: worst 5% of scenarios
SENSITIVITY_SAMPLES = 2_000  # scenarios reused for each tornado evaluation
SENSITIVITY_QUANTILES = (0.1, 0.9)
SENSITIVITY_TOP_DRIVERS = 10  # tornado entries kept per option
_PERCENTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


class NonFiniteMetricError(ValueError):
    """Some scenarios produced NaN or inf for an option's metric.

    ``counts`` maps option name -> number of non-finite scenarios. The message carries an
    example of offending inputs so the model author (or the LLM) can guard the formula.
    """

    def __init__(self, message: str, counts: dict[str, int]):
        super().__init__(message)
        self.counts = counts


@dataclass
class SimulationRun:
    """Summary plus raw per-scenario metric values (rows: scenarios, columns: options)."""

    result: SimulationResult
    option_names: list[str]
    scenarios: np.ndarray  # shape (n_scenarios, n_options), host NumPy array
    notes: list[str] = field(default_factory=list)  # repairs and skipped tornado entries


def simulate(
    model: DecisionModel,
    *,
    n_scenarios: int = 10_000,
    seed: int | None = None,
    backend: BackendChoice = "auto",
    sensitivity: bool = True,
    sampling: Sampling = "random",
    top_drivers: int | None = SENSITIVITY_TOP_DRIVERS,
) -> SimulationRun:
    """Run a Monte Carlo simulation of every option in ``model``.

    ``top_drivers`` limits the tornado entries per option; ``None`` keeps all of them.

    Raises ``NonFiniteMetricError`` if any scenario yields NaN/inf (e.g. ``log`` of a
    negative value), ``FormulaError`` for invalid formulas and ``BackendUnavailableError``
    when ``backend="cupy"`` cannot run. The same ``seed`` reproduces results on the same
    backend; NumPy and CuPy streams differ.
    """
    if n_scenarios < 1:
        raise ValueError("n_scenarios must be >= 1")
    start = time.perf_counter()
    be = get_backend(backend, n_scenarios=n_scenarios)
    xp = be.xp
    formulas = [compile_with_derived(option.formula, model.derived_map()) for option in model.options]
    copula = build_copula([v.name for v in model.variables], model.correlations)
    notes = list(copula.notes) if copula else []

    uniforms, risk_uniforms, sampling_notes = draw_uniforms(
        be, seed, n_scenarios, len(model.variables), 2 * len(model.risks), sampling
    )
    notes.extend(sampling_notes)
    if copula is not None:
        uniforms = copula.apply(uniforms, be)
    inputs = _Inputs(model, uniforms, risk_uniforms, be)

    scenarios = xp.empty((n_scenarios, len(model.options)), dtype=xp.float64)
    sensitivities: list[Sensitivity] = []
    bad: dict[str, int] = {}
    examples: list[str] = []
    for j, (option, formula) in enumerate(zip(model.options, formulas, strict=True)):
        env = inputs.option_env(option, formula.names)
        scenarios[:, j] = formula.evaluate(env, xp, size=n_scenarios)
        invalid = ~xp.isfinite(scenarios[:, j])
        count = int(to_host(invalid.sum()))
        if count:
            bad[option.name] = count
            row = int(to_host(xp.argmax(invalid)))
            values = {name: float(to_host(env[name][row])) for name in sorted(formula.names)}
            examples.append(f"{option.name!r}: {count}/{n_scenarios} scenarios, e.g. inputs {values}")
        elif sensitivity:
            drivers, skipped = _tornado(inputs, option, formula, env, top_drivers)
            sensitivities.extend(drivers)
            notes.extend(skipped)
    if bad:
        raise NonFiniteMetricError(
            "metric is NaN/inf in some scenarios; guard the formula "
            "(e.g. log(maximum(x, 1e-9)), x / maximum(y, 1)): " + "; ".join(examples),
            bad,
        )

    higher = model.metric.higher_is_better
    prob_best = to_host(_prob_best(scenarios, higher, xp))
    stats = [
        _option_stats(option.name, scenarios[:, j], float(prob_best[j]), higher, be)
        for j, option in enumerate(model.options)
    ]
    hit_rates = to_host(inputs.fired.astype(xp.float64).mean(axis=0))
    host_scenarios = to_host(scenarios)
    result = SimulationResult(
        metric=model.metric,
        n_scenarios=n_scenarios,
        seed=seed,
        backend=be.name,
        elapsed_s=time.perf_counter() - start,
        options=stats,
        sensitivities=sensitivities,
        risk_hit_rates={risk.name: float(rate) for risk, rate in zip(model.risks, hit_rates, strict=True)},
        notes=notes,
    )
    return SimulationRun(result, [o.name for o in model.options], host_scenarios, notes)


class _Inputs:
    """Variable values per scenario, built lazily from shared uniforms."""

    def __init__(self, model: DecisionModel, uniforms: Any, risk_uniforms: Any, backend: Backend):
        self.model = model
        self.backend = backend
        self.uniforms = uniforms
        self.column = {v.name: i for i, v in enumerate(model.variables)}
        n_risks = len(model.risks)
        probabilities = backend.asarray(np.array([r.probability for r in model.risks], dtype=np.float64))
        self.fired = risk_uniforms[:, :n_risks] < probabilities
        self._risk_u = risk_uniforms[:, n_risks:]
        self._base: dict[str, Any] = {}
        self._magnitude: dict[int, Any] = {}

    def distribution(self, option: DecisionOption, name: str) -> Distribution:
        return option.overrides.get(name, self.model.variables[self.column[name]].distribution)

    def base_values(self, option: DecisionOption, name: str) -> Any:
        """Values before risk events; overrides reuse the variable's uniform column."""
        column = self.uniforms[:, self.column[name]]
        if name in option.overrides:
            return ppf(option.overrides[name], column, self.backend)
        if name not in self._base:
            self._base[name] = ppf(self.model.variables[self.column[name]].distribution, column, self.backend)
        return self._base[name]

    def apply_risks(self, option: DecisionOption, name: str, values: Any, rows: slice) -> Any:
        """Shift ``values`` (already restricted to ``rows``) by the risks hitting ``name``."""
        xp = self.backend.xp
        for i, risk in enumerate(self.model.risks):
            if risk.variable != name or (risk.options is not None and option.name not in risk.options):
                continue
            shift = xp.where(self.fired[rows, i], self._magnitudes(i)[rows], 0.0)
            values = values * (1 + shift) if risk.effect == RiskEffect.MULTIPLY else values + shift
        return values

    def option_env(self, option: DecisionOption, names: frozenset[str]) -> dict[str, Any]:
        everything = slice(None)
        return {n: self.apply_risks(option, n, self.base_values(option, n), everything) for n in names}

    def _magnitudes(self, i: int) -> Any:
        """Magnitude per scenario, computed only where the risk fired (0 elsewhere).

        The inverse CDF (betaincinv for PERT) dominates CPU time, so skipping the
        scenarios where the event did not happen matters.
        """
        if i not in self._magnitude:
            xp = self.backend.xp
            fired = self.fired[:, i]
            values = xp.zeros(fired.shape[0], dtype=xp.float64)
            values[fired] = ppf(self.model.risks[i].magnitude, self._risk_u[:, i][fired], self.backend)
            self._magnitude[i] = values
        return self._magnitude[i]


def _tornado(
    inputs: _Inputs,
    option: DecisionOption,
    formula: CompiledFormula,
    env: dict[str, Any],
    top_drivers: int | None = SENSITIVITY_TOP_DRIVERS,
) -> tuple[list[Sensitivity], list[str]]:
    """Mean metric with each variable pinned at its p10 and p90, other inputs as sampled.

    Pins replace the variable's pre-risk value; risk events on it still apply. Only the
    first ``SENSITIVITY_SAMPLES`` scenarios are used, identical for every pin.
    """
    xp = inputs.backend.xp
    m = min(SENSITIVITY_SAMPLES, next(iter(env.values())).shape[0]) if env else 0
    rows = slice(0, m)
    sub_env = {name: values[rows] for name, values in env.items()}
    entries: list[Sensitivity] = []
    skipped: list[str] = []
    for name in sorted(formula.names, key=inputs.column.__getitem__):
        dist = inputs.distribution(option, name)
        low, high = (quantile(dist, q) for q in SENSITIVITY_QUANTILES)
        if is_constant(dist) or low == high:
            continue
        means = []
        for pin in (low, high):
            pinned = dict(sub_env)
            pinned[name] = inputs.apply_risks(option, name, xp.full(m, pin, dtype=xp.float64), rows)
            means.append(formula.evaluate(pinned, xp, size=m).mean())
        at_low, at_high = (float(v) for v in to_host(xp.stack(means)))
        if not (math.isfinite(at_low) and math.isfinite(at_high)):
            skipped.append(f"tornado entry {option.name}/{name} skipped: metric not finite at p10/p90")
            continue
        entries.append(
            Sensitivity(
                option=option.name,
                variable=name,
                metric_at_low=at_low,
                metric_at_high=at_high,
                swing=abs(at_high - at_low),
            )
        )
    entries.sort(key=lambda s: s.swing, reverse=True)
    return entries[:top_drivers], skipped


def _prob_best(scenarios: Any, higher_is_better: bool, xp: Any) -> Any:
    """Share of scenarios in which each option is best; ties split the scenario equally."""
    best = scenarios.max(axis=1, keepdims=True) if higher_is_better else scenarios.min(axis=1, keepdims=True)
    is_best = (scenarios == best).astype(xp.float64)
    return (is_best / is_best.sum(axis=1, keepdims=True)).mean(axis=0)


def _option_stats(name: str, x: Any, prob_best: float, higher_is_better: bool, be: Backend) -> OptionStats:
    """Summary statistics of one option's metric; the bad tail depends on the metric direction.

    higher_is_better=True:  prob_loss = P(x < 0), var_95 = p5,  cvar_95 = mean of lowest 5%.
    higher_is_better=False: prob_loss = P(x > 0), var_95 = p95, cvar_95 = mean of highest 5%.
    """
    xp = be.xp
    n = x.shape[0]
    ordered = xp.sort(x)
    pct = xp.quantile(ordered, xp.asarray(_PERCENTILES))
    k = max(1, math.ceil(TAIL_SHARE * n))
    if higher_is_better:
        loss, var, tail = (x < 0).astype(xp.float64).mean(), pct[0], ordered[:k]
    else:
        loss, var, tail = (x > 0).astype(xp.float64).mean(), pct[-1], ordered[-k:]
    std = x.std(ddof=1) if n > 1 else xp.zeros(())
    packed = xp.stack([x.mean(), std, loss, var, tail.mean(), ordered[0], ordered[-1]])
    mean, std_v, loss_v, var_v, cvar_v, lo, hi = (float(v) for v in to_host(packed))
    p5, p25, p50, p75, p95 = (float(v) for v in to_host(pct))
    if lo == hi:
        lo, hi = lo - 0.5, hi + 0.5
    edges = xp.linspace(lo, hi, HISTOGRAM_BINS + 1)
    counts, _ = xp.histogram(x, bins=edges)
    host_edges, host_counts = to_host(edges), to_host(counts)
    return OptionStats(
        option=name,
        mean=mean,
        std=std_v,
        p5=p5,
        p25=p25,
        p50=p50,
        p75=p75,
        p95=p95,
        prob_loss=loss_v,
        var_95=var_v,
        cvar_95=cvar_v,
        prob_best=prob_best,
        histogram=[
            HistogramBin(left=float(host_edges[i]), right=float(host_edges[i + 1]), count=int(host_counts[i]))
            for i in range(HISTOGRAM_BINS)
        ],
    )
