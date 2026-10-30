"""The risk register (09 §2.4-2.5): event kinds that fired in the history before T.

Each risk has a base rate per month (Gamma posterior over the history window), size
distributions, the first and the last date, and the KB ids of the evidence. Negative and
positive events are both listed (a positive event also costs: load, expectations).
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import TYPE_CHECKING, Any

import numpy as np

from variator.actors import stats
from variator.actors.history import parse_date, parse_ts
from variator.actors.stats import Param

if TYPE_CHECKING:
    from variator.actors.learn import Learned

MAX_EVIDENCE = 25
PER_MONTH = 21.0


def _d(p: Param | None) -> dict[str, Any] | None:
    return p.to_dict() if p is not None else None


def _risk(
    L: Learned,
    code: str,
    title: str,
    kind: str,
    polarity: str,
    dates: list[date],
    evidence: list[str],
    *,
    actors: list[str] | None = None,
    size: dict[str, Any] | None = None,
    effect: str = "",
    extra: dict[str, Any] | None = None,
    dataset: str = "sim",
) -> dict[str, Any]:
    h = L.h
    bd = len(h.window_days)
    dates = sorted(d for d in dates if d is not None)
    rate = stats.rate(len(dates), bd, L.ctx.tag(dataset), "events per month", per=PER_MONTH)
    out: dict[str, Any] = {
        "code": code,
        "title": title,
        "kind": kind,
        "polarity": polarity,
        "count": len(dates),
        "base_rate_per_month": rate.to_dict(),
        "first": dates[0].isoformat() if dates else None,
        "last": dates[-1].isoformat() if dates else None,
        "size": {k: v for k, v in (size or {}).items() if v is not None},
        "actors": sorted(set(actors or [])),
        "evidence": evidence[:MAX_EVIDENCE],
        "evidence_count": len(evidence),
        "effect": effect,
    }
    if extra:
        out.update(extra)
    return out


def build_register(L: Learned) -> list[dict[str, Any]]:
    h = L.h
    C, P, CO = L.clients, L.people, L.company
    risks: list[dict[str, Any]] = []
    loc = lambda t: h.local(parse_ts(t)).date()  # noqa: E731

    # ------------------------------------------------------------------ incidents by severity
    inc = CO.events.get("incident", [])
    for sev, title in (
        ("SEV1", "Major incident (SEV1)"),
        ("SEV2", "Incident (SEV2)"),
        ("SEV3", "Minor incident (SEV3)"),
    ):
        rows = [r for r in inc if r.get("severity") == sev]
        if not rows:
            continue
        causes = Counter(r.get("cause_kind") for r in rows)
        risks.append(
            _risk(
                L,
                f"incident_{sev.lower()}",
                title,
                "incident",
                "negative",
                [loc(r["started_at"]) for r in rows],
                [r["kb"] for r in rows],
                actors=sorted({c for r in rows for c in r.get("affected_clients") or []}),
                size={
                    "restore_hours": _d(CO.params.get(f"restore_hours_{sev}")),
                    "affected_clients": _d(CO.params.get(f"affected_clients_{sev}")),
                    "downtime_hours_per_client": _d(CO.params.get(f"downtime_hours_{sev}")),
                },
                effect="clients lose service; affected clients open tickets and their churn risk rises",
                extra={"causes": dict(causes)},
                dataset="sim_incident",
            )
        )
    # ------------------------------------------------------------------ vendor capacity
    caps = CO.events.get("vendor_capacity", [])
    if caps:
        linked = [r for r in inc if r.get("cause_kind") == "cloud provider capacity"]
        risks.append(
            _risk(
                L,
                "vendor_capacity",
                "Cloud provider capacity shortage",
                "vendor",
                "negative",
                [parse_date(le["effective_date"]) for le in caps],
                [le["kb"] for le in caps] + [r["kb"] for r in linked],
                actors=sorted({le.get("vendor") for le in caps if le.get("vendor")}),
                size={
                    "incidents_caused": len(linked),
                    "severity_mix": dict(Counter(r.get("severity") for r in linked)),
                },
                effect="an incident of the platform; GPU capacity is short",
                dataset="sim_life_event",
            )
        )
    # ------------------------------------------------------------------ clients
    churn = C.events.get("churn", [])
    if churn:
        arr_lost = []
        ev = []
        for le in churn:
            c = h.clients.get(le["client"])
            ev.append(le["kb"])
            if c and c.contract:
                ev.append(h.kb("sim", c.contract))
                acv = _acv_at(L, c.contract, parse_date(le["effective_date"]))
                if acv:
                    arr_lost.append(acv)
        size = {
            "arr_lost_usd": _d(
                stats.lognormal(
                    np.array(arr_lost), L.ctx.tag("sim_contract"), "USD per year", "churn_arr"
                ).median
            )
            if len(arr_lost) >= 2
            else None,
            "arr_lost_total_usd": round(sum(arr_lost), 2),
        }
        risks.append(
            _risk(
                L,
                "client_churn",
                "Client gives notice (churn)",
                "client",
                "negative",
                [parse_date(le["effective_date"]) for le in churn],
                ev,
                actors=[le["client"] for le in churn],
                size=size,
                effect="the contract ends at a billing boundary after the notice; ARR falls",
                extra={
                    "hazard_realised": _d(C.company.get("churn_hazard_realised")),
                    "hazard_base": _d(C.company.get("churn_hazard_base")),
                    "incident_effect_k": _d(C.company.get("churn_incident_k")),
                    "model": C.churn_model,
                },
                dataset="sim_life_event",
            )
        )
    for typ, code, title, pol, effect, size in (
        (
            "downgrade",
            "client_downgrade",
            "Client reduces its scope (downgrade)",
            "negative",
            "the ACV falls from the next billing period",
            {"share_of_acv": _d(C.company.get("downgrade_size_share"))},
        ),
        (
            "late_payment",
            "late_payment_spell",
            "Client pays late for a period (cash-flow problems)",
            "negative",
            "invoices are paid later: cash and DSO",
            {
                "extra_days": _d(
                    C.company.get("late_extra_days_observed") or C.company.get("late_extra_days")
                ),
                "spell_days": _d(C.company.get("late_spell_days")),
            },
        ),
        (
            "upsell_request",
            "client_upsell",
            "Client asks for more (upsell request)",
            "positive",
            "a deal; if won, the ACV grows and an implementation adds tickets and support load",
            {
                "share_of_acv": _d(C.company.get("upsell_size_share")),
                "win_share": _d(CO.params.get("upsell_win_share")),
                "support_multiplier": _d(C.company.get("support_mult_after_deal")),
            },
        ),
    ):
        rows = C.events.get(typ, [])
        if rows:
            risks.append(
                _risk(
                    L,
                    code,
                    title,
                    "client",
                    pol,
                    [parse_date(le["effective_date"]) for le in rows],
                    [le["kb"] for le in rows],
                    actors=[le["client"] for le in rows],
                    size=size,
                    effect=effect,
                    dataset="sim_life_event",
                )
            )
    lost = CO.events.get("deal_lost", [])
    if lost:
        risks.append(
            _risk(
                L,
                "deal_lost",
                "New-business deal lost",
                "sales",
                "negative",
                [parse_date(d.get("closed_at")) for d in lost],
                [d["kb"] for d in lost],
                size={
                    "reasons": dict(Counter(d.get("lost_reason") for d in lost)),
                    "win_rate": _d(CO.params.get("win_rate")),
                },
                effect="the pipeline does not become ARR",
                dataset="sim_deal",
            )
        )
    # ------------------------------------------------------------------ people
    ev = P.events
    res_rows = ev.get("resignation_received", [])
    if res_rows:
        les = {le.get("employee"): le for le in ev.get("decides_to_leave", [])}
        evid = []
        for e in res_rows:
            evid.append(e["kb"])
            if e["employee"] in les:
                evid.append(les[e["employee"]]["kb"])
        reasons = Counter(
            les[e["employee"]].get("reason", "unknown") for e in res_rows if e["employee"] in les
        )
        risks.append(
            _risk(
                L,
                "resignation",
                "Employee resigns",
                "person",
                "negative",
                [parse_date(e["effective_date"]) for e in res_rows],
                evid,
                actors=[e["employee"] for e in res_rows],
                size={
                    "backfill_lead_months": _d(CO.params.get("hiring_lead_months")),
                    "notice_working_days": _notice_bdays(L, res_rows),
                },
                effect="capacity falls after the notice; a requisition runs; the hire ramps up",
                extra={"hazard": _d(P.company.get("resignation_hazard")), "reasons": dict(reasons)},
                dataset="sim_employee_event",
            )
        )
    senior = [v for v in CO.vacancies if v["senior"]]
    if senior:
        effects = CO.info.get("mttr_vacancy_effects", {})
        leavers = {v["leaver"] for v in senior}
        risks.append(
            _risk(
                L,
                "key_person_absence",
                "A senior seat is empty (key-person absence)",
                "person",
                "negative",
                [v["from"] for v in senior],
                [f"person.{x.lower()}" for x in sorted(leavers)]
                + [e["kb"] for e in res_rows if e["employee"] in leavers],
                actors=[v["leaver"] for v in senior],
                size={
                    "vacancy_months": _d(
                        stats.normal_mean(
                            np.array([(v["to"] - v["from"]).days / 30.44 for v in senior]),
                            L.ctx.tag("sim_employee_event"),
                            "months",
                        )
                    )
                    if len(senior) >= 2
                    else round((senior[0]["to"] - senior[0]["from"]).days / 30.44, 2),
                    "mttr_multiplier": {
                        k: v.to_dict() for k, v in effects.items() if k in {s["seat"] for s in senior}
                    },
                },
                effect="slower incident restore and less capacity until the hire is at full pace",
                extra={
                    "vacancies": [
                        {**v, "from": v["from"].isoformat(), "to": v["to"].isoformat()} for v in senior
                    ],
                    "mttr_model_selected": CO.info.get("mttr_model", {}).get("selected"),
                },
                dataset="sim_employee_event",
            )
        )
    loads = h.table("sprint_load").rows
    over = [r for r in loads if r["load"] > 1.05]
    if over:
        sp = {n: (a, b) for n, a, b in h.sprints}
        burn = [le for le in ev.get("decides_to_leave", []) if le.get("burnout")]
        risks.append(
            _risk(
                L,
                "team_overload",
                "Team over capacity (sprint load above 1.05)",
                "team",
                "negative",
                [sp[r["sprint"]][0] for r in over if r["sprint"] in sp],
                [le["kb"] for le in burn],
                actors=[le.get("employee") for le in burn],
                size={
                    "load": _d(CO.params.get("team_load")),
                    "overload_sprint_share": _d(CO.params.get("overload_sprint_share")),
                },
                effect="burnout raises the resignation hazard; more incidents; fewer deals won",
                extra={"sprints": [r["sprint"] for r in over]},
                dataset="sprint_load",
            )
        )
    for kind, code, title, pol, effect in (
        (
            "salary_review_declined",
            "raise_declined",
            "Salary review declined after growth",
            "negative",
            "an unmet expectation raises the resignation hazard",
        ),
        (
            "growth_recognised",
            "employee_growth",
            "Employee grows in the role",
            "positive",
            "a faster pace and a higher salary expectation",
        ),
        (
            "rotation_started",
            "rotation",
            "Engineer rotates to another component",
            "negative",
            "the pace falls for about a month",
        ),
        ("sickness_reported", "sickness", "Sickness absence", "negative", "capacity falls for a few days"),
    ):
        rows = ev.get(kind, [])
        if rows:
            size = {}
            if kind == "sickness_reported":
                size["days_per_spell"] = _d(P.company.get("sick_days_per_spell"))
            if kind == "growth_recognised":
                size["raise_ask_share"] = _d(P.company.get("raise_ask_share"))
            risks.append(
                _risk(
                    L,
                    code,
                    title,
                    "person",
                    pol,
                    [parse_date(e["effective_date"]) for e in rows],
                    [e["kb"] for e in rows],
                    actors=[e["employee"] for e in rows],
                    size=size,
                    effect=effect,
                    dataset="sim_employee_event",
                )
            )
    # ------------------------------------------------------------------ NPS detractors (v2)
    det = CO.events.get("nps_detractor", [])
    if det:
        risks.append(
            _risk(
                L,
                "nps_detractor",
                "Client contact answers the NPS survey as a detractor (score 0-6)",
                "client",
                "negative",
                [parse_date(r.get("date")) for r in det],
                [r["kb"] for r in det],
                actors=[r["client"] for r in det],
                size={
                    "nps_now": _d(CO.params.get("nps_now")),
                    "nps_history": _d(CO.params.get("nps_history")),
                },
                effect="a follow-up task of the account manager; a low NPS in the renewal window adds churn",
                dataset=L.h.optional.get("nps", "nps"),
            )
        )
    # ------------------------------------------------------------------ service level (v2)
    breach = CO.params.get("sla_breach_day_share")
    if breach is not None and breach.extra.get("k"):
        risk = _risk(
            L,
            "sla_breach_day",
            "Platform availability below the SLA target on a day",
            "service",
            "negative",
            [],
            [],
            size={"availability": _d(CO.params.get("availability")), "breach_day_share": _d(breach)},
            effect="SLA gaps lower the NPS of the affected clients",
            dataset=L.h.optional.get("monitoring", ""),
        )
        risk["count"] = int(breach.extra["k"])
        risk["base_rate_per_month"] = stats.rate(
            breach.extra["k"],
            breach.n,
            L.ctx.tag(L.h.optional.get("monitoring", "")),
            "days per month",
            per=30.44,
        ).to_dict()
        days = sorted(str(d)[:10] for d in CO.info.get("sla_breach_days", []))
        hit = [r["kb"] for r in inc if str(r.get("started_at", ""))[:10] in set(days)]
        risk["note"] = (
            "counted on the calendar days of the monitoring dataset; evidence: the incidents of those days"
        )
        risk["first"], risk["last"] = (days[0], days[-1]) if days else (None, None)
        risk["evidence"] = (hit or [r["kb"] for r in inc])[-MAX_EVIDENCE:]
        risk["evidence_count"] = len(hit)
        risks.append(risk)
    # ------------------------------------------------------------------ vendor cost changes
    g = CO.params.get("cloud_cost_growth_per_year")
    if g is not None:
        vi = CO.vendor_info
        ev_ids = [x for v in vi.values() for x in v.get("evidence", [])]
        months = sorted(r["period"] for r in h.latest("cloud_bill").values())
        dates = [date.fromisoformat(m + "-01") for m in months]
        risk = _risk(
            L,
            "vendor_cost_change",
            "Vendor cost changes (cloud trend, subcontractor bills)",
            "vendor",
            "negative",
            dates,
            ev_ids,
            actors=list(vi),
            size={
                "cloud_growth_per_year": _d(g),
                "bill_variability_cv": {
                    k: round(float(v.get("monthly_fee").extra.get("cv", 0.0)), 4)
                    for k, v in CO.vendors.items()
                    if v.get("monthly_fee")
                },
            },
            effect="opex and infrastructure cost drift",
            dataset="sim_cloud_bill",
        )
        risk["note"] = "the dates are the monthly cloud bills; the rate is not an event rate"
        risks.append(risk)
    for n, r in enumerate(risks, start=1):
        r["id"] = f"R{n}"
    return [{"id": r.pop("id"), **r} for r in risks]


def _acv_at(L: Learned, contract: str, d: date | None) -> float | None:
    vers = L.h.versions("contract").get(contract, [])
    acv = None
    for v in vers:
        for item in v.get("acv_schedule") or []:
            f = parse_date(item["from"])
            if d is None or (f and f <= d):
                acv = float(item["acv"])
    return acv


def _notice_bdays(L: Learned, rows: list[dict]) -> dict[str, Any] | None:
    xs = []
    for e in rows:
        a, b = parse_date(e["effective_date"]), parse_date(e.get("end_date"))
        if a and b:
            xs.append(L.h.bdays(a, b))
    if not xs:
        return None
    return {"median": float(np.median(xs)), "min": min(xs), "max": max(xs), "n": len(xs)}
