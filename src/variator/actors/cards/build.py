"""Card skeletons from the WP10 artifact set: who, habit rows, facts for the writer, citations.

``build_plan(actors_dir, history_dir, cases_dir)`` returns the plan of every card: identity,
the habit rows (``fmt.row``), sparkline series, life events, the case items that change the
actor, the risks it took part in, and the facts the writer may use (each with its evidence
ids). Nothing here calls an LLM; ``writer`` adds the profile text.

Rules:
- people are the staff at T (no leavers), each with work habits only: no salary, no salary
  reviews and no personal resignation chance (no score of people; the staff rate is on the
  cohort cards);
- a fact that names a date on or after T is left out (as-of: nothing after T);
- every number of a fact text is a habit parameter or a record value (WP10 ``facts``).
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from variator.actors.cards import SCHEMA, fmt
from variator.actors.cards.changes import changes_by_card, load_cases, missing_items, shocks_by_risk

ENGINEER_ROLES = ("backend", "frontend", "devops", "qa")
DEPARTMENT_ORDER = [
    "Management",
    "Finance and HR",
    "Delivery",
    "Engineering",
    "Support",
    "Sales",
    "Marketing",
]
FUTURE_ROLES = {  # role -> a future hire of the role (single executive seats have no cohort card)
    "backend": "backend engineer",
    "frontend": "frontend engineer",
    "devops": "DevOps engineer",
    "qa": "QA engineer",
    "support": "support engineer",
    "sales": "sales and account manager",
    "marketing": "marketing manager (social media)",
    "pm": "project manager",
    "analyst_designer": "business analyst and designer",
}
SIZE_LABELS = {
    "affected_clients": "Clients affected",
    "downtime_hours_per_client": "Downtime per client",
    "restore_hours": "Time to restore",
    "incidents_caused": "Incidents caused",
    "severity_mix": "Severity mix",
    "arr_lost_total_usd": "ARR lost, all events",
    "arr_lost_usd": "ARR lost per event",
    "share_of_acv": "Size, share of the contract value",
    "extra_days": "Extra days to pay",
    "spell_days": "Length of a spell",
    "support_multiplier": "Support load after a won deal",
    "win_share": "Won",
    "reasons": "Reasons",
    "win_rate": "Win rate",
    "backfill_lead_months": "Time to backfill",
    "vacancy_months": "Months without the person",
    "load": "Sprint load",
    "overload_sprint_share": "Overloaded sprints",
    "raise_ask_share": "Raise asked",
    "days_per_spell": "Days per spell",
    "nps_history": "NPS, all surveys",
    "nps_now": "NPS, latest survey",
    "availability": "Availability",
    "breach_day_share": "Days below the SLA",
    "cloud_growth_per_year": "Cloud cost growth per year",
}
SIZE_DICT_ROWS = {"reasons", "severity_mix"}
PRIMARY = {
    "client": ["arr", "pay_delay_days", "tickets_per_month", "nps_level", "churn_hazard", "upsell_rate"],
    "person": [
        "throughput_points_per_sprint",
        "cycle_time_days",
        "own_tasks_per_day",
        "first_response_hours",
        "work_start_minute",
        "work_end_minute",
        "vacation_days_per_year",
        "sick_spells_per_year",
    ],
    "vendor": ["monthly_fee", "monthly_fee_now"],
    "regulator": ["payroll_tax_rate", "employee_deduction_rate"],
    "company": ["arr_at_t", "cash_at_t", "incidents_per_month", "mttr_hours", "win_rate", "nps_now"],
}
EXTERNAL_LENSES = {
    "company": {
        "business model", "customers", "competitors", "regulation", "operations", "labour", "risks",
        "benchmarks", "market", "technology", "customers pain", "economics",
    },
    "person": {"labour", "operations", "risks"},
    "client": {"customers", "competitors", "market", "customers pain", "economics"},
    "vendor": {"operations", "benchmarks", "economics", "technology", "risks"},
    "regulator": {"regulation", "risks"},
    "cohort": {"customers", "labour", "operations", "risks", "benchmarks"},
}
MORE = {
    "client": ["late_payment_rate", "downgrade_rate", "pay_delay_sd_days", "notice_days"],
    "person": ["sick_days_per_year", "growth_rate"],
    "company": [
        "deploys_per_week",
        "team_load",
        "latency_p95_ms",
        "availability",
        "cloud_cost_per_year",
        "support_tickets_per_month",
        "dso_days_at_t",
        "sales_cycle_days",
        "deals_per_month",
        "hiring_lead_months",
    ],
}
MAX_PRIMARY = 6
# never on a person card (09 §2: no ranking or score of people)
PERSON_BLOCKED_PARAMS = {"resignation_hazard"}
PERSON_BLOCKED_TOPICS = {"pay", "risk"}
ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DEGENERATE = re.compile(r"(\d[\d,.]*)-\1 \(median \1\)")
CONTAINERS = {
    "person": "people",
    "client": "sales",
    "cohort-clients": "sales",
    "vendor": "finance",
    "regulator": "finance",
    "company": "finance",
    "cohort-future": "company",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_research(path: str | Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    raw = _load(Path(path))
    cited = raw.get("cited_facts") or []
    if cited:
        out = []
        for i, item in enumerate(cited, 1):
            fact = item.get("fact") or {}
            out.append(
                {
                    "id": f"web-{i}",
                    "statement": fact.get("statement", ""),
                    "source": fact.get("source", ""),
                    "evidence": item.get("evidence", ""),
                    "title": item.get("source_title", ""),
                    "purpose": item.get("query", "") or "market context",
                    "confidence": fact.get("confidence", 0.0),
                    "period": fact.get("period"),
                }
            )
        return [f for f in out if f["statement"] and f["source"]]
    return [
        {
            "id": f"web-{i}",
            "statement": item.get("statement", ""),
            "source": item.get("source", ""),
            "evidence": item.get("evidence", ""),
            "title": item.get("source_title", ""),
            "purpose": item.get("purpose", "market context"),
            "confidence": item.get("confidence", 0.0),
            "period": item.get("period"),
        }
        for i, item in enumerate(raw.get("facts") or [], 1)
        if item.get("statement") and item.get("source")
    ]


def _external_for(kind: str, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lenses = EXTERNAL_LENSES.get(kind, EXTERNAL_LENSES["company"])
    selected = [
        f
        for f in facts
        if any(lens.casefold() in str(f.get("purpose", "")).casefold() for lens in lenses)
    ]
    return selected[:8] if selected else facts[:8]


def card_external_id(card_id: str, kind: str) -> str:
    """The sim external id of a card's KB page: CARD-CL-001, CARD-COHORT-clients-standard, CARD-COMPANY."""
    safe = re.sub(r"[^0-9A-Za-z_.@-]+", "-", card_id).strip("-")
    if kind == "company":
        return "CARD-COMPANY"
    return f"CARD-COHORT-{safe}" if kind == "cohort" else f"CARD-{safe}"


