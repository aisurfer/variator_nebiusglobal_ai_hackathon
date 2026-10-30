"""The week scenario tree (E2) for the best forks: the same engine as the month Monte Carlo, one week a step.

- Genealogy: ``orgsim.tree.plan_genealogy`` (k children per node, at most M nodes per level, uniform
  selection on its own stream), so all forks share the same tree shape.
- The step into a node uses ``derived_rng(seed, key)`` of the node key only (common random numbers
  across forks). The residual ``Exp(1)`` of each client notice and each resignation is drawn again in
  each node: the exponential has no memory, so this is exact and gives the branches their spread.
- Parameters stay at their points (posterior medians): the tree shows the process noise; the month
  Monte Carlo shows both the parameter uncertainty and the noise.
- ``replay`` steps one path from its node keys and gives the same weeks as the tree (the day level).
"""

# ruff: noqa: E501 - node summaries, one per line

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

import numpy as np

from variator.future.company import (
    DEALS_PER_PERIOD,
    HIRE_SLOTS,
    INC_PER_PERIOD,
    MAX_DEALS,
    SEVERITIES,
    CompanyModel,
    week_periods,
)
from variator.future.forks import Fork, compile_knobs
from variator.orgsim.tree import plan_genealogy
from variator.synthetic.company import derived_rng

SCHEMA = "variator.future.tree/1"
ROOT = "week"
KINDS = (("pessimistic", 0.1), ("median", 0.5), ("optimistic", 0.9))
LABEL = {"pessimistic": "Pessimistic", "median": "Median", "optimistic": "Optimistic"}
VALUES = {"cash": 0, "load": 3, "incidents": 0, "arr": 0}
LAG_WEEKS = 30
DAY_EXAMPLE_DAYS = 60


def tree_theta(model: CompanyModel, b: int) -> dict[str, np.ndarray]:
    """The points of all parameters; the churn covariate at its expected strength (weight x k)."""
    th = {k: np.full(b, p.point) for k, p in model.params.items()}
    th["churn_k"] = th["churn_k"] * model.params["churn_cov_weight"].point
    return th


def node_draws(model: CompanyModel, rng: np.random.Generator) -> dict[str, np.ndarray]:
    """The draws of one node, in a fixed order, with the keys of ``MonthDraws.period`` (no scenario axis)."""
    C, P = model.n_clients, model.n_people
    u, z = rng.random, rng.standard_normal
    return {
        "cl_notice_e": rng.standard_exponential(C),
        "pp_resign_e": rng.standard_exponential(P),
        "deal_redraw_u": u(MAX_DEALS),
        "cl_up_u": u(C), "cl_upwin_u": u(C), "cl_down_u": u(C), "cl_late_u": u(C), "cl_pay_z": z(C), "cl_notice_frac": u(C),
        "pp_grow_u": u(P), "pp_grant_u": u(P), "pp_quit_frac": u(P), "pp_bf_lead_z": z(P), "pp_pace_z": z(P),
        "load_z": z(), "deal_u": u(), "cloud_z": z(),
        "cl_hit_u": u((C, 3)), "inc_u": u(3), "inc_dt_z": z((3, INC_PER_PERIOD)),
        "shock_u": u(len(model.shocks)), "shock_size_u": u(len(model.shocks)),
        "vendor_z": z(len(model.vendor_ids)),
        "vendor_pay_u": u(len(model.vendor_ids)),
        "deal_cycle_z": z(DEALS_PER_PERIOD), "deal_value_z": z(DEALS_PER_PERIOD), "deal_win_u": u(DEALS_PER_PERIOD),
        "deal_arrive_frac": u(DEALS_PER_PERIOD),
        "hire_lead_z": z(HIRE_SLOTS), "hire_pace_z": z(HIRE_SLOTS),
        "churn_cov_on": np.array(True),
    }  # fmt: skip


def root_draws(model: CompanyModel, seed: int) -> dict[str, np.ndarray]:
    rng = derived_rng(seed, f"{ROOT}:first")
    n_open = len(model.world.open_receivables["amount"])
    return {
        "cl_notice_e": rng.standard_exponential((1, model.n_clients)),
        "pp_resign_e": rng.standard_exponential((1, model.n_people)),
        "pipeline_win_u": rng.random((1, len(model.deal_ids))),
        "open_pay_z": rng.standard_normal((1, n_open)),
    }


