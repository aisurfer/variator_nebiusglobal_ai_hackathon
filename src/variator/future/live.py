"""Live month run for a page state: the case config with the user's edits -> forks, layers, placed events ->
the month Monte Carlo on actors -> ``month.json`` (the API route ``web/month.py``).

The state has the shape of ``CaseState`` (workspace/src/variator/engine/types.ts): ``activePresets``,
``edits`` (path -> value), ``prefs`` (weights, risk aversion), ``timeline`` (moves, events, off), ``n``,
``seed``; and ``forks`` (the forks that are on; default all). Same state and seed give the same result;
results are cached by a hash of the normalised state.
"""

# ruff: noqa: E501 - error texts, one per line

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from variator.future import export
from variator.future.forks import case_config, company_forks, company_layers, compile_knobs, placed_events
from variator.future.params import ACTORS_DIR, HISTORY_DIR, Habits

CASES = ("company_best_year", "delivery_best_way")
MAX_N, MIN_N, DEFAULT_N, DEFAULT_SEED = 2000, 50, 1000, 7
CACHE_SIZE = 32
INCLUDE = ("week",)  # optional parts of the answer
LIVE_TREE_NODES = 60  # nodes per tree level in the live answer (about 0.3 s per fork; the page draws it well)
MAX_WEEK_FORKS, WEEK_N, MAX_WEEK_N = 3, 500, 1000  # the live event trees: forks, paths per fork
DIST_KEYS = {"value", "low", "mode", "high", "mean", "std", "p"}
# case B variables the actor engine reads (the rest are inputs of the formula model; the actors learn them)
READ_B = {
    "tech_debt", "k_inc_debt", "debt_lead", "gpu_setup", "latency_slo", "k_lat_gpu", "k_lat_debt", "k_infra_gpu",
    "budget", "sla_target", "burnout_load", "burnout_attrition", "runway_red", "runway_green", "runway_cap",
    "m_debt", "m_hire", "m_gpu", "m_price", "hire_count", "debt_share", "engineer_cost", "gpu_reserve_cost",
    "price_change", "cash0", "ev_gpu_month", "ev_gpu_size", "ev_vendor_month", "ev_vendor_size", "ev_account_month",
    "ev_account_size", "ev_kp_month", "g_cash", "g_churn",
}  # fmt: skip


class StateError(ValueError):
    """The page state is not valid (the API answers 400)."""


_lock = threading.Lock()
_base: dict[tuple[str, str], tuple[Habits, Any]] = {}
_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
# simulation state -> the simulated cube with its doc: a change of the weights, the caution or md_* ranks it again
_sims: OrderedDict[str, dict[str, Any]] = OrderedDict()
SIM_CACHE_SIZE = 16


