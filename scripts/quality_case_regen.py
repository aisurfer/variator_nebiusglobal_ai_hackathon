"""The case re-drafting test of case B (docs/knowledge/case-regen-test.md): runs and the summary.

python scripts/quality_case_regen.py run --configs DIR --label regen   # DIR/run-NNN/config.json
python scripts/quality_case_regen.py run --control 100 --label control  # the hand-written case, 100 seeds
python scripts/quality_case_regen.py summary --label regen

Each run puts its config as ``company_best_year.json`` into a temporary ``VARIATOR_CASES_DIR`` and runs the
engine CLI (the month Monte Carlo n = 1000 with the layers and the week tree, seed 1000 + run index). The
record of the run (``reports/quality/case-regen/<label>/runs.jsonl``) holds the forks, their keys and lever
parameters, the ranking, the tie set (200 bootstrap resamples of the scenarios, re-ranked with
``export.rank_options``), the timings and the errors. Runs already recorded are skipped (resumable).
No model calls here: the drafts come from ``variator.future.design``.
"""

# ruff: noqa: E501 - records and fixtures, one per line

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[1]
CASE = "company_best_year"
HAND_WRITTEN = REPO / "workspace" / "config" / "cases" / f"{CASE}.json"
OUT_ROOT = REPO / "reports" / "quality" / "case-regen"
SEED0 = 1000
BOOT = 200
TIE_SHARE = 0.10
PLAN = "plan"  # the key of a fork without levers (keep the plan)
LEVERS = ("debt", "hire", "gpu", "price")


# ---------------------------------------------------------------------------------------------- keys
def lever_of(target: str, op: str = "") -> str | None:
    """The lever of an intervention, as ``forks.compile_knobs`` reads it (None = not a lever)."""
    if target == "capacity.debt_share":
        return "debt"
    if target.startswith("role:") and op == "add":
        return "hire"
    if target == "vendor:second_cloud":
        return "gpu"
    if target == "price.list":
        return "price"
    return None


def fork_key(interventions: Iterable[Mapping[str, Any]]) -> str:
    """The fork key: the sorted set of its levers joined by ``+``; no lever = ``plan``."""
    levers = {lever_of(str(i.get("target", "")), str(i.get("op", ""))) for i in interventions}
    levers.discard(None)
    return "+".join(x for x in LEVERS if x in levers) or PLAN


def lever_params(interventions: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """The parameters of the fork's levers (the last intervention of a lever wins, as in ``compile_knobs``)."""
    out: dict[str, dict[str, float]] = {}
    for i in interventions:
        lever = lever_of(str(i.get("target", "")), str(i.get("op", "")))
        p = i.get("params") or {}
        at, v = float(i.get("at", 0.0) or 0.0), float(i.get("value", 0.0) or 0.0)
        if lever == "debt":
            out[lever] = {"month": at, "share": v, "cutPerYear": float(p.get("debt_cut_per_year", 0.4))}
        elif lever == "hire":
            out[lever] = {"month": at, "count": v, "costPerYear": float(p.get("cost_per_year", 150_000.0))}
        elif lever == "gpu":
            out[lever] = {"month": at, "costPerYear": v, "relief": float(p.get("relief", 0.7))}
        elif lever == "price":
            out[lever] = {"month": at, "change": v}
    return out


# ---------------------------------------------------------------------------------------------- tie set
def resample_cube(cube: Any, idx: np.ndarray) -> Any:
    """The cube on the scenario rows ``idx`` (a bootstrap resample)."""
    from variator.metrics.cube import MetricCube

    S = cube.values.shape[0]
    return MetricCube(
        run=cube.run, options=list(cube.options), metrics=list(cube.metrics), periods=list(cube.periods),
        values=cube.values[idx], status=None if cube.status is None else cube.status[idx],
        checks={k: v[idx] for k, v in cube.checks.items()},
        inputs={k: (v[idx] if np.shape(v)[:1] == (S,) else v) for k, v in cube.inputs.items()},
        seed=cube.seed, grain=cube.grain, anchor=cube.anchor, notes=list(cube.notes),
    )  # fmt: skip


def first_counts(cube: Any, rank_first: Any, n_boot: int = BOOT, seed: int = 0) -> Counter[str]:
    """How often each fork is ranked first over ``n_boot`` bootstrap resamples of the scenarios.
    ``rank_first(cube) -> option``: the engine's ranking (``export.rank_options``)."""
    rng = np.random.default_rng(seed)
    S = cube.values.shape[0]
    counts: Counter[str] = Counter()
    for _ in range(n_boot):
        counts[rank_first(resample_cube(cube, rng.integers(0, S, S)))] += 1
    return counts


def tie_set(counts: Mapping[str, int], n_boot: int, share: float = TIE_SHARE) -> list[str]:
    """The forks ranked first in at least ``share`` of the resamples, most often first first."""
    return [f for f, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])) if c >= share * n_boot - 1e-9]


