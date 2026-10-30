"""Suggested "why" questions per KB branch, checked by asking them (WP19): ``examples.json``.

    python scripts/mvp/branch_examples.py --out /tmp/variator-1000/mvp-branches \
        --day-out /tmp/variator-1000/mvp-artifacts/future/company_best_year/day

Candidates come from the branch records: the signals (``Incidents rose in March 2027``, ``Incidents fell
in ...``, ``Cash fell in ...``, ``Engineering load over capacity in ...``), the client notices with declared
causes (``Why did X give notice?``) and the SEV1/SEV2 incidents. Each candidate goes to the workspace Ask of
the branch (``POST /api/kb/company/ask?branch=<tenant>``). A question is kept when the answer is about the
intended record and names declared causes ("Chains from the effect to the root cause"), none of them the
opening balances of the state at T. Up to ``--keep`` per branch
go to ``<branch>/delivery/examples.json`` and to ``examples.json`` and ``askExamples`` of the day set.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ASK = "http://127.0.0.1:8088/workspace/api/kb/company/ask?branch={tenant}"  # the app under /workspace/ (WP27)
SIGNAL = re.compile(
    r"^(Incidents rose|Incidents fell|Cash fell|Engineering load over capacity) in (\w+) (\d{4})$"
)
QUESTION = {
    "Incidents rose": "Why did incidents rise in {m}?",
    "Incidents fell": "Why did incidents fall in {m}?",
    "Cash fell": "Why did cash fall in {m}?",
    "Engineering load over capacity": "Why was engineering load over capacity in {m}?",
}
CHAINS = "Chains from the effect to the root cause"
NO_REASON = ("sim.PROC-OPENING",)  # "Opening balances": the state at T, not a reason


def candidates(delivery: Path) -> list[tuple[str, str]]:
    """(question, entity the answer must be about), signals first."""
    last: dict[str, dict[str, Any]] = {}
    with gzip.open(delivery / "fixtures" / "timeline.jsonl.gz", "rt", encoding="utf-8") as fh:
        for x in fh:
            ln = json.loads(x)
            if ln["source"] == "sim":
                last[ln["external_id"]] = ln
    sig, notice, inc = [], [], []
    for rid, ln in last.items():
        p = ln["payload"]
        f = p.get("fields") or {}
        m = SIGNAL.match(str(f.get("title", "")))
        if rid.startswith("SIG-") and m and p.get("causes"):
            sig.append((QUESTION[m.group(1)].format(m=m.group(2)), f"sim.{rid}"))
        elif (
            # the notice itself, not the contract: a contract of the state at T keeps "Opening balances"
            # among its causes, so an answer about the contract leads with it
            ln["object_type"] == "life_event"
            and str(f.get("title", "")).endswith(" gives notice")
            and f.get("client_name")
            and p.get("causes")
            and str(f.get("effective_date")) >= "2026-10-27"
        ):
            notice.append((f"Why did {f['client_name']} give notice?", f"sim.{rid}"))
        elif ln["object_type"] == "incident" and f.get("severity") in ("SEV1", "SEV2") and p.get("causes"):
            inc.append((f"What caused incident {rid}?", f"sim.{rid}"))
    return sig + notice[:3] + inc[:2]


def ask(tenant: str, question: str) -> dict[str, Any]:
    req = urllib.request.Request(
        ASK.format(tenant=tenant),
        data=json.dumps({"question": question}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def check(tenant: str, question: str, entity: str) -> dict[str, Any] | None:
    try:
        a = ask(tenant, question)
    except Exception as exc:  # noqa: BLE001 - a failed call is a rejected question
        print(f"  {question}: {type(exc).__name__}", flush=True)
        return None
    text = str(a.get("answer", ""))
    subject = text.split("\n", 1)[0]  # "Subject: “title” (Kind, sim.SIG-0001; matched ...)"
    why = text.split(CHAINS, 1)[1].split("Interpretation", 1)[0] if CHAINS in text else ""
    # a real cause: chains are named, and none of them (nor a root cause) is the opening balances of T
    ok = bool(why) and entity in subject and not any(x in why for x in NO_REASON)
    print(f"  {'keep' if ok else 'drop'}: {question}", flush=True)
    if not ok:
        return None
    chains = why.split("Root causes", 1)[0]
    return {
        "question": question,
        "entityId": entity,
        "intent": a.get("intent"),
        "chains": [x.strip("• ").strip() for x in chains.splitlines() if x.strip().startswith("•")][:6],
        "answer": text[:1500],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="branch_examples.py", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--out", type=Path, required=True, help="the work directory of branch_day.py")
    ap.add_argument("--day-out", type=Path, default=None, help="the day set directory")
    ap.add_argument("--keep", type=int, default=4)
    ap.add_argument("--tenants", default="", help="comma-separated tenants (default: every branch)")
    args = ap.parse_args(argv)
    tenants = [t for t in args.tenants.split(",") if t] or sorted(
        p.name for p in (args.out / "branches").iterdir() if (p / "delivery").is_dir()
    )
    every: dict[str, list[dict[str, Any]]] = {}
    for t in tenants:
        print(t, flush=True)
        kept = []
        for q, entity in candidates(args.out / "branches" / t / "delivery"):
            if len(kept) >= args.keep:
                break
            r = check(t, q, entity)
            if r:
                kept.append(r)
        doc = {"tenant": t, "checkedAt": datetime.now(UTC).isoformat(timespec="seconds"), "examples": kept}
        (args.out / "branches" / t / "delivery" / "examples.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        every[t] = kept
    if args.day_out:
        day = args.day_out / "day_examples.json"
        d = json.loads(day.read_text(encoding="utf-8"))
        for exs in d["dayExamples"].values():
            for e in exs:
                if e.get("tenant") in every:
                    e["askExamples"] = [x["question"] for x in every[e["tenant"]]]
        files = {}
        for name, obj in (("day_examples.json", d), ("examples.json", every)):
            text = json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
            (args.day_out / name).write_text(text, encoding="utf-8")
            files[name] = {
                "path": name,
                "bytes": len(text.encode()),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        man_path = args.day_out / "manifest.json"
        man = json.loads(man_path.read_text(encoding="utf-8"))
        man["files"] = list(files.values())
        man_path.write_text(json.dumps(man, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({t: len(v) for t, v in every.items()}))
    return 0 if all(every.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
