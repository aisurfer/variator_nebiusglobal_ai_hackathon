"""The week level as an EVENT TREE (WP18c): a prefix tree over the material events of an ensemble of paths.

- Paths: n weekly paths per fork from ``CompanyModel.run_weeks`` (full parameter uncertainty, the same
  habits, hypotheses, placed events and keyed streams as the month level).
- Material events of a path (first occurrence of each): a key or big client gives notice; an engineer or an
  executive resigns; a major SEV1 outage (4 h or more); a pipeline or big deal is won or lost; a case shock;
  all hires of the fork on board; cash below the goal; the team over capacity 3 weeks in a row.
- Tree: a node holds the scenarios that share the same splits. A node splits on the material event that
  most of its scenarios have later (``split``: the event, at its median week), into the scenarios with it
  and the rest (``rest: true``). When no single event reaches ``MIN_SHARE`` of all scenarios, the rare
  events together split off as "Other events" (``other: true``). Depth and size are capped.
- Schema (WP23): ``EventTree {version, unit, option, label, n, weeks, metrics, nodes, paths}``,
  ``Node {id, parent, from, to, share, n, split, bands, paths}``, ``Path {id, kind?, scenario, seed, leaf,
  nodes, events, weeks}``. ``bands[metric]`` = ``[[week, p10, p50, p90], ...]`` for the weeks from..to.
"""

# ruff: noqa: E501 - event texts, one per line

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

VERSION = 2
METRICS = ("cash", "load", "incidents", "arr")
MIN_SHARE = 0.025  # a split branch holds at least 2.5% of all scenarios; rarer events join "Other events"
SPLIT_SHARE = 0.05  # a node splits only when it holds at least 5% of all scenarios
MAX_DEPTH = 5
MAX_NODES = 61
BIG_CLIENT = 150_000.0  # a client with this ACV or a key client: its notice is material
MAJOR_SEV1_HOURS = 4.0
OVERLOAD_WEEKS = 3
PATHS_PER_LEAF = 2
FIXED = Path(__file__).with_name("data") / "fixed_paths.json"
DECIMALS = {"cash": 0, "load": 3, "incidents": 0, "arr": 0, "money": 0, "progress": 4, "spend": 0, "fte": 2}


def _r(x: float, metric: str) -> float:
    d = DECIMALS[metric]
    return round(float(x)) if d == 0 else round(float(x), d)


def _money(x: float) -> str:
    return f"${x / 1e6:.2f}M" if abs(x) >= 1e6 else f"${x / 1e3:.0f}k"


HIDDEN = {"key", "_label"}  # internal fields of an event row


def _pct(x: float) -> str:
    return f"{abs(x) * 100:.0f}%" if abs(x) >= 0.01 else f"{abs(x) * 100:.1f}%"


def shock_text(code: str, title: str, size: float | None) -> str:
    """A case shock as people read it: "Currency move raises offshore cost by about 9%"."""
    if size is None:
        return title
    var = code.split(":", 1)[-1]
    if var == "gpu_shortage":
        return f"{title}: about {_pct(size)} less GPU capacity"
    if var == "regulation_cost":
        return f"{title}: about {_money(size)} a year"
    if var == "reputation_hit":
        return f"{title}: churn up about {abs(size) * 100:.0f} points"
    return f"{title} by about {_pct(size)}"