# ---------------------------------------------------------------------------------------------- one run
def run_one(task: Mapping[str, Any]) -> dict[str, Any]:
    """One run: the engine CLI on the run's config, then the record (never raises)."""
    rec: dict[str, Any] = {"run": task["run"], "seed": task["seed"], "label": task["label"], "ok": False}
    t0 = time.perf_counter()
    tmp = Path(tempfile.mkdtemp(prefix=f"case-regen-{task['run']}-", dir=task.get("tmp") or None))
    try:
        src = Path(task["config"])
        if not src.is_file():
            rec["error"] = f"no {src.name} in {src.parent.name}"
            llm = src.parent / "llm.json"  # the drafter's record (variator.future.design): status and errors
            if llm.is_file():
                info = json.loads(llm.read_text(encoding="utf-8"))
                rec["draftStatus"] = info.get("status")
                why = info.get("error") or info.get("errors")
                rec["error"] = f"{rec['error']}; draft {info.get('status')}: {why}"[:2000]
            return rec
        cfg = json.loads(src.read_text(encoding="utf-8"))
        cases = tmp / "cases"
        cases.mkdir()
        (cases / f"{CASE}.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        out = tmp / "out"
        env = dict(os.environ, VARIATOR_CASES_DIR=str(cases))
        cmd = [sys.executable, "-m", "variator.future.cli", "run", "--case", CASE, "--n", str(task["n"]),
               "--seed", str(task["seed"]), "--actors", task["actors"], "--history", task["history"], "--out", str(out)]  # fmt: skip
        if task.get("no_tree"):
            cmd += ["--no-tree", "--no-layers"]
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=task.get("timeout", 900))
        rec["engine_s"] = round(time.perf_counter() - t0, 2)
        if p.returncode != 0:
            rec["error"] = f"engine exit {p.returncode}: {p.stderr[-3000:]}"
            return rec
        rec.update(record(cfg, out, task["seed"]))
        rec["ok"] = True
    except Exception as e:  # noqa: BLE001 - a failed run is a record, not a crash of the batch
        rec["error"] = f"{type(e).__name__}: {e}"
    finally:
        rec["wall_s"] = round(time.perf_counter() - t0, 2)
        shutil.rmtree(tmp, ignore_errors=True)
    return rec


def record(cfg: Mapping[str, Any], out: Path, seed: int) -> dict[str, Any]:
    """The record of a finished engine run (``out``: the CLI output directory)."""
    from variator.future import export
    from variator.metrics.cube import load_cube

    doc = json.loads((out / "month.json").read_text(encoding="utf-8"))
    forks = {f["id"]: f for f in doc["forks"]}
    asked = [str(f.get("id")) for f in cfg.get("forks", [])]
    if sorted(forks) != sorted(asked):  # the engine fell back to the built-in forks: the draft was not read
        raise ValueError(f"the engine ran the forks {list(forks)}, the config has {asked}")
    rows = []
    for o in doc["ranking"]["options"]:
        st = o["stats"]
        ivs = forks[o["option"]]["interventions"]
        rows.append({
            "fork": o["option"], "label": doc["options"][o["option"]]["label"], "key": fork_key(ivs),
            "rank": o["rank"], "score": o["score"], "scoreTimesFeasible": o["scoreTimesFeasible"],
            "pBest": o["pBest"], "pFeasible": o["pFeasible"],
            "cashP50": st["cash"]["p50"], "churnP50": st["churn"]["p50"], "npsP50": st["nps"]["p50"],
            "slaP50": st["sla"]["p50"], "runwayP50": st["runway"]["p50"],
        })  # fmt: skip
    t0 = time.perf_counter()
    cube = load_cube(out / "cube")
    counts = first_counts(cube, lambda c: export.rank_options(c, cfg).options[0].option, BOOT, seed)
    ties = tie_set(counts, BOOT)
    keys = {r["fork"]: r["key"] for r in rows}
    boot_s = time.perf_counter() - t0
    tree = (
        json.loads((out / "week-tree.json").read_text(encoding="utf-8"))
        if (out / "week-tree.json").is_file()
        else {}
    )
    return {
        "forks": [
            {"id": f["id"], "label": f["label"], "key": fork_key(f["interventions"]), "levers": lever_params(f["interventions"]),
             "unmodelled": [f"{i['kind']} {i['op']} {i['target']}" for i in f["interventions"] if lever_of(i["target"], i["op"]) is None]}
            for f in doc["forks"]
        ],
        "ranking": rows,
        "recommended": rows[0]["fork"],
        "recommendedKey": rows[0]["key"],
        "firstCounts": dict(counts),
        "tieSet": ties,
        "tieKeys": sorted({keys[f] for f in ties}),
        "layers": {k: v["winner"] for k, v in doc.get("layers", {}).items()},
        "nLayers": len(doc.get("layers", {})),
        "layersSource": "config" if isinstance(cfg.get("layers"), list) else "built-in",
        "nRisks": len(cfg.get("risks", [])),
        "treeForks": tree.get("forks", []),
        "eventTreeForks": sorted(doc.get("eventTrees", {})),
        "timings": doc.get("timings", {}) | {"bootstrap_s": round(boot_s, 2)},
    }  # fmt: skip


