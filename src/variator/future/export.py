"""From a month result to the page: fans, the risk calendar, the metric cube, the ranking and ``month.json``.

The cube ``[scenario, fork, metric, month]`` goes through ``metrics.cube.to_tensor_run``, so ``mcda.rank``,
``mcda.best_path`` and ``rag.rag_report`` work on it without change. ``month.json`` has the schema of
``PreviewFuture`` (workspace/src/variator/engine/types.ts) with ``preview: false`` and ``source: "actors"``.
"""

# ruff: noqa: E501 - metric and calendar texts, one per line

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from variator.domain.criteria import RagThresholds
from variator.future.company import Result
from variator.future.params import Param
from variator.mcda import best_path, rank
from variator.metrics.cube import CriterionSpec, MetricCube, MetricInfo, to_tensor_run
from variator.orgsim import risk
from variator.rag.core import rag_report

ENGINE = "variator.future/1"
QUANTILES = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)
QKEYS = ("p5", "p10", "p25", "p50", "p75", "p90", "p95")
# id, label, unit, higher, description, decimals (None = whole numbers)
MONTH_METRICS: list[tuple[str, str, str, bool, str, int | None]] = [
    ("cash", "Cash", "USD", True, "Cash balance at the end of each month.", None),
    (
        "churn",
        "Churn, year to date",
        "share",
        False,
        "ARR of the clients that gave notice since T, and downgrades, over the ARR at T.",
        6,
    ),
    ("sla", "Availability, year to date", "share", True, "Client-weighted availability since T.", 6),
    ("nps", "NPS", "points", True, "Net promoter score at the end of each month.", 3),
    (
        "runway",
        "Runway",
        "months",
        True,
        "Months of cash left at the burn rate of the last 3 months (max 60).",
        3,
    ),
    ("arr", "ARR", "USD", True, "Annual recurring revenue in force at the end of the month.", None),
    ("revenue", "Revenue in the month", "USD", True, "Revenue of the contracts in force in the month.", None),
    (
        "cost",
        "Cost in the month",
        "USD",
        False,
        "Payroll with the payroll tax, vendors, cloud, GPU reserve and compliance.",
        None,
    ),
    (
        "infra_cost",
        "Infrastructure cost, annual rate",
        "USD",
        False,
        "Cloud and the GPU reserve at the rate of the month.",
        None,
    ),
    (
        "gpu_hit",
        "GPU shortage after the reserve",
        "share",
        False,
        "Size of a GPU shortage that still hits the platform.",
        4,
    ),
    ("latency", "p95 latency", "ms", False, "p95 API latency.", 1),
    ("incidents", "Incidents in the month", "count", False, "SEV1, SEV2 and SEV3 incidents in the month.", 0),
    (
        "availability",
        "Availability in the month",
        "share",
        True,
        "Client-weighted availability of the month.",
        6,
    ),
    ("load", "Team load", "ratio", False, "Engineering demand / capacity (1 = full).", 3),
    (
        "capacity",
        "Engineering capacity",
        "ratio",
        True,
        "Engineering capacity relative to T (ramp-up and vacancies included).",
        3,
    ),
    ("headcount", "Headcount", "people", True, "People employed at the end of the month.", 0),
    ("clients", "Clients", "clients", True, "Clients with a contract in force at the end of the month.", 0),
    ("new_arr", "New ARR won", "USD", True, "ACV of the new-business deals won since T.", None),
]
WEEK_METRICS = [
    ("cash", "Cash", "USD", True, "Cash balance at the end of the week."),
    ("load", "Team load", "ratio", False, "Engineering demand / capacity in the week (1 = full)."),
    ("incidents", "Incidents", "count", False, "Incidents in the week."),
    ("arr", "ARR", "USD", True, "ARR in force at the end of the week."),
]
DAY_METRICS = [
    ("cash", "Cash", "USD", True, "Cash balance at the end of the day."),
    ("load", "Team load", "ratio", False, "Planned work / capacity on the day (1 = full)."),
    ("incidents", "Incidents", "count", False, "Incidents on the day."),
]
# check id -> label, consequence, kind, ref
CHECK_ROWS = {
    "cash_negative": ("Cash below zero", "You need a credit line or emergency cuts", "threshold", None),
    "runway_low": (
        "Runway below 6 months",
        "Cash runs out within 6 months: the plan is not acceptable then",
        "check",
        "runway_low",
    ),
    "sla_breach": (
        "SLA breach",
        "A month below the SLA: penalties and angry clients are possible",
        "flag",
        "sla_breach",
    ),
    "churn_high": (
        "Churn above 20%",
        "You lose more clients and their recurring revenue",
        "flag",
        "churn_high",
    ),
    "over_budget": (
        "Cost above budget",
        "Spending runs ahead of the budget: cuts or a new budget are needed",
        "flag",
        "over_budget",
    ),
    "burnout": (
        "Team over capacity",
        "Load above 1.05: people leave more often and incidents rise",
        "flag",
        "burnout",
    ),
}
MILESTONE_ROWS = {
    "price_live": ("New prices late", "The higher prices start later: less revenue this year", "ms_price"),
    "debt_done": ("Debt work late", "Incidents and latency stay high for longer", "ms_debt"),
    "hires_ready": ("Hires late", "The team stays overloaded for longer", "ms_hire"),
    "gpu_ready": (
        "GPU capacity late",
        "A GPU shortage can still hit before the second cloud is ready",
        "ms_gpu",
    ),
}
# criterion -> RAG limits (03-REQUIREMENTS §8 fix 4: fixed runway bands; the goals of the case otherwise)
RAG_LIMITS = {
    "cash": RagThresholds(green=1_500_000.0, red=0.0),
    "runway": RagThresholds(green=18.0, red=6.0),
    "churn": RagThresholds(green=0.15, red=0.22),
    "sla": RagThresholds(green=0.997, red=0.995),
    "nps": RagThresholds(green=40.0, red=20.0),
}