def _stack(rows: Sequence[Mapping[str, np.ndarray]]) -> dict[str, np.ndarray]:
    return {k: np.stack([r[k] for r in rows]) for k in rows[0]}


def _select(st: dict[str, Any], idx: np.ndarray) -> dict[str, Any]:
    """The child states: rows ``idx`` of the parent batch (axis 1 of (F, S, ...), axis 0 of (S,))."""
    b = st["S"]
    out: dict[str, Any] = {}
    for k, v in st.items():
        if isinstance(v, np.ndarray) and v.ndim >= 2 and v.shape[1] == b:
            out[k] = v[:, idx].copy()
        elif isinstance(v, np.ndarray) and v.ndim == 1 and v.shape[0] == b:
            out[k] = v[idx].copy()
        elif isinstance(v, list):
            out[k] = [x[:, idx].copy() for x in v]
        else:
            out[k] = v
    out["S"] = len(idx)
    return out


def _step_level(model, st, level, periods, knobs, seed, keys, events: bool):
    rows = [node_draws(model, derived_rng(seed, key)) for key in keys]
    d = _stack(rows)
    st["resid_c"] = d.pop("cl_notice_e")[None].copy()
    st["resid_p"] = d.pop("pp_resign_e")[None].copy()
    redraw = d.pop("deal_redraw_u")[None]
    st["deal_win_u"] = np.where(st["deal_close"] >= periods.d0[level - 1], redraw, st["deal_win_u"])
    d["churn_cov_on"] = d["churn_cov_on"].astype(bool)
    ev: list[list[dict[str, Any]]] | None = [[] for _ in keys] if events else None
    metrics, checks = model.step(
        st, level, periods, knobs, tree_theta(model, len(keys)), d, [ev] if ev is not None else None
    )
    return metrics, checks, ev


def grow(
    model: CompanyModel,
    fork: Fork,
    *,
    seed: int,
    weeks: int = 52,
    k: int = 3,
    max_nodes: int = 60,
    layers: Sequence[Fork] = (),
    cash_scale: float = 1.0,
) -> dict[str, Any]:
    """Grow the tree of one fork: per level the node values and events, and the genealogy."""
    knobs = compile_knobs([fork], layers)
    knobs.cash_scale *= cash_scale
    periods = week_periods(model.world, weeks, LAG_WEEKS)
    g = plan_genealogy(weeks, k=k, max_nodes=max_nodes, seed=seed, root_key=ROOT)
    st = model.init(knobs, tree_theta(model, 1), root_draws(model, seed), periods)
    th = tree_theta(model, 1)
    values = [
        {
            "cash": st["cash"][0].copy(),
            "load": np.full(1, th["team_load_now"][0]),
            "incidents": np.zeros(1),
            "arr": st["arr_prev"][0].copy(),
        }
    ]
    events: list[list[list[dict[str, Any]]]] = [[[]]]
    for level in range(1, weeks + 1):
        st = _select(st, g.parents[level])
        m, _, ev = _step_level(model, st, level, periods, knobs, seed, g.keys[level], True)
        values.append({name: m[name][0].copy() for name in VALUES})
        events.append(ev)
    return {
        "genealogy": g,
        "values": values,
        "events": events,
        "fork": fork.id,
        "label": fork.label,
        "k": k,
        "max_nodes": max_nodes,
        "weeks": weeks,
    }


def replay(
    model: CompanyModel, fork: Fork, seed: int, keys: Sequence[str], weeks: int | None = None
) -> list[dict[str, Any]]:
    """Step one path from its node keys (``keys[0]`` is the root): the weekly values and events."""
    weeks = weeks or len(keys) - 1
    knobs = compile_knobs([fork])
    periods = week_periods(model.world, max(weeks, 52), LAG_WEEKS)
    st = model.init(knobs, tree_theta(model, 1), root_draws(model, seed), periods)
    out = [
        {
            "week": 0,
            "values": {"cash": float(st["cash"][0, 0]), "arr": float(st["arr_prev"][0, 0])},
            "events": [],
        }
    ]
    for level in range(1, weeks + 1):
        m, _, ev = _step_level(model, st, level, periods, knobs, seed, [keys[level]], True)
        out.append(
            {"week": level, "values": {name: float(m[name][0, 0]) for name in VALUES}, "events": ev[0]}
        )
    return out