# ---------------------------------------------------------------------------------------------- batch
def tasks_of(args: argparse.Namespace) -> list[dict[str, Any]]:
    base = {"label": args.label, "n": args.n, "actors": args.actors, "history": args.history,
            "tmp": args.tmp, "no_tree": args.no_tree, "timeout": args.timeout}  # fmt: skip
    if args.control:
        return [
            base | {"run": i, "seed": SEED0 + i, "config": str(HAND_WRITTEN)} for i in range(args.control)
        ]
    runs = sorted(p for p in Path(args.configs).iterdir() if p.is_dir() and p.name.startswith("run-"))
    out = []
    for p in runs:
        i = int(p.name.split("-", 1)[1])
        out.append(base | {"run": i, "seed": SEED0 + i, "config": str(p / "config.json"), "dir": str(p)})
    return out


def done_runs(path: Path, retry_failed: bool = False) -> set[int]:
    """The runs already recorded (the last record of a run wins); ``retry_failed``: only the completed ones."""
    if not path.is_file():
        return set()
    last = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            last[r["run"]] = r
    return {k for k, r in last.items() if r.get("ok") or not retry_failed}


def cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.out_root) / args.label
    out.mkdir(parents=True, exist_ok=True)
    jsonl = out / "runs.jsonl"
    seen = done_runs(jsonl, args.retry_failed)
    todo = [t for t in tasks_of(args) if t["run"] not in seen]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(seen)} runs recorded, {len(todo)} to run with {args.workers} workers", file=sys.stderr)
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as ex, jsonl.open("a", encoding="utf-8") as fh:
        futs = [ex.submit(run_one, t) for t in todo]
        for k, fut in enumerate(as_completed(futs), 1):
            rec = fut.result()
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            state = (
                f"{rec['recommended']} ({rec['recommendedKey']}) ties {rec['tieSet']}"
                if rec["ok"]
                else f"FAILED {rec.get('error', '')[:200]}"
            )
            print(
                f"[{k}/{len(todo)} {time.perf_counter() - t0:.0f}s] run {rec['run']:03d}: {state} {rec.get('wall_s')}s",
                file=sys.stderr,
            )
    return 0


# ---------------------------------------------------------------------------------------------- summary
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval of k successes in n trials (95% with z = 1.96)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _share(k: int, n: int, note: str) -> dict[str, Any]:
    return {"k": k, "n": n, "share": k / n if n else None, "wilson95": wilson(k, n), "note": note}


def _q(xs: Sequence[float]) -> dict[str, float] | None:
    if not xs:
        return None
    a = np.asarray(xs, dtype=float)
    return {"n": int(a.size), "min": float(a.min()), "p10": float(np.quantile(a, 0.1)), "p50": float(np.quantile(a, 0.5)),
            "p90": float(np.quantile(a, 0.9)), "max": float(a.max())}  # fmt: skip


