"""CLI: ``python -m variator.actors.cli learn --history DIR --out DIR [--as-of YYYY-MM-DD]``.

Reads the history, learns the actors and writes the artifact set. When ``truth.json`` is in
the history directory, the recovery check runs and ``recovery.json`` is written (``--no-recovery``
turns it off). The exit code is 0; with ``--require-coverage`` it is 2 when the coverage is
below the target.
"""

from __future__ import annotations

import argparse
import sys
import time

from variator.actors.artifacts import write_set
from variator.actors.learn import learn
from variator.actors.recovery import load_truth, recovery


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m variator.actors.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("learn", help="learn actors, habits, cohorts and risks from a history directory")
    lp.add_argument("--history", required=True, help="history directory (datasets/, delivery/, truth.json)")
    lp.add_argument("--out", required=True, help="output directory of the artifact set")
    lp.add_argument("--as-of", default=None, help="as-of date T (default: the day after the last row)")
    lp.add_argument("--no-recovery", action="store_true", help="do not compare with truth.json")
    lp.add_argument(
        "--require-coverage", action="store_true", help="exit 2 when the coverage is below the target"
    )
    args = ap.parse_args(argv)
    t0 = time.time()
    L = learn(args.history, args.as_of)
    rec = None
    if not args.no_recovery:
        truth = load_truth(args.history)
        if truth is not None:
            rec = recovery(L, truth)
    m = write_set(L, args.out, rec)
    total = sum(f["bytes"] for f in m["files"])
    print(
        f"as of {m['as_of']}: {m['counts']['actors']} actors, {m['counts']['cohorts']} cohorts, "
        f"{m['counts']['risks']} risks, {m['counts']['facts_files']} fact files, {total / 1024:.0f} KiB "
        f"-> {args.out} ({time.time() - t0:.1f} s)"
    )
    if rec is not None:
        s = rec["summary"]
        print(
            f"recovery: {s['inside']}/{s['estimate_rows_enough_data']} inside p1-p99 "
            f"= {100 * s['coverage']:.1f}% (target {100 * rec['target_coverage']:.0f}%); "
            f"all estimate rows {100 * s['coverage_all_estimate_rows']:.1f}%"
        )
        if args.require_coverage and not s["meets_target"]:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