def _r(x: float, decimals: int | None) -> float:
    return round(float(x)) if decimals is None else round(float(x), decimals)


def fan(arr: np.ndarray, decimals: int | None) -> list[dict[str, float]]:
    """Quantiles per month of (S, T + 1)."""
    qs = np.quantile(arr, QUANTILES, axis=0)
    return [
        {"month": m, **{k: _r(qs[i, m], decimals) for i, k in enumerate(QKEYS)}} for m in range(arr.shape[1])
    ]


def month_dates(res: Result, anchor: date) -> list[str]:
    b = res.periods.month_bounds
    h = res.metrics["cash"].shape[-1] - 1
    return [anchor.isoformat()] + [
        (anchor + timedelta(days=int(b[m]) + 1)).isoformat() for m in range(1, h + 1)
    ]


# ---------------------------------------------------------------------------------------------- calendar
def _row(meta: dict[str, Any], mask: np.ndarray) -> dict[str, Any]:
    """mask (S, H) of one fork."""
    m = mask[:, None, :]
    by, ever, med = risk.p_failed_by(m)[0], risk.p_ever(m)[0], risk.median_first_failure(m)[0]
    first = risk.first_failure_distribution(m)[0]
    return {
        "rowType": "risk",  # a failure to watch ("progress" rows count something good, such as live by month m)
        **meta,
        "pByMonth": [round(float(x), 5) for x in by],
        "pEver": round(float(ever), 5),
        "medianFirstMonth": None if np.isnan(med) else float(med) + 1,
        "pFirstByMonth": [round(float(x), 5) for x in first[:-1]],
    }


def risk_calendar(res: Result, cfg: Mapping[str, Any]) -> list[list[dict[str, Any]]]:
    """Per fork the rows: every monthly check, then the milestones of the fork (late at the marker month)."""
    variables = {v["name"]: v["dist"] for v in cfg.get("variables", [])}
    H = res.checks["cash_negative"].shape[-1]
    months = np.arange(1, H + 1)
    out = []
    for j, _ in enumerate(res.forks):
        rows = []
        for cid, (label, cons, kind, ref) in CHECK_ROWS.items():
            meta = {"id": cid, "label": label, "consequence": cons, "kind": kind, "ref": ref}
            rows.append(_row(meta, res.checks[cid][j]))
        for mid, (label, cons, var) in MILESTONE_ROWS.items():
            done = res.milestones[mid][j]
            if np.isnan(done).all():
                continue
            marker = float(variables.get(var, {}).get("value", H))
            first = min(max(int(np.ceil(marker - 1e-9)), 1), H)
            late = (np.nan_to_num(done, nan=np.inf) > marker + 1e-9)[:, None] & (months >= first)[None, :]
            meta = {"id": mid, "label": label, "consequence": cons, "kind": "milestone", "ref": mid}
            rows.append(
                _row(meta, late)
                | {"markerMonth": marker, "doneMonthP50": round(float(np.nanmedian(done)), 2)}
            )
        out.append(rows)
    return out