# ---------------------------------------------------------------------------------------------- material events
def material_events(
    events: Sequence[tuple[int, Mapping[str, Any]]],
    series: Mapping[str, np.ndarray],
    *,
    people: Mapping[str, Mapping[str, Any]],
    names: Mapping[str, str],
    hire_count: int,
    cash_low: float,
    overload: float,
) -> list[dict[str, Any]]:
    """The events of one path with ``material`` flags and a ``key`` for the material ones (first occurrence)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    joins = 0

    def add(
        week: int, e: Mapping[str, Any], key: str | None, text: str | None = None, label: str | None = None
    ) -> None:
        mat = key is not None and key not in seen
        if mat:
            seen.add(key)
        row = {
            "week": int(week),
            "type": e.get("type", "event"),
            "text": text or e.get("text", ""),
            "material": mat,
        }
        if e.get("text") and row["text"] != e["text"]:
            row["raw"] = e["text"]  # the text of the engine (WP19 parses it in the v1 tree)
        if mat:
            row["key"] = key
            if label:
                row["_label"] = label  # the split label: the same for every path (no size)
        for k in ("entityId", "actor"):
            if e.get(k):
                row[k] = e[k]
        out.append(row)

    for week, e in events:
        code = e.get("code", "")
        if code == "notice":
            big = e.get("key") or e.get("acv", 0) >= BIG_CLIENT
            name = names.get(e.get("actor", ""), "A new client")
            add(
                week,
                e,
                f"notice:{e.get('actor')}" if big else None,
                f"{name} gives notice (ACV {_money(e.get('acv', 0))})",
            )
        elif code == "resign":
            who = people.get(e.get("actor", ""), {})
            add(week, e, f"resign:{e['actor']}" if who.get("material") else None)
        elif code == "incident":
            if e.get("severity") == "SEV1":
                major = e.get("hours", 0) >= MAJOR_SEV1_HOURS
                add(week, e, "sev1_major" if major else None, f"SEV1 outage ({e.get('hours', 0):.1f} h)")
            elif e.get("severity") == "SEV2":
                add(week, e, None, f"SEV2 incident ({e.get('hours', 0):.1f} h)")
        elif code in ("deal_won", "deal_lost"):
            if e.get("pipeline"):  # a deal of the pipeline at T, by name
                add(week, e, f"{code}:{e.get('actor')}")
            elif code == "deal_won" and e.get("value", 0) >= BIG_CLIENT:  # lost new deals are routine
                add(week, e, "deal_won:big", f"A big new deal won (ACV {_money(e['value'])})")
            else:
                add(week, e, None)
        elif code.startswith("shock:"):
            title = str(e.get("text", code)).split(":")[0]
            add(week, e, code, shock_text(code, title, e.get("size")), title)
        elif code == "hire_join":
            joins += 1
            add(week, e, None)
            if hire_count and joins == hire_count:
                add(week, {"type": "decision"}, "hires_done", f"All {hire_count} new engineers on board")
        elif code in ("upsell", "downgrade", "backfill_join"):
            add(week, e, None)
    cash, load = series["cash"], series["load"]
    low = np.nonzero(cash[1:] < cash_low)[0]
    if low.size:
        add(int(low[0]) + 1, {"type": "money"}, "cash_low", f"Cash falls below {_money(cash_low)}")
    over = load[1:] > overload
    run = np.convolve(over.astype(int), np.ones(OVERLOAD_WEEKS, dtype=int), "valid")
    hit = np.nonzero(run >= OVERLOAD_WEEKS)[0]
    if hit.size:
        add(
            int(hit[0]) + OVERLOAD_WEEKS,
            {"type": "people"},
            "overload",
            f"Team over capacity for {OVERLOAD_WEEKS} weeks in a row",
        )
    out.sort(key=lambda r: (r["week"], not r["material"]))
    return out


GENERIC = {
    "deal_won:big": "A big new deal won (ACV $150k or more)",
    "sev1_major": "Major SEV1 outage (4 h or more)",
    "scope_creep": "The business adds features (scope creep)",
}
NOT_A_CAUSE = {"cash_low", "over_budget", "late"}  # consequences: material events of the paths, never a split


def label_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """key -> the split description (type, text, entityId, actor) from the first row with that key."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("key") and r["key"] not in out:
            out[r["key"]] = {k: r[k] for k in ("type", "text", "entityId", "actor") if k in r}
            if r.get("_label"):
                out[r["key"]]["text"] = r["_label"]
            if r["key"] in GENERIC:
                out[r["key"]]["text"] = GENERIC[r["key"]]
            elif r["key"].startswith("notice:"):
                out[r["key"]]["text"] = r["text"].split(" (")[0]
    return out