def _after_t(text: str, values: dict[str, Any] | None, t: date) -> bool:
    dates = [m.group(0) for m in ISO.finditer(text)]
    for v in (values or {}).values():
        if isinstance(v, str) and ISO.fullmatch(v):
            dates.append(v)
    out = False
    for d in dates:
        try:
            out = out or date.fromisoformat(d) >= t
        except ValueError:
            continue
    return out


def _clean(text: str, name: str) -> str:
    """Fact texts for people: no doubled names, and 'now' for the as-of date T."""
    text = text.replace(f"{name}: {name}:", f"{name}:")
    text = DEGENERATE.sub(r"\1", text)  # "15,000-15,000 (median 15,000)" -> "15,000"
    text = re.sub(r" at T\)", " so far)", text)
    text = re.sub(r"\bat T\b", "now", text)
    return re.sub(r"\bbefore T\b", "so far", text)


def _facts(raw: list[dict[str, Any]], kind: str, name: str, t: date) -> tuple[list[dict], int]:
    """Writer facts of an actor: filtered (people rules, as-of) and cleaned. Returns (facts, dropped)."""
    out, dropped = [], 0
    for f in raw:
        text = _clean(str(f["text"]), name)
        if kind == "person" and (f.get("topic") in PERSON_BLOCKED_TOPICS or "salary" in text.lower()):
            dropped += 1
            continue
        if _after_t(text, f.get("values"), t):
            dropped += 1
            continue
        out.append(
            {
                "key": f["id"],
                "topic": f.get("topic") or "",
                "text": text,
                "evidence": list(f.get("evidence") or []),
                "param": f.get("param"),
                "date": next(
                    (v for v in (f.get("values") or {}).values() if isinstance(v, str) and ISO.fullmatch(v)),
                    None,
                ),
            }
        )
    return out, dropped