# ---------------------------------------------------------------------------------------------- cube and ranking
def build_cube(res: Result, run_id: str, anchor: date, seed: int) -> MetricCube:
    ids = [m[0] for m in MONTH_METRICS]
    values = np.stack([res.metrics[k] for k in ids], axis=2)  # (F, S, K, T)
    values = np.transpose(values, (1, 0, 2, 3))  # (S, F, K, T)
    infos = [
        MetricInfo(id=i, title=lab, unit=u, direction="higher" if hi else "lower")
        for i, lab, u, hi, _, _ in MONTH_METRICS
    ]
    pad = lambda m: np.concatenate([np.zeros(m.shape[:-1] + (1,), bool), m], -1)  # noqa: E731 - month 0 never fails
    cube = MetricCube(
        run=run_id,
        options=list(res.forks),
        metrics=infos,
        periods=month_dates(res, anchor),
        values=values,
        checks={"runway_low": pad(np.transpose(res.checks["runway_low"], (1, 0, 2)))},
        inputs={k: v for k, v in res.theta.items() if np.ndim(v) == 1},
        seed=seed,
        grain="month",
        anchor=anchor.isoformat(),
        notes=list(res.notes),
    )
    # statuses of the flag rules: SLA and churn bands, over budget as amber on cash
    sla_t = float(res.theta["sla_target"][0])
    cube.set_status_from_bands("sla", {"good": sla_t + 0.002, "warn": sla_t}, "higher")
    cube.set_status_from_bands("churn", {"good": 0.15, "warn": 0.2}, "lower")
    k = cube.metric_index("cash")
    over = pad(np.transpose(res.checks["over_budget"], (1, 0, 2)))
    cube.status[:, :, k, :] = np.where(over, 1, cube.status[:, :, k, :])
    return cube


def rag_limits(cfg: Mapping[str, Any]) -> dict[str, RagThresholds]:
    """RAG limits per criterion: the variables of the case (``criteria[].rag``) win over the defaults."""
    var = {v["name"]: v["dist"] for v in cfg.get("variables", [])}

    def value(name: str) -> float | None:
        d = var.get(name, {})
        x = d.get("value", d.get("mode"))
        return None if x is None else float(x)

    out = dict(RAG_LIMITS)
    for c in cfg.get("criteria", []):
        rag = c.get("rag")
        if isinstance(rag, Mapping):
            g, r = value(str(rag.get("green"))), value(str(rag.get("red")))
            if g is not None and r is not None:
                out[c["name"]] = RagThresholds(green=g, red=r)
    return out


def criteria_specs(cfg: Mapping[str, Any]) -> list[CriterionSpec]:
    weights = cfg.get("defaults", {}).get("weights", {})
    limits = rag_limits(cfg)
    return [
        CriterionSpec(
            metric=c["name"], name=c["name"], higher_is_better=bool(c["higher"]),
            weight=float(weights.get(c["name"], c.get("weight", 1.0))), unit=c.get("unit"),
            rag=limits.get(c["name"]),
        )
        for c in cfg.get("criteria", [])
    ]  # fmt: skip


def criterion_ties(cfg: Mapping[str, Any]) -> dict[str, float]:
    """Meaningful difference per criterion of the case: ``tie`` of the exported config (a variable ``md_*``
    that the user can edit, or a number), else ``tie_from`` / ``tie_within``; criteria without one are left
    out (every difference counts)."""
    var = {v["name"]: v["dist"] for v in cfg.get("variables", [])}
    out = {}
    for c in cfg.get("criteria", []):
        src = c.get("tie", c.get("tie_from", c.get("tie_within")))
        if isinstance(src, str) and src in var:
            out[c["name"]] = max(float(var[src].get("value", var[src].get("mode", 0.0))), 0.0)
        elif isinstance(src, int | float) and not isinstance(src, bool) and src > 0:
            out[c["name"]] = float(src)
    return out


