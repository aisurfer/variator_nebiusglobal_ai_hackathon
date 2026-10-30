"""Facts for actor cards (09 §2.2): what a later LLM step may say, each with evidence ids.

Every number in a fact comes from a habit parameter or a record (``param`` names the
parameter); the text is a plain template. The LLM step writes the card text from these facts
and must not add numbers.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from variator.actors.stats import Param

if TYPE_CHECKING:
    from variator.actors.learn import Learned

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
SCHEMA = "variator.actors.facts/1"


def _n(x: float | None, d: int = 1) -> str:
    if x is None:
        return "?"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    if abs(x) >= 100:
        return f"{x:.0f}"
    return f"{x:.{d}f}".rstrip("0").rstrip(".") if d else f"{x:.0f}"


def _range(p: Param, d: int = 1, pct: bool = False) -> str:
    if pct:
        return f"{100 * p.low:.0f}-{100 * p.high:.0f}% (median {100 * p.point:.0f}%)"
    return f"{_n(p.low, d)}-{_n(p.high, d)} (median {_n(p.point, d)})"


class _Facts:
    def __init__(self, actor: str, kind: str, name: str):
        self.doc: dict[str, Any] = {"schema": SCHEMA, "actor": actor, "kind": kind, "name": name, "facts": []}

    def add(
        self,
        topic: str,
        text: str,
        evidence: list[str] | None = None,
        param: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> None:
        f: dict[str, Any] = {
            "id": f"f{len(self.doc['facts']) + 1}",
            "topic": topic,
            "text": text,
            "evidence": sorted(set(evidence or []), key=lambda x: (evidence or []).index(x)),
        }
        if param:
            f["param"] = param
        if values:
            f["values"] = values
        self.doc["facts"].append(f)


def _pv(p: Param) -> dict[str, Any]:
    return {
        k: v for k, v in p.to_dict().items() if k in ("point", "low", "mode", "high", "n", "source", "unit")
    }


def build_facts(L: Learned) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for a in L.actors:
        if a["kind"] == "person":
            out[a["id"]] = _person(L, a)
        elif a["kind"] == "client":
            out[a["id"]] = _client(L, a)
        elif a["kind"] == "vendor":
            out[a["id"]] = _vendor(L, a)
        elif a["kind"] == "regulator":
            out[a["id"]] = _regulator(L, a)
    out["company"] = _company(L)
    return out


# ---------------------------------------------------------------------------------------------- person
def _person(L: Learned, a: dict[str, Any]) -> dict[str, Any]:
    h = L.h
    pid = a["id"]
    P = L.people.params.get(pid, {})
    info = L.people.info.get(pid, {})
    ev = L.people.evidence.get(pid, {})
    me = f"person.{pid.lower()}"
    F = _Facts(pid, "person", a["name"])
    mgr = h.people.get(a.get("manager") or "")
    F.add(
        "identity",
        f"{a['name']} is {a['title']} in {a['department']}"
        + (f", reports to {mgr.name}" if mgr else "")
        + f"; in the company since {a['start']} "
        f"({a['tenure_months_at_t']} months at T).",
        [me],
        values={"start": a["start"]},
    )
    if a["status_at_t"] in ("notice", "left"):
        F.add(
            "status",
            f"{a['name']} resigned; the last working day is {a['last_day']}.",
            ev.get("resignation", []) + [me],
            values={"last_day": a["last_day"], "status": a["status_at_t"]},
        )
    if a.get("salary_at_t"):
        F.add(
            "pay",
            f"Annual salary at T: {_n(a['salary_at_t'])} USD.",
            ev.get("salary_review", []) or [me],
            values={"salary": a["salary_at_t"]},
        )
    p = P.get("throughput_points_per_sprint")
    if p:
        w = p.extra.get("shrinkage_weight", 1.0)
        F.add(
            "pace",
            f"Delivers {_range(p)} story points per two-week sprint at full availability "
            f"({info.get('delivered_issues', 0)} tickets, "
            f"{info.get('delivered_points', 0)} points in the history; "
            f"own-data weight {w:.2f}).",
            ev.get("throughput", []),
            "throughput_points_per_sprint",
            _pv(p),
        )
    p = P.get("cycle_time_days")
    if p:
        F.add(
            "pace",
            f"A ticket takes a median of {_n(p.point)} calendar days from the start to the review "
            f"({_n(p.low)}-{_n(p.high)} for the median).",
            ev.get("throughput", [])[:3],
            "cycle_time_days",
            _pv(p),
        )
    if info.get("blocked_times"):
        F.add(
            "pace",
            f"Was blocked {info['blocked_times']} times (about {_n(info.get('blocked_days', 0), 0)} "
            "working days in total).",
            ev.get("throughput", [])[:2],
            values={"blocked_times": info["blocked_times"]},
        )
    p = P.get("own_tasks_per_day")
    if p:
        F.add(
            "pace",
            f"Opens about {_range(p, 2)} own business tasks per working day "
            f"({info.get('own_tasks', 0)} tasks in the history).",
            ev.get("own_tasks_per_day", []),
            "own_tasks_per_day",
            _pv(p),
        )
    p = P.get("first_response_hours")
    if p:
        F.add(
            "pace",
            f"Answers a client ticket for the first time in a median of {_n(p.point, 2)} working hours "
            f"({_n(p.low, 2)}-{_n(p.high, 2)}).",
            ev.get("first_response_hours", []),
            "first_response_hours",
            _pv(p),
        )
    if info.get("activity_window"):
        F.add(
            "hours",
            f"Works from about {info['earliest_activity']}; most activity falls between "
            f"{info['activity_window'][0]} and {info['activity_window'][1]} local time.",
            ev.get("throughput", [])[:2] or [me],
            "work_start_minute",
            {"window": info["activity_window"], "earliest": info["earliest_activity"]},
        )
    p = P.get("sick_spells_per_year")
    if p:
        F.add(
            "absence",
            f"{info.get('sick_spells', 0)} sick spells in the history ({info.get('sick_days', 0)} working "
            f"days); expected {_range(p)} spells per year.",
            ev.get("sick_spells_per_year", []) or [me],
            "sick_spells_per_year",
            _pv(p),
        )
    if info.get("vacation_month"):
        F.add(
            "absence",
            f"Takes the main holiday in {MONTHS[info['vacation_month'] - 1]} "
            f"({info.get('vacation_main_days')} working days).",
            ev.get("vacation", [])[:4],
            values={"month": info["vacation_month"], "days": info.get("vacation_main_days")},
        )
    for pl in info.get("planned_leave", []):
        F.add(
            "absence",
            f"Leave booked from {pl['first']} to {pl['last']} ({pl['days']} working days).",
            pl.get("evidence", []),
            values=pl,
        )
    for g in info.get("growth_events", []):
        F.add(
            "life_event",
            f"Growth in the role recognised on {g} (pace and salary expectation rise).",
            ev.get("growth", []),
            values={"date": g},
        )
    for r in info.get("salary_reviews", []):
        txt = (
            f"Salary raised to {_n(r['salary'])} USD from {_n(r.get('previous'))} USD "
            f"(review of {r['date']})."
            if r["granted"]
            else f"Salary review declined on {r['date']}: expected {_n(r.get('expectation'))} USD, "
            f"stays at {_n(r.get('salary'))} USD."
        )
        F.add("life_event", txt, ev.get("salary_review", []), values=r)
    for d in info.get("rotations", []):
        F.add(
            "life_event",
            f"Rotated to another component on {d} (slower for about a month).",
            ev.get("rotation", []),
            values={"date": d},
        )
    p = P.get("resignation_hazard")
    if p and a["status_at_t"] == "active":
        own = "" if p.source.startswith("data:") else " (mostly the staff value: one-time events are rare)"
        F.add(
            "risk",
            f"Probability to resign within a year: {_range(p, pct=True)}{own}.",
            ev.get("resignation", []) or [me],
            "resignation_hazard",
            _pv(p),
        )
    return F.doc


# ---------------------------------------------------------------------------------------------- client
def _client(L: Learned, a: dict[str, Any]) -> dict[str, Any]:
    h = L.h
    cid = a["id"]
    P = L.clients.params.get(cid, {})
    info = L.clients.info.get(cid, {})
    ev = L.clients.evidence.get(cid, {})
    ctr = a["kb_ids"]
    F = _Facts(cid, "client", a["name"])
    am = h.people.get(a.get("account_manager") or "")
    F.add(
        "identity",
        f"{a['name']} ({a['industry']}) is a {'key ' if a['key'] else ''}client"
        + (f" since {a['start']}" if a.get("start") else "")
        + f"; billing {a['billing']}"
        + (f"; account manager {am.name}" if am else "")
        + ".",
        ctr,
        values={"key": a["key"]},
    )
    p = P.get("arr")
    if p:
        F.add(
            "revenue",
            f"ARR at T: {_n(p.point)} USD (status {a['status_at_t']}).",
            ctr + ev.get("renewal", [])[:2],
            "arr",
            _pv(p),
        )
    if a.get("notice_at"):
        F.add(
            "status",
            f"Gave notice on {a['notice_at']}; the service ends on {a['end']}.",
            ev.get("churn", []),
            values={"notice_at": a["notice_at"], "end": a["end"]},
        )
    if info.get("term_end") and a["status_at_t"] == "active":
        F.add(
            "contract",
            f"The current term ends on {info['term_end']}; notice period "
            f"{_n(P['notice_days'].point, 0) if 'notice_days' in P else '?'} days.",
            ctr,
            values={"term_end": info["term_end"]},
        )
    p = P.get("pay_delay_days")
    if p:
        sd = P.get("pay_delay_sd_days")
        F.add(
            "payments",
            f"Pays an invoice {_range(p, 0)} days after issue on average"
            + (f", give or take {_n(sd.point, 0)} days" if sd else "")
            + f" ({info.get('invoices_paid', 0)} paid invoices).",
            ev.get("pay_delay_days", []),
            "pay_delay_days",
            _pv(p),
        )
    for le in [x for x in L.clients.events.get("late_payment", []) if x["client"] == cid]:
        F.add(
            "life_event",
            f"Cash-flow problems from {le['effective_date']} to {le.get('end_date')}: payments about "
            f"{_n(le.get('extra_days'), 0)} days late.",
            [le["kb"]] + ev.get("late_payment", [])[:3],
            values={
                "from": le["effective_date"],
                "to": le.get("end_date"),
                "extra_days": le.get("extra_days"),
            },
        )
    p = P.get("tickets_per_month")
    if p:
        F.add(
            "support",
            f"Opens {_range(p, 2)} support tickets per month ({info.get('tickets_base', 0)} base tickets, "
            f"{info.get('tickets_incident', 0)} after incidents, "
            f"{info.get('tickets_after_deal', 0)} after deals).",
            ev.get("tickets_per_month", []),
            "tickets_per_month",
            _pv(p),
        )
    for le in [x for x in L.clients.events.get("upsell_request", []) if x["client"] == cid]:
        F.add(
            "life_event",
            f"{le.get('title')} on {le['effective_date']} "
            f"(about {100 * float(le.get('size_share') or 0):.0f}% of the ACV).",
            [le["kb"]],
            values={"date": le["effective_date"], "size_share": le.get("size_share")},
        )
    for le in [x for x in L.clients.events.get("downgrade", []) if x["client"] == cid]:
        F.add(
            "life_event",
            f"Reduced its scope on {le['effective_date']} by about "
            f"{100 * float(le.get('size_share') or 0):.0f}%.",
            [le["kb"]],
            values={"date": le["effective_date"]},
        )
    p = P.get("nps_level")
    if p:
        F.add(
            "satisfaction",
            f"NPS level {_range(p, 0)} points ({p.extra.get('responses')} survey answers, mean score "
            f"{_n(p.extra.get('mean_score'), 1)}; own-data weight {p.extra.get('shrinkage_weight', 0):.2f}).",
            ev.get("nps", []),
            "nps_level",
            _pv(p),
        )
    hits = [r for r in L.company.events.get("incident", []) if cid in (r.get("affected_clients") or [])]
    if hits:
        sev = Counter(r.get("severity") for r in hits)
        F.add(
            "service",
            f"Affected by {len(hits)} incidents in the history ("
            + ", ".join(f"{k} {v}" for k, v in sorted(sev.items()))
            + ").",
            [r["kb"] for r in hits][-6:],
            values={"incidents": len(hits), "by_severity": dict(sev)},
        )
    p = P.get("churn_hazard")
    if p:
        F.add(
            "risk",
            f"Probability to give notice within a year: {_range(p, pct=True)} (cohort base hazard"
            + (
                f" x {p.extra.get('multiplier', 1):.2f} for recent downtime"
                if p.extra.get("multiplier", 1) > 1.001
                else ""
            )
            + ").",
            ev.get("churn", []) or ctr,
            "churn_hazard",
            _pv(p),
        )
    for name, label, key in (
        ("upsell_rate", "asks for more (upsell)", "upsell_request"),
        ("late_payment_rate", "starts a late-payment spell", "late_payment"),
        ("downgrade_rate", "reduces its scope", "downgrade"),
    ):
        p = P.get(name)
        if p and a["status_at_t"] == "active":
            F.add(
                "risk",
                f"Expected times per year it {label}: {_range(p, 2)}.",
                ev.get(key, []) or ctr,
                name,
                _pv(p),
            )
    for r in info.get("renewals", []):
        F.add(
            "contract",
            f"Renewed on {r['date']} with an uplift of {100 * float(r['uplift']):.1f}%.",
            ev.get("renewal", []),
            values=r,
        )
    return F.doc


# -------------------------------------------------------------------------------------- vendor, regulator
def _vendor(L: Learned, a: dict[str, Any]) -> dict[str, Any]:
    vid = a["id"]
    info = L.company.vendor_info.get(vid, {})
    P = L.company.vendors.get(vid, {})
    F = _Facts(vid, "vendor", a.get("name") or vid)
    F.add(
        "identity",
        f"{a.get('name')}: {info.get('title') or 'vendor agreement'}; payment terms "
        f"{info.get('payment_terms_days')} days.",
        [x for x in [info.get("contract")] if x],
        values={"terms_days": info.get("payment_terms_days")},
    )
    p = P.get("monthly_fee")
    if p:
        F.add(
            "cost",
            f"Bills {_range(p, 0)} USD per month ({info.get('bills')} bills; variation "
            f"{100 * p.extra.get('cv', 0):.1f}%).",
            info.get("evidence", []),
            "monthly_fee",
            _pv(p),
        )
    if info.get("growth_per_year") is not None and abs(info["growth_per_year"]) > 0.005:
        F.add(
            "cost",
            f"The bills changed by {100 * info['growth_per_year']:+.1f}% per year over the history.",
            info.get("evidence", []),
            values={"growth_per_year": info["growth_per_year"]},
        )
    caps = [le for le in L.company.events.get("vendor_capacity", []) if le.get("vendor") == vid]
    if caps:
        F.add(
            "risk",
            f"{len(caps)} capacity shortages in the history (last on {caps[-1]['effective_date']}).",
            [le["kb"] for le in caps],
            values={"events": len(caps)},
        )
    return F.doc


def _regulator(L: Learned, a: dict[str, Any]) -> dict[str, Any]:
    F = _Facts(a["id"], "regulator", a.get("name") or a["id"])
    for kind, s in sorted(L.company.tax_info.get("schedule", {}).items()):
        F.add(
            "schedule",
            f"{kind}: {s['payments']} payments in the history, median {_n(s['median_amount'])} USD; "
            f"{s['late_payments']} paid late.",
            s.get("evidence", []),
            values={k: v for k, v in s.items() if k != "evidence"},
        )
    for name, p in L.company.tax.items():
        F.add(
            "rate",
            f"{name.replace('_', ' ')}: {100 * p.point:.1f}% of gross pay.",
            L.company.tax_info.get("payroll_evidence", []),
            name,
            _pv(p),
        )
    return F.doc


def _company(L: Learned) -> dict[str, Any]:
    co = L.company.params
    F = _Facts("company", "company", L.h.company.get("name") or "company")
    for name, label in (
        ("arr_at_t", "ARR at T, USD"),
        ("cash_at_t", "cash at T, USD"),
        ("incidents_per_month", "incidents per month"),
        ("mttr_hours", "MTTR, hours"),
        ("deploys_per_week", "deploys per 5 working days"),
        ("win_rate", "win rate of new deals"),
        ("deals_per_month", "new deals per month"),
        ("team_load", "sprint load"),
        ("nps_now", "NPS of the latest survey"),
        ("latency_p95_ms", "p95 latency, ms"),
        ("availability", "daily availability"),
    ):
        p = co.get(name)
        if p:
            F.add("process", f"{label}: {_range(p, 2)}.", L.company.evidence.get(name, []), name, _pv(p))
    for r in L.risks[:12]:
        F.add(
            "risk",
            f"{r['id']} {r['title']}: {r['count']} events, "
            f"{_n(r['base_rate_per_month']['point'], 2)} per month ({r['first']} to {r['last']}).",
            r["evidence"][:5],
            values={"risk": r["id"], "code": r["code"]},
        )
    return F.doc
