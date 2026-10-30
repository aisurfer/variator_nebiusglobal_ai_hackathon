"""Texts of the notable events of a future branch (day level): facts packets for the headless writers.

A forward run (``orgsim.company.forward.sim.ForwardRun``) lists its notable events in ``run.notable``:
post-incident reviews, client notices, resignations, hires of a lever, the fork decision and the
monthly leadership reviews. ``packets`` turns each into a ``facts.Packet`` with one text field,
``description``; ``Writers`` (Nemotron through Token Factory) write it and ``check.check_artifact`` rejects a
number, date, id, person or organisation that is not in the packet. Code made every fact; the text is
prose around them. An event whose text is dropped keeps its code-made ``statement``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from variator.narrative.check import Names
from variator.narrative.facts import (
    Packet,
    count_forms,
    date_forms,
    first_name,
    fmt_count,
    fmt_date,
    fmt_minutes,
    fmt_money,
    fmt_time,
    minute_forms,
    money_forms,
    placeholder_text,
)
from variator.narrative.writers import Writers
from variator.orgsim.company.forward.deliver import directory

COMPANY = "Larchwood Systems"
LEVERS = {
    "debt": "spend part of the engineering capacity on technical debt",
    "hire": "hire engineers",
    "gpu": "reserve GPU capacity on a second cloud",
    "price": "raise list prices",
}


def _person(pk: Packet, run: Any, pid: str) -> str:
    p = run.people.get(pid)
    if p is None:
        name = run.name(pid)
        pk.people[pid] = {"first": first_name(name), "name": name, "role": ""}
        return first_name(name)
    first = first_name(p.name, p.login)
    pk.people[pid] = {"first": first, "name": p.name, "role": p.title}
    return first


def _org(pk: Packet, run: Any, oid: str) -> str:
    name = run.name(oid)
    pk.orgs[oid] = name
    return name


def _ts(x: str) -> datetime:
    return datetime.fromisoformat(x)


def packets(run: Any, tag: str) -> list[dict[str, Any]]:
    """Artifacts ``{id, type, record, packet}`` of the run's notable events; ids are prefixed with ``tag``."""
    out = []
    for n in run.notable:
        fn = BUILDERS.get(n["type"])
        if fn is None:
            continue
        rec = run.book.records[n["record"]]
        pk = fn(run, rec, n)
        if pk is None:
            continue
        aid = f"{tag}__{n['id'].replace(':', '__')}"
        pk.id = aid
        out.append(
            {"id": aid, "type": n["type"], "record": rec.id, "field": "description", "packet": pk.to_json()}
        )
    return out


# ================================================================================================ builders
def _postmortem(run: Any, rec: Any, n: dict[str, Any]) -> Packet:
    inc = run.book.records[n["ctx"]["incident"]]
    s = inc.state
    pk = Packet(
        "",
        "post-incident review notes",
        f"Notes of the post-incident review that {COMPANY} held after the incident; written by the head of "
        "delivery for the engineering and delivery teams.",
    )
    head = _person(pk, run, "LW-E03")
    responder = _person(pk, run, s["assignee"]) if s.get("assignee") in run.people else None
    start, resolved = _ts(s["started_at"]), _ts(s["resolved_at"])
    cause = s.get("cause_kind", "")
    facts: dict[str, Any] = {
        "incident": pk.need("incident", inc.id),
        "severity": pk.need("severity", s["severity"]),
        "service": s["component"],
        "symptom": s["title"].split(": ", 1)[-1],
        "date": pk.need("date", fmt_date(start), date_forms(start)),
        "started": fmt_time(start),
        "resolved": fmt_time(resolved),
        "restore_time": pk.need(
            "restore", fmt_minutes(s["restore_minutes"]), minute_forms(fmt_minutes(s["restore_minutes"]))
        ),
        "client_downtime": fmt_minutes(s["downtime_minutes"]),
        "clients_affected": pk.need(
            "clients",
            fmt_count(s["affected_count"], "client"),
            count_forms(fmt_count(s["affected_count"], "client")),
        ),
        "head_of_delivery": head,
    }
    if responder:
        facts["on_call_engineer"] = responder
    if s.get("client"):
        facts["largest_client_affected"] = _org(pk, run, s["client"])
    if cause == "deploy":
        dep = run.book.records[s["related"][0]]
        facts["cause"] = f"a change shipped in deploy {dep.state['version']}"
        facts["deploy"] = dep.state["version"]
        facts["follow_up"] = "add a check for this failure to the release pipeline"
    elif cause == "cloud provider capacity":
        facts["cause"] = f"GPU capacity shortage at {_org(pk, run, run.st.cloud.id)}"
        facts["follow_up"] = "move inference load faster when the cloud runs short of GPU capacity"
    else:
        facts["cause"] = cause
        facts["follow_up"] = "add an alert that catches this earlier"
    pk.facts = facts
    pk.slot(
        "description",
        "what happened, the impact on clients, the cause and the follow-up, as review notes",
        (60, 130),
        ["incident", "restore", "clients"],
    )
    return pk


