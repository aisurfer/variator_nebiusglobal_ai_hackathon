"""Command line: plan | write | check | apply (resumable; every step reads and writes ``--work``).

    python -m variator.narrative.cli plan  --history DIR --work DIR [--types t1,t2]
    python -m variator.narrative.cli write --history DIR --work DIR [--ids a,b | --pilot N] [--parallel 6]
    python -m variator.narrative.cli check --history DIR --work DIR
    python -m variator.narrative.cli apply --history DIR --work DIR --out DIR [--placeholders]

``write`` runs the rounds of requests, checks and retries (``writers.Writers``); ``check``
re-checks the stored answers without calls; ``apply`` merges the accepted artifacts (or all
artifacts with placeholder texts) with the base delivery into ``--out`` (a new directory).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from variator.llm.models import ROLE_MODEL
from variator.narrative.base import Base


def _plan(work: Path) -> dict[str, Any]:
    return json.loads((work / "plan.json").read_text(encoding="utf-8"))


def pilot_ids(plan: dict[str, Any], n: int) -> list[str]:
    """A mixed sample: round-robin over the types, spread over the year."""
    by: dict[str, list[str]] = {}
    for a in plan["artifacts"]:
        by.setdefault(a["type"], []).append(a["id"])
    order = ["postmortem", "weekly_sync", "leadership_review", "qbr", "escalation", "decision_rationale"]
    picks: list[str] = []
    k = 0
    while len(picks) < n and any(by.values()):
        for t in order:
            xs = by.get(t) or []
            if xs and len(picks) < n:
                idx = (len(xs) * (2 * k + 1)) // (2 * (k + 2)) if k else len(xs) // 3
                picks.append(xs.pop(min(idx, len(xs) - 1)))
        k += 1
    # a decision rationale needs its review in the same run to cite it
    for a in list(picks):
        art = next(x for x in plan["artifacts"] if x["id"] == a)
        if art["type"] == "decision_rationale" and art["trigger"]["review"] not in picks:
            picks.append(art["trigger"]["review"])
    return picks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="variator.narrative.cli")
    ap.add_argument("command", choices=["plan", "write", "check", "apply"])
    ap.add_argument("--history", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out")
    ap.add_argument("--types")
    ap.add_argument("--ids")
    ap.add_argument("--pilot", type=int)
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--model", default=ROLE_MODEL["narrative"])
    ap.add_argument("--placeholders", action="store_true", help="apply every artifact with placeholder texts")
    a = ap.parse_args(argv)
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    if a.command == "plan":
        from variator.narrative.planner import plan

        b = Base(a.history)
        p = plan(b, types=tuple(a.types.split(",")) if a.types else None)
        (work / "plan.json").write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
        print(
            json.dumps(
                {"artifacts": len(p["artifacts"]), "counts": p["counts"], "links": p["links"]}, indent=1
            )
        )
        return 0
    p = _plan(work)
    if a.command == "write":
        from variator.narrative.writers import Writers

        ids = a.ids.split(",") if a.ids else pilot_ids(p, a.pilot) if a.pilot else None
        w = Writers(p, work, parallel=a.parallel, timeout=a.timeout, max_attempts=a.attempts, model=a.model)
        print(json.dumps(w.run(ids), indent=1))
        return 0
    if a.command == "check":
        from variator.narrative.writers import recheck

        r = recheck(p, work)
        (work / "accepted.json").write_text(
            json.dumps(r["texts"], ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(json.dumps(r["report"], indent=1, ensure_ascii=False)[:4000])
        return 0
    if a.command == "apply":
        from variator.narrative.apply import apply

        if not a.out:
            ap.error("apply needs --out")
        b = Base(a.history)
        if a.placeholders:
            texts, keep = None, None
        else:
            acc = work / "accepted.json"
            if not acc.is_file():
                ap.error("run `check` first (accepted.json)")
            texts = json.loads(acc.read_text(encoding="utf-8"))
            keep = set(texts)
        r = apply(b, p, a.out, texts=texts, keep=keep)
        print(json.dumps({k: r[k] for k in ("out", "narrative", "verify")}, indent=1))
        return 0 if not r["verify"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
