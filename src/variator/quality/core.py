"""Data quality score and Occam's razor simplification.

``Q`` in [0, 1] says how much a variable estimate can be trusted. When ``Q`` is below
the threshold, the simplifier keeps only the structure that the data supports: a
three-point distribution instead of a fitted shape, no unsupported correlations, no
drivers with a very small effect. Each change is written to ``notes``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np
from pydantic import BaseModel, Field

from variator.domain import DecisionModel, Distribution, DistributionKind
from variator.simulation.distributions import quantile

DEFAULT_THRESHOLD = 0.5
MIN_SWING_SHARE = 0.02  # drivers below 2% of the total tornado swing are frozen
DELTA_AIC = 2.0  # a more complex trend model must be better by more than this
FULL_SAMPLE = 30  # points for a full sample-size score
HALF_LIFE_DAYS = 365.0
ORIGIN_SCORE = {"data": 1.0, "web": 0.7, "llm": 0.5, "assumption": 0.4}


class QualityScore(BaseModel):
    q: float = Field(ge=0, le=1)
    parts: dict[str, float]
    issues: list[str] = Field(default_factory=list)


def score_series(
    values: Sequence[float | None] | np.ndarray,
    *,
    origin: str = "data",
    age_days: float | None = None,
) -> QualityScore:
    """Quality of one input column from its size, gaps, age, source and stability.

    Q is the weighted geometric mean of the parts, so one very bad part pulls Q down.
    """
    raw = np.asarray([np.nan if v is None else v for v in values], dtype=np.float64)
    finite = raw[np.isfinite(raw)]
    n, total = finite.size, max(raw.size, 1)
    issues: list[str] = []
    parts = {
        "size": min(1.0, n / FULL_SAMPLE),
        "completeness": n / total,
        "freshness": 1.0 if age_days is None else math.exp(-math.log(2) * max(age_days, 0) / HALF_LIFE_DAYS),
        "origin": ORIGIN_SCORE.get(origin, 0.4),
        "stability": _stability(finite),
    }
    if n < 5:
        issues.append(f"only {n} values")
    if parts["completeness"] < 0.8:
        issues.append(f"{1 - parts['completeness']:.0%} of values missing")
    if parts["stability"] < 0.5:
        issues.append("level shift between the first and the second half")
    weights = {"size": 2.0, "completeness": 1.0, "freshness": 1.0, "origin": 1.5, "stability": 1.0}
    log_q = sum(weights[k] * math.log(max(v, 1e-6)) for k, v in parts.items()) / sum(weights.values())
    return QualityScore(
        q=round(math.exp(log_q), 4), parts={k: round(v, 4) for k, v in parts.items()}, issues=issues
    )


def _stability(x: np.ndarray) -> float:
    """1 / (1 + shift of the mean between halves in units of the pooled std)."""
    if x.size < 8:
        return 0.6  # too short to test; neutral-low
    a, b = np.array_split(x, 2)
    pooled = float(np.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2))
    if pooled == 0:
        return 1.0 if a.mean() == b.mean() else 0.3
    return 1.0 / (1.0 + abs(float(a.mean() - b.mean())) / pooled)


class TrendFit(BaseModel):
    model: str = Field(description="constant, linear or seasonal")
    aic: dict[str, float]
    params: dict[str, float]
    note: str


def select_trend(y: Sequence[float], *, period: int = 12) -> TrendFit:
    """Choose the simplest trend model by AIC (least squares, Gaussian errors).

    Models from simple to complex: constant, linear, linear + seasonal dummies (needs
    two full periods). A complex model wins only if its AIC is lower by ``DELTA_AIC``.
    """
    arr = np.asarray(y, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < 3:
        mean = float(arr.mean()) if n else 0.0
        return TrendFit(model="constant", aic={}, params={"level": mean}, note="fewer than 3 points")
    t = np.arange(n, dtype=np.float64)
    designs = {"constant": np.ones((n, 1)), "linear": np.column_stack([np.ones(n), t])}
    if n >= 2 * period:
        dummies = np.eye(period)[np.arange(n) % period][:, 1:]
        designs["seasonal"] = np.column_stack([np.ones(n), t, dummies])
    aic: dict[str, float] = {}
    coefs: dict[str, np.ndarray] = {}
    for name, design in designs.items():
        beta, *_ = np.linalg.lstsq(design, arr, rcond=None)
        rss = float(((arr - design @ beta) ** 2).sum())
        k = design.shape[1] + 1
        aic[name] = n * math.log(max(rss / n, 1e-12)) + 2 * k
        coefs[name] = beta
    chosen = "constant"
    for name in ("linear", "seasonal"):
        if name in aic and aic[name] < aic[chosen] - DELTA_AIC:
            chosen = name
    beta = coefs[chosen]
    params = {"level": float(beta[0])}
    if chosen != "constant":
        params["slope"] = float(beta[1])
    return TrendFit(
        model=chosen,
        aic={k: round(v, 3) for k, v in aic.items()},
        params=params,
        note=f"{chosen} model kept (simpler models are preferred unless AIC improves by > {DELTA_AIC})",
    )


def fit_distribution(
    values: Sequence[float], q: float, *, threshold: float = DEFAULT_THRESHOLD
) -> Distribution:
    """A distribution for observed values; the simplest shape when the quality is low.

    Low quality or few points: PERT(min, median, max), or uniform for 2 points.
    Good quality: normal if roughly symmetric, lognormal if positive and right-skewed.
    """
    x = np.asarray(values, dtype=np.float64)
    x = x[np.isfinite(x)]
    if x.size == 0:
        raise ValueError("no finite values")
    lo, mid, hi = float(x.min()), float(np.median(x)), float(x.max())
    if x.size == 1 or lo == hi:
        return Distribution.fixed(mid)
    if x.size == 2:
        return Distribution(kind=DistributionKind.UNIFORM, low=lo, high=hi)
    if q < threshold or x.size < FULL_SAMPLE:
        return Distribution(kind=DistributionKind.PERT, low=lo, mode=mid, high=hi)
    mean, std = float(x.mean()), float(x.std(ddof=1))
    skew = float(((x - mean) ** 3).mean() / std**3) if std > 0 else 0.0
    if lo > 0 and skew > 1:
        return Distribution(kind=DistributionKind.LOGNORMAL, mean=mean, std=std)
    return Distribution(kind=DistributionKind.NORMAL, mean=mean, std=std)


def swing_shares(model: DecisionModel, *, n_scenarios: int = 2_000, seed: int = 0) -> dict[str, float]:
    """Variable -> largest share of the tornado swing over all criteria and options.

    One single-metric run per criterion (the option formula is swapped for the
    criterion formula). A variable that no formula uses gets 0.
    """
    from variator.simulation import simulate

    shares = {v.name: 0.0 for v in model.variables}
    for criterion in model.effective_criteria():
        data = model.model_dump(mode="python")
        for option, original in zip(data["options"], model.options, strict=True):
            option["formula"] = model.option_formulas(original)[criterion.name]
            option["metrics"] = {}
        data["criteria"] = []
        single = DecisionModel.model_validate(data)
        run = simulate(single, n_scenarios=n_scenarios, seed=seed, sensitivity=True, top_drivers=None)
        result = run.result
        by_option: dict[str, dict[str, float]] = {}
        for s in result.sensitivities:
            by_option.setdefault(s.option, {})[s.variable] = s.swing
        for swings in by_option.values():
            total = sum(swings.values())
            for name, swing in swings.items():
                if total > 0:
                    shares[name] = max(shares[name], swing / total)
    return shares


class Simplification(BaseModel):
    model: DecisionModel
    notes: list[str]


def simplify(
    model: DecisionModel,
    quality: Mapping[str, float],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    swing_shares: Mapping[str, float] | None = None,
    keep: Sequence[str] = (),
) -> Simplification:
    """Apply Occam's razor to ``model``.

    ``quality``: variable -> Q (missing variables count as assumptions, Q = 0.4).
    ``swing_shares``: variable -> largest share of the total tornado swing over all
    criteria and options (see ``swing_shares``); drivers below ``MIN_SWING_SHARE`` are
    frozen at their median. ``keep`` lists variables that rules or the user need as
    random variables.
    """
    notes: list[str] = []
    data = model.model_dump(mode="python")
    low_q = {v.name for v in model.variables if quality.get(v.name, ORIGIN_SCORE["assumption"]) < threshold}
    keep_set = set(keep)
    for var in data["variables"]:
        name, dist = var["name"], Distribution.model_validate(var["distribution"])
        if name in low_q and dist.kind in (DistributionKind.NORMAL, DistributionKind.LOGNORMAL):
            low, mode, high = (quantile(dist, p) for p in (0.05, 0.5, 0.95))
            var["distribution"] = Distribution(
                kind=DistributionKind.PERT, low=low, mode=mode, high=high
            ).model_dump()
            notes.append(f"{name}: Q < {threshold}, {dist.kind} replaced by PERT(p5, p50, p95)")
    if swing_shares:
        for var in data["variables"]:
            name = var["name"]
            if name in keep_set or name not in swing_shares:
                continue
            if swing_shares[name] < MIN_SWING_SHARE:
                dist = Distribution.model_validate(var["distribution"])
                if dist.kind != DistributionKind.FIXED:
                    var["distribution"] = Distribution.fixed(quantile(dist, 0.5)).model_dump()
                    notes.append(
                        f"{name}: effect < {MIN_SWING_SHARE:.0%} of the total swing, frozen at its median"
                    )
    kept = []
    for corr in data["correlations"]:
        if corr["a"] in low_q or corr["b"] in low_q:
            notes.append(f"correlation {corr['a']}~{corr['b']} removed: not supported by the data quality")
        else:
            kept.append(corr)
    data["correlations"] = kept
    return Simplification(model=DecisionModel.model_validate(data), notes=notes)