def _client_notice(run: Any, rec: Any, n: dict[str, Any]) -> Packet:
    ctx = n["ctx"]
    c = run.clients[ctx["client"]]
    pk = Packet(
        "",
        "account note on a client notice",
        f"A short internal note by the account manager at {COMPANY} after the client gave notice on its "
        "contract; for the leadership team.",
    )
    am = _person(pk, run, c.account_manager) if c.account_manager in run.people else None
    d = date.fromisoformat(n["date"])
    end = ctx["end"]
    facts: dict[str, Any] = {
        "client": pk.need("client", _org(pk, run, c.id)),
        "notice_received": fmt_date(d),
        "service_ends": pk.need("end", fmt_date(end), date_forms(end)),
        "annual_value": fmt_money(ctx["acv"]),
        "reason_given": ctx["reason"],
    }
    if am:
        facts["account_manager"] = am
    if ctx.get("incidents"):
        facts["incidents_in_the_last_months"] = ctx["incidents"]
    if ctx.get("detractor"):
        facts["latest_nps_answer"] = "a low score (detractor)"
    pk.facts = facts
    pk.voice = "the account manager"
    pk.slot(
        "description",
        "what the client said, why it leaves, what it means for the account",
        (40, 100),
        ["client", "end"],
    )
    return pk


def _resignation(run: Any, rec: Any, n: dict[str, Any]) -> Packet:
    ctx = n["ctx"]
    pk = Packet("", "HR note on a resignation", f"A short HR note at {COMPANY} for the leadership team.")
    who = _person(pk, run, ctx["person"])
    p = run.people[ctx["person"]]
    _person(pk, run, "LW-E02")
    d = date.fromisoformat(n["date"])
    pk.facts = {
        "employee": who,
        "role": p.title,
        "resignation_date": fmt_date(d),
        "last_day": pk.need("last", fmt_date(ctx["last_day"]), date_forms(ctx["last_day"])),
        "reason": ctx["reason"],
        "next_step": "the CEO decides on a backfill",
    }
    pk.need("who", p.name, [p.name, who])
    pk.slot("description", "who resigns, why, the last day and the next step", (30, 80), ["who", "last"])
    return pk


def _hire(run: Any, rec: Any, n: dict[str, Any]) -> Packet:
    ctx = n["ctx"]
    p = run.people[ctx["person"]]
    pk = Packet("", "welcome note", f"A short welcome note to the engineering team at {COMPANY}.")
    who = _person(pk, run, p.id)
    if p.manager in run.people:
        _person(pk, run, p.manager)
    d = date.fromisoformat(n["date"])
    pk.need("who", p.name, [p.name, who])
    pk.facts = {
        "new_colleague": p.name,
        "role": p.title,
        "start_date": pk.need("start", fmt_date(d), date_forms(d)),
        "why_the_role_exists": "the company decided to add engineering capacity",
        "first_weeks": "onboarding at half pace, then three quarters, then full pace",
    }
    if p.manager in run.people:
        pk.facts["manager"] = run.people[p.manager].name
    pk.slot("description", "who joins, the role, the start and the first weeks", (30, 70), ["who", "start"])
    return pk


