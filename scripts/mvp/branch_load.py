"""Load the branch deliveries of ``branch_day.py`` into their KB tenants, one at a time (WP19).

    python scripts/mvp/branch_load.py --out /tmp/variator-1000/mvp-branches [--tenants a,b] [--parts 2]

Each branch: the disk guard (8 GB free), then a one-off kb container (``compose run``, never ``exec`` into
``kb-api``) under ``nice`` replays the journal; ``ANALYZE`` refreshes the statistics of the new tenant (a
plan made before autoanalyze saturates Postgres); a second one-off drains search, descriptors and graph with
``--parts``
processes of ``--threads`` ONNX threads each. A probe of the KB API through caddy runs every 30 s during
the load. ``--drain-only`` resumes a load that stopped after its replay. The tenant ``company`` is refused.
Times, probes and the free disk go to ``<out>/load.json``. It stops at the first failure or below the guard,
and it always starts ``kb-worker`` again at its end.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from variator.orgsim.company.forward.pipeline import COMPOSE, MIN_FREE_GB, free_gb, load


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="branch_load.py", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--out", type=Path, required=True, help="the work directory of branch_day.py")
    ap.add_argument(
        "--tenants", default="", help="comma-separated tenants (default: every branch, demo first)"
    )
    ap.add_argument("--parts", type=int, default=1, help="drain processes for search")
    ap.add_argument("--threads", type=int, default=3, help="ONNX threads per drain process")
    ap.add_argument("--drain-only", default="", help="comma-separated tenants whose journal is loaded")
    args = ap.parse_args(argv)
    found = sorted(p.name for p in (args.out / "branches").iterdir() if (p / "delivery").is_dir())
    tenants = [t for t in args.tenants.split(",") if t] or sorted(
        found, key=lambda t: (not t.endswith("-p50"), t)
    )
    drain_only = {t for t in args.drain_only.split(",") if t}
    try:
        return _load_all(args.out, tenants, args.parts, args.threads, drain_only)
    finally:
        # the load stops kb-worker while the journal loads: start it again whatever happened
        subprocess.run([*COMPOSE, "up", "-d", "--no-deps", "--no-build", "kb-worker"], check=False)


def _load_all(out: Path, tenants: list[str], parts: int, threads: int, drain_only: set[str]) -> int:
    path = out / "load.json"
    done = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    for t in tenants:
        if free_gb() < MIN_FREE_GB:
            print(f"stop: {free_gb():.1f} GB free, {MIN_FREE_GB} GB needed", flush=True)
            return 2
        bdir = out / "branches" / t
        r = load(bdir / "delivery", t, parts, bdir / "load.log", threads=threads, seed=t not in drain_only)
        done = [x for x in done if x["tenant"] != t] + [r]
        path.write_text(json.dumps(done, indent=1) + "\n", encoding="utf-8")
        print(json.dumps(r), flush=True)
        if r["returncode"] != 0:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
