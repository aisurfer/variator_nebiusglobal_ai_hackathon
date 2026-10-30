"""Short series behind some habits (sparklines): counts or amounts per month, sprint or survey.

Read from the history with the as-of cut of ``variator.actors.history.load_history`` (rows at
or after T are never read). A series is ``{"label", "points": [[x, y], ...]}``; x is a period
label (``2026-03``, ``S12``, ``2026-Q2``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import Any

from variator.actors.history import ENGINEER_ROLES, History, parse_date


def _months(h: History) -> list[str]:
    """The 12 full or partial months that end before T, oldest first."""
    out: list[str] = []
    y, m = h.as_of.year, h.as_of.month
    for _ in range(12):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    first = f"{h.start.year:04d}-{h.start.month:02d}"
    return [x for x in reversed(out) if x >= first]


def _month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _first_versions(h: History, otype: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for r in h.records.get(otype, []):
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


def _per_month(h: History, rows: list[tuple[date, float]], label: str) -> dict[str, Any]:
    months = _months(h)
    acc: Counter = Counter()
    for d, v in rows:
        acc[_month(d)] += v
    return {"label": label, "points": [[m, round(acc.get(m, 0.0), 2)] for m in months]}


def build_series(h: History) -> dict[str, dict[str, dict[str, Any]]]:
    """Card id -> habit key -> series."""
    out: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    _clients(h, out)
    _people(h, out)
    _vendors(h, out)
    _company(h, out)
    return dict(out)


def _clients(h: History, out: dict[str, dict[str, dict[str, Any]]]) -> None:
    tickets: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for r in _first_versions(h, "support_ticket"):
        if r.get("client"):
            tickets[r["client"]].append((h.local(r["ts"]).date(), 1.0))
    for cid, rows in tickets.items():
        out[cid]["tickets_per_month"] = _per_month(h, rows, "Tickets per month")
    std = [x for cid, rows in tickets.items() for x in rows if cid in h.clients and not h.clients[cid].key]
    if std:
        out["clients:standard"]["tickets_per_month"] = _per_month(
            h, std, "Tickets per month, all standard clients"
        )
    # payment days per paid invoice (the last 12)
    invoices = h.latest("invoice")
    pays: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for p in h.latest("payment").values():
        inv = invoices.get(str(p.get("settles") or ""))
        if not inv or inv.get("direction") == "payable" or not inv.get("client"):
            continue
        a, b = parse_date(inv.get("issued_at")), parse_date(p.get("paid_at"))
        if a and b and b >= a and inv.get("kind") != "opening_balance":
            pays[inv["client"]].append((b, float((b - a).days)))
    for cid, rows in pays.items():
        rows = sorted(rows)[-12:]
        if len(rows) >= 2:
            out[cid]["pay_delay_days"] = {
                "label": "Days to pay, last paid invoices",
                "points": [[d.isoformat(), v] for d, v in rows],
            }


def _people(h: History, out: dict[str, dict[str, dict[str, Any]]]) -> None:
    issues = {r["key"]: r for r in h.table("issues")}
    first_review: dict[str, dict[str, Any]] = {}
    for r in sorted(h.table("transitions").rows, key=lambda r: r["ts"]):
        if r["to"] == "In Review" and r["key"] not in first_review:
            first_review[r["key"]] = r
    sprint_of: dict[date, int] = {}
    for n, a, b in h.sprints:
        for d in h.workdays(a, b):
            sprint_of[d] = n
    pts: dict[str, Counter] = defaultdict(Counter)
    for k, r in first_review.items():
        iss = issues.get(k)
        if iss is None or iss.get("project") not in ("PLAT", "IMPL"):
            continue
        s = sprint_of.get(h.local(r["ts"]).date())
        if s is not None:
            pts[r["actor"]][s] += iss.get("points") or 0
    for pid, p in h.people.items():
        if p.role not in ENGINEER_ROLES or pid not in pts:
            continue
        first = max(p.start, h.start)
        sprints = [n for n, a, b in h.sprints if b > first and (p.last_day is None or a <= p.last_day)]
        out[pid]["throughput_points_per_sprint"] = {
            "label": "Points per sprint",
            "points": [[f"S{n + 1}", float(pts[pid].get(n, 0))] for n in sprints],
        }


def _vendors(h: History, out: dict[str, dict[str, dict[str, Any]]]) -> None:
    bills: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in h.latest("invoice").values():
        if r.get("direction") == "payable" and r.get("vendor") and r.get("period"):
            bills[r["vendor"]].append((str(r["period"]), float(r["amount"])))
    for r in h.latest("cloud_bill").values():
        bills[str(r.get("vendor") or "VN-03")].append((str(r["period"]), float(r["amount"])))
    months = set(_months(h))
    for vid, rows in bills.items():
        rows = sorted(x for x in rows if x[0] in months)
        if len(rows) >= 3:
            out[vid]["monthly_fee"] = {
                "label": "Bill per month, USD",
                "points": [[p, round(v, 2)] for p, v in rows],
            }
    taxes: list[tuple[date, float]] = []
    for r in h.latest("tax_payment").values():
        d = parse_date(r.get("due_date"))
        if r.get("authority") and d and d < h.as_of:
            taxes.append((d, float(r["amount"])))
    if taxes:
        out["TA-UK"]["payroll_tax_rate"] = _per_month(h, taxes, "Tax payments per month, USD")


def _company(h: History, out: dict[str, dict[str, dict[str, Any]]]) -> None:
    inc = [(h.local(r["ts"]).date(), 1.0) for r in _first_versions(h, "incident")]
    out["company"]["incidents_per_month"] = _per_month(h, inc, "Incidents per month")
    arr = {}
    for r in h.table("arr_daily"):
        arr[_month(r["date"])] = float(r["arr"])
    months = _months(h)
    out["company"]["arr_at_t"] = {
        "label": "ARR at the end of the month, USD",
        "points": [[m, arr[m]] for m in months if m in arr],
    }
    deals = [
        (h.local(r["ts"]).date(), 1.0)
        for r in _first_versions(h, "deal")
        if r.get("kind") != "opening_balance"
    ]
    out["prospects"]["deals_per_month"] = _per_month(h, deals, "New deals per month")
    dep = [(h.local(r["ts"]).date(), 1.0) for r in _first_versions(h, "deploy")]
    out["company"]["deploys_per_week"] = _per_month(h, dep, "Deploys per month")
