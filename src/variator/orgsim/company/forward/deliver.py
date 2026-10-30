"""A forward run -> a corpus delivery of its own tenant (a branch of the knowledge base).

The delivery holds:

- the state at T: history lines of the cause catalog, vendor agreements, client contracts, open
  invoices and bills, open deals and the last payroll (``StateAtT.snapshot_lines``);
- every version of every record the run made after T, as ``sim`` lines with the event envelope
  ``{run: "day", option: <fork>, scenario: <k>, ...}`` (the KB marks them as simulation);
- the directory of the history plus the people hired and the prospects met after T.

Every relation is a declared field: causes become causal links, ``contract``/``settles``/``records``/
``deal``/``incident``/``related`` become references. Texts of notable events (``description``) come
from the writers; a record without an accepted text keeps only its code-made ``statement``.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path
from typing import Any

from variator.orgsim.company.forward import evalcases
from variator.orgsim.company.forward.records import CONTAINERS, Rec
from variator.orgsim.company.forward.sim import ForwardRun
from variator.orgsim.delivery import write_delivery
from variator.orgsim.events import Cause, Event

LAG = timedelta(minutes=1)
PERSON_FIELDS = ("owner", "assignee", "author", "reporter", "employee", "actor", "contact")
ORG_FIELDS = ("client", "vendor", "counterparty", "authority")
ROLE_GROUPS = {
    "ceo": ["grp:management", "grp:sales"],
    "finance": ["grp:management", "grp:finance"],
    "delivery": ["grp:management", "grp:delivery", "grp:engineering", "grp:support"],
    "pm": ["grp:delivery"],
    "analyst_designer": ["grp:delivery"],
    "backend": ["grp:engineering"],
    "frontend": ["grp:engineering"],
    "devops": ["grp:engineering"],
    "qa": ["grp:engineering"],
    "support": ["grp:support"],
    "sales": ["grp:sales"],
    "marketing": ["grp:marketing"],
}


def _value(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, list):
        return [_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _value(x) for k, x in v.items() if x is not None}
    return v


def sim_fields(
    run: ForwardRun, r: Rec, state: dict[str, Any], texts: dict[str, dict[str, str]]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in state.items():
        if v is None or v == [] or v == {}:
            continue
        out[k] = _value(v)
    if r.otype in ("contract", "invoice", "payment") and "number" not in out:
        out["number"] = r.id
    for field, aid in r.text_slots.items():
        t = texts.get(aid)
        if t and t.get(field):
            out[field] = t[field]
    for k in PERSON_FIELDS + ORG_FIELDS:
        v = out.get(k)
        if isinstance(v, str):
            out[f"{k}_name"] = run.name(v)
        elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            out[f"{k}_name"] = ", ".join(run.name(x) for x in v)
    return out


def kept(run: ForwardRun, r: Rec) -> bool:
    """Whether a record goes into the KB branch (the ``lean`` profile keeps the KB load short).

    Left out: bank entries of client receipts (the payment records stay), deploys that no incident
    cites, leave and sickness records, escalated support tickets. Nothing delivered refers to them;
    the day series and the event lane still count them.
    """
    if r.otype == "bank_entry":
        return r.state.get("direction") != "credit"
    if r.otype == "deploy":
        return r.id in run_cited(run)
    if r.otype == "employee_event":
        return r.state.get("type") not in ("vacation", "sick_leave")
    return r.otype != "support_ticket"


def run_cited(run: ForwardRun) -> set[str]:
    if not hasattr(run, "_cited"):
        run._cited = {c[1] for r in run.book.records.values() for v in r.versions for c in v.causes}  # type: ignore[attr-defined]
        for r in run.book.records.values():
            rel = r.state.get("related")
            run._cited |= set(rel if isinstance(rel, list) else [])  # type: ignore[attr-defined]
    return run._cited  # type: ignore[attr-defined]


def future_lines(
    run: ForwardRun, texts: dict[str, dict[str, str]] | None = None, *, lean: bool = True
) -> list[dict[str, Any]]:
    """Timeline lines of every version the run made (``lean``: without the records ``kept`` leaves out).

    A history record that the run continues (an invoice open at T) has only its new versions here;
    its state at T is a snapshot line.
    """
    texts = texts or {}
    items = [
        (v.ts, v.order, r, k)
        for r in run.book.records.values()
        if not lean or kept(run, r)
        for k, v in enumerate(r.versions)
    ]
    items.sort(key=lambda x: (x[0], x[1]))
    out = []
    for seq, (ts, _, r, k) in enumerate(items):
        v = r.versions[k]
        ev = Event(
            run="day",
            option=run.fork.id,
            scenario=run.k,
            ts=ts,
            process=v.process,
            kind=v.kind,
            entity_id=r.id,
            fields=sim_fields(run, r, v.fields, texts),
            causes=tuple(Cause(type=t, id=i) for t, i in v.causes),
            seq=seq,
        )
        out.append(
            ev.to_timeline_line(
                tenant=run.tenant,
                source="sim",
                object_type=r.otype,
                external_id=r.id,
                container=r.container,
                acl=CONTAINERS[r.container],
                observe_lag=LAG,
            )
        )
    return out


def directory(run: ForwardRun, tenant: str, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """sources.yaml and people.yaml of the branch: the history's, plus the new people and organisations."""
    st = run.st
    sources = copy.deepcopy(st.sources)
    sources["tenant"]["id"] = tenant
    sources["tenant"]["name"] = f"{st.company} (simulation: {label})"
    people = copy.deepcopy(st.people_yaml)
    known = {p["id"] for p in people["people"]}
    for p in people["people"]:
        if p["id"] in run.left_people:
            p["end_date"] = run.left_people[p["id"]].isoformat()
    for p in run.new_people:
        if p.id in known:
            continue
        email = f"{p.login}@{st.domain}"
        people["people"].append(
            {
                "id": p.id,
                "name": p.name,
                "role": p.title,
                "department": p.department,
                "manager": p.manager,
                "email": email,
                "principal": f"user:{p.login}",
                "groups": ["grp:all-staff", *ROLE_GROUPS.get(p.role, [])],
                "accounts": {
                    "jira-company": f"712020:{p.login}",
                    "confluence-company": f"712020:{p.login}",
                    "gitlab-company": p.login.replace(".", ""),
                    "mail-company": email,
                },
                "start_date": p.start.isoformat(),
                "end_date": run.left_people[p.id].isoformat() if p.id in run.left_people else None,
            }
        )
    orgs = {o["id"] for o in people["organizations"]}
    people["organizations"] += [o for o in run.new_orgs if o["id"] not in orgs]
    cons = {c["id"] for c in people["contacts"]}
    people["contacts"] += [c for c in run.new_contacts if c["id"] not in cons]
    return sources, people


