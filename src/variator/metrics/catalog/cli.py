"""Command line of the metric catalogue.

``python -m variator.metrics.catalog.cli compute --history DIR --out DIR [--params FILE] [--as-of DATE]``
computes every metric of the CEO brief over the history and writes the artifact set.
``python -m variator.metrics.catalog.cli params --out FILE`` writes the default parameters to edit.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from variator.metrics.catalog.artifacts import as_of_violations, compute_all, write_set
from variator.metrics.catalog.datasets import load_history
from variator.metrics.catalog.params import ParamsError, merge_params
from variator.metrics.catalog.registry import default_params
from variator.metrics.catalog.sanity import sanity


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m variator.metrics.catalog.cli", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compute", help="compute the catalogue over a history and write the artifact set")
    c.add_argument("--history", type=Path, required=True, help="history directory (datasets/, delivery/)")
    c.add_argument("--out", type=Path, required=True, help="artifact set directory")
    c.add_argument("--params", type=Path, default=None, help="JSON with a partial parameter override")
    c.add_argument(
        "--as-of", type=date.fromisoformat, default=None, help="as-of date (default: T of the history)"
    )
    c.add_argument("--built-at", default=None, help="build time in the files (default: now, UTC)")
    p = sub.add_parser("params", help="write the default parameters as JSON")
    p.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "params":
        args.out.write_text(
            json.dumps(default_params().model_dump(mode="json"), indent=1) + "\n", encoding="utf-8"
        )
        return 0
    params = default_params()
    if args.params:
        try:
            params = merge_params(params, json.loads(args.params.read_text(encoding="utf-8")))
        except (OSError, ValueError, ParamsError) as exc:
            print(f"error: parameters: {exc}", file=sys.stderr)
            return 2
    t0 = time.perf_counter()
    data = load_history(args.history)
    t1 = time.perf_counter()
    evals = compute_all(data, params, args.as_of, computed_at=args.built_at)
    t2 = time.perf_counter()
    problems = [x for ev in evals.values() for x in as_of_violations(ev)]
    if problems:
        print("error: as-of or id check failed:\n  " + "\n  ".join(problems[:20]), file=sys.stderr)
        return 3
    check = sanity(data, evals)
    manifest = write_set(args.out, data, params, evals, built_at=args.built_at, sanity_doc=check)
    t3 = time.perf_counter()
    print(
        json.dumps(
            {
                "asOf": manifest["asOf"],
                "out": str(args.out),
                "seconds": {
                    "load": round(t1 - t0, 2),
                    "compute": round(t2 - t1, 2),
                    "write": round(t3 - t2, 2),
                },
                "metrics": {k: [v["value"], v["status"]] for k, v in manifest["metrics"].items()},
                "sanity": [[r["check"], r["ours"], r["truth"], r["diff"]] for r in check["rows"]],
                "files": len(manifest["files"]) + 1,
                "bytes": sum(f["bytes"] for f in manifest["files"]),
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
