"""Story effort from history and code metrics, release forecast and probabilistic RICE.

Effort model (least squares on logs, Occam's razor by AIC):

    M0: log(hours) = b0 + b1 * log(points)
    M1: M0 + bug indicator
    M2: M1 + z(complexity), z(churn), z(coupling) of the story's component

A bigger model wins only if its AIC is lower by more than ``DELTA_AIC``. Each estimate
is log-normal with the full prediction variance s^2 * (1 + x (X'X)^-1 x'). A missing
story-point value uses the median points of the history and adds that spread to the
variance; a missing component gets the average component (z = 0).
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy.stats import norm

from variator.synthetic.company import derived_rng

DELTA_AIC = 2.0
FEATURES = ("ccn_avg", "commits", "fan_in")
Z_10 = float(norm.ppf(0.9))


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def actual_hours(worklogs: Sequence[Mapping[str, str]]) -> dict[str, float]:
    hours: dict[str, float] = defaultdict(float)
    for w in worklogs:
        hours[w["issue"]] += float(w["hours"])
    return dict(hours)


def component_z(metrics: Mapping[str, Mapping[str, float]]) -> dict[str, np.ndarray]:
    """Component -> z-scores of FEATURES over all components (0 if a feature does not vary)."""
    names = sorted(metrics)
    if not names:
        return {}
    m = np.array([[float(metrics[n].get(f, 0.0)) for f in FEATURES] for n in names])
    std = m.std(axis=0)
    z = np.where(std > 0, (m - m.mean(axis=0)) / np.where(std > 0, std, 1.0), 0.0)
    return {n: z[i] for i, n in enumerate(names)}


def resolve_component(issue: Mapping[str, str], known: Sequence[str]) -> tuple[str | None, str]:
    """T0: the component field, else a known component name in the summary."""
    value = (issue.get("component") or "").strip()
    if value in known:
        return value, "field"
    words = issue.get("summary", "").lower().replace("_", " ").split()
    hits = [c for c in known if c.lower() in words]
    if len(hits) == 1:
        return hits[0], "summary"
    return None, "unknown"


@dataclass
class EffortModel:
    name: str
    columns: list[str]
    beta: np.ndarray
    xtx_inv: np.ndarray
    sigma: float
    n: int
    aic: dict[str, float]
    r2: float
    log_points_median: float
    log_points_var: float
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "coefficients": {c: round(float(b), 4) for c, b in zip(self.columns, self.beta, strict=True)},
            "sigma_log": round(self.sigma, 4),
            "r2": round(self.r2, 3),
            "n": self.n,
            "aic": {k: round(v, 2) for k, v in self.aic.items()},
            "notes": self.notes,
        }


def _row(points: float, is_bug: bool, z: np.ndarray | None, columns: Sequence[str]) -> np.ndarray:
    values = {"intercept": 1.0, "log_points": math.log(points), "bug": float(is_bug)}
    if z is not None:
        values.update({f"z_{f}": float(v) for f, v in zip(FEATURES, z, strict=True)})
    return np.array([values.get(c, 0.0) for c in columns])


def fit_effort(
    issues: Sequence[Mapping[str, str]],
    hours: Mapping[str, float],
    metrics: Mapping[str, Mapping[str, float]] | None = None,
) -> EffortModel:
    """Fit M0/M1/M2 on finished issues with points and logged hours; keep the simplest by AIC."""
    zs = component_z(metrics or {})
    rows = []
    for issue in issues:
        h = hours.get(issue["key"], 0.0)
        if issue.get("status") != "Done" or h <= 0 or not issue.get("story_points"):
            continue
        comp, _ = resolve_component(issue, list(zs))
        rows.append(
            (float(issue["story_points"]), issue.get("type") == "Bug", zs.get(comp) if comp else None, h)
        )
    if len(rows) < 8:
        raise ValueError(f"only {len(rows)} finished issues with points and hours; need 8 or more")
    y = np.log([r[3] for r in rows])
    candidates = {
        "M0": ["intercept", "log_points"],
        "M1": ["intercept", "log_points", "bug"],
    }
    if zs:
        candidates["M2"] = ["intercept", "log_points", "bug", *(f"z_{f}" for f in FEATURES)]
    fits = {}
    for name, cols in candidates.items():
        x = np.array(
            [_row(p, b, z if z is not None else np.zeros(len(FEATURES)), cols) for p, b, z, _ in rows]
        )
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)
        rss = float(((y - x @ beta) ** 2).sum())
        n, k = len(y), len(cols)
        aic = n * math.log(max(rss / n, 1e-12)) + 2 * (k + 1)
        fits[name] = (cols, x, beta, rss, aic)
    chosen = "M0"
    for name in ("M1", "M2"):
        if name in fits and fits[name][4] < fits[chosen][4] - DELTA_AIC:
            chosen = name
    cols, x, beta, rss, _ = fits[chosen]
    n, k = x.shape
    sigma = math.sqrt(rss / max(n - k, 1))
    log_points = np.log([r[0] for r in rows])
    tss = float(((y - y.mean()) ** 2).sum())
    notes = [f"{chosen} kept: simpler models win unless AIC improves by > {DELTA_AIC}"]
    if not zs:
        notes.append("no code metrics: M2 not tried")
    return EffortModel(
        name=chosen,
        columns=cols,
        beta=beta,
        xtx_inv=np.linalg.pinv(x.T @ x),
        sigma=sigma,
        n=n,
        aic={name: f[4] for name, f in fits.items()},
        r2=1 - rss / tss if tss > 0 else 0.0,
        log_points_median=float(np.median(log_points)),
        log_points_var=float(log_points.var()),
        notes=notes,
    )


@dataclass
class StoryEstimate:
    key: str
    summary: str
    component: str | None
    component_source: str
    points: float | None
    mu: float  # mean of log(hours)
    sd: float  # std of log(hours)

    @property
    def p10(self) -> float:
        return math.exp(self.mu - Z_10 * self.sd)

    @property
    def p50(self) -> float:
        return math.exp(self.mu)

    @property
    def p90(self) -> float:
        return math.exp(self.mu + Z_10 * self.sd)

    @property
    def mean(self) -> float:
        return math.exp(self.mu + self.sd**2 / 2)

    @property
    def std(self) -> float:
        return self.mean * math.sqrt(math.exp(self.sd**2) - 1)

    def row(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "component": self.component or "",
            "component_source": self.component_source,
            "points": self.points if self.points is not None else "",
            "p10": round(self.p10, 1),
            "p50": round(self.p50, 1),
            "p90": round(self.p90, 1),
            "mean": round(self.mean, 1),
            "mu_log": round(self.mu, 4),
            "sd_log": round(self.sd, 4),
        }


def estimate(
    model: EffortModel,
    issues: Sequence[Mapping[str, str]],
    metrics: Mapping[str, Mapping[str, float]] | None = None,
) -> list[StoryEstimate]:
    """Estimates for all issues that are not Done."""
    zs = component_z(metrics or {})
    b_points = float(model.beta[model.columns.index("log_points")])
    out = []
    for issue in issues:
        if issue.get("status") == "Done":
            continue
        comp, source = resolve_component(issue, list(zs) or _components_in(issues))
        points = float(issue["story_points"]) if issue.get("story_points") else None
        log_p = math.log(points) if points else model.log_points_median
        x = _row(
            math.exp(log_p),
            issue.get("type") == "Bug",
            zs.get(comp) if comp else np.zeros(len(FEATURES)),
            model.columns,
        )
        var = model.sigma**2 * (1 + float(x @ model.xtx_inv @ x))
        if points is None:
            var += b_points**2 * model.log_points_var  # unknown size: add the spread of sizes
        out.append(
            StoryEstimate(
                issue["key"],
                issue.get("summary", ""),
                comp,
                source,
                points,
                float(x @ model.beta),
                math.sqrt(var),
            )
        )
    return out


def _components_in(issues: Sequence[Mapping[str, str]]) -> list[str]:
    return sorted({i["component"] for i in issues if i.get("component")})


def release_forecast(
    estimates: Sequence[StoryEstimate],
    capacity_hours: float,
    *,
    common_sd: float = 0.15,
    n: int = 20_000,
    seed: int = 0,
    max_sprints: int = 30,
) -> dict[str, Any]:
    """Monte Carlo of the total effort; a common log factor models team-wide over/under-runs."""
    if not estimates:
        return {"stories": 0}
    rng = derived_rng(seed, "it_release_forecast")
    mu = np.array([e.mu for e in estimates])
    sd = np.array([e.sd for e in estimates])
    common = rng.normal(0, common_sd, size=(n, 1))
    own = rng.normal(0, 1, size=(n, len(estimates))) * np.sqrt(np.maximum(sd**2 - common_sd**2, 0))
    total = np.exp(mu + common + own).sum(axis=1)
    sprints = np.ceil(total / capacity_hours)
    p = {k: float(np.quantile(total, q)) for k, q in (("p10", 0.1), ("p50", 0.5), ("p90", 0.9))}
    return {
        "stories": len(estimates),
        "total_hours": {k: round(v) for k, v in p.items()},
        "sprints": {k: int(math.ceil(v / capacity_hours)) for k, v in p.items()},
        "p_done_by_sprint": {
            int(k): round(float((sprints <= k).mean()), 3)
            for k in range(1, max_sprints + 1)
            if 0.02 < (sprints <= k).mean() < 0.999 or k == int(np.quantile(sprints, 0.9))
        },
    }


def rice_ranking(
    issues: Sequence[Mapping[str, str]],
    estimates: Sequence[StoryEstimate],
    *,
    top_k: int = 5,
    n: int = 20_000,
    seed: int = 0,
    hours_per_month: float = 140.0,
) -> list[dict[str, Any]]:
    """RICE = reach * impact * confidence / effort, with effort as a log-normal distribution.

    Returns per story: RICE p10/p50/p90, expected rank and P(rank <= top_k).
    """
    by_key = {i["key"]: i for i in issues}
    rows = [e for e in estimates if e.key in by_key]
    if not rows:
        return []
    rng = derived_rng(seed, "it_rice_ranking")
    value = np.array(
        [
            float(by_key[e.key]["reach"])
            * float(by_key[e.key]["impact"])
            * float(by_key[e.key]["confidence"])
            for e in rows
        ]
    )
    effort = np.exp(
        np.array([e.mu for e in rows]) + rng.normal(size=(n, len(rows))) * np.array([e.sd for e in rows])
    )
    rice = value / (effort / hours_per_month)  # (n, stories), effort in person-months
    ranks = (-rice).argsort(axis=1).argsort(axis=1) + 1
    q10, q50, q90 = np.quantile(rice, [0.1, 0.5, 0.9], axis=0)
    out = [
        {
            "key": e.key,
            "summary": e.summary,
            "rice_p10": round(float(q10[j]), 1),
            "rice_p50": round(float(q50[j]), 1),
            "rice_p90": round(float(q90[j]), 1),
            "expected_rank": round(float(ranks[:, j].mean()), 2),
            f"p_top_{top_k}": round(float((ranks[:, j] <= top_k).mean()), 3),
        }
        for j, e in enumerate(rows)
    ]
    return sorted(out, key=lambda r: r["expected_rank"])