def summarize(recs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ok = [r for r in recs if r.get("ok")]
    failed = [{"run": r["run"], "error": str(r.get("error", ""))[:500]} for r in recs if not r.get("ok")]
    rec_keys = Counter(r["recommendedKey"] for r in ok)
    modal = sorted(rec_keys.items(), key=lambda x: (-x[1], x[0]))[0][0] if ok else None
    strict = sum(r["recommendedKey"] == modal for r in ok)
    agree = sum(r["recommendedKey"] == modal or modal in r["tieKeys"] for r in ok)
    n = len(ok)
    # the numbers of the modal fork: per run the best-ranked fork with the modal key
    nums: dict[str, list[float]] = {"pBest": [], "cashP50": [], "churnP50": [], "npsP50": []}
    present = 0
    for r in ok:
        row = next((x for x in r["ranking"] if x["key"] == modal), None)
        if row is None:
            continue
        present += 1
        for k in nums:
            nums[k].append(row[k])
    lever_runs = Counter()
    lever_forks = Counter()
    params: dict[str, list[float]] = {k: [] for k in ("price.change", "hire.count", "debt.share", "gpu.costPerYear",
                                                     "price.month", "hire.month", "debt.month", "gpu.month")}  # fmt: skip
    for r in ok:
        seen = set()
        for f in r["forks"]:
            for lev, p in f["levers"].items():
                lever_forks[lev] += 1
                seen.add(lev)
                for k, v in p.items():
                    if f"{lev}.{k}" in params:
                        params[f"{lev}.{k}"].append(v)
        for lev in seen:
            lever_runs[lev] += 1
    tie_sizes = Counter(len(r["tieSet"]) for r in ok)
    # supplementary (not pre-registered): forks whose score x P(feasible) equals the first one's to 4 decimals;
    # the ranking keeps the option order on equal keys, so a bootstrap never shows such a tie
    score_ties = [
        sorted(
            {
                x["key"]
                for x in r["ranking"]
                if x["scoreTimesFeasible"] == r["ranking"][0]["scoreTimesFeasible"]
            }
        )
        for r in ok
    ]
    agree_score = sum(
        r["recommendedKey"] == modal or modal in r["tieKeys"] or modal in t
        for r, t in zip(ok, score_ties, strict=True)
    )
    timings = [r.get("wall_s", 0.0) for r in ok]
    return {
        "runs": len(recs),
        "completed": n,
        "failed": len(failed),
        "failedRuns": failed,
        "recommendedKeys": dict(rec_keys.most_common()),
        "recommendedForks": dict(Counter(r["recommended"] for r in ok).most_common()),
        "modalKey": modal,
        # primary (Changes of the pre-registration): all runs in the denominator, a failed draft = a disagreement
        "agreement": _share(agree, len(recs), "all runs; a failed run counts as a disagreement"),
        "agreementValid": _share(agree, n, "completed runs only"),
        "strictAgreement": _share(strict, len(recs), "all runs; a failed run counts as a disagreement"),
        "strictAgreementValid": _share(strict, n, "completed runs only"),
        "tieSets": {"sizes": {str(k): v for k, v in sorted(tie_sizes.items())},
                    "keySets": dict(Counter("|".join(r["tieKeys"]) for r in ok).most_common()),
                    "withModalKey": sum(modal in r["tieKeys"] for r in ok)},
        "modalFork": {"runsWithModalKey": present, **{k: _q(v) for k, v in nums.items()}},
        "supplementary": {
            "note": "not pre-registered: exact score ties (score x P(feasible) equal to 4 decimals) with the first fork",
            "scoreTieKeySets": dict(Counter("|".join(t) for t in score_ties).most_common()),
            "agreementWithScoreTies": _share(agree_score, len(recs), "all runs; a failed run counts as a disagreement"),
        },
        "inputs": {
            "forksPerRun": _q([len(r["forks"]) for r in ok]),
            "keysPerRun": _q([len({f["key"] for f in r["forks"]}) for r in ok]),
            "leverRuns": {k: lever_runs.get(k, 0) for k in LEVERS},
            "leverForks": {k: lever_forks.get(k, 0) for k in LEVERS},
            "forkKeys": dict(Counter(f["key"] for r in ok for f in r["forks"]).most_common()),
            "parameters": {k: _q(v) for k, v in params.items()},
            "hypotheses": _q([r["nLayers"] for r in ok]),
            "risks": _q([r["nRisks"] for r in ok]),
            "unmodelledInterventions": sum(len(f["unmodelled"]) for r in ok for f in r["forks"]),
        },
        "timePerRun_s": _q(timings),
    }  # fmt: skip


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def _rng(q: Mapping[str, float] | None, fmt: str = "{:.3g}") -> str:
    return (
        "n/a"
        if not q
        else f"{fmt.format(q['p10'])} to {fmt.format(q['p90'])} (median {fmt.format(q['p50'])})"
    )


def _share_md(a: Mapping[str, Any]) -> str:
    lo, hi = a["wilson95"]
    return f"{a['k']}/{a['n']} = {_pct(a['share'])}, 95% Wilson {_pct(lo)} to {_pct(hi)}"


def summary_md(label: str, s: Mapping[str, Any]) -> str:
    mf = s["modalFork"]
    lines = [
        f"# Case re-drafting test: {label}",
        "",
        f"Runs {s['runs']}, completed {s['completed']}, failed {s['failed']}. Pre-registration: docs/knowledge/case-regen-test.md.",
        "",
        f"- Modal key of the recommended fork: **{s['modalKey']}** ({s['recommendedKeys']})",
        f"- **Agreement** (recommended key = modal, or the tie set holds it; all runs, failed = disagreement): {_share_md(s['agreement'])}",
        f"- Agreement over valid drafts: {_share_md(s['agreementValid'])}",
        f"- Strict agreement (without tie sets; all runs): {_share_md(s['strictAgreement'])}",
        f"- Strict agreement over valid drafts: {_share_md(s['strictAgreementValid'])}",
        f"- Recommended forks: {s['recommendedForks']}",
        f"- Tie sets (forks first in >= 10% of {BOOT} bootstrap resamples): sizes {s['tieSets']['sizes']}; "
        f"key sets {s['tieSets']['keySets']}; holding the modal key {s['tieSets']['withModalKey']}",
        f"- Supplementary, not pre-registered: key sets tied with the first fork on score x P(feasible) to 4 decimals "
        f"{s['supplementary']['scoreTieKeySets']}; agreement counting them {_pct(s['supplementary']['agreementWithScoreTies']['share'])}",
        "",
        f"## The modal fork over the runs (P10 to P90; {mf['runsWithModalKey']} runs have a fork with the modal key)",
        f"- P(best): {_rng(mf['pBest'])}",
        f"- Cash median at month 12: {_rng(mf['cashP50'], '{:,.0f}')}",
        f"- Churn median: {_rng(mf['churnP50'])}",
        f"- NPS median: {_rng(mf['npsP50'])}",
        "",
        "## Inputs",
        f"- Forks per run: {_rng(s['inputs']['forksPerRun'])}; fork keys: {s['inputs']['forkKeys']}",
        f"- Runs with each lever: {s['inputs']['leverRuns']}; forks with each lever: {s['inputs']['leverForks']}",
        *(f"- {k}: " + ("n/a" if not q else f"{q['min']:.4g} to {q['max']:.4g} (P10 {q['p10']:.4g}, median {q['p50']:.4g}, P90 {q['p90']:.4g})")
          for k, q in s["inputs"]["parameters"].items()),
        f"- Hypotheses per run: {_rng(s['inputs']['hypotheses'])}; risks per run: {_rng(s['inputs']['risks'])}",
        f"- Interventions outside the levers (not modelled): {s['inputs']['unmodelledInterventions']}",
        "",
        f"Time per run (s): {_rng(s['timePerRun_s'])}",
        "",
        "## Failed runs",
        *([f"- run {f['run']:03d}: {f['error']}" for f in s["failedRuns"]] or ["None."]),
        "",
    ]  # fmt: skip
    return "\n".join(lines)


def cmd_summary(args: argparse.Namespace) -> int:
    out = Path(args.out_root) / args.label
    recs = [json.loads(x) for x in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    last = {r["run"]: r for r in recs}  # a resumed run: the last record wins
    s = summarize([last[k] for k in sorted(last)])
    (out / "summary.json").write_text(json.dumps(s, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(summary_md(args.label, s), encoding="utf-8")
    print(summary_md(args.label, s))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--configs", help="a directory of run-NNN/config.json (drafted cases)")
    g.add_argument("--control", type=int, help="the hand-written case with this many seeds")
    r.add_argument("--label", required=True)
    r.add_argument("--n", type=int, default=1000)
    r.add_argument("--workers", type=int, default=6)
    r.add_argument("--actors", default=os.environ.get("VARIATOR_FUTURE_ACTORS", ""))
    r.add_argument("--history", default=os.environ.get("VARIATOR_FUTURE_HISTORY", ""))
    r.add_argument("--tmp", default=None, help="the parent of the temporary run directories")
    r.add_argument("--limit", type=int, default=0, help="run at most this many (0 = all)")
    r.add_argument("--timeout", type=int, default=900)
    r.add_argument("--retry-failed", action="store_true", help="run again the runs whose last record failed")
    r.add_argument(
        "--no-tree", action="store_true", help="skip the layers and the week tree (a quick check only)"
    )
    r.add_argument("--out-root", default=str(OUT_ROOT))
    s = sub.add_parser("summary")
    s.add_argument("--label", required=True)
    s.add_argument("--out-root", default=str(OUT_ROOT))
    args = ap.parse_args(argv)
    if args.cmd == "run":
        if not args.actors or not args.history:
            ap.error(
                "--actors and --history (or VARIATOR_FUTURE_ACTORS / VARIATOR_FUTURE_HISTORY) are required"
            )
        return cmd_run(args)
    return cmd_summary(args)


if __name__ == "__main__":
    sys.exit(main())