def _decision(run: Any, rec: Any, n: dict[str, Any]) -> Packet:
    lever = n["ctx"]["lever"]
    fk = run.fork
    pk = Packet("", "CEO memo on a decision", f"A short memo of the CEO of {COMPANY} to the leadership team.")
    _person(pk, run, "LW-E01")
    _person(pk, run, "LW-E03")
    _person(pk, run, "LW-E02")
    d = date.fromisoformat(n["date"])
    learned = run.st.param("company", "incidents_per_month") or {"point": 0.0}
    facts: dict[str, Any] = {
        "decision": rec.state["title"],
        "date": pk.need("date", fmt_date(d), date_forms(d)),
        "goal": LEVERS[lever],
        "incidents_per_month_last_year": f"{float(learned['point']):.1f}",
        "cash_today": fmt_money(run.st.cash_at_t),
    }
    if lever == "debt":
        pct = f"{round(fk.vars['debt_share'] * 100)}%"
        facts["share_of_capacity"] = pk.need("share", pct)
        facts["effect_expected_from"] = fmt_date(fk.dates["debt_ready"])
    elif lever == "hire":
        cnt = int(round(fk.vars.get("hire_count", 5)))
        facts["engineers_to_hire"] = pk.need(
            "count", fmt_count(cnt, "engineer"), count_forms(fmt_count(cnt, "engineer"))
        )
        facts["typical_time_to_hire"] = "about three months"
    elif lever == "gpu":
        facts["cost_per_year"] = pk.need(
            "cost",
            fmt_money(fk.vars["gpu_reserve_cost"]),
            money_forms(fmt_money(fk.vars["gpu_reserve_cost"])),
        )
    elif lever == "price":
        facts["price_change"] = pk.need("change", f"{round(fk.lever('price_change') * 100)}%")
    pk.facts = facts
    pk.voice = "the CEO"
    req = ["date"] + [k for k in ("share", "count", "cost", "change") if k in pk.required]
    pk.slot("description", "the decision, why now and what the team should expect", (50, 120), req)
    return pk


def _review(run: Any, rec: Any, n: dict[str, Any]) -> Packet:
    ctx = n["ctx"]
    m = date.fromisoformat(ctx["month"] + "-01")
    pk = Packet(
        "",
        "minutes of the monthly leadership review",
        f"Minutes of the monthly leadership review of {COMPANY}: the CEO, the head of finance and the head "
        "of "
        "delivery look at the last month.",
    )
    for pid in ("LW-E01", "LW-E02", "LW-E03"):
        _person(pk, run, pid)
    month = f"{m:%B %Y}"
    facts: dict[str, Any] = {
        "month": pk.need("month", month, [month, f"{m:%B}"]),
        "cash_at_review": fmt_money(ctx["cash"]),
        "arr": fmt_money(ctx["arr"]),
        "incidents_in_month": pk.need(
            "incidents", fmt_count(ctx["n"], "incident"), count_forms(fmt_count(ctx["n"], "incident"))
        ),
        "incidents_per_month_before": f"{ctx['base']:.1f}",
        "engineering_load": f"{ctx['load']:.2f} (1.00 = full)",
        "client_notices": len(ctx["notices"]),
    }
    if ctx["notices"]:
        facts["clients_that_gave_notice"] = [
            _org(pk, run, run.book.records[x].state["client"]) for x in ctx["notices"]
        ]
    if ctx.get("signal"):
        sig = run.book.records[ctx["signal"]].state
        facts["incident_trend"] = sig["statement"]
    if run.decision_id:
        facts["decision_in_force"] = run.book.records[run.decision_id].state["title"]
    pk.facts = facts
    pk.slot(
        "description",
        "what the month showed and what the team will watch next month; notes, not new decisions",
        (60, 140),
        ["month", "incidents"],
    )
    return pk


BUILDERS = {
    "postmortem": _postmortem,
    "client_notice": _client_notice,
    "resignation": _resignation,
    "hire": _hire,
    "decision_rationale": _decision,
    "leadership_review": _review,
}


