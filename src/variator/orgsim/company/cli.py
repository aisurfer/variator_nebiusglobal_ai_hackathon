"""Command line: ``python -m variator.orgsim.company.cli generate --anchor YYYY-MM-DD --seed N --out DIR``."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from variator.orgsim.company.generator import generate_history, write_history
from variator.orgsim.company.spec import CompanySpec, default_spec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m variator.orgsim.company.cli", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="generate one year of history before the anchor date")
    g.add_argument(
        "--anchor", type=date.fromisoformat, default=None, help="T (default: today in the company zone)"
    )
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--out", type=Path, required=True)
    g.add_argument("--spec", type=Path, default=None, help="a spec JSON (default: the MVP company)")
    s = sub.add_parser("spec", help="write the default spec as JSON")
    s.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "spec":
        default_spec().dump(args.out)
        return 0
    spec = CompanySpec.load(args.spec) if args.spec else default_spec()
    t0 = time.perf_counter()
    h = generate_history(spec, args.anchor, args.seed)
    t1 = time.perf_counter()
    summary = write_history(h, args.out)
    t2 = time.perf_counter()
    print(
        json.dumps(
            {
                "anchor": h.anchor.isoformat(),
                "seed": h.seed,
                "lines": len(h.lines),
                "generate_s": round(t1 - t0, 2),
                "write_s": round(t2 - t1, 2),
                "by_object_type": summary["manifest_counts"]["by_object_type"],
                "checks": {k: v["status"] for k, v in summary["checks"].items()},
                "stats": {k: v for k, v in h.stats.items() if k not in ("by_type",)},
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
