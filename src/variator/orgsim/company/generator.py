"""One year of history of the MVP company: ``generate_history`` and ``write_history``.

Steps:

1. The day loop (``engine.Engine``) runs every working day from T - 12 months to T - 1.
2. ARR calibration: every client amount is scaled so that ARR at T = ``Truth.arr_target``
   (the life events do not depend on the amounts, so the scale changes no event).
3. Finance writes invoices, payments, payroll, bills, taxes and the bank; the opening balance
   makes cash at T = ``Truth.cash_target``.
4. Reports, then ``render``: ids, cut at T, lines, gold links, directory.

``write_history`` writes ``delivery/`` (corpus contract, ``orgsim.delivery.write_delivery``),
``datasets/`` (flat CSV and JSON Lines for metrics) and ``truth.json`` (hidden parameters,
outside the delivery). The same spec, anchor and seed give the same bytes.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from variator.orgsim.calendar import add_months, anchor_date
from variator.orgsim.company.cases import build_cases
from variator.orgsim.company.engine import Engine
from variator.orgsim.company.model import Money, World
from variator.orgsim.company.processes.finance import Finance
from variator.orgsim.company.processes.monitoring import DEPENDENTS, LATENCY_FACTOR, SERVICES
from variator.orgsim.company.processes.reports import Reports
from variator.orgsim.company.render import Renderer
from variator.orgsim.company.spec import CompanySpec, default_spec
from variator.orgsim.company.ukcal import company_calendar
from variator.orgsim.delivery import write_delivery
from variator.orgsim.events import Event, canonical_json


@dataclass
class History:
    spec: CompanySpec
    anchor: date
    seed: int
    start: date
    lines: list[dict[str, Any]]
    events: list[Event]
    gold: list[dict[str, Any]]
    cases: list[dict[str, Any]]
    sources: dict[str, Any]
    people: dict[str, Any]
    dashboard: dict[str, Any]
    truth: dict[str, Any]
    datasets: dict[str, list[dict[str, Any]]]
    stats: dict[str, Any] = field(default_factory=dict)


def generate_history(
    spec: CompanySpec | None = None, anchor: date | None = None, seed: int = 7, *, months: int = 12
) -> History:
    """The history of ``months`` months before ``anchor`` (default: today in the company time zone)."""
    spec = spec or default_spec()
    cal0 = company_calendar(spec.company.timezone, 2000, 2000)
    anchor = anchor_date(anchor, cal0)
    start = add_months(anchor, -months)
    cal = company_calendar(spec.company.timezone, start.year - 1, anchor.year + 1)
    w = World(spec, anchor, seed, cal, start)
    eng = Engine(w)
    eng.run()
    raw_arr = eng.clients.arr_on(anchor)
    scale = spec.truth.arr_target / raw_arr if raw_arr > 0 else 1.0
    fin = Finance(w, eng, scale)
    fin.run()
    rep = Reports(w, eng, fin)
    rep.run()
    r = Renderer(w, eng, scale, rep.implicit)
    r.assign_ids()
    r.cut()
    sources, people, dashboard = r.directory()
    lines, events = r.lines()
    gold = r.gold(lines)
    cases = build_cases(w, lines, rep.implicit, r.slug)
    monitoring = eng.monitoring.rows()
    datasets = _datasets(w, eng, fin, r, lines, events, scale, monitoring)
    stats = _stats(w, eng, fin, r, lines, scale, raw_arr, monitoring)
    truth = _truth(w, eng, fin, scale, raw_arr, stats)
    return History(
        spec,
        anchor,
        seed,
        start,
        lines,
        events,
        gold,
        cases,
        sources,
        people,
        dashboard,
        truth,
        datasets,
        stats,
    )


# ---------------------------------------------------------------------------------------------- datasets
def _iso(x: Any) -> Any:
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    if hasattr(x, "ref") and callable(x.ref):
        return x.ref()
    return x


def _datasets(
    w: World,
    eng: Engine,
    fin: Finance,
    r: Renderer,
    lines: list[dict[str, Any]],
    events: list[Event],
    scale: float,
    monitoring: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for name, rows in w.datasets.items():
        out[name] = [{k: _iso(v) for k, v in row.items()} for row in rows]
    # NPS responses as delivered, and the monitoring series (not delivered as records)
    out["nps_responses"] = sorted(
        (
            {
                "date": ln["payload"]["fields"]["responded_at"][:10],
                "response_id": ln["external_id"],
                "client": ln["payload"]["fields"]["client"],
                "contact": ln["payload"]["fields"]["contact"],
                "survey": ln["payload"]["fields"]["survey"],
                "score": ln["payload"]["fields"]["score"],
            }
            for ln in lines
            if ln["source"] == "sim" and ln["object_type"] == "nps_response"
        ),
        key=lambda x: (x["date"], x["response_id"]),
    )
    out["monitoring_daily"] = monitoring
    # sim records: one row per version (the delivered fields)
    for ln in lines:
        if ln["source"] != "sim":
            continue
        p = ln["payload"]
        row = {
            "id": ln["external_id"],
            "ts": p["ts"],
            "kind": p["kind"],
            "process": p["process"],
            **{k: v for k, v in p["fields"].items()},
            "causes": [f"{c['type']}:{c['id']}" for c in p["causes"]],
        }
        out.setdefault(f"sim_{ln['object_type']}", []).append(row)
    # ARR and clients per day
    d = w.start
    while d < w.anchor:
        arr, n = 0.0, 0
        for c in w.clients.values():
            if c.active_on(d):
                arr += Money(c.acv_on(d), 1200).value(scale)
                n += 1
        out.setdefault("arr_daily", []).append(
            {"date": d.isoformat(), "arr": round(arr, 2), "active_clients": n}
        )
        d += timedelta(days=1)
    # Jira issues and transitions, merge requests, mails
    for i in w.issues:
        trans = [u for u in i.updates if u.kind == "transition"]
        blocked = sum(1 for u in trans if u.data["to"] == "Blocked")
        out.setdefault("issues", []).append(
            {
                "key": i.ext,
                "project": i.project,
                "type": i.itype,
                "component": i.component or "",
                "points": i.points,
                "priority": i.priority,
                "status": trans[-1].data["to"] if trans else "To Do",
                "created": i.created.isoformat(),
                "done_at": i.done_at.isoformat() if i.done_at and i.done_at < w.end else "",
                "assignee": i.assignee or "",
                "reporter": i.reporter,
                "parent": i.parent.ext if i.parent else "",
                "client": i.client or "",
                "sprints": len(i.sprints),
                "blocked_times": blocked,
                "unplanned": i.unplanned,
                "comments": sum(1 for u in i.updates if u.kind == "comment"),
            }
        )
        for u in trans:
            out.setdefault("transitions", []).append(
                {
                    "key": i.ext,
                    "ts": u.ts.isoformat(),
                    "from": u.data["from"],
                    "to": u.data["to"],
                    "actor": u.actor,
                }
            )
    for m in w.mrs:
        out.setdefault("merge_requests", []).append(
            {
                "iid": m.iid,
                "key": m.issue.ext,
                "author": m.author,
                "reviewer": m.reviewer,
                "created": m.created.isoformat(),
                "merged_at": m.merged_at.isoformat() if m.merged_at and m.merged_at < w.end else "",
                "notes": len(m.notes),
                "deploy": m.deployed_in.ext
                if m.deployed_in and m.deployed_in.versions and m.deployed_in.first_ts < w.end
                else "",
            }
        )
    for m in w.mails:
        out.setdefault("emails", []).append(
            {
                "id": m.ext,
                "sent": m.sent.isoformat(),
                "kind": m.kind,
                "process": m.process,
                "from": m.sender[1],
                "to": ";".join(a for _, a in m.to),
                "thread": m.thread,
                "reply_to": m.parent.ext if m.parent else "",
            }
        )
    out["sprint_load"] = [dict(x) for x in eng.work.loads]
    out["people"] = [
        {
            "id": pid,
            "name": p.person.name,
            "title": p.person.title,
            "role": p.person.role,
            "department": p.person.department,
            "seat": p.seat,
            "start": p.start.isoformat(),
            "last_day": p.last_day.isoformat() if p.last_day else "",
            "salary_at_t": p.salary,
            "hire_slot": p.hire_slot or "",
        }
        for pid, p in sorted(w.people.items())
    ]
    out["clients"] = [
        {
            "id": c.id,
            "name": c.profile.name,
            "industry": c.profile.industry,
            "key": c.profile.key,
            "billing": c.profile.billing,
            "new": c.new,
            "start": c.start.isoformat() if c.start else "",
            "end": c.end.isoformat() if c.end else "",
            "status_at_t": c.status,
            "acv_at_t": Money(c.acv_on(w.anchor), 1200).value(scale) if c.active_on(w.anchor) else 0.0,
            "account_manager": c.profile.account_manager,
        }
        for c in sorted(w.clients.values(), key=lambda c: c.id)
    ]
    out["metrics_monthly"] = _monthly(w, out)
    out["events"] = [e.envelope() for e in events]
    return out


def _monthly(w: World, ds: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))

    def key(s: str) -> str:
        return s[:7]

    for x in ds.get("sim_invoice", []):
        if x["kind"] in ("invoice_issued", "opening_balance") and x.get("direction") == "receivable":
            rows[key(x["issued_at"])]["revenue_invoiced"] += x["amount"]
        if x["kind"] in ("bill_received", "opening_balance") and x.get("direction") == "payable":
            rows[key(x["period"])][f"cost_{x.get('cost_class', 'opex')}"] += x["amount"]
    for x in ds.get("bank", []):
        if x["kind"] == "incoming":
            rows[key(x["date"])]["collections"] += x["amount"]
    for x in ds.get("cloud_cost_daily", []):
        rows[key(x["date"])]["cost_infra"] += x["total"]
        rows[key(x["date"])]["cost_gpu"] += x["gpu"]
    for x in ds.get("payroll_lines", []):
        rows[x["period"]]["cost_payroll"] += x["gross"] + x["employer_tax"]
    seen: set[str] = set()
    for x in ds.get("sim_incident", []):
        if x["id"] in seen:
            continue
        seen.add(x["id"])
        rows[key(x["started_at"])]["incidents"] += 1
        rows[key(x["started_at"])]["sev1"] += x["severity"] == "SEV1"
    for x in ds.get("sim_incident", []):
        if x["kind"] == "incident_resolved":
            rows[key(x["started_at"])]["restore_minutes"] += x.get("restore_minutes", 0)
    for x in ds.get("sim_deploy", []):
        if x["kind"] == "deployed":
            rows[key(x["deployed_at"])]["deploys"] += 1
    for x in ds.get("sim_support_ticket", []):
        if x["kind"] == "ticket_opened":
            rows[key(x["opened_at"])]["support_tickets"] += 1
    out = []
    for m in sorted(rows):
        if m < f"{w.start:%Y-%m}":
            continue
        r = rows[m]
        inc = r.get("incidents", 0)
        out.append(
            {
                "month": m,
                **{k: round(v, 2) for k, v in sorted(r.items()) if k != "restore_minutes"},
                "mttr_h": round(r.get("restore_minutes", 0) / 60 / inc, 3) if inc else 0.0,
            }
        )
    return out


# ------------------------------------------------------------------------------------------ stats and truth
def _stats(
    w: World,
    eng: Engine,
    fin: Finance,
    r: Renderer,
    lines: list[dict[str, Any]],
    scale: float,
    raw_arr: float,
    monitoring: list[dict[str, Any]],
) -> dict[str, Any]:
    t = w.spec.truth
    by_type = Counter(f"{ln['source']}:{ln['object_type']}" for ln in lines)
    sla = eng.monitoring.sla_report(monitoring)
    nps = eng.nps.summary(w.records["nps_response"])
    nps["company_model"] = eng.nps.compare(sla["sla_series"])
    nps["churn_notices_nps"] = sum(
        1
        for le in w.records["life_event"]
        if le.state.get("event_type") == "churn" and "NPS" in le.state.get("statement", "")
    )
    clients0 = [c for c in w.clients.values() if not c.new]
    churned = [c for c in w.clients.values() if c.end is not None and c.end <= w.anchor]
    notices = [c for c in w.clients.values() if c.status in ("notice", "churned")]
    leavers = [p for p in w.people.values() if p.last_day and p.last_day < w.anchor]
    incidents = [x for x in w.records["incident"]]
    restore = [x.state.get("restore_minutes") for x in incidents if x.state.get("restore_minutes")]
    deploys = [x for x in w.records["deploy"] if x.versions]
    loads = [x["load"] for x in eng.work.loads]
    receivable = 0.0
    rev90 = 0.0
    for recs in w.records["invoice"]:
        s = recs.versions[-1].snapshot
        if s.get("direction") != "receivable":
            continue
        issued = date.fromisoformat(s["issued_at"])
        if s.get("status") != "paid":
            receivable += s["amount"]
        if (w.anchor - issued).days <= 90:
            rev90 += s["amount"]
    weeks = len(w.days) / 5
    arr_t = sum(
        Money(c.acv_on(w.anchor), 1200).value(scale) for c in w.clients.values() if c.active_on(w.anchor)
    )
    return {
        "lines": len(lines),
        "by_type": dict(sorted(by_type.items())),
        "arr_at_t": round(arr_t, 2),
        "arr_raw_at_t": round(raw_arr, 2),
        "arr_scale": round(scale, 6),
        "clients_at_t": sum(1 for c in w.clients.values() if c.active_on(w.anchor)),
        "clients_at_start": len(clients0),
        "new_clients": sum(1 for c in w.clients.values() if c.new),
        "churn_notices": len(notices),
        "churned_by_t": len(churned),
        "realised_churn_notice_rate": round(len(notices) / max(len(clients0), 1), 4),
        "cash_opening": fin.opening,
        "cash_at_t": fin.cash_at_t,
        "dso_days_at_t": round(receivable / max(rev90 / 90, 1e-9), 1),
        "headcount_at_t": sum(1 for p in w.people.values() if p.employed(w.anchor)),
        "leavers": len(leavers),
        "hires": eng.people.hires,
        "open_requisitions_at_t": sum(1 for p in w.people.values() if p.last_day and p.last_day < w.anchor)
        - sum(1 for p in w.people.values() if p.hire_slot and p.start < w.anchor),
        "incidents": len(incidents),
        "incidents_per_month": round(len(incidents) / 12, 2),
        "mttr_h_mean": round(float(np.mean(restore)) / 60, 3) if restore else 0.0,
        "deploys_per_week": round(len(deploys) / weeks, 2),
        "team_load_mean": round(float(np.mean(loads)), 3) if loads else 0.0,
        "team_load_max": round(float(np.max(loads)), 3) if loads else 0.0,
        "deals_new": len(eng.sales.all),
        "deals_won_new": sum(1 for d in eng.sales.won if d.prospect is not None),
        "upsells_won": sum(1 for d in eng.sales.won if d.prospect is None),
        "nps": nps,
        "sla": sla,
        "dropped_refs": dict(sorted(r.dropped.items())),
        "truth_targets": {
            "arr": t.arr_target,
            "cash": t.cash_target,
            "churn": t.base_churn,
            "attrition": t.attrition,
            "incidents_per_month": t.incidents_base,
            "mttr_h": t.mttr_h,
            "deploys_per_week": t.deploy_freq,
            "team_load": t.team_load,
            "ar_days": t.ar_days,
            "sla_target": t.sla_target,
            "latency_ms": t.latency_base,
            "nps_base": t.nps_base,
        },
    }


def _truth(
    w: World, eng: Engine, fin: Finance, scale: float, raw_arr: float, stats: dict[str, Any]
) -> dict[str, Any]:
    return {
        "generator": "variator.orgsim.company",
        "anchor": w.anchor.isoformat(),
        "start": w.start.isoformat(),
        "seed": w.seed,
        "spec": w.spec.model_dump(mode="json"),
        "calibration": {
            "arr_raw_at_t": round(raw_arr, 2),
            "arr_scale": round(scale, 8),
            "cash_opening": fin.opening,
            "cash_at_t": fin.cash_at_t,
            "note": "Client amounts are raw x arr_scale; the opening cash makes cash at T the target.",
        },
        "habits": {
            "people": {pid: p.habits.model_dump() for pid, p in sorted(w.people.items())},
            "clients": {cid: c.habits.model_dump() for cid, c in sorted(w.clients.items())},
        },
        "clients": [
            c.profile.model_dump(mode="json") | {"new": c.new}
            for c in sorted(w.clients.values(), key=lambda c: c.id)
        ],
        "hires": [
            {"id": pid, "slot": p.hire_slot, "seat": p.seat, "start": p.start.isoformat()}
            for pid, p in sorted(w.people.items())
            if p.hire_slot
        ],
        "couplings": COUPLINGS,
        "nps_levels": {
            survey: {
                cid: {
                    "level": round(lv["level"], 2),
                    "habit": lv["habit"],
                    "sla_points": round(lv["sla_points"], 2),
                    "overdue_points": round(lv["overdue_points"], 2),
                    "support_points": round(lv["support_points"], 2),
                    "tickets": lv["tickets"],
                }
                for cid, lv in sorted(levels.items())
            }
            for survey, levels in sorted(eng.nps.levels.items())
        },
        "monitoring": {
            "services": SERVICES,
            "latency_factor": {k: round(v, 4) for k, v in LATENCY_FACTOR.items()},
            "dependents": {k: list(v) for k, v in DEPENDENTS.items()},
            "gpu_events": [
                {"date": d.isoformat(), "hit": round(h, 3), "life_event": le.ext}
                for d, h, le in eng.monitoring.gpu
            ],
        },
        "realised": stats,
    }


COUPLINGS = [
    "incident -> monitoring: each service of incident.services is down for incident.downtime_minutes; "
    "availability = 1 - downtime / 1440 per service and day",
    "traffic load, incident restore time, cloud GPU capacity life event (k_lat_gpu) "
    "-> p95 latency per service",
    "weekly SLA gaps of the client (its incidents, latency) x recency x incident sensitivity (k_nps_sla), "
    "late implementation (k_nps_overdue), support quality seen (k_nps_support), client habit -> NPS score",
    "NPS signal of the latest survey below nps_churn_floor, in the renewal window "
    "-> churn hazard (k_churn_nps)",
    "NPS detractor -> follow-up BIZ ticket of the account manager",
]


# ---------------------------------------------------------------------------------------------- writing
def _csv_text(rows: list[dict[str, Any]], fixed: list[str] | None = None) -> str:
    cols: list[str] = list(fixed or [])
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(cols)
    for r in rows:
        wr.writerow(
            [
                ""
                if r.get(k) is None
                else (json.dumps(r[k], sort_keys=True) if isinstance(r[k], (list, dict)) else r[k])
                for k in cols
            ]
        )
    return buf.getvalue()


def _gzip(text: str) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(text.encode("utf-8"))
    return buf.getvalue()


JSONL = ("events",)
# datasets with a fixed column order (the header is written even when there are no rows)
COLUMNS = {
    "nps_responses": ["date", "response_id", "client", "contact", "survey", "score"],
    "monitoring_daily": ["date", "service", "p95_ms", "availability", "downtime_minutes", "error_rate"],
}


def write_history(h: History, out_dir: str | Path, *, sample_days: int = 10) -> dict[str, Any]:
    """Write ``delivery/``, ``datasets/`` and ``truth.json`` under ``out_dir``; return a summary."""
    out = Path(out_dir)
    delivery = out / "delivery"
    manifest = write_delivery(
        delivery,
        tenant=h.sources["tenant"]["id"],
        sources=h.sources,
        people=h.people,
        lines=h.lines,
        seed=h.seed,
        language="en",
        name=h.spec.company.name,
        dashboard=h.dashboard,
        gold_links=h.gold,
        cases=h.cases,
        extra_manifest={
            "history": {
                "anchor": h.anchor.isoformat(),
                "start": h.start.isoformat(),
                "generator": "variator.orgsim.company",
                "truth": "../truth.json (hidden)",
                "datasets": "../datasets",
            }
        },
    )
    if sample_days:
        cut = h.lines[-1]["at"] if h.lines else ""
        first = None
        days = sorted({ln["at"][:10] for ln in h.lines})
        if len(days) > sample_days:
            first = days[-sample_days]
        sample = [ln for ln in h.lines if first is None or ln["at"][:10] >= first]
        write_delivery(
            delivery / "sample",
            tenant=h.sources["tenant"]["id"],
            sources=h.sources,
            people=h.people,
            lines=sample,
            seed=h.seed,
            language="en",
            name=f"{h.spec.company.name} (sample)",
            dashboard=h.dashboard,
            sample_of=h.sources["tenant"]["id"],
        )
        del cut
    ds = out / "datasets"
    ds.mkdir(parents=True, exist_ok=True)
    sizes = {}
    for name in sorted(h.datasets):
        rows = h.datasets[name]
        if name in JSONL or name.startswith("sim_"):
            text = "".join(canonical_json(r) + "\n" for r in rows)
            path = ds / f"{name}.jsonl.gz"
            path.write_bytes(_gzip(text))
        else:
            path = ds / f"{name}.csv"
            path.write_text(_csv_text(rows, COLUMNS.get(name)), encoding="utf-8")
        sizes[path.name] = len(rows)
    (out / "truth.json").write_text(
        json.dumps(h.truth, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    summary = {
        "manifest_counts": manifest["counts"],
        "checks": manifest["checks"],
        "datasets": sizes,
        "stats": h.stats,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return summary