def with_case_ties(run: Any, cfg: Mapping[str, Any], names: Mapping[str, str] | None = None) -> Any:
    """The run with the meaningful differences of the case (``mcda.with_ties``; unchanged without them).
    ``names``: criterion name of the case -> criterion name in the run, when they differ."""
    ties = criterion_ties(cfg)
    if not ties:
        return run
    try:
        from variator.mcda import with_ties
    except ImportError:  # mcda without meaningful differences
        return run
    names = names or {}
    known = {c.name for c in run.criteria}
    return with_ties(run, {names.get(k, k): v for k, v in ties.items() if names.get(k, k) in known})


def case_run(cube: MetricCube, cfg: Mapping[str, Any]) -> Any:
    """The MCDA run of the cube with the criteria, weights and meaningful differences of the case."""
    return with_case_ties(to_tensor_run(cube, criteria_specs(cfg)), cfg)


def risk_aversion(cfg: Mapping[str, Any]) -> float:
    return float(cfg.get("defaults", {}).get("riskAversion", 0.25))


def rank_options(cube: MetricCube, cfg: Mapping[str, Any]) -> Any:
    """The ranking of the forks only (``mcda.rank``), as ``decide`` makes it."""
    return rank(case_run(cube, cfg), risk_aversion=risk_aversion(cfg))


