"""Shared parts of the preview data (scripts/mvp/proto_future.py, proto_delivery.py): the L0 run, the preview
random streams, the risk-calendar rows, the week-tree export and the working days after the fork date.

Deterministic: every random number comes from ``synthetic.company.derived_rng`` streams whose labels never
name the fork, so the forks share their random numbers (common random numbers).
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_case import _labels, _source  # noqa: E402
from make_company_facts import BACKFILLS, LEFT  # noqa: E402

from variator.mcda import best_path  # noqa: E402
from variator.orgsim import risk  # noqa: E402
from variator.orgsim.streams import stream_label  # noqa: E402
from variator.report.playground import export_model  # noqa: E402
from variator.rules import compile_rules  # noqa: E402
from variator.simulation import simulate_tensor  # noqa: E402
from variator.synthetic.company import derived_rng  # noqa: E402

SEED = 42
N = 4096
ANCHOR = date(2026, 10, 26)  # T: the last day of the company history (the knowledge-base anchor)
WEEKS = 52
K = 3
MAX_NODES = 60
DAYS = 60
BDAYS_PER_MONTH = 250 / 12
TIMEZONE = "Europe/London"
KINDS = [("pessimistic", 0.1), ("median", 0.5), ("optimistic", 0.9)]
LABEL = {"pessimistic": "Pessimistic", "median": "Median", "optimistic": "Optimistic"}


def money(x: float) -> str:
    sign = "−" if x < 0 else ""
    a = abs(x)
    if a >= 1e6:
        return f"{sign}${a / 1e6:.2f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.0f}k"
    return f"{sign}${a:.0f}"


def signed_money(x: float) -> str:
    return ("+" if x >= 0 else "") + money(x)


def rng(*parts: object) -> np.random.Generator:
    """A preview stream; the label never names the fork (common random numbers)."""
    return derived_rng(SEED, stream_label("preview", *parts))


def metrics(rows: list[tuple]) -> list[dict[str, Any]]:
    return [{"id": i, "label": lab, "unit": u, "higher": h, "description": d} for i, lab, u, h, d in rows]


def l0_run(case: str, probes: dict[str, str], n: int = N) -> dict[str, Any]:
    """The L0 formula model of the case at its defaults: simulate_tensor with probes (plus the condition of
    every milestone and goal, "met__<item>"), the best path and the labels."""
    model, packs, page, scenarios, timeline = _source(case)
    data = export_model(model, packs, page, scenarios, timeline)
    rules = compile_rules(
        packs,
        variables=[v.name for v in model.variables],
        options=[o.name for o in model.options],
        criteria=[c.name for c in model.effective_criteria()],
        derived=model.derived_map(),
    )
    run = simulate_tensor(
        model, rules=rules, n_scenarios=n, seed=SEED, probes=probes | item_probes(data["timeline"])
    )
    return {
        "run": run,
        "path": best_path(run),
        "labels": _labels(data),
        "options": list(run.option_names),
        "timeline": data["timeline"],
        "variables": {v["name"]: v for v in data["variables"]},
    }


def item_probes(timeline: dict[str, Any] | None) -> dict[str, str]:
    """A probe per milestone and goal: its condition ("met")."""
    items = timeline["items"] if timeline else []
    return {f"met__{i['name']}": i["condition"] for i in items if i["condition"]}


def item_month(item: dict[str, Any], variables: dict[str, dict]) -> float:
    m = item["month"]
    if isinstance(m, str):
        d = variables[m]["dist"]
        return float(d.get("value", d.get("mode")))
    return float(m)


def deadline_mask(met: np.ndarray, month: float, horizon: int) -> np.ndarray:
    """(n, options, horizon): a milestone or goal fails at its marker month (rounded up) when not met."""
    first = min(max(math.ceil(month - 1e-9), 1), horizon)
    when = np.arange(1, horizon + 1) >= first
    return (~met.astype(bool))[:, :, None] & when[None, None, :]


def calendar(
    rows: list[tuple[dict[str, Any], np.ndarray, set[str] | None]], options: list[str]
) -> list[list[dict]]:
    """Per option: the rows that apply to it. A row = (meta: id, label, consequence, kind, ref; the failure
    mask (n, options, months) by month 1..H; the options it applies to or None for all)."""
    out: list[list[dict[str, Any]]] = [[] for _ in options]
    for meta, mask, only in rows:
        by, ever, med = risk.p_failed_by(mask), risk.p_ever(mask), risk.median_first_failure(mask)
        for j, o in enumerate(options):
            if only is not None and o not in only:
                continue
            out[j].append(
                {
                    **meta,
                    "pByMonth": [round(float(x), 5) for x in by[j]],
                    "pEver": round(float(ever[j]), 5),
                    "medianFirstMonth": None if np.isnan(med[j]) else float(med[j]) + 1,
                }
            )
    return out


def timeline_rows(l0: dict[str, Any], kinds: tuple[str, ...], text: dict[str, tuple[str, str]], horizon: int):
    """Calendar rows of the timeline milestones (late) and goals (missed), from the item conditions."""
    run, rows = l0["run"], []
    for it in l0["timeline"]["items"] if l0["timeline"] else []:
        if it["kind"] not in kinds or not it["condition"]:
            continue
        label, consequence = text[it["name"]]
        mask = deadline_mask(run.probes[f"met__{it['name']}"], item_month(it, l0["variables"]), horizon)
        meta = {
            "id": it["name"],
            "label": label,
            "consequence": consequence,
            "kind": it["kind"],
            "ref": it["name"],
        }
        rows.append((meta, mask, set(it["options"]) if it["options"] else None))
    return rows


def fan(arr: np.ndarray, months: int, decimals: int | None) -> list[dict[str, float]]:
    """Quantiles of (n, months + 1) per month; decimals None = whole numbers."""
    qs = np.quantile(arr, [0.1, 0.25, 0.5, 0.75, 0.9], axis=0)

    def r(x: float) -> float:
        return round(float(x)) if decimals is None else round(float(x), decimals)

    return [
        {"month": m, **{key: r(qs[i, m]) for i, key in enumerate(("p10", "p25", "p50", "p75", "p90"))}}
        for m in range(months + 1)
    ]


def export_tree(
    tree: Any,
    option: str,
    label: str,
    rounding: dict[str, int],
    summary: Callable[[int, dict[str, Any], dict[str, Any]], str],
    band_rounding: dict[str, int] | None = None,
) -> dict[str, Any]:
    """A grown week tree as the preview's WeekTree: levels of the nodes with a final path, shares, bands.
    ``rounding``: decimals of the node values per metric (0 = whole numbers); ``band_rounding``: of the
    bands."""
    band_rounding = band_rounding or rounding
    parents = tree.parents
    shares = [np.zeros(len(level)) for level in tree.states]
    shares[-1][:] = 1.0 / len(tree.states[-1])
    for level in range(len(tree.states) - 1, 0, -1):
        np.add.at(shares[level - 1], parents[level], shares[level])

    def rnd(x: float, decimals: int) -> float:
        return round(float(x)) if decimals == 0 else round(float(x), decimals)

    bands: dict[str, list[dict[str, float]]] = {m: [] for m in rounding}
    for w, level in enumerate(tree.states):
        for metric in bands:
            v = np.array([s[metric] for s in level], dtype=float)
            p10, p50, p90 = (rnd(x, band_rounding[metric]) for x in np.quantile(v, [0.1, 0.5, 0.9]))
            bands[metric].append({"week": w, "p10": p10, "p50": p50, "p90": p90})
    levels: list[list[dict[str, Any]]] = []
    keep_prev: dict[int, int] = {}
    for w, level in enumerate(tree.states):
        keep: dict[int, int] = {}
        row = []
        for i, s in enumerate(level):
            if shares[w][i] <= 0:
                continue  # no final path goes through this node
            parent = -1 if w == 0 else keep_prev[int(parents[w][i])]
            prev = tree.states[w - 1][int(parents[w][i])] if w else s
            keep[i] = len(row)
            row.append(
                {
                    "id": f"w{w}n{len(row)}",
                    "week": w,
                    "parent": parent,
                    "share": round(float(shares[w][i]), 6),
                    "values": {m: rnd(s[m], rounding[m]) for m in rounding},
                    "summary": summary(w, s, prev),
                }
            )
        levels.append(row)
        keep_prev = keep
    return {
        "preview": True,
        "option": option,
        "label": label,
        "k": K,
        "maxNodes": MAX_NODES,
        "weeks": len(tree.states) - 1,
        "levels": levels,
        "bands": bands,
    }


def staff_at_t(spec: Any) -> list[Any]:
    """The people on staff at T (``spec.Person``): the spec people who did not leave before T, then the
    backfills in their seats (same role and department; not key people yet). Day examples name only them."""
    by_id = {p.id: p for p in spec.people}
    staff = [p for p in spec.people if p.id not in LEFT]
    for pid, name, title, seat, manager, _start in BACKFILLS:
        new = {"id": pid, "name": name, "title": title, "manager": manager, "key": False}
        staff.append(by_id[seat].model_copy(update=new))
    return staff


def bdays(cal: Any, n: int) -> list[date]:
    out, d = [], ANCHOR
    for _ in range(n):
        d = cal.add_business_days(d, 1)
        out.append(d)
    return out


def day_index(days: list[date], d: date) -> int | None:
    """Index of the first working day on or after ``d`` inside the window."""
    for i, x in enumerate(days):
        if x >= d:
            return i
    return None


class Events:
    """The event list of one example day series."""

    def __init__(self, days: list[date]) -> None:
        self.days, self.items = days, []

    def add(self, i: int | None, etype: str, text: str, entity: str | None = None) -> None:
        if i is None or not 0 <= i < len(self.days):
            return
        self.items.append(
            {
                "date": self.days[i].isoformat(),
                "type": etype,
                "text": text,
                **({"entityId": entity} if entity else {}),
                **(
                    {"person": entity[len("person.") :].upper()}
                    if entity and entity.startswith("person.")
                    else {}
                ),
            }
        )

    def sorted(self) -> list[dict[str, Any]]:
        return sorted(self.items, key=lambda e: (e["date"], e["type"], e["text"]))
