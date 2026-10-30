"""CEO brief of the metric catalogue for each KB branch (WP19): artifact sets ``branch-metrics-<tenant>``.

    python scripts/mvp/branch_metrics.py --out /tmp/variator-1000/mvp-branches \
        --sets /tmp/variator-1000/mvp-artifacts [--seed]

For every branch of ``<out>/report.json``: the same forward run again (fork, scenario and week-tree leaf of
the pinned tree ``<out>/week-tree.used.json``; its cash must equal the report's), its datasets (the history
plus the run's rows, ``forward.datasets.write_datasets``, kept in ``<out>/datasets/<tenant>``), the
catalogue as of the branch's last day (``variator.metrics.catalog``, read-only) and the artifact set in the
schema of ``history-metrics-ceo`` (manifest schema 2: ``briefs/ceo.json``, ``metrics/<id>.json``,
``catalog.json``). Differences: ``kind: "branch-metrics"`` (the history brief does not pick these sets),
``tenant`` and ``branch`` in the manifest, and the metrics that cannot be computed on simulated records are
left out (``omitted``), never shown as unknown. ``--seed`` copies each set into the stack
(``make -C deploy/mvp seed-artifacts``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from variator.metrics.catalog.artifacts import as_of_violations, compute_all, write_set
from variator.metrics.catalog.datasets import load_history
from variator.metrics.catalog.registry import default_params
from variator.orgsim.company.forward.datasets import NOT_ON_SIMULATED, write_datasets
from variator.orgsim.company.forward.forks import Case
from variator.orgsim.company.forward.pipeline import MIN_FREE_GB, ROOT, free_gb, leaves, one_run
from variator.orgsim.company.forward.state import StateAtT

KIND = "branch-metrics"


def set_id(tenant: str) -> str:
    return f"{KIND}-{tenant}"


def build(row: dict[str, Any], paths: dict[str, Any], st: StateAtT, case: Case, out: Path, sets: Path,
          seed: int, months: int, built_at: str) -> dict[str, Any]:  # fmt: skip
    """One branch: run, datasets, catalogue as of its last day, the artifact set. Returns its summary."""
    t0 = time.perf_counter()
    tenant, fork = row["tenant"], row["fork"]
    leaf = next((v for v in paths.get(fork, {}).values() if v["leaf"] == row.get("leaf")), None)
    path = leaf["events"] if leaf else None
    run = one_run(st, case, fork, row["scenario"], seed, months, tenant, path=path)
    cash, want = run.series[-1]["values"]["cash"], row["summary"]["cash_end"]
    if abs(cash - want) > 0.005:
        raise ValueError(f"{tenant}: the run again ends with cash {cash}, the report says {want}")
    ds = out / "datasets" / tenant
    if ds.exists():
        shutil.rmtree(ds)
    write_datasets(run, ds)
    data = load_history(ds, as_of_anchor=run.end)  # T = the first day after the window
    as_of = run.days[-1]
    params = default_params()
    evals = compute_all(data, params, as_of, computed_at=built_at)
    problems = [x for ev in evals.values() for x in as_of_violations(ev)]
    if problems:
        raise ValueError(f"{tenant}: as-of or id check failed: {problems[:5]}")
    omitted = []
    for mid in list(evals):
        ev = evals[mid]
        if mid in NOT_ON_SIMULATED:
            why = "needs sprint or Jira records, which the forward run does not make"
        elif ev.kb.value is None or ev.kb.status == "unknown":
            why = "unknown on the simulated records"
        else:
            continue
        omitted.append({"id": mid, "reason": why})
        del evals[mid]
    target = sets / set_id(tenant)
    man = write_set(target, data, params, evals, built_at=built_at, sanity_doc=None)
    branch = {
        "tenant": tenant,
        "fork": fork,
        "forkLabel": run.fork.label,
        "kind": row["kind"],
        "scenario": run.k,
        "leaf": row.get("leaf"),
        "window": [run.start.isoformat(), as_of.isoformat()],
        "deliverySha256": row.get("delivery_sha256"),
        "datasets": str(ds),
    }
    man.update(
        kind=KIND,
        title=f"Branch metrics of {data.company}: {run.fork.label}, {row['kind']} example ({tenant})",
        tenant=tenant,
        branch=branch,
        omitted=omitted,
        madeBy="python scripts/mvp/branch_metrics.py",
    )
    man["history"]["tenant"] = tenant
    text = json.dumps(man, ensure_ascii=False, indent=1) + "\n"
    (target / "manifest.json").write_text(text, encoding="utf-8")
    return {
        "set": set_id(tenant),
        "dir": str(target),
        "asOf": man["asOf"],
        "metrics": len(man["metrics"]),
        "omitted": [o["id"] for o in omitted],
        "bytes": sum(f["bytes"] for f in man["files"]),
        "seconds": round(time.perf_counter() - t0, 1),
        "values": {k: v["value"] for k, v in man["metrics"].items()},
    }


def seed_set(dir_: Path, name: str) -> None:
    cmd = ["make", "-C", str(ROOT / "deploy" / "mvp"), "seed-artifacts", f"DIR={dir_}", f"SET={name}",
           f"MIN_FREE_GB={int(MIN_FREE_GB)}"]  # fmt: skip
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="branch_metrics.py", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--out", type=Path, required=True, help="the work directory of branch_day.py")
    ap.add_argument("--sets", type=Path, default=Path("/tmp/variator-1000/mvp-artifacts"), help="set directory")
    ap.add_argument("--history", type=Path, default=Path("/tmp/variator-1000/mvp-history-mgmt"))
    ap.add_argument("--actors", type=Path, default=Path("/tmp/variator-1000/mvp-artifacts/actors"))
    ap.add_argument("--tenants", default="", help="comma-separated tenants (default: every branch)")
    ap.add_argument("--seed", action="store_true", help="copy each set into the stack (make seed-artifacts)")
    args = ap.parse_args(argv)
    rep = json.loads((args.out / "report.json").read_text(encoding="utf-8"))
    tree = args.out / "week-tree.used.json"
    paths: dict[str, Any] = {}
    if rep.get("week_tree_sha256"):
        if hashlib.sha256(tree.read_bytes()).hexdigest() != rep["week_tree_sha256"]:
            print(f"error: {tree} is not the tree of the report", file=sys.stderr)
            return 2
        _, paths = leaves(tree)
    want = [t for t in args.tenants.split(",") if t]
    rows = [b for b in rep["branches"] if not want or b["tenant"] in want]
    st, case = StateAtT(args.history, args.actors), Case()
    built_at = datetime.now(UTC).isoformat(timespec="seconds")
    done = []
    for row in rows:
        s = build(row, paths, st, case, args.out, args.sets, rep["seed"], rep["months"], built_at)
        if args.seed:
            if free_gb() < MIN_FREE_GB:
                print(f"stop: less than {MIN_FREE_GB} GB free", file=sys.stderr)
                return 3
            seed_set(Path(s["dir"]), s["set"])
            s["seeded"] = True
        print(f"{s['set']}: {s['metrics']} metrics, left out {s['omitted']}, {s['seconds']} s", flush=True)
        done.append(s)
    (args.out / "branch-metrics.json").write_text(json.dumps(done, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
