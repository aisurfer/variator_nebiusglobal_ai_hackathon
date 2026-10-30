"""A forward run as a history directory: the history datasets plus the run's rows (for the metric catalogue).

``write_datasets(run, out)`` writes ``<out>/datasets`` in the format of ``orgsim.company.generator``
(``sim_<type>.jsonl.gz`` one row per version, ``bank.csv``, ``people.csv``, ``clients.csv``,
``monitoring_daily.csv``, ``nps_responses.csv``; the other history tables are copied) and
``<out>/delivery`` (manifest with ``history.anchor`` = the end of the window, ``sources.yaml``,
``people.yaml``). A metric defined over records then works on the history and on the future alike.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from variator.orgsim.company.forward.deliver import directory, sim_fields
from variator.orgsim.company.forward.sim import ForwardRun
from variator.orgsim.events import canonical_json

COPY = ("issues.csv", "transitions.csv", "merge_requests.csv", "sprint_load.csv", "arr_daily.csv")
# catalogue metrics over records the run does not make (sprints, Jira issues: copied as of T only), so their
# values after T would be stale: left out of the day metrics and of the branch briefs
NOT_ON_SIMULATED = frozenset({"ops.team_load", "ops.throughput", "ops.overdue_implementations"})


def _gz(rows: list[str]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=6) as gz:
        gz.write("".join(rows).encode("utf-8"))
    return buf.getvalue()


def _csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        return list(r.fieldnames or []), list(r)


def _write_csv(path: Path, cols: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: "" if row.get(k) is None else row.get(k) for k in cols})


def _bank_kind(r: Any) -> str:
    if r.state.get("direction") == "credit":
        return "incoming"
    ref = str(r.state.get("records", ""))
    if ref.startswith("PR-"):
        return "payroll"
    if ref.startswith("TAX-"):
        return "tax"
    if ref.startswith("CB"):
        return "direct_debit"
    return "outgoing"


def write_datasets(run: ForwardRun, out: str | Path) -> Path:
    out = Path(out)
    st = run.st
    src = st.history / "datasets"
    ds = out / "datasets"
    ds.mkdir(parents=True, exist_ok=True)
    # bank balances in time order (the run books a day's entries in process order, not in time order)
    balance: dict[str, float] = {}
    cash = st.cash_at_t
    for r in sorted(run.book.of("bank_entry"), key=lambda r: (r.versions[0].ts, r.id)):
        sign = 1 if r.state.get("direction") == "credit" else -1
        cash = round(cash + sign * float(r.state.get("amount", 0)), 2)
        balance[r.id] = cash
    # sim records: the history rows, then one row per version of the run
    by_type: dict[str, list[str]] = {}
    for p in sorted(src.glob("sim_*.jsonl.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            by_type[p.name[4:-9]] = [x if x.endswith("\n") else x + "\n" for x in fh if x.strip()]
    for r in run.book.records.values():
        for v in r.versions:
            row = {
                "id": r.id,
                "ts": v.ts.isoformat(),
                "kind": v.kind,
                "process": v.process,
                **sim_fields(run, r, v.fields, {}),
                "causes": [f"{t}:{i}" for t, i in v.causes],
            }
            if r.id in balance:
                row["balance_after"] = balance[r.id]
            by_type.setdefault(r.otype, []).append(canonical_json(row) + "\n")
    for ot, rows in by_type.items():
        (ds / f"sim_{ot}.jsonl.gz").write_bytes(_gz(rows))
    # bank: every entry of the run, client receipts included
    cols, rows = _csv_rows(src / "bank.csv")
    for r in sorted(run.book.of("bank_entry"), key=lambda r: r.id):
        s = r.state
        sign = 1 if s.get("direction") == "credit" else -1
        rows.append({
            "id": r.id, "ts": r.versions[0].ts.isoformat(), "date": s.get("booked_at"),
            "amount": round(sign * float(s.get("amount", 0)), 2), "kind": _bank_kind(r),
            "counterparty": s.get("counterparty") or "", "reference": s.get("reference", ""),
            "balance_after": balance[r.id],
        })  # fmt: skip
    _write_csv(ds / "bank.csv", cols, rows)
    # people: leavers get their last day, hires are added
    cols, rows = _csv_rows(src / "people.csv")
    for row in rows:
        if row["id"] in run.left_people:
            row["last_day"] = run.left_people[row["id"]].isoformat()
    for p in run.new_people:
        rows.append({
            "id": p.id, "name": p.name, "title": p.title, "role": p.role, "department": p.department,
            "seat": p.seat, "start": p.start.isoformat(),
            "last_day": run.left_people[p.id].isoformat() if p.id in run.left_people else "",
            "salary_at_t": p.salary, "hire_slot": "",
        })  # fmt: skip
    _write_csv(ds / "people.csv", cols, rows)
    # clients: ends and new clients
    cols, rows = _csv_rows(src / "clients.csv")
    known = {r["id"] for r in rows}
    for row in rows:
        c = run.clients.get(row["id"])
        if c is not None and c.end:
            row["end"] = c.end.isoformat()
    for cid, c in sorted(run.clients.items()):
        if cid in known:
            continue
        rows.append({
            "id": cid, "name": c.name, "industry": "", "key": False, "billing": c.billing, "new": True,
            "start": c.start.isoformat() if c.start else "", "end": c.end.isoformat() if c.end else "",
            "status_at_t": "prospect", "acv_at_t": 0.0, "account_manager": c.account_manager,
        })  # fmt: skip
    _write_csv(ds / "clients.csv", cols, rows)
    # monitoring and NPS
    cols, rows = _csv_rows(src / "monitoring_daily.csv")
    _write_csv(ds / "monitoring_daily.csv", cols, rows + run.monitoring)
    cols, rows = _csv_rows(src / "nps_responses.csv")
    for r in sorted(run.book.of("nps_response"), key=lambda r: r.id):
        s = r.state
        rows.append(
            {
                "date": str(s["responded_at"])[:10],
                "response_id": r.id,
                "client": s["client"],
                "contact": s.get("contact") or "",
                "survey": s["survey"],
                "score": s["score"],
            }
        )
    _write_csv(ds / "nps_responses.csv", cols, rows)
    for name in COPY:
        if (src / name).is_file():
            shutil.copyfile(src / name, ds / name)
    # the delivery metadata the catalogue reads (T = the end of the window)
    d = out / "delivery"
    (d / "config").mkdir(parents=True, exist_ok=True)
    man = dict(st.manifest)
    man["history"] = {**(man.get("history") or {}), "anchor": run.end.isoformat()}
    (d / "manifest.json").write_text(json.dumps(man, indent=1) + "\n", encoding="utf-8")
    sources, people = directory(run, st.tenant, run.fork.label)
    sources["tenant"]["id"] = st.tenant
    (d / "config" / "sources.yaml").write_text(yaml.safe_dump(sources, sort_keys=False), encoding="utf-8")
    (d / "config" / "people.yaml").write_text(yaml.safe_dump(people, sort_keys=False), encoding="utf-8")
    (d / "dashboard.json").write_text(json.dumps(st.dashboard, indent=1) + "\n", encoding="utf-8")
    return out
