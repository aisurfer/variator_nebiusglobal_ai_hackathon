"""Case B: company_model inputs calibrated to the state at T (QA m7), from config/company.facts.json.

The facts hold the metric catalogue at T (``metricsAtT``). Two kinds of inputs:
- direct: the input is the metric (ARR, cash, receivables days, team load, deploys a week);
- solved: the input is set so that the model at its input modes, without shocks and levers, gives the
  metric at T (time to restore, incidents, latency, NPS by support quality, churn by the base churn).
An uncertain input keeps its spread: a scale input (a level) scales low, mode and high; a shift input (a
share) moves them. The forecast still differs from T: shocks, the uncertainty and the links act over the year.
Every other input stays a model assumption; its help says so.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from variator.domain import DecisionModel, Distribution, DistributionKind
from variator.expr import compile_with_derived

# input -> (kind, metric at T, model value that must match it); kind: fixed, scale or shift
CALIBRATION = [
    ("arr0", "fixed", "rev.arr", None),
    ("cash0", "fixed", "fin.cash", None),
    ("ar_days", "shift", "fin.dso", "ar_days"),
    ("team_load", "shift", "ops.team_load", "team_load"),
    ("deploy_freq", "scale", "ops.deploys", "deploy_freq"),
    ("mttr_h", "scale", "ops.mttr", "mttr_eff"),
    ("incidents_base", "scale", "ops.incidents", "incidents"),
    ("latency_base", "scale", "ops.latency", "latency"),
    ("support_quality", "shift", "cust.nps", "nps"),
    ("base_churn", "shift", "rev.churn", "churn"),
]
SHOCK_SOURCES = ("assumption: external", "assumption: market", "assumption: people", "assumption: clients")


def _centre(d: Distribution) -> float:
    if d.kind == DistributionKind.FIXED:
        return float(d.value)
    if d.kind in (DistributionKind.PERT, DistributionKind.TRIANGULAR):
        return float(d.mode)
    if d.kind in (DistributionKind.NORMAL, DistributionKind.LOGNORMAL):
        return float(d.mean)
    return float((d.low + d.high) / 2)


def _moved(d: Distribution, kind: str, x: float) -> Distribution:
    """``d`` with its centre at ``x``: fixed, scaled (levels) or shifted (shares)."""
    if kind == "fixed" or d.kind == DistributionKind.FIXED:
        return Distribution.fixed(x)
    c = _centre(d)
    data = d.model_dump()
    for key in ("low", "mode", "high", "mean"):
        if data.get(key) is not None:
            data[key] = data[key] * x / c if kind == "scale" else data[key] + x - c
    if data.get("std") is not None and kind == "scale":
        data["std"] = data["std"] * x / c
    return Distribution.model_validate(data)


def _at_modes(model: DecisionModel, dists: dict[str, Distribution], name: str) -> float:
    """A derived value (or an input) of the plan: every input at its centre, shocks and levers off."""
    env = {}
    for v in model.variables:
        d = dists.get(v.name, v.distribution)
        env[v.name] = np.full(1, _centre(d))
    return float(compile_with_derived(name, model.derived_map()).evaluate(env)[0])


def calibrate(
    model: DecisionModel, metrics: dict[str, float]
) -> tuple[dict[str, Distribution], dict[str, str]]:
    """(input -> calibrated distribution, input -> source text) in the order of ``CALIBRATION``."""
    dists: dict[str, Distribution] = {}
    sources: dict[str, str] = {}
    for name, kind, metric, output in CALIBRATION:
        target = float(metrics[metric])
        d = model.variable(name).distribution
        if output is None or output == name:
            dists[name] = _moved(d, kind, target)
            how = "the value"
        else:
            y0 = _at_modes(model, dists, output)
            c = _centre(dists.get(name, d))
            if kind == "scale":  # the output is proportional to the input
                x = c * target / y0
            else:  # linear: solve with the slope at the centre
                step = 0.01 * max(abs(c), 1e-3)
                probe = {**dists, name: _moved(d, "shift", c + step)}
                slope = (_at_modes(model, probe, output) - y0) / step
                x = c + (target - y0) / slope
            dists[name] = _moved(d, kind, x)
            how = f"the model's {output} at T"
        sources[name] = f"history at T: {metric} = {target:g} (calibrated: {how} equals it)"
    return dists, sources


def check(model: DecisionModel, dists: dict[str, Distribution]) -> dict[str, float]:
    """The plan at the input modes after the calibration, for the outputs with a metric at T."""
    out: dict[str, Any] = {}
    for _name, _kind, metric, output in CALIBRATION:
        if output:
            out[metric] = _at_modes(model, dists, output)
    out["ops.sla"] = _at_modes(model, dists, "sla")
    return out