# ---------------------------------------------------------------------------------------------- export
def _money(x: float) -> str:
    a = abs(x)
    sign = "−" if x < 0 else ""
    return f"{sign}${a / 1e6:.2f}M" if a >= 1e6 else f"{sign}${a / 1e3:.0f}k"


def _summary(
    week: int, v: Mapping[str, float], prev: Mapping[str, float], ev: Sequence[Mapping[str, Any]]
) -> str:
    if week == 0:
        return f"Today: cash {_money(v['cash'])}, ARR {_money(v['arr'])}, load {v['load']:.2f}."
    delta = v["cash"] - prev["cash"]
    sign = "+" if delta >= 0 else ""
    inc = int(v["incidents"])
    text = f"Week {week}: cash {_money(v['cash'])} ({sign}{_money(delta)}), ARR {_money(v['arr'])}, load {v['load']:.2f}, {inc} incident{'' if inc == 1 else 's'}"
    notable = [e["text"] for e in ev if e["type"] in ("client", "people", "shock", "decision")][:2]
    return text + (f"; {'; '.join(notable)}." if notable else ".")


def export(tree: Mapping[str, Any], seed: int, pinned: Mapping[str, int] | None = None) -> dict[str, Any]:
    """The ``WeekTree`` of the page (pruned to the nodes on a final path) with the leaf examples."""
    g, vals, evs = tree["genealogy"], tree["values"], tree["events"]
    levels_n = len(g.parents)
    shares = [np.zeros(len(p)) for p in g.parents]
    shares[-1][:] = 1.0 / len(g.parents[-1])
    for lv in range(levels_n - 1, 0, -1):
        np.add.at(shares[lv - 1], g.parents[lv], shares[lv])
    keep_ids: list[dict[int, int]] = []
    levels = []
    for w in range(levels_n):
        keep: dict[int, int] = {}
        row = []
        for i in range(len(g.parents[w])):
            if shares[w][i] <= 0:
                continue
            parent = -1 if w == 0 else keep_ids[w - 1][int(g.parents[w][i])]
            v = {m: float(vals[w][m][i]) for m in VALUES}
            pv = v if w == 0 else {m: float(vals[w - 1][m][int(g.parents[w][i])]) for m in VALUES}
            keep[i] = len(row)
            row.append(
                {
                    "id": f"w{w}n{len(row)}",
                    "week": w,
                    "parent": parent,
                    "share": round(float(shares[w][i]), 6),
                    "values": {
                        m: (round(x) if VALUES[m] == 0 else round(x, VALUES[m])) for m, x in v.items()
                    },
                    "summary": _summary(w, v, pv, evs[w][i]),
                }
            )
        levels.append(row)
        keep_ids.append(keep)
    bands: dict[str, list[dict[str, float]]] = {m: [] for m in VALUES}
    for w in range(levels_n):
        alive = shares[w] > 0
        for m, dec in VALUES.items():
            p10, p50, p90 = np.quantile(vals[w][m][alive], [0.1, 0.5, 0.9])
            r = (lambda x: round(float(x))) if dec == 0 else (lambda x, d=dec: round(float(x), d))
            bands[m].append({"week": w, "p10": r(p10), "p50": r(p50), "p90": r(p90)})
    leaf_cash = vals[-1]["cash"]
    examples = []
    for kind, q in KINDS:
        leaf = int(np.argmin(np.abs(leaf_cash - np.quantile(leaf_cash, q))))
        if pinned and kind in pinned:  # a leaf that the day level already uses (data/fixed_paths.json)
            leaf = int(pinned[kind])
        path = [leaf]
        for lv in range(levels_n - 1, 0, -1):
            path.append(int(g.parents[lv][path[-1]]))
        path = path[::-1]
        examples.append(
            {
                "kind": kind,
                "label": LABEL[kind],
                "fork": tree["fork"],
                "seed": seed,
                "leaf": f"w{levels_n - 1}n{keep_ids[-1][leaf]}",
                "nodes": [f"w{w}n{keep_ids[w][i]}" for w, i in enumerate(path)],
                "keys": [g.keys[w][i] for w, i in enumerate(path)],
                "cashEnd": round(float(leaf_cash[leaf])),
                "events": [{"week": w, **e} for w, i in enumerate(path) for e in evs[w][i]],
                "weeks": [
                    {"week": w, **{m: float(vals[w][m][i]) for m in VALUES}} for w, i in enumerate(path)
                ],
            }
        )
    return {
        "preview": False,
        "source": "actors",
        "option": tree["fork"],
        "label": tree["label"],
        "k": tree["k"],
        "maxNodes": tree["max_nodes"],
        "weeks": tree["weeks"],
        "levels": levels,
        "bands": bands,
        "examples": examples,
    }