# ---------------------------------------------------------------------------------------------- the tree
def build_tree(
    keyweeks: Sequence[Mapping[str, int]],
    labels: Mapping[str, Mapping[str, Any]],
    series: Mapping[str, np.ndarray],
    weeks: int,
    metrics: Sequence[str] = METRICS,
    outcome_metric: str = "cash",
) -> list[dict[str, Any]]:
    """Nodes of the event tree. ``keyweeks[s]``: material key -> week of path s; ``series[m]``: (n, weeks + 1).
    A split takes the event that explains the most variance of ``outcome_metric`` at the horizon."""
    n = len(keyweeks)
    nodes: list[dict[str, Any]] = []
    min_n = MIN_SHARE * n
    outcome = series[outcome_metric][:, -1]  # a split separates the outcome at the horizon most

    def remaining(ids: np.ndarray, frm: int, used: set[str]) -> Counter:
        c: Counter = Counter()
        for s in ids:
            for k, w in keyweeks[s].items():
                if w >= frm and k not in used:
                    c[k] += 1
        return c

    def make(
        ids: np.ndarray, parent: str | None, frm: int, split: dict | None, depth: int, used: set[str]
    ) -> None:
        node = {
            "id": f"n{len(nodes)}",
            "parent": parent,
            "from": int(frm),
            "to": int(weeks),
            "share": round(len(ids) / n, 6),
            "n": int(len(ids)),
            "split": split,
            "_ids": np.asarray(ids, dtype=int),
        }
        nodes.append(node)
        if len(ids) < SPLIT_SHARE * n or depth >= MAX_DEPTH or len(nodes) + 2 > MAX_NODES or frm >= weeks - 1:
            return
        counts = remaining(ids, frm, used)
        y = outcome[ids]
        best = []
        for k, c in counts.items():
            if k in NOT_A_CAUSE or not min_n <= c <= len(ids) - min_n:
                continue
            mask = np.array([keyweeks[s].get(k, -1) >= frm for s in ids])
            p = mask.mean()
            best.append(
                (p * (1 - p) * (y[mask].mean() - y[~mask].mean()) ** 2, k)
            )  # the cash variance it explains
        if best:
            _, key = max(best, key=lambda x: (x[0], x[1]))
            ws = np.array([keyweeks[s][key] for s in ids if keyweeks[s].get(key, -1) >= frm])
            at = int(min(max(round(float(np.median(ws))), frm + 1), weeks - 1))
            has = np.array([s for s in ids if keyweeks[s].get(key, -1) >= frm])
            rest = np.setdiff1d(ids, has)
            node["to"] = at
            lab = dict(labels.get(key, {"type": "event", "text": key}))
            sp = {
                "week": at,
                **lab,
                "key": key,
                "weekP10": int(np.quantile(ws, 0.1)),
                "weekP90": int(np.quantile(ws, 0.9)),
            }
            make(has, node["id"], at, sp, depth + 1, used | {key})
            make(
                rest,
                node["id"],
                at,
                {
                    "week": at,
                    "type": lab.get("type", "event"),
                    "text": "No such event",
                    "of": lab.get("text", key),
                    "rest": True,
                    "key": key,
                },
                depth + 1,
                used | {key},
            )
            return
        anyk = [
            s
            for s in ids
            if any(w >= frm and k not in used and k not in NOT_A_CAUSE for k, w in keyweeks[s].items())
        ]
        if min_n <= len(anyk) <= len(ids) - min_n:
            first = [
                min(w for k, w in keyweeks[s].items() if w >= frm and k not in used and k not in NOT_A_CAUSE)
                for s in anyk
            ]
            at = int(min(max(round(float(np.median(first))), frm + 1), weeks - 1))
            node["to"] = at
            items = [{"text": labels.get(k, {}).get("text", k), "n": c} for k, c in counts.most_common(8)]
            make(
                np.array(anyk),
                node["id"],
                at,
                {"week": at, "type": "other", "text": "Other events", "other": True, "items": items},
                MAX_DEPTH,
                used,
            )
            make(
                np.setdiff1d(ids, anyk),
                node["id"],
                at,
                {"week": at, "type": "other", "text": "No such event", "of": "Other events", "rest": True},
                MAX_DEPTH,
                used,
            )

    make(np.arange(n), None, 0, None, 0, set())
    for nd in nodes:
        ids, lo, hi = nd["_ids"], nd["from"], nd["to"]
        nd["bands"] = {
            m: [
                [w, *(_r(x, m) for x in np.quantile(series[m][ids, w], [0.1, 0.5, 0.9]))]
                for w in range(lo, hi + 1)
            ]
            for m in metrics
        }
    return nodes