def write_branch(
    run: ForwardRun,
    out: str | Path,
    *,
    tenant: str,
    label: str,
    texts: dict[str, dict[str, str]] | None = None,
    extra: dict[str, Any] | None = None,
    sample_days: int = 10,
) -> dict[str, Any]:
    """Write ``<out>/delivery`` (and its ``sample/``); return the manifest."""
    out = Path(out)
    st = run.st
    snap = st.snapshot_lines()
    lines = snap + future_lines(run, texts)
    lines.sort(key=lambda ln: ln["at"])
    sources, people = directory(run, tenant, label)
    gold, cases = evalcases.build(lines, people, prefix="BR")
    meta = {
        "branch": {
            "case": "company_best_year",
            "fork": run.fork.id,
            "fork_label": run.fork.label,
            "scenario": run.k,
            "seed": run.seed,
            "anchor": st.anchor.isoformat(),
            "fork_date": st.fork_date.isoformat(),
            "window": [run.start.isoformat(), run.end.isoformat()],
            "source": "actors",
            "base_tenant": st.tenant,
            "base_run_id": st.manifest.get("run_id"),
            "snapshot_records": len(snap),
            **(extra or {}),
        }
    }
    manifest = write_delivery(
        out / "delivery",
        tenant=tenant,
        sources=sources,
        people=people,
        lines=lines,
        seed=run.seed,
        language="en",
        name=sources["tenant"]["name"],
        dashboard=st.dashboard,
        cases=cases,
        gold_links=gold,
        extra_manifest=meta,
    )
    if sample_days:
        days = sorted({ln["at"][:10] for ln in lines})
        first = days[-sample_days] if len(days) > sample_days else days[0]
        write_delivery(
            out / "delivery" / "sample",
            tenant=tenant,
            sources=sources,
            people=people,
            lines=[ln for ln in lines if ln["at"][:10] >= first],
            seed=run.seed,
            language="en",
            name=f"{sources['tenant']['name']} (sample)",
            dashboard=st.dashboard,
            sample_of=tenant,
        )
    return manifest