def decide(cube: MetricCube, cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Ranking (score x P(feasible), P(best)), the best path and the RAG shares at the horizon."""
    run = case_run(cube, cfg)
    ra = risk_aversion(cfg)
    ranking = rank(run, risk_aversion=ra)
    path = best_path(run, risk_aversion=ra)
    limits = {c.name: c.rag for c in run.criteria if c.rag is not None}
    rag = rag_report(run, limits)
    rag_by = {o.option: o for o in rag.options}
    options = []
    for o in ranking.options:
        r = rag_by[o.option]
        options.append(
            {
                "option": o.option,
                "rank": o.rank,
                "score": round(o.score, 4),
                "scoreTimesFeasible": round(o.score * o.p_feasible, 4),
                "pBest": round(o.prob_best, 4),
                "pFeasible": round(o.p_feasible, 4),
                "robustness": round(o.robustness, 4),
                "topsis": round(o.topsis, 4),
                "pareto": o.pareto,
                "rag": {
                    "green": round(r.overall.green, 4),
                    "amber": round(r.overall.amber, 4),
                    "red": round(r.overall.red, 4),
                },
                "stats": {
                    k: {q: float(f"{getattr(v, q):.6g}") for q in ("mean", "p5", "p50", "p95", "cvar")}
                    for k, v in o.stats.items()
                },
            }
        )
    return {
        "weights": ranking.weights,
        "riskAversion": ra,
        "options": options,
        "tradeoffs": ranking.tradeoffs,
        "reversals": ranking.reversals,
        "warnings": ranking.warnings,
        "bestPath": path.model_dump(),
        "ragLimits": {k: v.model_dump() for k, v in limits.items()},
        "pairwise": pairwise(run, ranking.options[0].option, ra, ranking.weights),
    }


def _g(x: float) -> float:
    return float(f"{float(x):.6g}")


def pairwise(
    run: Any, leader: str, risk_aversion: float, weights: Mapping[str, float]
) -> list[dict[str, Any]]:
    """Exact data for the close call: the leader ``a`` against each rival ``b``, per criterion and in total.

    Per criterion, ``d = (b - a)`` in the better direction (positive: b is better), per scenario:
    ``bWins`` / ``aWins`` = share of scenarios where one is better by more than the meaningful difference
    (ties respected), ``meanDiff`` and ``medianMargin`` of d in criterion units, ``scoreDiff`` = the weight x
    (normalised b - normalised a) of the ranking's decision matrix (the same ties). ``crit: "total"``: the
    weighted scenario scores (an infeasible scenario loses) and ``scoreDiff`` = score b - score a.
    """
    from variator.mcda import criterion_ties, decision_matrix, normalize, tensor_stats, weighted_scores

    names = [c.name for c in run.criteria]
    higher = np.asarray(run.higher_is_better, dtype=bool)
    w = np.array([float(weights[n]) for n in names])
    tie = criterion_ties(run)
    m = decision_matrix(tensor_stats(run), risk_aversion)
    x = run.values  # (S, O, K)
    try:  # the scaling of mcda.rank: fixed anchors from the baseline (QA M3) when mcda has them
        from variator.mcda.core import run_anchors, scenario_scores, score_matrix

        a_ = run_anchors(run)
        norm = score_matrix(m, higher, tie, a_)  # (O, K)
        per_s = scenario_scores(x, higher, w, tie, a_)
    except ImportError:  # min-max over the forks
        norm = normalize(m, higher, tie)
        per_s = weighted_scores(x, higher, w, tie)
    per = np.where(run.infeasible_mask(), -np.inf, per_s)  # (S, O)
    sign = np.where(higher, 1.0, -1.0)
    a = run.option_names.index(leader)
    out = []
    for b, rival in enumerate(run.option_names):
        if b == a:
            continue
        d = (x[:, b, :] - x[:, a, :]) * sign
        for k, name in enumerate(names):
            out.append(
                {
                    "a": leader, "b": rival, "crit": name,
                    "bWins": round(float((d[:, k] > tie[k]).mean()), 4),
                    "aWins": round(float((d[:, k] < -tie[k]).mean()), 4),
                    "meanDiff": _g(d[:, k].mean()), "medianMargin": _g(np.median(d[:, k])),
                    "scoreDiff": round(float(w[k] * (norm[b, k] - norm[a, k])), 5), "tie": _g(tie[k]),
                }
            )  # fmt: skip
        both = np.isfinite(per[:, a]) & np.isfinite(per[:, b])
        ds = per[both, b] - per[both, a]
        out.append(
            {
                "a": leader, "b": rival, "crit": "total",
                "bWins": round(float((per[:, b] > per[:, a] + 1e-12).mean()), 4),
                "aWins": round(float((per[:, a] > per[:, b] + 1e-12).mean()), 4),
                "meanDiff": _g(ds.mean()) if ds.size else 0.0, "medianMargin": _g(np.median(ds)) if ds.size else 0.0,
                "scoreDiff": round(float((norm[b] - norm[a]) @ w), 5), "tie": 0.0,
            }
        )  # fmt: skip
    return out


# ---------------------------------------------------------------------------------------------- goals, milestones, checks
# One source of numbers per page state (QA M1/M2): the actor equivalents of the goals, milestones and
# rules of the formula model, with the ids and the thresholds of the case config (they follow its edits).
COND = re.compile(r"^\s*(\w+)\s*(>=|<=|>|<)\s*([\w.]+)\s*$")
OPS = {">=": np.greater_equal, "<=": np.less_equal, ">": np.greater, "<": np.less}


def config_values(cfg: Mapping[str, Any]) -> dict[str, float]:
    out = {}
    for v in cfg.get("variables", []):
        d = v["dist"]
        x = d.get("value", d.get("mode"))
        if x is not None:
            out[v["name"]] = float(x)
    return out


def _month(x: Any, var: Mapping[str, float]) -> float:
    return float(var.get(x, 0.0)) if isinstance(x, str) else float(x)


def _cum(done: np.ndarray, H: int) -> list[float]:
    """P(done by month m), m = 1..H (NaN or inf = never)."""
    d = np.nan_to_num(np.asarray(done, dtype=float), nan=np.inf)
    return [round(float((d <= m).mean()), 5) for m in range(1, H + 1)]


def goal_rows(
    cfg: Mapping[str, Any], series: Mapping[str, np.ndarray], deadlines: Mapping[str, np.ndarray]
) -> list[dict[str, Any]]:
    """The goals of the case. A level goal (``cash >= g_cash``) reads its metric series (S, H + 1):
    ``pByMonth[m]`` = P(the condition holds at month m), not cumulative. A deadline goal (``deadlines``: item
    -> done month per scenario) is cumulative: P(done by month m)."""
    var = config_values(cfg)
    out = []
    for it in (cfg.get("timeline") or {}).get("items", []):
        if it.get("kind") not in ("goal", "milestone") or not it.get("condition"):
            continue
        name, month = it["name"], _month(it["month"], var)
        if name in deadlines:
            H = len(next(iter(series.values()))[0]) - 1 if series else int(month)
            by = _cum(deadlines[name], H)
            done = np.nan_to_num(np.asarray(deadlines[name], dtype=float), nan=np.inf)
            out.append({"id": name, "label": it["label"], "kind": it["kind"], "month": month, "cumulative": True,
                        "pMet": round(float((done <= month + 1e-9).mean()), 5), "pByMonth": by})  # fmt: skip
            continue
        if it["kind"] != "goal":
            continue
        m = COND.match(it["condition"])
        if not m or m.group(1) not in series:
            continue
        metric, op, rhs = m.groups()
        thr = var.get(rhs, None) if not rhs.replace(".", "", 1).isdigit() else float(rhs)
        if thr is None:
            continue
        x = series[metric]
        ok = OPS[op](x, thr)
        at = min(int(round(month)), x.shape[1] - 1)
        out.append({"id": name, "label": it["label"], "kind": "goal", "month": month, "metric": metric, "op": op,
                    "threshold": thr, "thresholdVar": rhs if rhs in var else None, "cumulative": False,
                    "pMet": round(float(ok[:, at].mean()), 5), "pByMonth": [round(float(v), 5) for v in ok[:, 1:].mean(0)]})  # fmt: skip
    return out


def milestone_rows(
    cfg: Mapping[str, Any], fork: str, done: Mapping[str, np.ndarray], H: int
) -> list[dict[str, Any]]:
    """The milestones of the fork's plan items: P(done by month m), P(done by the marker month), the median."""
    var = config_values(cfg)
    items = (cfg.get("timeline") or {}).get("items", [])
    # a fork that no plan item names (a drafted case): its milestones follow its levers (the NaN rows below)
    named = any(fork in (it.get("options") or ()) for it in items)
    out = []
    for it in items:
        if it.get("kind") != "milestone" or (named and it.get("options") and fork not in it["options"]):
            continue
        d = done.get(it["name"])
        if d is None or np.isnan(np.asarray(d, dtype=float)).all():
            continue
        d = np.nan_to_num(np.asarray(d, dtype=float), nan=np.inf)
        marker = _month(it["month"], var)
        med = float(np.median(d))
        out.append({"id": it["name"], "label": it["label"], "markerMonth": marker,
                    "pMet": round(float((d <= marker + 1e-9).mean()), 5), "pByMonth": _cum(d, H),
                    "medianMonth": round(med, 2) if np.isfinite(med) else None})  # fmt: skip
    return out


def churn_breakdown(res: Result, j: int) -> dict[str, Any]:
    """Year-1 churn in parts (QA m2), shares of the ARR at T.

    - ``alreadyNoticed``: clients on notice at T (known end dates); not in the churn criterion.
    - ``newNotices``: notices given after T (p10/p50/p90); with ``downgrades`` they make the churn criterion.
    - ``terminations``: the ARR of all contracts that end within the horizon, plus downgrades: the definition
      of the history metric ``rev.churn`` (gross ARR), so the two compare.
    - ``total``: already noticed + new notices + downgrades."""
    if not res.churn_parts:
        return {}

    def q(x: np.ndarray) -> dict[str, float]:
        qs = np.quantile(x, [0.1, 0.5, 0.9])
        return {k: round(float(v), 5) for k, v in zip(("p10", "p50", "p90"), qs, strict=True)}

    known = res.noticed_at_t
    arr0 = float(res.metrics["arr"][j, 0, 0]) or 1.0
    already = sum(c["arr"] for c in known) / arr0
    parts = {k: v[j] for k, v in res.churn_parts.items()}
    crit = parts["new_notices"] + parts["downgrades"]
    return {
        "alreadyNoticed": {"arrShare": round(already, 5), "clients": known},
        "newNotices": q(parts["new_notices"]),
        "downgrades": q(parts["downgrades"]),
        "criterion": {**q(crit), "definition": "new notices after T + downgrades, over the ARR at T (the churn fan and criterion)"},
        "terminations": {**q(parts["terminations"] + parts["downgrades"]),
                         "definition": "contracts that end within the horizon + downgrades, over the ARR at T (as rev.churn of the history)"},
        "total": {"p50": round(float(np.median(already + crit)), 5), "definition": "already noticed + new notices + downgrades"},
    }  # fmt: skip


def check_rows(calendar_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The actor equivalents of the formula rules: the calendar rows of the checks and flags (``rule`` =
    the rule id of the case config where one matches)."""
    return [
        {"id": r["id"], "rule": r.get("ref"), "kind": r["kind"], "pEver": r["pEver"], "pByMonth": r["pByMonth"],
         "medianFirstMonth": r.get("medianFirstMonth")}
        for r in calendar_rows
        if r.get("kind") in ("check", "flag", "threshold")
    ]  # fmt: skip


# ---------------------------------------------------------------------------------------------- month.json
def git_commit(repo: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, text=True, timeout=10
            ).stdout.rstrip()
        except (OSError, subprocess.SubprocessError):
            return ""

    dirty = run("status", "--porcelain", "--", *ENGINE_PATHS).splitlines()
    return {
        "commit": run("rev-parse", "HEAD"),
        "dirty": bool(dirty),
        "dirtyFiles": [d[3:] for d in dirty][:20],
    }


# the code that a future run imports: a change there that is not committed makes the run "dirty"
ENGINE_PATHS = (
    "src/variator/future", "src/variator/mcda", "src/variator/metrics/cube.py", "src/variator/rag",
    "src/variator/domain", "src/variator/simulation", "src/variator/orgsim", "src/variator/actors",
    "workspace/config/cases",
)  # fmt: skip


def metric_defs(rows: Sequence[tuple]) -> list[dict[str, Any]]:
    return [{"id": r[0], "label": r[1], "unit": r[2], "higher": r[3], "description": r[4]} for r in rows]


def month_json(
    res: Result,
    *,
    case_id: str,
    cfg: Mapping[str, Any],
    params: Mapping[str, Param],
    decision: Mapping[str, Any],
    provenance: Mapping[str, Any],
    anchor: date,
    labels: Mapping[str, str],
    layers: Mapping[str, Any] | None = None,
    week_trees: Mapping[str, Any] | None = None,
    timings: Mapping[str, float] | None = None,
    forks: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    cal = risk_calendar(res, cfg)
    n = res.metrics["cash"].shape[1]
    H = res.metrics["cash"].shape[2] - 1
    options = {}
    for j, f in enumerate(res.forks):
        options[f] = {
            "preview": False,
            "option": f,
            "label": labels.get(f, f),
            "fans": {m: fan(res.metrics[m][j], dec) for m, _, _, _, _, dec in MONTH_METRICS},
            "riskCalendar": cal[j],
            "goals": goal_rows(cfg, {m: res.metrics[m][j] for m in ("cash", "churn")}, {}),
            "milestones": milestone_rows(cfg, f, {k: v[j] for k, v in res.milestones.items()}, H),
            "checks": check_rows(cal[j]),
            "churnBreakdown": churn_breakdown(res, j),
        }
    return {
        "preview": False,
        "source": "actors",
        "caseId": case_id,
        "engine": ENGINE,
        "note": "Month Monte Carlo on actors: the learned habits of the history up to T, the state at T, "
        "forks as dated interventions. Each fan holds both the parameter uncertainty and the process noise.",
        "provenance": dict(provenance),
        "seed": int(provenance.get("seed", 0)),
        "n": int(n),
        "anchor": anchor.isoformat(),
        "horizonMonths": H,
        "months": month_dates(res, anchor),
        "metrics": metric_defs(MONTH_METRICS),
        "weekMetrics": metric_defs(WEEK_METRICS),
        "dayMetrics": metric_defs(DAY_METRICS),
        "options": options,
        "ranking": dict(decision),
        "forks": list(forks),
        "layers": dict(layers or {}),
        "parameters": [p.as_row() for p in params.values()],
        "weekTrees": dict(week_trees or {}),
        "dayExamples": {},
        "timings": dict(timings or {}),
        "notes": list(res.notes),
    }