def day_examples(trees: Mapping[str, Any], anchor: date, as_of: date) -> dict[str, list[dict[str, Any]]]:
    """A bridge for the Day section until WP19: the 3 example paths laid out on working days.

    Cash is interpolated between the week ends; the events of a week fall on its first working day.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for fork, t in trees.items():
        rows = []
        for ex in t["examples"]:
            weeks = ex["weeks"]
            days, d = [], as_of
            while len(days) < DAY_EXAMPLE_DAYS:
                if d.weekday() < 5:
                    days.append(d)
                d += timedelta(days=1)
            pts = []
            for day in days:
                x = (day - as_of).days / 7.0
                w = min(int(x), len(weeks) - 2)
                a, b = weeks[w], weeks[w + 1]
                f = x - w
                pts.append(
                    {
                        "date": day.isoformat(),
                        "values": {
                            "cash": round(a["cash"] + f * (b["cash"] - a["cash"])),
                            "load": round(b["load"], 3),
                            "incidents": 0,
                        },
                    }
                )
            events = []
            for e in ex["events"]:
                day = as_of + timedelta(days=7 * (e["week"] - 1))
                while day.weekday() >= 5:
                    day += timedelta(days=1)
                if day > days[-1]:
                    continue
                events.append(
                    {
                        "date": day.isoformat(),
                        "type": e["type"],
                        "text": e["text"],
                        **({"entityId": e["entityId"]} if e.get("entityId") else {}),
                    }
                )
                if e["type"] == "incident":
                    for p in pts:
                        if p["date"] == day.isoformat():
                            p["values"]["incidents"] += 1
            rows.append(
                {
                    "preview": False,
                    "source": "actors-week",
                    "option": fork,
                    "kind": ex["kind"],
                    "label": ex["label"],
                    "summary": f"{ex['label']} example (week-tree leaf {ex['leaf']}): cash after {len(weeks) - 1} weeks {_money(ex['cashEnd'])}.",
                    "yearEnd": {"cash": ex["cashEnd"]},
                    "days": pts,
                    "events": events,
                }
            )
        out[fork] = rows
    return out


def build_trees(
    model: CompanyModel,
    forks: Sequence[Fork],
    best: Sequence[str],
    *,
    seed: int,
    weeks: int = 52,
    k: int = 3,
    max_nodes: int = 60,
    layers: Sequence[Fork] = (),
    cash_scale: float = 1.0,
    pinned: Mapping[str, Mapping[str, int]] | None = None,
) -> dict[str, Any]:
    """The trees of the ``best`` forks; ``pinned``: fork -> kind -> leaf index (the day level's leaves); ``layers`` (hypotheses, placed events) and ``cash_scale`` as in the
    month run of the same state (the live route)."""
    by = {f.id: f for f in forks}
    trees = {}
    for fid in best:
        g = grow(
            model,
            by[fid],
            seed=seed,
            weeks=weeks,
            k=k,
            max_nodes=max_nodes,
            layers=layers,
            cash_scale=cash_scale,
        )
        trees[fid] = export(g, seed, (pinned or {}).get(fid))
    return {
        "schema": SCHEMA,
        "caseId": "company_best_year",
        "seed": seed,
        "k": k,
        "maxNodes": max_nodes,
        "weeks": weeks,
        "forks": list(best),
        "theta": "points (posterior medians); the churn covariate at weight x k",
        "replay": "variator.future.tree.replay(model, fork, seed, keys)",
        "weekTrees": trees,
        "dayExamples": day_examples(trees, model.world.anchor, model.world.as_of),
        "severities": list(SEVERITIES),
    }
