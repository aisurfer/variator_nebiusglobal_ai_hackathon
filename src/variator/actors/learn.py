"""Learn the company from its history: the actor registry, habits, cohorts, risks and card facts.

``learn(history_dir, as_of=None)`` returns a ``Learned`` object; ``Learned.documents()``
gives the JSON documents of the artifact set (``artifacts.write_set`` writes them).
No LLM: parameters that need general knowledge are marked ``llm:pending``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from variator.actors import stats
from variator.actors.clients import ClientsResult, cohort_of, learn_clients
from variator.actors.company import CompanyResult, learn_company
from variator.actors.context import Context, build_context
from variator.actors.history import ENGINEER_ROLES, History, load_history
from variator.actors.people import PeopleResult, learn_people
from variator.actors.stats import Param

SCHEMA = "variator.actors/1"
SOURCE_ORDER = ("data:", "group:", "web:", "llm:", "assumption:")
# Parameters of a future employee that the history cannot give for a role without hires.
LLM_PENDING = {
    "ramp_up_months": "months to the full pace of the role (general knowledge of the profession)",
    "time_to_first_delivery_days": "working days to the first own delivery in the role",
    "first_year_risks": "typical risks of the first year in the role (text)",
}


@dataclass
class Learned:
    h: History
    ctx: Context
    people: PeopleResult
    clients: ClientsResult
    company: CompanyResult
    actors: list[dict[str, Any]] = field(default_factory=list)
    cohorts: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def as_of(self) -> date:
        return self.h.as_of

    def habits_doc(self) -> dict[str, Any]:
        def block(d: dict[str, Param]) -> dict[str, Any]:
            return {k: v.to_dict() for k, v in sorted(d.items())}

        return {
            "schema": SCHEMA,
            "as_of": self.as_of.isoformat(),
            "format": "each parameter: point (posterior median), low (p1), mode, high (p99), n, source; "
            "low/mode/high are the PERT inputs of variator.domain",
            "people": {pid: block(p) for pid, p in sorted(self.people.params.items())},
            "clients": {cid: block(p) for cid, p in sorted(self.clients.params.items())},
            "vendors": {vid: block(p) for vid, p in sorted(self.company.vendors.items())},
            "regulators": {self.company.tax_info.get("authority") or "tax": block(self.company.tax)},
            "company": block(self.company.params),
            "staff": block(self.people.company),
            "client_base": block(self.clients.company),
            "models": {
                "churn": self.clients.churn_model,
                "mttr": self.company.info.get("mttr_model"),
            },
        }

    def documents(self) -> dict[str, Any]:
        from variator.actors.recovery import shrinkage_table

        return {
            "actors.json": {
                "schema": SCHEMA,
                "as_of": self.as_of.isoformat(),
                "company": self.h.company,
                "actors": self.actors,
            },
            "habits.json": self.habits_doc(),
            "cohorts.json": {
                "schema": SCHEMA,
                "as_of": self.as_of.isoformat(),
                "cohorts": self.cohorts,
                "pooling": shrinkage_table(self),
            },
            "risk_register.json": {"schema": SCHEMA, "as_of": self.as_of.isoformat(), "risks": self.risks},
            **{f"facts/{aid}.json": doc for aid, doc in sorted(self.facts.items())},
        }


def learn(history_dir: str | Path, as_of: date | str | None = None) -> Learned:
    h = load_history(history_dir, as_of)
    ctx = build_context(h)
    people = learn_people(ctx)
    clients = learn_clients(ctx)
    company = learn_company(ctx)
    _merge_nps(ctx, clients, company)
    out = Learned(h, ctx, people, clients, company)
    out.actors = _registry(out)
    out.cohorts = _cohorts(out)
    from variator.actors.facts import build_facts
    from variator.actors.risks import build_register

    out.risks = build_register(out)
    out.facts = build_facts(out)
    return out


def _merge_nps(ctx: Context, clients: ClientsResult, company: CompanyResult) -> None:
    """NPS habits (v2 datasets) into the client and company results."""
    from variator.actors.nps import learn_nps

    n = learn_nps(ctx)
    if not n.responses:
        return
    for cid, p in n.clients.items():
        clients.params.setdefault(cid, {})["nps_level"] = p
        clients.evidence.setdefault(cid, {})["nps"] = n.evidence.get(cid, [])
    company.params.update(n.company)
    for coh, d in n.cohorts.items():
        clients.cohorts.setdefault(coh, {}).update(d)
    company.info["nps"] = n.info
    company.info["nps_answers"] = [[r["client"], str(r["survey"])] for r in n.responses]
    has_sim_ids = "nps_response" in ctx.h.records
    last = [r for r in n.responses if str(r["survey"]) == max(str(x["survey"]) for x in n.responses)]
    company.evidence["nps_now"] = [
        ctx.h.kb("sim", r["id"]) if has_sim_ids else f"dataset:{r['id']}" for r in last[-5:]
    ]
    det = [r for r in n.responses if float(r["score"]) <= 6]
    has_sim = "nps_response" in ctx.h.records
    company.events["nps_detractor"] = [
        {**r, "kb": ctx.h.kb("sim", r["id"]) if has_sim else r["id"]} for r in det
    ]


# ---------------------------------------------------------------------------------------------- registry
def _months_between(a: date, b: date) -> float:
    return round((b - a).days / 30.44, 1)


def _registry(L: Learned) -> list[dict[str, Any]]:
    h = L.h
    out: list[dict[str, Any]] = []
    for p in sorted(h.people.values(), key=lambda p: p.id):
        status = "active"
        if p.last_day is not None:
            status = "left" if p.last_day < h.as_of else "notice"
        cohorts = ["staff", f"role:{p.role}"] + (["engineers"] if p.role in ENGINEER_ROLES else [])
        out.append(
            {
                "id": p.id,
                "kind": "person",
                "name": p.name,
                "title": p.title,
                "role": p.role,
                "department": p.department,
                "manager": p.manager,
                "seat": p.seat,
                "executive": p.executive,
                "start": p.start.isoformat(),
                "tenure_months_at_t": _months_between(p.start, min(h.as_of, p.last_day or h.as_of)),
                "status_at_t": status,
                "last_day": p.last_day.isoformat() if p.last_day else None,
                "salary_at_t": round(p.salary, -2) if p.salary else None,
                "hire": p.hire_slot is not None,
                "cohorts": cohorts,
                "kb_ids": [f"person.{p.id.lower()}"],
                "habits": f"habits.json#/people/{p.id}",
                "facts": f"facts/{p.id}.json",
            }
        )
    for c in sorted(h.clients.values(), key=lambda c: c.id):
        out.append(
            {
                "id": c.id,
                "kind": "client",
                "name": c.name,
                "industry": c.industry,
                "key": c.key,
                "new_in_history": c.new,
                "billing": c.billing,
                "status_at_t": c.status,
                "arr_at_t": c.acv,
                "start": c.start.isoformat() if c.start else None,
                "end": c.end.isoformat() if c.end else None,
                "notice_at": c.notice_at.isoformat() if c.notice_at else None,
                "account_manager": c.account_manager,
                "cohorts": ["clients", f"clients:{cohort_of(c)}"],
                "kb_ids": [h.kb("sim", c.contract)] if c.contract else [],
                "habits": f"habits.json#/clients/{c.id}",
                "facts": f"facts/{c.id}.json",
            }
        )
    for vid, info in sorted(L.company.vendor_info.items()):
        org = h.orgs.get(vid, {})
        out.append(
            {
                "id": vid,
                "kind": "vendor",
                "name": info.get("name") or org.get("name"),
                "vendor_kind": info.get("kind") or org.get("kind"),
                "contract": info.get("contract"),
                "payment_terms_days": info.get("payment_terms_days"),
                "kb_ids": [info["contract"]] if info.get("contract") else [],
                "habits": f"habits.json#/vendors/{vid}",
                "facts": f"facts/{vid}.json",
            }
        )
    ta = L.company.tax_info.get("authority")
    if ta:
        out.append(
            {
                "id": ta,
                "kind": "regulator",
                "name": L.company.tax_info.get("authority_name"),
                "schedule": {
                    k: {kk: vv for kk, vv in v.items() if kk != "evidence"}
                    for k, v in L.company.tax_info.get("schedule", {}).items()
                },
                "habits": f"habits.json#/regulators/{ta}",
                "facts": f"facts/{ta}.json",
            }
        )
    return out


# ---------------------------------------------------------------------------------------------- cohorts
def _members(L: Learned, pred) -> list[str]:
    return [a["id"] for a in L.actors if pred(a)]


def _group_values(
    params: dict[str, dict[str, Param]], ids: list[str], name: str, source: str
) -> Param | None:
    """The cohort value of a per-actor parameter: the geometric mean of the members (range: p1..p99 span)."""
    vals = [params[i][name] for i in ids if name in params.get(i, {})]
    if not vals:
        return None
    pts = np.array([max(v.point, 1e-12) for v in vals])
    g = float(np.exp(np.mean(np.log(pts))))
    return Param(
        g,
        min(v.low for v in vals),
        g,
        max(v.high for v in vals),
        len(vals),
        source,
        vals[0].unit,
        "geometric mean of the members; low/high: the widest member interval",
    )


def _cohorts(L: Learned) -> list[dict[str, Any]]:
    h = L.h
    P = L.people
    out: list[dict[str, Any]] = []
    staff_params = {k: v.to_dict() for k, v in sorted(P.company.items())}
    out.append(
        {
            "id": "staff",
            "kind": "people",
            "title": "All staff",
            "members": _members(L, lambda a: a["kind"] == "person"),
            "params": staff_params,
        }
    )
    roles = sorted({p.role for p in h.people.values()})
    for role in roles:
        ids = [p.id for p in h.people.values() if p.role == role]
        params: dict[str, Any] = {}
        for name in (
            "throughput_points_per_sprint",
            "own_tasks_per_day",
            "first_response_hours",
            "cycle_time_days",
            "sick_spells_per_year",
            "resignation_hazard",
        ):
            g = _group_values(P.params, ids, name, L.ctx.group(f"role:{role}"))
            if g is not None:
                params[name] = g.to_dict()
        out.append(
            {
                "id": f"role:{role}",
                "kind": "role",
                "title": f"Role {role}",
                "members": sorted(ids),
                "params": params,
            }
        )
    out.append(
        {
            "id": "engineers",
            "kind": "people",
            "title": "Engineers (backend, frontend, devops, qa)",
            "members": sorted(p.id for p in h.people.values() if p.role in ENGINEER_ROLES),
            "params": {
                k: P.company[k].to_dict()
                for k in ("throughput_engineers", "throughput_spread", "blocked_time_share")
                if k in P.company
            },
        }
    )
    C = L.clients
    for coh in ("key", "standard"):
        ids = sorted(c.id for c in h.clients.values() if cohort_of(c) == coh)
        params = {k: v.to_dict() for k, v in sorted(C.cohorts.get(coh, {}).items())}
        g = _group_values(C.params, ids, "pay_delay_days", L.ctx.group(f"clients:{coh}"))
        if g is not None:
            params["pay_delay_days"] = g.to_dict()
        arr = sum(h.clients[i].acv for i in ids)
        out.append(
            {
                "id": f"clients:{coh}",
                "kind": "clients",
                "title": "Key clients" if coh == "key" else "Standard clients (the client cohort)",
                "members": ids,
                "arr_at_t": arr,
                "active_at_t": sum(1 for i in ids if h.clients[i].status == "active"),
                "params": params,
            }
        )
    allp = {k: v.to_dict() for k, v in sorted(C.cohorts.get("all", {}).items())}
    allp.update({k: v.to_dict() for k, v in sorted(C.company.items())})
    out.append(
        {
            "id": "clients",
            "kind": "clients",
            "title": "All clients",
            "members": sorted(h.clients),
            "params": allp,
        }
    )
    # prospects: new-business deals
    co = L.company.params
    out.append(
        {
            "id": "prospects",
            "kind": "deals",
            "title": "New-business deals (future clients)",
            "members": [],
            "pipeline_at_t": L.company.info.get("open_pipeline"),
            "params": {
                k: co[k].to_dict()
                for k in (
                    "deals_per_month",
                    "win_rate",
                    "deal_value_median",
                    "deal_value_log_sd",
                    "sales_cycle_days",
                )
                if k in co
            },
            "new_client_habits": (
                "clients:standard (a new client takes the cohort habits until its own history)"
            ),
        }
    )
    out += _future_cohorts(L, roles)
    return out


def _future_cohorts(L: Learned, roles: list[str]) -> list[dict[str, Any]]:
    h = L.h
    P = L.people
    hires = [x for x in P.hires if x.get("filled")]
    by_role: dict[str, list[dict]] = {}
    for x in hires:
        by_role.setdefault(x.get("role_key") or "", []).append(x)
    lead = L.company.params.get("hiring_lead_months")
    ratios = [x["salary_ratio"] for x in hires if x.get("salary_ratio")]
    ramps = [
        P.info[x["employee"]].get("ramp_share_of_steady_pace")
        for x in hires
        if x.get("employee") in P.info and P.info[x["employee"]].get("ramp_share_of_steady_pace")
    ]
    out = []
    tag_hires = L.ctx.tag("sim_requisition")
    for role in roles:
        own = by_role.get(role, [])
        params: dict[str, Any] = {}
        pending: list[dict[str, str]] = []
        if lead is not None:
            d = lead.to_dict()
            d["source"] = tag_hires if own else "group:hires"
            params["hiring_lead_months"] = d
        if ratios:
            r = np.array(ratios)
            params["salary_vs_predecessor"] = stats.Param(
                float(np.mean(r)),
                float(r.min()),
                float(np.median(r)),
                float(r.max()),
                len(r),
                tag_hires if own else "group:hires",
                "ratio",
                "hires of the history (low/high: min/max)",
            ).to_dict()
        if role in ENGINEER_ROLES and ramps:
            f20 = [x["first_20_bdays"] for x in ramps if "first_20_bdays" in x]
            n20 = [x["next_20_bdays"] for x in ramps if "next_20_bdays" in x]
            for name, xs in (("ramp_share_first_20_bdays", f20), ("ramp_share_next_20_bdays", n20)):
                if xs:
                    params[name] = stats.Param(
                        float(np.mean(xs)),
                        float(min(xs)),
                        float(np.mean(xs)),
                        float(max(xs)),
                        len(xs),
                        "group:hires",
                        "share of the steady pace",
                        "engineering hires of the history",
                    ).to_dict()
        for name in ("throughput_points_per_sprint", "own_tasks_per_day", "first_response_hours"):
            ids = [p.id for p in h.people.values() if p.role == role]
            g = _group_values(P.params, ids, name, L.ctx.group(f"role:{role}"))
            if g is not None:
                params[f"pace_{name}"] = g.to_dict()
        for name in ("sick_spells_per_year", "resignation_hazard", "growth_rate", "vacation_month_share_08"):
            if name in P.company:
                params[name] = P.company[name].to_dict()
        if len(own) < 2:
            for k, why in LLM_PENDING.items():
                if k == "ramp_up_months" and role in ENGINEER_ROLES and ramps and own:
                    continue
                pending.append(
                    {
                        "param": k,
                        "source": "llm:pending",
                        "why": why,
                        "reason": f"{len(own)} hire(s) of the role in the history",
                    }
                )
        out.append(
            {
                "id": f"future:{role}",
                "kind": "future_employee",
                "title": f"Future {role} hire",
                "basis": {
                    "hires_of_role": [x["requisition"] for x in own],
                    "hires_all": [x["requisition"] for x in hires],
                    "evidence": [x["kb"] for x in own] or [x["kb"] for x in hires],
                },
                "params": params,
                "llm_pending": pending,
                "draw": "at a hire the month model draws an individual actor from these parameters",
            }
        )
    return out


def source_rank(tag: str) -> int:
    for i, p in enumerate(SOURCE_ORDER):
        if tag.startswith(p):
            return i
    return len(SOURCE_ORDER)


def finite(x: float) -> bool:
    return x is not None and math.isfinite(x)