def _rows(
    params: dict[str, dict[str, Any]], primary: list[str], more: list[str], blocked: set[str] = frozenset()
) -> list[dict]:
    keys = [k for k in primary if k in params and k not in blocked][:MAX_PRIMARY]
    rest = [k for k in primary if k in params and k not in keys and k not in blocked]
    rest += [k for k in more if k in params and k not in keys and k not in rest and k not in blocked]
    return [fmt.row(k, params[k], primary=True) for k in keys] + [
        fmt.row(k, params[k], primary=False) for k in rest
    ]


def _fixed(
    key: str, label: str, value: float, unit: str, source: str, help_: str = "", primary: bool = True
) -> dict:
    r = fmt.row(
        key,
        {"point": value, "low": value, "high": value, "mode": value, "n": 1, "source": source, "unit": unit},
        primary=primary,
        label=label,
    )
    if help_:
        r["help"] = help_
    return r


def _events(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ev = [f for f in facts if f["topic"] == "life_event" or (f["topic"] == "contract" and f.get("date"))]
    return [
        {"key": f["key"], "text": f["text"], "date": f.get("date"), "evidence": f["evidence"][:3]} for f in ev
    ]


def _names_in(texts: list[str], directory: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """People and organisations of the directory named in the texts."""
    blob = " ".join(texts)
    people = [{"id": p["id"], "name": p["name"]} for p in directory.get("people") or [] if p["name"] in blob]
    orgs = [
        {"id": o["id"], "name": o["name"]}
        for o in directory.get("organizations") or []
        if o.get("name") and o["name"] in blob
    ]
    return people, orgs


def build_plan(
    actors_dir: str | Path,
    history_dir: str | Path | None = None,
    cases_dir: str | Path | None = None,
    research_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(actors_dir)
    manifest = _load(root / "manifest.json")
    actors_doc = _load(root / "actors.json")
    habits = _load(root / "habits.json")
    cohorts = {c["id"]: c for c in _load(root / "cohorts.json")["cohorts"]}
    register = _load(root / "risk_register.json")["risks"]
    external = _load_research(research_path)
    t = date.fromisoformat(manifest["as_of"])
    actors = {a["id"]: a for a in actors_doc["actors"]}
    company = actors_doc.get("company") or {}
    directory: dict[str, Any] = {}
    series: dict[str, dict[str, Any]] = {}
    hist_root = Path(history_dir) if history_dir else Path(manifest.get("history", {}).get("root", ""))
    if hist_root and (hist_root / "delivery" / "config" / "people.yaml").is_file():
        import yaml

        directory = yaml.safe_load(
            (hist_root / "delivery" / "config" / "people.yaml").read_text(encoding="utf-8")
        )
    if hist_root and (hist_root / "datasets").is_dir():
        from variator.actors.cards.series import build_series
        from variator.actors.history import load_history

        series = build_series(load_history(hist_root, t, timeline=False))

    def facts_of(aid: str) -> list[dict[str, Any]]:
        p = root / "facts" / f"{aid}.json"
        return _load(p)["facts"] if p.is_file() else []

    cards: list[dict[str, Any]] = []
    dropped_facts = 0
    staff = [a for a in actors.values() if a["kind"] == "person" and a.get("status_at_t") == "active"]
    key_clients = [a for a in actors.values() if a["kind"] == "client" and a.get("key")]
    vendors = [a for a in actors.values() if a["kind"] == "vendor"]
    regulators = [a for a in actors.values() if a["kind"] == "regulator"]

    # ------------------------------------------------------------------ people
    for a in sorted(
        staff,
        key=lambda a: (
            DEPARTMENT_ORDER.index(a["department"]) if a["department"] in DEPARTMENT_ORDER else 99,
            a["id"],
        ),
    ):
        facts, dr = _facts(facts_of(a["id"]), "person", a["name"], t)
        dropped_facts += dr
        params = habits["people"].get(a["id"], {})
        cards.append(
            {
                "id": a["id"],
                "kind": "person",
                "group": "people",
                "section": a["department"],
                "name": a["name"],
                "title": a["title"],
                "role": a.get("role"),
                "subtitle": a["department"],
                "dossier": (a.get("kb_ids") or [None])[0],
                "link": {"field": "employee", "id": a["id"], "name": a["name"]},
                "container": CONTAINERS["person"],
                "facts": facts,
                "habits": _rows(params, PRIMARY["person"], MORE["person"], PERSON_BLOCKED_PARAMS),
                "events": _events(facts),
            }
        )
    # ------------------------------------------------------------------ key clients
    for a in sorted(key_clients, key=lambda a: a["id"]):
        facts, dr = _facts(facts_of(a["id"]), "client", a["name"], t)
        dropped_facts += dr
        cards.append(
            {
                "id": a["id"],
                "kind": "client",
                "group": "clients",
                "section": "Key clients",
                "name": a["name"],
                "title": f"Key client · {a.get('industry', '')}".rstrip(" ·"),
                "subtitle": f"{fmt.money(a.get('arr_at_t') or 0)} ARR",
                "dossier": f"org.{a['id']}",
                "contract": (a.get("kb_ids") or [None])[0],
                "link": {"field": "client", "id": a["id"], "name": a["name"]},
                "container": CONTAINERS["client"],
                "facts": facts,
                "habits": _rows(habits["clients"].get(a["id"], {}), PRIMARY["client"], MORE["client"]),
                "events": _events(facts),
            }
        )
    # ------------------------------------------------------------------ vendors
    for a in sorted(vendors, key=lambda a: a["id"]):
        facts, dr = _facts(facts_of(a["id"]), "vendor", a["name"], t)
        dropped_facts += dr
        now = habits["vendors"].get(a["id"], {}).get("monthly_fee_now")
        fee = next((f for f in facts if f.get("param") == "monthly_fee"), None)
        if now and fee:
            facts.append(
                {
                    "key": f"f{len(facts) + 1}",
                    "topic": "cost",
                    "param": "monthly_fee_now",
                    "date": None,
                    "text": f"The latest bill was {fmt.num(now['point'])} USD.",
                    "evidence": fee["evidence"][-1:],
                }
            )
        kind_title = {"cloud": "Cloud provider"}.get(a.get("vendor_kind"), "Vendor")
        cards.append(
            {
                "id": a["id"],
                "kind": "vendor",
                "group": "vendors",
                "section": "Vendors",
                "name": a["name"],
                "title": f"{kind_title} · terms {a.get('payment_terms_days')} days",
                "subtitle": kind_title,
                "dossier": f"org.{a['id']}",
                "contract": (a.get("kb_ids") or [None])[0],
                "link": {"field": "vendor", "id": a["id"], "name": a["name"]},
                "container": CONTAINERS["vendor"],
                "facts": facts,
                "habits": _rows(habits["vendors"].get(a["id"], {}), PRIMARY["vendor"], []),
                "events": _events(facts),
            }
        )
    # ------------------------------------------------------------------ cohorts
    cards += _cohort_cards(actors, habits, cohorts, register, regulators, root, t)
    # ------------------------------------------------------------------ company
    facts, dr = _facts(facts_of("company"), "company", company.get("name", "the company"), t)
    dropped_facts += dr
    active_clients = [
        a for a in actors.values() if a["kind"] == "client" and a.get("status_at_t") in ("active", "notice")
    ]
    ident = (
        f"{company.get('name')} is an IT services company that builds and supports platforms "
        f"for its clients; it now has {len(staff)} employees, {len(active_clients)} clients under contract "
        f"({len(key_clients)} key clients) and {len(vendors)} vendors."
    )
    facts.insert(
        0,
        {
            "key": "f0",
            "topic": "identity",
            "text": ident,
            "evidence": ["org.LW"] + [(a.get("kb_ids") or [""])[0] for a in key_clients[:2]],
            "param": None,
            "date": None,
        },
    )
    cards.insert(
        0,
        {
            "id": "company",
            "kind": "company",
            "group": "company",
            "section": "Company",
            "name": company.get("name", "Company"),
            "title": "Company profile",
            "subtitle": f"{len(staff)} people · {len(active_clients)} clients",
            "dossier": "org.LW",
            "link": {"field": "counterparty", "id": "LW", "name": company.get("name")},
            "container": CONTAINERS["company"],
            "facts": facts,
            "habits": _rows(habits["company"], PRIMARY["company"], MORE["company"]),
            "events": [],
        },
    )

    # ------------------------------------------------------------------ series, names, changes, risks
    groups = {
        "@engineers": [a["id"] for a in staff if a.get("role") in ENGINEER_ROLES],
        "@staff": [a["id"] for a in staff],
        "@key_clients": [a["id"] for a in key_clients],
        "@future_engineers": [f"future:{r}" for r in ENGINEER_ROLES],
        "@future": [f"future:{r}" for r in FUTURE_ROLES],
        "@company": ["company"],
    }
    cases = load_cases(cases_dir) if cases_dir else {}
    changes = changes_by_card(cases, groups)
    shocks = shocks_by_risk(cases)
    risks_of: dict[str, list[str]] = {}
    for r in register:
        for aid in r.get("actors") or []:
            risks_of.setdefault(aid, []).append(r["id"])
    for c in cards:
        for row in c["habits"]:
            s = series.get(c["id"], {}).get(row["key"])
            if s and len(s["points"]) >= 2:
                row["series"] = s
        c["people"], c["orgs"] = _names_in([c["name"]] + [f["text"] for f in c["facts"]], directory)
        c["changes"] = changes.get(c["id"], [])
        c["risks"] = [] if c["kind"] == "person" else risks_of.get(c["id"], [])
        if c["id"] == "company":
            c["risks"] = [r["id"] for r in register]
        c["externalId"] = card_external_id(c["id"], c["kind"])
        c["kbId"] = f"sim.{c['externalId']}"
        c["external"] = _external_for(c["kind"], external)
    return {
        "schema": SCHEMA,
        "asOf": (date.fromordinal(t.toordinal() - 1)).isoformat(),
        "t": t.isoformat(),
        "company": {
            "name": company.get("name"),
            "tenant": company.get("tenant"),
            "timezone": company.get("timezone"),
        },
        "source": {
            "actors": str(root),
            "history": str(hist_root) if hist_root else None,
            "cases": str(cases_dir) if cases_dir else None,
            "actorsManifestAsOf": manifest["as_of"],
            "research": {"path": str(research_path), "facts": len(external)} if research_path else None,
        },
        "cards": cards,
        "risks": _risks(register, shocks, {c["id"] for c in cards}, external),
        "stats": {"facts_dropped": dropped_facts, "stale_case_items": missing_items(cases) if cases else []},
    }


# ---------------------------------------------------------------------------------------------- cohorts
def _range(p: dict[str, Any], kind: str = "num") -> str:
    if kind == "pct":
        return f"{fmt.pct(p['low'])}-{fmt.pct(p['high'])} (median {fmt.pct(p['point'])})"
    if kind == "money":
        return f"{fmt.num(p['low'])}-{fmt.num(p['high'])} (median {fmt.num(p['point'])}) USD"
    return f"{fmt.num(p['low'])}-{fmt.num(p['high'])} (median {fmt.num(p['point'])})"


def _cohort_cards(actors, habits, cohorts, register, regulators, root: Path, t: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_code = {r["code"]: r for r in register}
    # mass clients
    std = cohorts.get("clients:standard")
    if std:
        P = dict(std["params"])
        base = habits.get("client_base", {})
        if "churn_hazard_base" in base:
            P["churn_hazard_base"] = base["churn_hazard_base"]
        members = [m for m in std.get("members") or [] if m in actors]
        ev = [(actors[m].get("kb_ids") or [""])[0] for m in members[:3]]
        facts = [
            {
                "key": "f1",
                "topic": "identity",
                "text": (
                    f"The standard clients are one cohort of {len(members)} clients, "
                    f"{std.get('active_at_t')} of them active now, with an ARR of "
                    f"{fmt.num(std.get('arr_at_t'))} USD; a new client takes the cohort habits until "
                    f"it has its own history."
                ),
                "evidence": ev,
                "param": None,
                "date": None,
            }
        ]
        texts = [
            (
                "pay_delay_days",
                f"A standard client pays an invoice {_range(P['pay_delay_days'])} days after issue.",
                by_code.get("late_payment_spell"),
            ),
            (
                "tickets_per_month",
                f"A standard client opens {_range(P['tickets_per_month'])} support tickets per month.",
                None,
            ),
            (
                "nps",
                f"The NPS of the cohort is {_range(P['nps'])} points ({P['nps']['n']} survey answers).",
                by_code.get("nps_detractor"),
            ),
            (
                "upsell_rate",
                f"A standard client asks for more {_range(P['upsell_rate'])} times per year.",
                by_code.get("client_upsell"),
            ),
            (
                "churn_hazard_base",
                "The chance that a client gives notice within a year is "
                f"{_range(P['churn_hazard_base'], 'pct')}.",
                by_code.get("client_churn"),
            ),
        ]
        for i, (k, text, risk) in enumerate([x for x in texts if x[0] in P], start=2):
            facts.append(
                {
                    "key": f"f{i}",
                    "topic": "habit",
                    "text": text,
                    "param": k,
                    "date": None,
                    "evidence": (risk or {}).get("evidence", ev)[:3],
                }
            )
        rows = [
            _fixed(
                "arr_at_t",
                "ARR of the cohort",
                float(std.get("arr_at_t") or 0),
                "USD per year",
                "data:sim_contract",
                "ARR of the standard clients at T.",
            )
        ]
        rows += _rows(
            P, ["pay_delay_days", "tickets_per_month", "nps", "churn_hazard_base", "upsell_rate"], []
        )
        out.append(
            _cohort(
                "clients:standard",
                "Standard clients",
                "Mass clients · one cohort",
                f"{std.get('active_at_t')} active clients",
                "cohort-clients",
                facts,
                rows,
                {"count": len(members), "kind": "clients"},
                ["standard client"],
            )
        )
    # leads
    pro = cohorts.get("prospects")
    if pro:
        P = pro["params"]
        pipe = pro.get("pipeline_at_t") or {}
        lost = by_code.get("deal_lost") or {}
        facts = [
            {
                "key": "f1",
                "topic": "identity",
                "text": (
                    f"Leads are new-business deals; the pipeline now holds {pipe.get('deals')} open deals "
                    f"worth {fmt.num(pipe.get('amount'))} USD, and a won deal becomes a standard client."
                ),
                "evidence": list(pipe.get("ids") or [])[:3],
                "param": None,
                "date": None,
            }
        ]
        texts = [
            ("deals_per_month", f"The company opens {_range(P['deals_per_month'])} new deals per month."),
            ("win_rate", f"It wins {_range(P['win_rate'], 'pct')} of its new deals."),
            ("deal_value_median", f"A new deal is worth {_range(P['deal_value_median'], 'money')}."),
            (
                "sales_cycle_days",
                f"A deal takes {_range(P['sales_cycle_days'])} days from the start to the close.",
            ),
        ]
        for i, (k, text) in enumerate([x for x in texts if x[0] in P], start=2):
            facts.append(
                {
                    "key": f"f{i}",
                    "topic": "habit",
                    "text": text,
                    "param": k,
                    "date": None,
                    "evidence": list(lost.get("evidence") or [])[:3],
                }
            )
        reasons = (lost.get("size") or {}).get("reasons") or {}
        if reasons:
            parts = ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
            facts.append(
                {
                    "key": f"f{len(facts) + 1}",
                    "topic": "risk",
                    "text": f"Lost deals by reason: {parts}.",
                    "evidence": list(lost.get("evidence") or [])[:3],
                    "param": None,
                    "date": None,
                }
            )
        rows = _rows(
            P, ["deals_per_month", "win_rate", "deal_value_median", "sales_cycle_days"], ["deal_value_log_sd"]
        )
        out.append(
            _cohort(
                "prospects",
                "Leads",
                "New-business deals · cohort",
                f"{pipe.get('deals')} open deals",
                "cohort-clients",
                facts,
                rows,
                {"count": pipe.get("deals"), "kind": "deals"},
                ["lead", "new-business deal", "pipeline"],
            )
        )
    # the tax authority (one actor, modelled as a rule set)
    for a in regulators:
        facts, _ = (
            _facts(_load(root / "facts" / f"{a['id']}.json")["facts"], "regulator", a["name"], t)
            if (root / "facts" / f"{a['id']}.json").is_file()
            else ([], 0)
        )
        out.append(
            {
                "id": a["id"],
                "kind": "regulator",
                "group": "cohorts",
                "section": "Authorities",
                "name": a["name"],
                "title": "Tax authority · rules and a payment calendar",
                "subtitle": "Tax authority",
                "dossier": f"org.{a['id']}",
                "link": {"field": "authority", "id": a["id"], "name": a["name"]},
                "container": CONTAINERS["regulator"],
                "facts": facts,
                "aliases": ["HM Revenue", "tax authority"],
                "habits": _rows(habits["regulators"].get(a["id"], {}), PRIMARY["regulator"], []),
                "events": [],
            }
        )
    # future hires
    for role, title in FUTURE_ROLES.items():
        co = cohorts.get(f"future:{role}")
        if not co:
            continue
        P = co["params"]
        basis = co.get("basis") or {}
        k = len(basis.get("hires_of_role") or [])
        ev = list(basis.get("evidence") or [])[:3]
        facts = [
            {
                "key": "f1",
                "topic": "identity",
                "text": (
                    f"A future {title} is a cohort, not a person: at a hire the model draws one new "
                    f"colleague from these habits; the history has {k} hires of this role and "
                    f"{len(basis.get('hires_all') or [])} hires in total."
                ),
                "evidence": ev,
                "param": None,
                "date": None,
            }
        ]
        pace_key = next(
            (
                x
                for x in (
                    "pace_throughput_points_per_sprint",
                    "pace_first_response_hours",
                    "pace_own_tasks_per_day",
                )
                if x in P
            ),
            None,
        )
        texts = (
            [
                (
                    "hiring_lead_months",
                    f"A hire takes {_range(P['hiring_lead_months'])} months from the requisition "
                    "to the start.",
                )
            ]
            if "hiring_lead_months" in P
            else []
        )
        if pace_key:
            unit = {
                "pace_throughput_points_per_sprint": "story points per sprint",
                "pace_first_response_hours": "working hours to the first reply to a ticket",
                "pace_own_tasks_per_day": "own business tasks per working day",
            }[pace_key]
            texts.append((pace_key, f"At full pace the role delivers {_range(P[pace_key])} {unit}."))
        if "ramp_share_first_20_bdays" in P:
            texts.append(
                (
                    "ramp_share_first_20_bdays",
                    f"In the first 20 working days a hire works at "
                    f"{_range(P['ramp_share_first_20_bdays'], 'pct')} of the full pace.",
                )
            )
        if "resignation_hazard" in P:
            texts.append(
                (
                    "resignation_hazard",
                    f"The staff rate of resignation is {_range(P['resignation_hazard'], 'pct')} per year.",
                )
            )
        for i, (key, text) in enumerate(texts, start=2):
            facts.append(
                {"key": f"f{i}", "topic": "habit", "text": text, "param": key, "date": None, "evidence": ev}
            )
        pending = co.get("llm_pending") or []
        if pending:
            names = ", ".join(x["param"].replace("_", " ") for x in pending)
            facts.append(
                {
                    "key": f"f{len(facts) + 1}",
                    "topic": "pending",
                    "date": None,
                    "param": None,
                    "evidence": [],
                    "text": f"These parameters wait for general knowledge of the profession: {names}.",
                }
            )
        order = [
            "hiring_lead_months",
            pace_key or "",
            "ramp_share_first_20_bdays",
            "salary_vs_predecessor",
            "sick_spells_per_year",
            "resignation_hazard",
        ]
        rows = _rows(
            P, [x for x in order if x], ["ramp_share_next_20_bdays", "growth_rate", "vacation_month_share_08"]
        )
        rows += [fmt.pending_row(x["param"], x.get("why", ""), x.get("reason", "")) for x in pending]
        out.append(
            _cohort(
                f"future:{role}",
                f"Future {title}",
                "Future hire · cohort",
                "No one hired yet",
                "cohort-future",
                facts,
                rows,
                {"count": 0, "kind": "future"},
                [title],
            )
        )
    return out


def _cohort(cid, name, title, subtitle, container, facts, rows, members, aliases) -> dict[str, Any]:
    return {
        "id": cid,
        "kind": "cohort",
        "group": "cohorts",
        "section": "Future hires" if cid.startswith("future:") else "Client cohorts",
        "name": name,
        "title": title,
        "subtitle": subtitle,
        "dossier": None,
        "link": None,
        "container": CONTAINERS[container],
        "facts": facts,
        "habits": rows,
        "events": [],
        "members": members,
        "aliases": aliases,
    }


# ---------------------------------------------------------------------------------------------- risks
def _size_rows(size: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for k, v in (size or {}).items():
        if k not in SIZE_LABELS:
            continue
        label = SIZE_LABELS[k]
        if isinstance(v, dict) and "point" in v and "low" in v:
            if v.get("mode", 0) > v.get("high", 0) * 1.0001 or v["high"] > 1000 * max(abs(v["point"]), 1e-9):
                d = fmt.display(
                    {**v, "low": v["point"], "high": v["point"]}
                )  # an unusable interval: the point only
            else:
                d = fmt.display(v)
            rows.append(
                {"label": label, "value": d["value"], "interval": d["interval"], "suffix": d["suffix"]}
            )
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            value = fmt.money(float(v)) if k.endswith("_usd") else fmt.num(float(v))
            rows.append({"label": label, "value": value, "interval": "", "suffix": ""})
        elif k in SIZE_DICT_ROWS and isinstance(v, dict) and v:
            parts = sorted(v.items(), key=lambda kv: -float(kv[1]))
            rows.append(
                {
                    "label": label,
                    "value": ", ".join(f"{a} {fmt.num(float(b))}" for a, b in parts),
                    "interval": "",
                    "suffix": "",
                }
            )
    return rows


def _risks(
    register: list[dict[str, Any]],
    shocks: dict[str, list[dict]],
    card_ids: set[str],
    external: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    out = []
    for r in register:
        b = r["base_rate_per_month"]
        d = fmt.display(b)
        out.append(
            {
                "id": r["id"],
                "code": r["code"],
                "title": r["title"],
                "kind": r["kind"],
                "polarity": r["polarity"],
                "count": r["count"],
                "perMonth": {
                    "value": d["value"],
                    "interval": d["interval"],
                    "point": b["point"],
                    "low": b["low"],
                    "high": b["high"],
                    "source": b.get("source", ""),
                },
                "first": r.get("first"),
                "last": r.get("last"),
                "effect": r.get("effect", ""),
                "note": r.get("note", ""),
                "examples": list(r.get("evidence") or [])[:6],
                "evidenceCount": r.get("evidence_count", 0),
                "actors": len(r.get("actors") or []),
                "cards": [a for a in r.get("actors") or [] if a in card_ids],
                "size": _size_rows(r.get("size") or {}),
                "shocks": shocks.get(r["code"], []),
                "external": (external or [])[:5],
            }
        )
    return out
