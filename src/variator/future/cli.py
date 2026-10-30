"""Command line of the future engine.

python -m variator.future.cli run --case company_best_year --n 1000 --seed 7 --out <dir>
python -m variator.future.cli backtest --months 6 --n 1000 --seed 7 --out <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from variator.future import export
from variator.future.company import CompanyModel
from variator.future.forks import case_config, company_forks, company_layers, compile_knobs
from variator.future.params import ACTORS_DIR, HISTORY_DIR, Habits
from variator.future.state import build_world
from variator.metrics.cube import save_cube

REPO = Path(__file__).resolve().parents[3]
OUT = Path("/tmp/variator-1000/mvp-artifacts/future")


def _dump(path: Path, doc: Any) -> str:
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), default=_default) + "\n"
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


def _default(x: Any) -> Any:
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, np.ndarray):
        return x.tolist()
    if hasattr(x, "isoformat"):
        return x.isoformat()
    raise TypeError(f"not JSON serialisable: {type(x)}")


def _history_meta(history: Path) -> dict[str, Any]:
    m = history / "delivery" / "manifest.json"
    if not m.is_file():
        return {"root": str(history)}
    doc = json.loads(m.read_text(encoding="utf-8"))
    return {
        "root": str(history),
        "contract": doc.get("contract"),
        "run_id": doc.get("run_id"),
        "config_hash": doc.get("config_hash"),
    }


def layer_results(model: CompanyModel, cfg: dict, forks: list, n: int, seed: int) -> dict[str, Any]:
    """Per world hypothesis: the ranking of the forks and the cash at the horizon."""
    out = {}
    for layer in company_layers(cfg):
        knobs = compile_knobs(forks, [layer])
        res = model.run(knobs, n, seed)
        cube = export.build_cube(res, f"company_best_year:{layer.id}", model.world.anchor, seed)
        dec = export.decide(cube, cfg)
        out[layer.id] = {
            "label": layer.label,
            "interventions": layer.as_dict()["interventions"],
            "winner": dec["options"][0]["option"],
            "ranking": [
                {k: o[k] for k in ("option", "rank", "score", "pBest", "pFeasible")}
                | {"cashP50": o["stats"]["cash"]["p50"], "churnP50": o["stats"]["churn"]["p50"]}
                for o in dec["options"]
            ],
            "unmodelled": knobs.unmodelled,
        }
    return out


def run_company(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    habits = Habits.load(args.actors)
    cfg = case_config(args.case)
    anchor = date.fromisoformat(cfg.get("forkDate", "2026-10-26"))
    world = build_world(habits, args.history, horizon=args.months, anchor=anchor)
    model = CompanyModel(habits, world, cfg, horizon=args.months)
    forks = company_forks(cfg)
    knobs = compile_knobs(forks)
    labels = {f.id: f.label for f in forks}
    t1 = time.perf_counter()
    res = model.run(knobs, args.n, args.seed)
    t2 = time.perf_counter()
    cube = export.build_cube(res, f"{args.case}:actors", anchor, args.seed)
    decision = export.decide(cube, cfg)
    t3 = time.perf_counter()
    layers = {} if args.no_layers else layer_results(model, cfg, forks, args.n, args.seed)
    t4 = time.perf_counter()
    trees: dict[str, Any] = {}
    tree_doc: dict[str, Any] | None = None
    event_trees: dict[str, Any] = {}
    if not args.no_tree:
        from variator.future import events as evt
        from variator.future import tree as week_tree

        # the v1 tree: the forks and leaves that the day level (WP19) uses, pinned (data/fixed_paths.json)
        fixed = evt.fixed_paths()
        pinned: dict[str, dict[str, int]] = {}
        for p in fixed["paths"]:
            pinned.setdefault(p["fork"], {})[p["kind"]] = int(p["leaf"].split("n", 1)[1])
        ids = {f.id for f in forks}
        if not set(pinned) <= ids:  # a drafted case with other fork ids: the best forks, no pinned leaves
            pinned = {}
        best = list(pinned) or [o["option"] for o in decision["options"][: args.tree_forks]]
        tree_doc = week_tree.build_trees(
            model, forks, best, seed=fixed.get("seed", args.seed), weeks=fixed.get("weeks", args.weeks),
            k=fixed.get("k", args.k), max_nodes=fixed.get("maxNodes", args.max_nodes), pinned=pinned,
        )  # fmt: skip
        trees = tree_doc["weekTrees"]
        t45 = time.perf_counter()
        # the event tree of every fork (WP18c)
        event_trees = evt.company_trees(model, cfg, forks, n=args.week_n, seed=args.seed, weeks=args.weeks)
        print(f"event trees: {time.perf_counter() - t45:.1f}s", file=sys.stderr)
    t5 = time.perf_counter()
    timings = {
        "setup_s": round(t1 - t0, 3),
        "month_mc_s": round(t2 - t1, 3),
        "rank_s": round(t3 - t2, 3),
        "layers_s": round(t4 - t3, 3),
        "week_tree_s": round(t5 - t4, 3),
        "shape": f"{args.n} scenarios x {knobs.n} forks x {args.months} months",
    }
    provenance = {
        "engine": export.ENGINE,
        "seed": args.seed,
        "n": args.n,
        "habits": {"path": habits.path, "as_of": habits.as_of, "sha256": habits.sha256},
        "history": _history_meta(Path(args.history)),
        "code": export.git_commit(REPO),
        "case": args.case,
    }
    doc = export.month_json(
        res, case_id=args.case, cfg=cfg, params=model.params, decision=decision, provenance=provenance,
        anchor=anchor, labels=labels, layers=layers, week_trees=trees, timings=timings,
        forks=[f.as_dict() for f in forks],
    )  # fmt: skip
    if tree_doc is not None:
        doc["dayExamples"] = tree_doc["dayExamples"]
    doc["eventTrees"] = event_trees
    for f, t in event_trees.items():  # the week fan at the end of the day window (WP19)
        doc["options"][f]["weekFan"] = t.get("weekFan")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files = {"month.json": _dump(out / "month.json", doc)}
    save_cube(cube, out / "cube")
    files["cube/cube.json"] = hashlib.sha256((out / "cube" / "cube.json").read_bytes()).hexdigest()
    files["cube/arrays.npz"] = hashlib.sha256((out / "cube" / "arrays.npz").read_bytes()).hexdigest()
    if tree_doc is not None:
        files["week-tree.json"] = _dump(out / "week-tree.json", tree_doc)
    manifest = {
        "id": f"future:{args.case}",
        "schema": "variator.future/1",
        "provenance": provenance,
        "timings": timings,
        "files": files,
        "cube_format": "npz+json (metrics.cube.save_cube; Parquet is not installed)",
        "winner": decision["options"][0]["option"],
    }
    _dump(out / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "out": str(out),
                "timings": timings,
                "ranking": [
                    (o["option"], o["rank"], o["pBest"], o["pFeasible"]) for o in decision["options"]
                ],
            },
            indent=1,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m variator.future.cli", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="month Monte Carlo, ranking, layers and the week tree of a case")
    r.add_argument("--case", default="company_best_year", choices=["company_best_year", "delivery_best_way"])
    r.add_argument("--n", type=int, default=1000)
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--months", type=int, default=12)
    r.add_argument("--out", default=None)
    r.add_argument("--actors", default=str(ACTORS_DIR))
    r.add_argument("--history", default=str(HISTORY_DIR))
    r.add_argument("--no-layers", action="store_true")
    r.add_argument("--no-tree", action="store_true")
    r.add_argument("--tree-forks", type=int, default=2)
    r.add_argument("--weeks", type=int, default=52)
    r.add_argument("--k", type=int, default=3)
    r.add_argument("--max-nodes", type=int, default=200, help="nodes per tree level (09 §13.3: M about 200)")
    r.add_argument("--week-n", type=int, default=1000, help="paths per fork of the week event trees")
    b = sub.add_parser(
        "backtest", help="learn before T - months, simulate to T, coverage of the actual values"
    )
    b.add_argument("--months", type=int, default=6)
    b.add_argument("--n", type=int, default=1000)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--out", default=str(OUT / "backtest"))
    b.add_argument("--history", default=str(HISTORY_DIR))
    args = ap.parse_args(argv)
    if args.cmd == "run":
        args.out = args.out or str(OUT / args.case)
        if args.case == "delivery_best_way":
            from variator.future import delivery

            return delivery.run_cli(args)
        return run_company(args)
    from variator.future import backtest

    return backtest.run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