def plan(arts: list[dict[str, Any]], people_yaml: dict[str, Any]) -> dict[str, Any]:
    """The plan object ``writers.Writers`` reads."""
    return {"company": COMPANY, "artifacts": arts, "names": Names.from_people_yaml(people_yaml).to_json()}


def forget_changed(work: Path, arts: list[dict[str, Any]]) -> list[str]:
    """Drop the stored texts and answers of artifacts whose facts packet changed since the last plan.

    An artifact id names a record (``<tenant>__postmortem__PIR-0003``); a new path can give the same id
    other facts, and a text written for the old facts must not be reused.
    """
    old_plan = work / "plan.json"
    if not old_plan.is_file():
        return []
    old = {a["id"]: a["packet"] for a in json.loads(old_plan.read_text(encoding="utf-8"))["artifacts"]}
    changed = [a["id"] for a in arts if a["id"] in old and old[a["id"]] != a["packet"]]
    if not changed:
        return []
    state_path = work / "texts.json"
    if state_path.is_file():
        st = json.loads(state_path.read_text(encoding="utf-8"))
        for aid in changed:
            st.pop(aid, None)
        state_path.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    gone = set(changed)
    for req in (work / "requests").glob("*.json"):
        if gone & set(json.loads(req.read_text(encoding="utf-8"))["artifacts"]):
            req.unlink()
            (work / "responses" / req.name).unlink(missing_ok=True)
    return changed


def write_texts(
    runs: dict[str, Any],
    work: Path,
    log: Callable[[str], None] = print,
    *,
    mode: str = "nemotron",
    parallel: int = 6,
) -> tuple[dict[str, dict[str, dict[str, str]]], dict[str, int], dict[str, Any]]:
    """The text step of ``orgsim.company.forward.pipeline.branches`` for the runs of all branches.

    ``mode``: ``nemotron`` (resumable under ``work``), ``placeholder`` (no LLM: the
    required facts joined) or ``none``. Returns the accepted texts by tenant and text slot, the planned
    texts by tenant and the writer summary.
    """
    arts: list[dict[str, Any]] = []
    owner: dict[str, str] = {}
    people: dict[str, Any] = {"people": [], "contacts": [], "organizations": []}
    for tenant, run in runs.items():
        for a in packets(run, tenant):
            arts.append(a)
            owner[a["id"]] = tenant
        _, ppl = directory(run, tenant, "x")
        for key in people:
            seen = {x["id"] for x in people[key]}
            people[key] += [x for x in ppl[key] if x["id"] not in seen]
    planned = {t: sum(1 for a in arts if owner[a["id"]] == t) for t in runs}
    summary: dict[str, Any] = {"mode": mode}
    accepted: dict[str, dict[str, str]] = {}
    if mode == "placeholder":
        for a in arts:
            pk = Packet.from_json(a["packet"])
            accepted[a["id"]] = {s: placeholder_text(pk, s) for s in pk.slots}
    elif mode == "nemotron" and arts:
        work.mkdir(parents=True, exist_ok=True)
        forget_changed(work, arts)
        pl = plan(arts, people)
        (work / "plan.json").write_text(json.dumps(pl, ensure_ascii=False, indent=1), encoding="utf-8")
        w = Writers(pl, work, parallel=parallel, log=log)
        t0 = time.perf_counter()
        summary = {"mode": mode, **w.run(), "seconds": round(time.perf_counter() - t0, 1)}
        accepted = {aid: s["texts"] for aid, s in w.load_state().items() if s.get("status") == "accepted"}
    out: dict[str, dict[str, dict[str, str]]] = {t: {} for t in runs}
    by_id = {a["id"]: a for a in arts}
    for aid, texts in accepted.items():
        a = by_id.get(aid)
        if a is None:
            continue
        tenant = owner[aid]
        rec = runs[tenant].book.records[a["record"]]
        out[tenant][rec.text_slots[a["field"]]] = texts
    return out, planned, summary