def sim_state(st: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a state that the simulation reads: without the weights, the caution, the meaningful
    differences (``md_*``) and the optional parts (``include``)."""
    out = dict(st)
    out["weights"], out["riskAversion"], out["include"], out["weekForks"], out["weekN"] = {}, None, [], [], 0
    out["edits"] = {p: x for p, x in st["edits"].items() if not p.startswith("v|md_")}
    return out


def prewarm(actors: str | Path = ACTORS_DIR, history: str | Path = HISTORY_DIR) -> dict[str, float]:
    """Simulate the default state of every case (the first rerank of the page is then instant)."""
    out = {}
    for case in CASES:
        t = time.perf_counter()
        run_state(case, {}, actors=actors, history=history)
        forks = [f["id"] for f in case_config(case).get("forks", [])]
        for i in range(0, len(forks), MAX_WEEK_FORKS):  # the event trees of every fork of the defaults
            run_state(
                case,
                {"include": ["week"], "weekForks": forks[i : i + MAX_WEEK_FORKS]},
                actors=actors,
                history=history,
            )
        out[case] = round(time.perf_counter() - t, 3)
    return out


def _world(actors: Path, history: Path) -> tuple[Habits, Any]:
    from variator.future.state import build_world

    key = (str(actors), str(history))
    with _lock:
        if key not in _base:
            habits = Habits.load(actors)
            _base[key] = (habits, build_world(habits, history))
        return _base[key]


# ---------------------------------------------------------------------------------------------- state
def _num(x: Any, what: str) -> float:
    if isinstance(x, bool):
        return float(x)
    if not isinstance(x, int | float) or not math.isfinite(float(x)):
        raise StateError(f"{what} must be a finite number")
    return float(x)


def _str_list(x: Any, what: str) -> list[str]:
    if x is None:
        return []
    if not isinstance(x, list) or not all(isinstance(i, str) for i in x):
        raise StateError(f"{what} must be a list of strings")
    return list(x)


def normalize(case_id: str, body: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict[str, Any]:
    """A canonical, validated state (sorted, defaults filled); raises ``StateError``."""
    if case_id not in CASES:
        raise StateError(f"unknown case {case_id!r}")
    if not isinstance(body, Mapping):
        raise StateError("the body must be a JSON object")
    forks_all = [f["id"] for f in cfg.get("forks", [])]
    presets_all = [h["id"] for h in cfg.get("hypotheses", [])] + (
        ["payroll_tax_july"] if case_id == CASES[0] else []
    )
    criteria = [c["name"] for c in cfg.get("criteria", [])]
    variables = {v["name"] for v in cfg.get("variables", [])}
    options = {o["name"] for o in cfg.get("options", [])}
    risks = {r["name"] for r in cfg.get("risks", [])}
    items = set(cfg.get("items", {})) | {i["name"] for i in (cfg.get("timeline") or {}).get("items", [])}
    forks = _str_list(body.get("forks"), "forks") or forks_all
    for f in forks:
        if f not in forks_all:
            raise StateError(f"unknown fork {f!r}")
    # the baseline fork is always on, as on the page: the ranking anchors its scale and its ties on it
    base = next(
        (f["id"] for f in cfg.get("forks", []) if f.get("baseline")), forks_all[0] if forks_all else None
    )
    presets = _str_list(body.get("activePresets"), "activePresets")
    for p in presets:
        if p not in presets_all:
            raise StateError(f"unknown hypothesis {p!r}")
    timeline = body.get("timeline") or {}
    if not isinstance(timeline, Mapping):
        raise StateError("timeline must be an object")
    events = _str_list(timeline.get("events"), "timeline.events")
    off = _str_list(timeline.get("off"), "timeline.off")
    for e in events + off:
        if e not in items:
            raise StateError(f"unknown timeline item {e!r}")
    moves = timeline.get("moves") or {}
    if not isinstance(moves, Mapping):
        raise StateError("timeline.moves must be an object")
    edits: dict[str, float] = {}
    for var, x in moves.items():
        if var not in variables:
            raise StateError(f"unknown month variable {var!r}")
        edits[f"v|{var}|value"] = _num(x, f"timeline.moves.{var}")
    prefs = body.get("prefs") or {}
    if not isinstance(prefs, Mapping):
        raise StateError("prefs must be an object")
    weights = {}
    for c, w in (prefs.get("weights") or {}).items():
        if c not in criteria:
            raise StateError(f"unknown criterion {c!r}")
        weights[c] = _num(w, f"weight of {c}")
        if weights[c] < 0:
            raise StateError("weights must be >= 0")
    ra = prefs.get("riskAversion")
    raw_edits = body.get("edits") or {}
    if not isinstance(raw_edits, Mapping):
        raise StateError("edits must be an object")
    for path, x in raw_edits.items():
        parts = str(path).split("|")
        head = parts[0]
        if head == "w" and len(parts) == 2:
            if parts[1] == "__risk":
                ra = x
            elif parts[1] in criteria:
                weights[parts[1]] = _num(x, path)
            else:
                raise StateError(f"unknown criterion in {path!r}")
            continue
        if head == "preset" and len(parts) == 2:
            if parts[1] not in presets_all:
                raise StateError(f"unknown hypothesis in {path!r}")
            if bool(x) and parts[1] not in presets:
                presets.append(parts[1])
            continue
        ok = (
            (head == "v" and len(parts) == 3 and parts[1] in variables and parts[2] in DIST_KEYS)
            or (
                head == "o"
                and len(parts) == 4
                and parts[1] in options
                and parts[2] in variables | {"price_change"}
                and parts[3] in DIST_KEYS
            )
            or (head == "r" and len(parts) == 3 and parts[1] in risks and parts[2] == "p")
            or (head == "rm" and len(parts) == 3 and parts[1] in risks and parts[2] in DIST_KEYS)
        )
        if not ok:
            raise StateError(f"unknown edit path {path!r}")
        edits[str(path)] = _num(x, path)
    if ra is not None:
        ra = _num(ra, "riskAversion")
        if not 0 <= ra <= 1:
            raise StateError("riskAversion must be in [0, 1]")
    n = body.get("n", DEFAULT_N)
    seed = body.get("seed", DEFAULT_SEED)
    if isinstance(n, bool) or not isinstance(n, int) or n < MIN_N:
        raise StateError(f"n must be an integer >= {MIN_N}")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
        raise StateError("seed must be an integer in [0, 2^31)")
    include = _str_list(body.get("include"), "include")
    for i in include:
        if i not in INCLUDE:
            raise StateError(f"unknown include {i!r} (known: {', '.join(INCLUDE)})")
    week_forks = _str_list(body.get("weekForks"), "weekForks")
    if len(week_forks) > MAX_WEEK_FORKS or any(f not in forks_all for f in week_forks):
        raise StateError(f"weekForks: at most {MAX_WEEK_FORKS} known fork ids")
    week_n = body.get("weekN", WEEK_N)
    if isinstance(week_n, bool) or not isinstance(week_n, int) or not MIN_N <= week_n <= MAX_WEEK_N:
        raise StateError(f"weekN must be an integer in [{MIN_N}, {MAX_WEEK_N}]")
    return {
        "include": sorted(set(include)),
        "weekForks": [f for f in forks_all if f in week_forks],
        "weekN": week_n,
        "case": case_id,
        "forks": [f for f in forks_all if f in forks or f == base],
        "presets": [p for p in presets_all if p in presets],
        "events": sorted(set(events)),
        "off": sorted(set(off)),
        "edits": dict(sorted(edits.items())),
        "weights": dict(sorted(weights.items())),
        "riskAversion": ra,
        "n": min(n, MAX_N),
        "nCapped": n > MAX_N,
        "seed": seed,
    }


def apply_state(cfg: Mapping[str, Any], st: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the case config with the edits, weights and risk aversion of the state."""
    out = copy.deepcopy(dict(cfg))
    var = {v["name"]: v for v in out.get("variables", [])}
    opt = {o["name"]: o for o in out.get("options", [])}
    rsk = {r["name"]: r for r in out.get("risks", [])}
    for path, x in st["edits"].items():
        parts = path.split("|")
        if parts[0] == "v":
            d = var[parts[1]]["dist"]
            if d.get("kind") == "fixed" and parts[2] != "value":
                d.update({"kind": "pert", "low": d["value"], "mode": d["value"], "high": d["value"]})
            d[parts[2]] = x
            if d.get("kind") == "fixed":
                d["value"] = x
        elif parts[0] == "o":
            ov = opt[parts[1]].setdefault("overrides", {}).setdefault(parts[2], {"kind": "fixed", "value": x})
            ov[parts[3]] = x
        elif parts[0] == "r":
            rsk[parts[1]]["p"] = x
        elif parts[0] == "rm":
            rsk[parts[1]]["magnitude"][parts[2]] = x
    d = out.setdefault("defaults", {})
    d["weights"] = {**d.get("weights", {}), **st["weights"]}
    if st["riskAversion"] is not None:
        d["riskAversion"] = st["riskAversion"]
    return out


def state_hash(st: Mapping[str, Any], habits_sha: str) -> str:
    blob = json.dumps({"state": st, "habits": habits_sha, "engine": export.ENGINE}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _clean(x: Any) -> Any:
    """JSON-safe: NaN and infinity become null; NumPy scalars become Python numbers."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_clean(v) for v in x]
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


# ---------------------------------------------------------------------------------------------- run
def run_state(
    case_id: str,
    body: Mapping[str, Any],
    *,
    actors: str | Path = ACTORS_DIR,
    history: str | Path = HISTORY_DIR,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    cfg = case_config(case_id) if case_id in CASES else {}
    st = normalize(case_id, body, cfg)
    habits, world = _world(Path(actors), Path(history))
    key = state_hash(st, habits.sha256)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            doc = copy.deepcopy(_cache[key])
            doc["provenance"]["cached"] = True
            return doc
    cfg2 = apply_state(cfg, st)
    skey = state_hash(sim_state(st), habits.sha256)
    with _lock:
        sim = _sims.get(skey)
        if sim is not None:
            _sims.move_to_end(skey)
    if sim is not None:  # the same simulation: rank its cube again for the weights, caution and md_*
        t1 = time.perf_counter()
        doc = copy.deepcopy(sim["doc"])
        doc["ranking"] = sim["decide"](cfg2)
        doc["timings"] = {"rerank_s": round(time.perf_counter() - t1, 3)}
        reranked = True
    else:
        doc, sim = (
            _run_company(cfg2, st, habits, world)
            if case_id == CASES[0]
            else _run_delivery(cfg2, st, habits, world)
        )
        with _lock:
            _sims[skey] = sim
            while len(_sims) > SIM_CACHE_SIZE:
                _sims.popitem(last=False)
        reranked = False
    if "week" in st["include"]:
        t3 = time.perf_counter()
        doc["eventTrees"] = _event_trees(sim, st, doc)
        for f, t in doc["eventTrees"].items():
            if t.get("weekFan") and f in doc["options"]:
                doc["options"][f]["weekFan"] = t["weekFan"]
        doc["timings"]["week_tree_s"] = round(time.perf_counter() - t3, 3)
    ignored = sorted(
        {
            p.split("|")[1]
            for p in st["edits"]
            if p.startswith("v|")
            and case_id == CASES[0]
            and p.split("|")[1] not in READ_B
            and not p.split("|")[1].startswith("md_")  # meaningful differences: the ranking reads them
        }
    )
    doc["provenance"].update(
        {
            "live": True,
            "stateHash": key,
            "simulationHash": skey,
            "cached": False,
            "reranked": reranked,
            "state": st,
            "ignoredEdits": ignored,
        }
    )
    if ignored:
        doc.setdefault("notes", []).append(
            "Edits of "
            + ", ".join(ignored)
            + " do not change the actor run: the actors learn these from the history."
        )
    if st["nCapped"]:
        doc.setdefault("notes", []).append(f"n is capped at {MAX_N}.")
    doc["timings"] = {**doc.get("timings", {}), "total_s": round(time.perf_counter() - t0, 3)}
    doc = _clean(doc)
    with _lock:
        _cache[key] = copy.deepcopy(doc)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return doc


def _drop_off(doc: dict[str, Any], off: list[str]) -> None:
    for o in doc["options"].values():
        o["riskCalendar"] = [r for r in o["riskCalendar"] if r["id"] not in off and r.get("ref") not in off]


def _run_company(
    cfg: dict[str, Any], st: Mapping[str, Any], habits: Habits, world: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(month.json of the state, the simulation entry: the doc, ``decide(cfg)`` on the cube, the tree builder)."""
    from variator.future.company import CompanyModel

    t0 = time.perf_counter()
    model = CompanyModel(habits, world, cfg)
    forks = [f for f in company_forks(cfg) if f.id in st["forks"]]
    layers = [x for x in company_layers(cfg) if x.id in st["presets"]]
    if st["events"]:
        layers.append(placed_events(cfg, st["events"]))
    knobs = compile_knobs(forks, layers)
    base_cash = next(
        (
            float(v["dist"].get("value", 0))
            for v in case_config(st["case"]).get("variables", [])
            if v["name"] == "cash0"
        ),
        0.0,
    )
    new_cash = next(
        (float(v["dist"].get("value", 0)) for v in cfg.get("variables", []) if v["name"] == "cash0"), 0.0
    )
    edit_cash_scale = new_cash / base_cash if base_cash > 0 and new_cash != base_cash else 1.0
    knobs.cash_scale *= edit_cash_scale
    t1 = time.perf_counter()
    res = model.run(knobs, st["n"], st["seed"])
    t2 = time.perf_counter()
    anchor = date.fromisoformat(cfg.get("forkDate", "2026-10-26"))
    cube = export.build_cube(res, f"{st['case']}:live", anchor, st["seed"])
    decision = export.decide(cube, cfg)
    doc = export.month_json(
        res, case_id=st["case"], cfg=cfg, params=model.params, decision=decision,
        provenance={"engine": export.ENGINE, "seed": st["seed"], "n": st["n"], "habits": {"path": habits.path, "as_of": habits.as_of, "sha256": habits.sha256}},
        anchor=anchor, labels={f.id: f.label for f in forks}, forks=[f.as_dict() for f in forks],
        timings={"setup_s": round(t1 - t0, 3), "month_mc_s": round(t2 - t1, 3), "rank_s": round(time.perf_counter() - t2, 3)},
    )  # fmt: skip
    doc["notes"] = list(doc.get("notes", [])) + [f"unmodelled: {u}" for u in knobs.unmodelled]
    _drop_off(doc, st["off"])

    default_world = (
        not st["presets"] and not st["events"] and not [p for p in st["edits"] if not p.startswith("v|md_")]
    )

    def trees(ids: list[str], week_n: int, keep: dict[str, Any]) -> dict[str, Any]:
        from variator.future import events as evt

        chosen = [f for f in forks if f.id in ids]
        return evt.company_trees(
            model, cfg, chosen, n=week_n, seed=st["seed"], layers=layers, cash_scale=edit_cash_scale,
            pinned=default_world, keep=keep,
        )  # fmt: skip

    sim = {
        "doc": copy.deepcopy(doc),
        "decide": lambda c: export.decide(cube, c),
        "trees": trees,
        "tree_cache": {},
    }
    return doc, sim


def _run_delivery(
    cfg: dict[str, Any], st: Mapping[str, Any], habits: Habits, world: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    from variator.future import delivery

    t0 = time.perf_counter()
    params = delivery.delivery_params(habits, cfg)
    layer: dict[str, float] = {}
    for p in st["presets"]:
        for k, v in delivery.LAYERS.get(p, {}).items():
            layer[k] = layer.get(k, 1.0) * v if k.endswith("_scale") else layer.get(k, 0.0) + v
    as_of = date.fromisoformat(habits.as_of)
    res = delivery.simulate(
        params, st["n"], st["seed"], as_of=as_of, layer=layer, forks=st["forks"], crew_=delivery.crew(habits)
    )
    t1 = time.perf_counter()
    line = delivery.charge_slowdown(
        res, habits, world, st["seed"]
    )  # the other work slows: inside in-house money
    t2 = time.perf_counter()
    anchor = date.fromisoformat(cfg.get("forkDate", "2026-10-26"))
    doc = delivery.month_doc(res, params, cfg, anchor=anchor, seed=st["seed"], habits=habits)
    if line is not None:
        doc["costLines"] = {"in_house": {"other_work_slows": line}}
    doc["timings"] = {"month_mc_s": round(t1 - t0, 3), "cost_line_s": round(t2 - t1, 3)}
    if st["events"]:
        doc.setdefault("notes", []).append(
            "Placed events of case A are not modelled on actors yet: " + ", ".join(st["events"])
        )
    _drop_off(doc, st["off"])
    cube = delivery.build_cube(res, anchor, st["seed"])

    def trees(ids: list[str], week_n: int, keep: dict[str, Any]) -> dict[str, Any]:
        out = delivery.event_trees(
            res, st["seed"], keep=keep
        )  # the month grid of the run (weekN does not apply)
        return {f: t for f, t in out.items() if f in ids}

    sim = {
        "doc": copy.deepcopy(doc),
        "decide": lambda c: delivery.decide(cube, c),
        "trees": trees,
        "tree_cache": {},
    }
    return doc, sim


def _event_trees(sim: dict[str, Any], st: Mapping[str, Any], doc: Mapping[str, Any]) -> dict[str, Any]:
    """The event trees of the asked forks (default: the two best of this ranking), cached per simulation."""
    ids = tuple(st["weekForks"]) or tuple(o["option"] for o in doc["ranking"]["options"][:2])
    missing = [f for f in ids if (f, st["weekN"]) not in sim["tree_cache"]]
    if missing:  # one week ensemble for the missing forks; a fork's paths do not depend on the others
        keep: dict[str, Any] = {}
        trees = sim["trees"](missing, st["weekN"], keep)
        for f in missing:
            sim["tree_cache"][(f, st["weekN"])] = (trees[f], keep.get(f))
    return {f: sim["tree_cache"][(f, st["weekN"])][0] for f in ids}


def week_path(
    case_id: str,
    body: Mapping[str, Any],
    fork: str,
    path: str,
    *,
    actors: str | Path = ACTORS_DIR,
    history: str | Path = HISTORY_DIR,
) -> dict[str, Any]:
    """The drill-down of one path of a fork's event tree for a state: ``s<scenario>`` (any of the weekN
    paths) or a listed path id (``fixed-median``): its node chain, events and weekly series."""
    from variator.future import events as evt

    state = {**dict(body), "include": ["week"], "weekForks": [fork]}
    doc = run_state(case_id, state, actors=actors, history=history)
    tree = doc["eventTrees"][fork]
    listed = next((p for p in tree["paths"] if p["id"] == path), None)
    if listed is None:
        if not (path.startswith("s") and path[1:].isdigit()) or int(path[1:]) >= tree["n"]:
            raise StateError(f"unknown path {path!r}: s0..s{tree['n'] - 1} or a listed path id")
        cfg = case_config(case_id)
        st = normalize(case_id, state, cfg)
        habits, _ = _world(Path(actors), Path(history))
        with _lock:
            sim = _sims.get(state_hash(sim_state(st), habits.sha256))
        if sim is None:
            raise StateError("the simulation of this state is not cached; ask again")
        raw = _event_raw(sim, st, fork)
        listed = evt.path_of(
            int(path[1:]), raw["rows"], raw["series"], raw["nodes"], raw["seed"], raw["metrics"]
        )
    return _clean(
        {
            "case": case_id,
            "fork": fork,
            "unit": tree.get("unit", "week"),
            "n": tree["n"],
            "stateHash": doc["provenance"]["stateHash"],
            "path": listed,
        }
    )


def _event_raw(sim: dict[str, Any], st: Mapping[str, Any], fork: str) -> dict[str, Any]:
    if (fork, st["weekN"]) not in sim["tree_cache"]:
        keep: dict[str, Any] = {}
        sim["tree_cache"][(fork, st["weekN"])] = (
            sim["trees"]([fork], st["weekN"], keep)[fork],
            keep.get(fork),
        )
    return sim["tree_cache"][(fork, st["weekN"])][1]