def _used(nd: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]) -> set[str]:
    used = set()
    while nd.get("parent"):
        if nd["split"] and nd["split"].get("key"):
            used.add(nd["split"]["key"])
        nd = by_id[nd["parent"]]
    return used


def place(keyweeks: Mapping[str, int], nodes: Sequence[Mapping[str, Any]]) -> list[str]:
    """Node ids root -> leaf of a path with these material events (the build rule)."""
    by_id = {nd["id"]: nd for nd in nodes}
    children: dict[str | None, list] = {}
    for nd in nodes:
        children.setdefault(nd["parent"], []).append(nd)
    nd = children[None][0]
    chain = [nd["id"]]
    while children.get(nd["id"]):
        a, b = children[nd["id"]]
        frm = nd["from"]
        if a["split"].get("other"):
            used = _used(nd, by_id)
            go = any(w >= frm and k not in used and k not in NOT_A_CAUSE for k, w in keyweeks.items())
        else:
            go = keyweeks.get(a["split"]["key"], -1) >= frm
        nd = a if go else b
        chain.append(nd["id"])
    return chain


# ---------------------------------------------------------------------------------------------- one fork
def event_tree(
    *,
    option: str,
    label: str,
    seed: int,
    rows: Sequence[Sequence[Mapping[str, Any]]],
    series: Mapping[str, np.ndarray],
    fixed: Sequence[Mapping[str, Any]] = (),
    unit: str = "week",
    metrics: Sequence[str] = METRICS,
    outcome_metric: str = "cash",
    keep: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The EventTree of one fork. ``rows[s]``: the events of path s (``material_events``); ``series[m]``:
    (n, steps + 1); ``fixed``: pinned paths (already replayed: kind, scenario, seed, events, weeks, extra fields)."""
    n, weeks = series[outcome_metric].shape[0], series[outcome_metric].shape[1] - 1
    keyweeks = [{r["key"]: r["week"] for r in rs if r.get("key")} for rs in rows]
    labels: dict[str, dict[str, Any]] = {}
    for rs in rows:
        for k, v in label_of(rs).items():
            labels.setdefault(k, v)
    nodes = build_tree(keyweeks, labels, series, weeks, metrics, outcome_metric)
    leaves = [nd for nd in nodes if not any(x["parent"] == nd["id"] for x in nodes)]
    cash_end = series[outcome_metric][:, -1]
    picks: dict[int, str | None] = {}
    for leaf in leaves:
        ids = leaf["_ids"]
        if ids.size:
            med = np.median(cash_end[ids])
            for s in ids[np.argsort(np.abs(cash_end[ids] - med))][:PATHS_PER_LEAF]:
                picks.setdefault(int(s), None)
    kinds = {"pessimistic": 0.1, "median": 0.5, "optimistic": 0.9}
    fixed_kinds = {f["kind"] for f in fixed}
    for kind, q in kinds.items():
        if kind not in fixed_kinds:
            s = int(np.argmin(np.abs(cash_end - np.quantile(cash_end, q))))
            picks[s] = kind
    paths = [path_of(s, rows, series, nodes, seed, metrics, kind) for s, kind in sorted(picks.items())]
    for f in fixed:
        kw = {r["key"]: r["week"] for r in f["events"] if r.get("key")}
        chain = place(kw, nodes)
        paths.append({**{k: v for k, v in f.items() if k != "events"}, "leaf": chain[-1], "nodes": chain,
                      "events": [{k: v for k, v in r.items() if k not in HIDDEN} for r in f["events"]]})  # fmt: skip
    for nd in nodes:
        nd["paths"] = [p["id"] for p in paths if nd["id"] in p["nodes"]][:3]
    if keep is not None:  # the raw data for the drill-down of any path (the live API)
        keep.update({"nodes": nodes, "rows": rows, "series": series, "metrics": list(metrics), "seed": seed})
    nodes = [
        {
            **{k: v for k, v in nd.items() if k != "_ids"},
            "split": {k: v for k, v in nd["split"].items() if k != "key"} if nd["split"] else None,
        }
        for nd in nodes
    ]
    return {
        "version": VERSION, "unit": unit, "option": option, "label": label, "n": int(n), "weeks": int(weeks),
        "metrics": list(metrics), "outcome": outcome_metric, "minShare": MIN_SHARE, "nodes": nodes, "paths": paths,
    }  # fmt: skip


def path_of(
    s: int,
    rows: Sequence[Sequence[Mapping[str, Any]]],
    series: Mapping[str, np.ndarray],
    nodes: Sequence[Mapping[str, Any]],
    seed: int,
    metrics: Sequence[str] = METRICS,
    kind: str | None = None,
) -> dict[str, Any]:
    """The drill-down of path s: its node chain, its events (material flagged) and its series."""
    keyweeks = {r["key"]: r["week"] for r in rows[s] if r.get("key")}
    chain = place(keyweeks, nodes)
    steps = series[metrics[0]].shape[1]
    return {
        "id": f"s{s}", **({"kind": kind} if kind else {}), "scenario": int(s), "seed": seed, "leaf": chain[-1], "nodes": chain,
        "events": [{k: v for k, v in r.items() if k not in HIDDEN} for r in rows[s]],
        "weeks": [{"week": w, **{m: _r(series[m][s, w], m) for m in metrics}} for w in range(steps)],
    }  # fmt: skip


WEEK_FAN = 26  # the end of the day window (WP19): the day examples are compared with this week


def week_fan(cash: np.ndarray, anchor: Any, n: int, week: int = WEEK_FAN) -> dict[str, Any]:
    """Cash quantiles of the week ensemble at ``week`` (the date of the day window end)."""
    from datetime import timedelta

    w = min(week, cash.shape[1] - 1)
    qs = np.quantile(cash[:, w], [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95])
    keys = ("p5", "p10", "p25", "p50", "p75", "p90", "p95")
    return {"week": w, "date": (anchor + timedelta(weeks=w)).isoformat(), "n": int(n),
            "cash": {k: round(float(v)) for k, v in zip(keys, qs, strict=True)}}  # fmt: skip


def fixed_paths() -> dict[str, Any]:
    """The pinned v1 leaves of the day level; none when the file is not installed (an image without it)."""
    if not FIXED.is_file():
        return {"paths": []}
    return json.loads(FIXED.read_text(encoding="utf-8"))


def company_context(model: Any, cfg: Mapping[str, Any], fork: Any) -> dict[str, Any]:
    """What makes an event of case B material: the people (engineers and executives), names, the fork's
    hires, the cash goal and the overload threshold."""
    p = model.world.people
    execs = {a["id"] for a in model.habits.actor_list("person") if a.get("executive")}
    people = {pid: {"material": bool(p.engineer[i]) or pid in execs} for i, pid in enumerate(p.ids)}
    names = dict(zip(model.world.clients.ids, model.world.clients.names, strict=True))
    names.update({d["client"]: d["name"] for d in model.world.pipeline})
    goal = next(
        (float(v["dist"].get("value", 1.5e6)) for v in cfg.get("variables", []) if v["name"] == "g_cash"),
        1.5e6,
    )
    hires = sum(int(i.value) for i in fork.interventions if i.target.startswith("role:") and i.op == "add")
    return {
        "people": people,
        "names": names,
        "hire_count": hires,
        "cash_low": goal,
        "overload": model.params["burnout_load"].point,
    }


_REPLAYS: dict[tuple, Any] = {}  # (habits, fork, leaf) -> the replayed steps (a pinned path never changes)


def replay_fixed(
    model: Any, fork: Any, spec: Mapping[str, Any], context: Mapping[str, Any]
) -> dict[str, Any]:
    """A pinned v1 tree leaf (WP19's day branch) replayed: the same seed and node keys as before."""
    from variator.future.tree import replay

    key = (model.habits.sha256, fork.id, repr(fork.interventions), spec["leaf"], int(spec["seed"]))
    if key not in _REPLAYS:
        _REPLAYS[key] = replay(model, fork, int(spec["seed"]), spec["keys"])
    steps = _REPLAYS[key]
    load0 = model.params["team_load_now"].point
    series = {
        m: np.array([s["values"].get(m, load0 if m == "load" else 0.0) for s in steps], dtype=float)
        for m in METRICS
    }
    events = [(s["week"], e) for s in steps for e in s["events"]]
    rows = material_events(events, series, **context)
    return {
        "id": f"fixed-{spec['kind']}", "kind": spec["kind"], "scenario": int(spec["leaf"].split("n", 1)[1]) + 1,
        "seed": int(spec["seed"]), "fixed": True, "treeLeaf": spec["leaf"], "keys": spec["keys"],
        "note": "the week-tree v1 leaf of WP19's day branch, replayed (parameters at their medians); not one of the n ensemble paths",
        "events": rows, "weeks": [{"week": w, **{m: _r(series[m][w], m) for m in METRICS}} for w in range(len(steps))],
    }  # fmt: skip


def company_trees(
    model: Any,
    cfg: Mapping[str, Any],
    forks: Sequence[Any],
    *,
    n: int,
    seed: int,
    weeks: int = 52,
    layers: Sequence[Any] = (),
    cash_scale: float = 1.0,
    pinned: bool = True,
    keep: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """EventTrees of case B for these forks (one week ensemble for all of them: common random numbers)."""
    from variator.future.forks import compile_knobs

    knobs = compile_knobs(list(forks), layers)
    knobs.cash_scale *= cash_scale
    res = model.run_weeks(knobs, n, seed, weeks=weeks)
    spec = fixed_paths() if pinned else {"paths": []}
    out = {}
    for j, fork in enumerate(forks):
        ctx = company_context(model, cfg, fork)
        fixed = (
            [replay_fixed(model, fork, p, ctx) for p in spec["paths"] if p["fork"] == fork.id]
            if not layers
            else []
        )
        series = {m: res.metrics[m][j] for m in METRICS}
        rows = [
            material_events(res.events[j][s], {m: series[m][s] for m in METRICS}, **ctx) for s in range(n)
        ]
        tree_fan = week_fan(series["cash"], model.world.anchor, n)
        raw: dict[str, Any] | None = {} if keep is not None else None
        out[fork.id] = event_tree(
            option=fork.id, label=fork.label, seed=seed, rows=rows, series=series, fixed=fixed, keep=raw
        )
        out[fork.id]["weekFan"] = tree_fan
        if keep is not None:
            keep[fork.id] = raw
    return out
