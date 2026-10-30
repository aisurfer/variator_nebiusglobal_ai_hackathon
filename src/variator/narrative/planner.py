"""Find the management moments of a history and plan the artifacts with exact facts.

Triggers (all read from the delivered base, as of the artifact's own time):

- ``postmortem``: every SEV1/SEV2 incident -> a post-incident review meeting and its notes;
  action items are the incident's existing follow-up tickets (review ticket, regression bug).
- ``weekly_sync``: every week after the first -> the delivery sync notes (sprint, load,
  blockers, incidents, people); action items are existing open tickets.
- ``leadership_review``: every month -> CEO, Head of Finance, Head of Delivery; numbers as of
  the meeting; a decision item only for a hiring decision of the base (the review moves to
  the decision's day); action items are existing open business tickets.
- ``qbr``: key client x quarter -> the quarterly business review (SLA, incidents, support,
  implementations, renewals, upsells, downgrades).
- ``escalation``: a key client hit by a SEV1/SEV2 incident replies to the incident notice
  of the base (2-4 messages); a late implementation of a key client opens a new thread.
- ``decision_rationale``: a new version of each base decision record with a rationale and
  causes (the life event, the load evidence, the meeting decision item).

Nothing after T and nothing after the artifact's own time is used. Every artifact time is
a working day, inside the history window and after its trigger. ``plan`` returns JSON-ready
artifacts; ``render`` turns them into timeline lines.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from variator.narrative.base import Base, Issue, parse_time
from variator.narrative.facts import (
    Packet,
    count_forms,
    date_forms,
    fmt_count,
    fmt_date,
    fmt_day,
    fmt_minutes,
    fmt_money,
    fmt_pct,
    fmt_time,
    minute_forms,
    money_forms,
)

PAGE_BASE = 2_000_000
MAIL_BASE = 900_000
THREAD_BASE = 900_000
SIM_SEQ_BASE = 900_000
SPACES = {
    "DEL": {"name": "Delivery", "id": "98002", "acl": ["grp:delivery", "grp:engineering", "grp:management"]},
    "ACCT": {"name": "Accounts", "id": "98003", "acl": ["grp:management", "grp:sales", "grp:delivery"]},
    "MGMT": {"name": "Management", "id": "98001", "acl": ["grp:management"]},
}
OPEN = ("Done", "Won't Do")
LEADER_TICKETS = (
    "Renewal prep",
    "Exit review",
    "Capacity plan",
    "Engineering hiring plan",
    "Hiring:",
    "Cash forecast",
    "Board pack",
    "Update the budget model",
    "Prepare the investor update",
    "Pricing review",
    "Risk review",
)


def _h(*parts: object) -> int:
    return int(hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def iso(t: datetime) -> str:
    return t.isoformat()


@dataclass
class Ctx:
    """Planning state shared by the triggers."""

    base: Base
    linked: dict[str, str] = field(default_factory=dict)  # ticket key -> artifact id
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    by_login: dict[str, str] = field(default_factory=dict)
    banned: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        b = self.base
        self.by_login = {p.login: pid for pid, p in b.people.items()}
        self.limit = b.end - timedelta(minutes=30)
        self.key_clients = [c["id"] for c in b.dataset("clients") if c.get("key") in ("True", "true", True)]
        self.banned = banned_phrases(b)
        self.trans: dict[str, list[tuple[datetime, str, str]]] = defaultdict(list)
        for r in b.dataset("transitions"):
            self.trans[r["key"]].append((parse_time(r["ts"]), r["from"], r["to"]))
        for v in self.trans.values():
            v.sort()

    def pid_of(self, account: dict[str, Any] | None) -> str | None:
        if not account:
            return None
        return self.by_login.get(str(account.get("accountId", "")).split(":")[-1])

    def assignee_at(self, it: Issue, t: datetime) -> str | None:
        p = it.at(t)
        return self.pid_of(p["fields"].get("assignee")) if p else None


def banned_phrases(b: Base) -> list[str]:
    """Phrases no overlay text may contain: negative-case decoys and acl-restricted facts."""
    import re

    out: set[str] = set()
    for cs in b.cases:
        if cs.get("category") == "negative":
            m = re.search(r"keywords?:\s*(.+)$", cs.get("rationale") or "", re.I | re.S)
            for kw in [x.strip(" .«»\"'") for x in re.split(r"[,;]", m.group(1))] if m else []:
                if len(kw) >= 4:
                    out.add(kw)
        if cs.get("category") == "acl":
            for f in cs.get("forbidden") or []:
                for fact in f.get("facts") or []:
                    out.update(x for x in fact.get("any") or [] if len(x) >= 3)
    return sorted(out)


# ================================================================================================ helpers
def meeting_day(b: Base, start: date, people: list[str], max_days: int = 10) -> date | None:
    """The first working day on or after ``start`` when the first person is present."""
    d = b.workday_on_or_after(start)
    for _ in range(max_days):
        if all(b.present(p, d) for p in people[:1]):
            return d
        d = b.next_workday(d)
    return None


def present_list(b: Base, pids: list[str], d: date) -> list[str]:
    out: list[str] = []
    for p in pids:
        if p and p not in out and b.present(p, d):
            out.append(p)
    return out


def tz_label(t: datetime) -> str:
    off = t.strftime("%z")
    return f"{off[:3]}:{off[3:]}"


def when_text(start: datetime, end: datetime) -> str:
    """The meeting time for the 'Date' page property (kb parse_when reads it)."""
    return f"{start:%Y-%m-%d} {start:%H:%M}–{end:%H:%M} {tz_label(start)}"


def first_workday_time(b: Base, t: datetime, hh: int = 9, mm: int = 30) -> datetime:
    """t if it is inside working hours of a working day, else the next working morning."""
    lt = b.local(t)
    if b.cal.is_workday(lt.date()) and 9 <= lt.hour < 18:
        return lt
    d = lt.date() if (b.cal.is_workday(lt.date()) and lt.hour < 9) else b.next_workday(lt.date())
    return b.at(d, hh, mm)


def link_time(ctx: Ctx, it: Issue, after: datetime) -> datetime | None:
    """When a remote link can be added to a ticket: after its last base version and after ``after``.

    The link version must be the newest version of the ticket (the base is never rewritten),
    on a working day when the ticket's creator (the actor intake assigns to it) is not away.
    """
    b = ctx.base
    creator = ctx.pid_of(it.last["fields"].get("creator"))
    t = max(after, it.last_at) + timedelta(minutes=6 + _h(it.key) % 20)
    t = first_workday_time(b, t, 9, 20 + _h(it.key, "m") % 30)
    for _ in range(15):
        if creator is None or not b.absent(creator, t.date()):
            break
        t = b.at(b.next_workday(t.date()), 9, 25)
    return t if t < ctx.limit else None


def task_state(it: Issue, t: datetime) -> str:
    st = it.status_at(t)
    return "DONE" if st in OPEN else "TODO"


# ======================================================================================== post-mortems
def plan_postmortems(ctx: Ctx) -> None:
    b = ctx.base
    incs = b.records("incident")
    for iid in sorted(incs):
        vs = incs[iid]
        last = vs[-1]
        f = last["fields"]
        if f.get("severity") not in ("SEV1", "SEV2") or f.get("status") != "resolved":
            continue
        started, detected, resolved = (parse_time(f[k]) for k in ("started_at", "detected_at", "resolved_at"))
        related = f.get("related") or []
        keys = [r.split(".", 1)[1] for r in related if str(r).startswith("jira.")]
        pir = next(
            (k for k in keys if b.issues[k].last["fields"]["summary"].startswith("Post-incident review")),
            None,
        )
        bug = next((k for k in keys if b.issues[k].last["fields"]["issuetype"]["name"] == "Bug"), None)
        deploy = next((r for r in related if str(r).startswith("DEP-")), None)
        mr = next((int(str(r).rsplit("-", 1)[1]) for r in related if ".mr-" in str(r)), None)
        head, pm = b.head("delivery"), b.head("pm")
        rd = b.local(resolved).date()
        chair_day = meeting_day(b, b.next_workday(rd), [head]) or meeting_day(b, b.next_workday(rd), [pm])
        if chair_day is None:
            continue
        start = b.at(chair_day, 15, 0)
        made = [b.issues[k].first_at for k in (pir, bug) if k]
        if made and max(made) + timedelta(minutes=40) > start:
            ready = first_workday_time(b, max(made) + timedelta(minutes=40), 15, 0)
            if b.local(ready).hour >= 17:
                ready = b.at(b.next_workday(b.local(ready).date()), 15, 0)
            start = ready
            chair_day = b.local(start).date()
            if not (b.present(head, chair_day) or b.present(pm, chair_day)):
                nd = meeting_day(b, b.next_workday(chair_day), [head]) or meeting_day(
                    b, b.next_workday(chair_day), [pm]
                )
                if nd is None:
                    continue
                start = b.at(nd, 15, 0)
        end = start + timedelta(minutes=45)
        publish = end + timedelta(minutes=25 + _h(iid) % 25)
        pir_issue = b.issues.get(pir) if pir else None
        pir_done = pir_issue.done_at() if pir_issue else None
        if pir_done and publish >= pir_done - timedelta(minutes=10):
            alt_end = pir_done - timedelta(minutes=50)
            alt_end = alt_end.replace(minute=alt_end.minute - alt_end.minute % 15, second=0, microsecond=0)
            alt_start = alt_end - timedelta(minutes=45)
            ls = b.local(alt_start)
            if (
                alt_start > resolved + timedelta(hours=2)
                and 9 <= ls.hour < 17
                and b.present(head, ls.date())
                and all(m + timedelta(minutes=30) <= alt_start for m in made)
            ):
                start, end, publish = alt_start, alt_end, alt_end + timedelta(minutes=20)
        if publish >= ctx.limit:
            continue
        d = b.local(start).date()
        chair = head if b.present(head, d) else pm
        mr_author = None
        if mr is not None and mr in b.mrs:
            uname = b.mrs[mr][-1]["payload"]["author"]["username"]
            mr_author = next((pid for pid, p in b.people.items() if p.login.replace(".", "") == uname), None)
        bug_issue = b.issues.get(bug) if bug else None
        bug_owner = ctx.assignee_at(bug_issue, start) if bug_issue else None
        support = (b.by_role("support", d) or [None])[0]
        attendees = present_list(
            b,
            [
                chair,
                f.get("assignee"),
                pm,
                mr_author,
                bug_owner,
                support if f.get("affected_count") else None,
            ],
            d,
        )
        author = pm if pm in attendees else chair
        pk = Packet(
            f"pm-{iid}",
            "post-incident review notes (internal Confluence page)",
            f"Internal notes of the post-incident review meeting held on {fmt_date(start)}, written by "
            f"{b.people[author].name} for the delivery and engineering teams of {b.company}.",
            banned=list(ctx.banned),
        )
        for p in attendees:
            pk.person(b, p)
        responder = pk.person(b, f["assignee"]) if f.get("assignee") in b.people else None
        affected = list(f.get("affected_clients") or [])
        names = [pk.org(b, c) for c in affected[:5]]
        key_hit = [b.org_name(c) for c in affected if c in ctx.key_clients]
        sev = pk.need("severity", f["severity"])
        day = pk.need("incident_date", fmt_date(b.local(started)), date_forms(b.local(started)))
        downtime = pk.need(
            "downtime",
            fmt_minutes(f.get("downtime_minutes", 0)),
            minute_forms(fmt_minutes(f.get("downtime_minutes", 0))),
        )
        restore = fmt_minutes(f.get("restore_minutes", 0))
        n_aff = pk.need(
            "affected", fmt_count(len(affected), "client"), count_forms(fmt_count(len(affected), "client"))
        )
        det = pk.need("detected", fmt_time(b.local(detected)))
        res = pk.need("restored", fmt_time(b.local(resolved)))
        facts: dict[str, Any] = {
            "incident": {
                "id": iid,
                "title": f["title"],
                "severity": sev,
                "component": f.get("component"),
                "date": day,
                "started": fmt_time(b.local(started)),
                "detected": det,
                "restored": res,
                "time_to_restore": restore,
                "client_downtime": downtime,
                "affected_clients": n_aff,
                "largest_affected_clients": names,
                "key_accounts_hit": key_hit,
                "on_call_responder": responder,
                **({"services_affected": f["services"]} if f.get("services") else {}),
            },
            "meeting": {
                "date": fmt_date(start),
                "time": f"{fmt_time(b.local(start))}-{fmt_time(b.local(end))}",
            },
        }
        cause_kind = f.get("cause_kind") or "unknown"
        if deploy:
            dv = b.record_at("deploy", deploy, publish) or {}
            dfields = dv.get("fields") or {}
            version = dfields.get("version", deploy)
            cause = {
                "kind": "a production deploy",
                "deploy": deploy,
                "version": version,
                "deployed": f"{fmt_time(b.local(parse_time(dfields['deployed_at'])))} on "
                f"{fmt_date(b.local(parse_time(dfields['deployed_at'])))}"
                if dfields.get("deployed_at")
                else None,
                "rolled_back": dfields.get("status") == "rolled_back",
            }
            pk.need("root_cause", version, [version, version.lstrip("v")])
            rc_keys = ["root_cause"]
            if mr is not None and mr in b.mrs:
                mrp = b.mrs[mr][-1]["payload"]
                cause["merge_request"] = f"!{mr}"
                cause["merge_request_title"] = mrp["title"]
                if mr_author:
                    cause["merge_request_author"] = pk.person(b, mr_author)
                pk.need("mr", f"!{mr}")
                rc_keys.append("mr")
        else:
            cause = {"kind": f"an outside cause: {cause_kind}"}
            le = next((c["id"] for c in last.get("causes") or [] if c["type"] == "life_event"), None)
            if le:
                lev = b.record_at("life_event", le, publish)
                if lev:
                    cause["vendor_event"] = lev["fields"].get("statement")
                    if lev["fields"].get("vendor"):
                        pk.org(b, lev["fields"]["vendor"])
            pk.need("root_cause", cause_kind)
            rc_keys = ["root_cause"]
        facts["root_cause"] = cause
        tasks: list[dict[str, Any]] = []
        followups = []
        for k in [bug, pir]:
            if not k:
                continue
            it = b.issues[k]
            if it.first_at > start:
                continue
            owner = ctx.assignee_at(it, start) or chair
            if not b.employed(owner, b.local(publish).date()):
                continue
            pk.person(b, owner)
            summary = it.last["fields"]["summary"]
            state = task_state(it, publish)
            verb = "Fix the regression" if k == bug else "Close the review ticket"
            tasks.append({"assignee": owner, "text": f"{verb} {k}: {summary}", "key": k, "state": state})
            followups.append(
                {
                    "ticket": k,
                    "summary": summary,
                    "owner": pk.people[owner]["first"],
                    "status_at_meeting": it.status_at(start),
                }
            )
        facts["follow_up_tickets"] = followups
        pk.facts = facts
        pk.slot(
            "summary",
            "What happened and who was affected; plain words, two to four sentences.",
            (40, 120),
            ["severity", "incident_date", "downtime", "affected"],
        )
        pk.slot(
            "root_cause",
            "The cause as the facts state it, and what made it reach production or the platform.",
            (20, 90),
            rc_keys,
        )
        pk.slot(
            "discussion",
            "What the meeting discussed: detection, response, the restore, the follow-ups. "
            "Mention follow-up tickets only by the ids given.",
            (40, 150),
            ["detected", "restored"],
        )
        props = [
            {"label": "Type", "text": "Post-incident review"},
            {"label": "Date", "text": when_text(b.local(start), b.local(end))},
            {"label": "Attendees", "mentions": attendees},
            {"label": "Incident", "text": iid},
            {"label": "Severity", "text": f["severity"]},
        ]
        if pir:
            props.append({"label": "Jira", "links": [[pir, b.issue_url(pir)]]})
        if mr is not None and mr in b.mrs:
            props.append({"label": "Change", "links": [[f"!{mr}", b.mr_url(mr)]]})
        timeline = [
            f"{fmt_time(b.local(started))} incident started ({f['title']})",
            f"{det} detected and assigned to {responder or 'the on-call engineer'}",
            f"{res} service restored; time to restore {restore}",
            f"Client downtime {downtime}; {n_aff} affected",
        ]
        if deploy:
            timeline.insert(
                0, f"Cause: deploy {cause['version']}" + (f" with merge request !{mr}" if mr else "")
            )
        blocks = [
            {"h": "Summary"},
            {"slot": "summary"},
            {"h": "Timeline"},
            {"bullets": timeline},
            {"h": "Root cause"},
            {"slot": "root_cause"},
            {"h": "Discussion"},
            {"slot": "discussion"},
        ]
        if tasks:
            blocks += [{"h": "Action items"}, {"tasks": tasks}]
        art = {
            "id": pk.id,
            "type": "postmortem",
            "trigger": {"incident": iid, "pir": pir, "bug": bug, "deploy": deploy, "mr": mr},
            "start": iso(start),
            "end": iso(end),
            "publish": iso(publish),
            "page": {
                "space": "DEL",
                "title": f"Post-incident review: {iid} {f['title']}",
                "labels": ["meeting-notes", "postmortem"],
                "author": author,
                "properties": props,
                "blocks": blocks,
            },
            "packet": pk.to_json(),
            "links": [],
            "implicit": [{"cause": f"sim.{iid}", "predicate": "caused_by", "quote": iid}],
        }
        for t in tasks:
            if t["state"] == "TODO":
                add_link(ctx, art, t["key"], "informed by", publish)
        ctx.artifacts.append(art)


def add_link(
    ctx: Ctx, art: dict[str, Any], key: str, relationship: str, after: datetime, frag: str | None = None
) -> None:
    """Plan a remote link from an existing ticket to the artifact's page (one per ticket)."""
    if key in ctx.linked or key not in ctx.base.issues:
        return
    it = ctx.base.issues[key]
    if it.last["fields"]["issuetype"]["name"] == "Epic" and relationship == "informed by":
        return
    t = link_time(ctx, it, after)
    if t is None:
        return
    ctx.linked[key] = art["id"]
    art["links"].append({"key": key, "relationship": relationship, "at": iso(t), "frag": frag})


# ================================================================================================ weekly sync
def plan_weekly_syncs(ctx: Ctx) -> None:
    b = ctx.base
    head, pm = b.head("delivery"), b.head("pm")
    incs = b.records("incident")
    ees = b.records("employee_event")
    monday = b.start - timedelta(days=b.start.weekday()) + timedelta(days=7)
    while monday < b.anchor:
        week = monday.isocalendar()
        d = b.workday_on_or_after(monday)
        monday += timedelta(days=7)
        if d.isocalendar()[1] != week[1]:
            continue
        chair = head if b.present(head, d) else pm if b.present(pm, d) else None
        if chair is None:
            continue
        start = b.at(d, 11, 0)
        end = start + timedelta(minutes=30)
        publish = end + timedelta(minutes=20 + _h(d) % 30)
        if publish >= ctx.limit:
            continue
        w0 = start - timedelta(days=7)
        seniors = [(b.by_role(r, d) or [None])[0] for r in ("backend", "frontend", "devops", "qa", "support")]
        attendees = present_list(b, [chair, head, pm, *b.by_role("analyst_designer", d), *seniors], d)
        author = pm if pm in attendees else chair
        aid = f"ws-{week[0]}-W{week[1]:02d}"
        pk = Packet(
            aid,
            "weekly delivery sync notes (internal Confluence page)",
            f"Internal notes of the weekly delivery sync of {b.company} held on {fmt_date(start)}, "
            f"written by {b.people[author].name} for the delivery and engineering teams.",
            banned=list(ctx.banned),
        )
        for p in attendees:
            pk.person(b, p)
        sp = b.sprint_at(start)
        facts: dict[str, Any] = {"meeting": {"date": fmt_date(start), "chair": pk.people[chair]["first"]}}
        req_sum: list[str] = []
        if sp:
            name = pk.need("sprint", sp["name"])
            facts["sprint"] = {
                "name": name,
                "start": fmt_date(b.local(parse_time(sp["startDate"]))),
                "end": fmt_date(b.local(parse_time(sp["endDate"]))),
            }
            req_sum.append("sprint")
            if sp.get("load") is not None:
                facts["sprint"]["planned_load"] = pk.need("load", f"{sp['load']:.2f}")
                facts["sprint"]["load_meaning"] = "planned work divided by the team's capacity; 1.00 is full"
                req_sum.append("load")
        done = []
        for key, tr in ctx.trans.items():
            if not key.startswith(("PLAT-", "IMPL-")):
                continue
            if any(w0 <= ts < start and to == "Done" for ts, _, to in tr):
                done.append(key)
        pts = sum(
            int(b.issues[k].last["fields"].get("customfield_10016") or 0) for k in done if k in b.issues
        )
        facts["last_week"] = {
            "tickets_done": pk.need(
                "done", fmt_count(len(done), "ticket"), count_forms(fmt_count(len(done), "ticket"))
            ),
            "story_points_done": pts,
        }
        req_sum.append("done")
        open_by: dict[str, int] = defaultdict(int)
        blocked, bugs, stale = [], [], []
        for key, it in b.issues.items():
            if not key.startswith(("PLAT-", "IMPL-")) or it.first_at > start:
                continue
            p = it.at(start)
            st = p["fields"]["status"]["name"]
            if st in OPEN or p["fields"]["issuetype"]["name"] == "Epic":
                continue
            open_by[st] += 1
            if st == "Blocked":
                lk = [
                    x["inwardIssue"]["key"]
                    for x in p["fields"].get("issuelinks") or []
                    if x.get("inwardIssue")
                ]
                blocked.append((key, lk[-1] if lk else None))
            elif p["fields"]["issuetype"]["name"] == "Bug" and p["fields"]["priority"]["name"] == "Highest":
                bugs.append(key)
            elif st in ("In Review", "In QA"):
                since = max((ts for ts, _, to in ctx.trans.get(key, []) if ts <= start), default=start)
                if (start - since).days >= 3:
                    stale.append((since, key))
        facts["open_now"] = dict(sorted(open_by.items()))
        disc: list[str] = []
        bl = []
        for key, by in blocked[:3]:
            it = b.issues[key]
            who = ctx.assignee_at(it, start)
            entry = {
                "ticket": key,
                "summary": it.last["fields"]["summary"],
                "owner": pk.person(b, who) if who else None,
            }
            if by:
                entry["blocked_by"] = by
            bl.append(entry)
        if bl:
            facts["blocked"] = bl
            pk.need("blocker", bl[0]["ticket"])
            disc.append("blocker")
        week_inc = []
        for iid in sorted(incs):
            v = b.record_at("incident", iid, start)
            if not v:
                continue
            det = parse_time(v["fields"]["detected_at"])
            if w0 <= det < start:
                week_inc.append(
                    {
                        "id": iid,
                        "severity": v["fields"]["severity"],
                        "title": v["fields"]["title"],
                        "date": fmt_date(b.local(det)),
                        "client_downtime": fmt_minutes(v["fields"].get("downtime_minutes", 0))
                        if v["fields"].get("status") == "resolved"
                        else "still open",
                    }
                )
        if week_inc:
            facts["incidents_last_week"] = week_inc
            pk.need("incident", week_inc[0]["id"])
            disc.append("incident")
        else:
            facts["incidents_last_week"] = "none"
        people_notes = []
        for vs in ees.values():
            v = None
            for x in vs:
                if parse_time(x["ts"]) <= start:
                    v = x
            if not v:
                continue
            ff = v["fields"]
            pid = ff.get("employee")
            if pid not in b.people:
                continue
            eff = date.fromisoformat(ff["effective_date"])
            if v["kind"] == "resignation_received" and w0 <= parse_time(v["ts"]) < start:
                people_notes.append(
                    {
                        "person": pk.person(b, pid),
                        "full_name": b.people[pid].name,
                        "change": "has resigned",
                        "last_day": fmt_date(date.fromisoformat(ff["end_date"])),
                    }
                )
            elif v["kind"] == "employee_joined" and w0.date() <= eff <= d:
                people_notes.append(
                    {
                        "person": pk.person(b, pid),
                        "full_name": b.people[pid].name,
                        "change": f"joined as {ff.get('role')}",
                        "date": fmt_date(eff),
                    }
                )
            elif v["kind"] == "employee_left" and w0.date() <= eff <= d:
                people_notes.append(
                    {
                        "person": pk.person(b, pid),
                        "full_name": b.people[pid].name,
                        "change": "left the company",
                        "date": fmt_date(eff),
                    }
                )
            elif (
                v["kind"] in ("leave_booked", "leave_started")
                and ff.get("end_date")
                and eff <= d + timedelta(days=4)
                and date.fromisoformat(ff["end_date"]) >= d
                and b.people[pid].role not in ("sales", "marketing", "finance", "ceo")
            ):
                people_notes.append(
                    {
                        "person": pk.person(b, pid),
                        "full_name": b.people[pid].name,
                        "change": "on holiday",
                        "from": fmt_date(eff),
                        "to": fmt_date(date.fromisoformat(ff["end_date"])),
                    }
                )
        if people_notes:
            facts["people"] = people_notes[:5]
            pk.need("person_change", people_notes[0]["person"])
            disc.append("person_change")
        pk.facts = facts
        pk.slot(
            "summary", "Where the sprint stands: progress last week and the planned load.", (35, 110), req_sum
        )
        pk.slot(
            "discussion",
            "What the team discussed: blockers, incidents of last week, people changes, "
            "and who picks up what. Mention tickets only by the ids given.",
            (40, 170),
            disc,
        )
        # action items: blocked tickets, open urgent bugs, stale reviews, then the oldest work in progress
        cands: list[tuple[str, str]] = [(k, "Unblock") for k, _ in blocked]
        cands += [(k, "Ship the fix for") for k in sorted(bugs)]
        cands += [(k, "Finish the review of") for _, k in sorted(stale)]
        tasks = []
        for key, verb in cands:
            if len(tasks) >= 4:
                break
            if key in ctx.linked:
                continue
            it = b.issues[key]
            who = ctx.assignee_at(it, start)
            if not who or not b.employed(who, d):
                continue
            pk.person(b, who)
            tasks.append(
                {
                    "assignee": who,
                    "text": f"{verb} {key}: {it.last['fields']['summary']}",
                    "key": key,
                    "state": "TODO",
                }
            )
        props = [
            {"label": "Type", "text": "Weekly delivery sync"},
            {"label": "Date", "text": when_text(b.local(start), b.local(end))},
            {"label": "Attendees", "mentions": attendees},
        ]
        if sp:
            props.append({"label": "Period", "text": sp["name"]})
        bullets = [f"Done last week: {facts['last_week']['tickets_done']} ({pts} story points)"]
        bullets += [f"{k}: {v}" for k, v in facts["open_now"].items()]
        blocks = [
            {"h": "Summary"},
            {"slot": "summary"},
            {"h": "Numbers"},
            {"bullets": bullets},
            {"h": "Discussion"},
            {"slot": "discussion"},
        ]
        if tasks:
            blocks += [{"h": "Action items"}, {"tasks": tasks}]
        art = {
            "id": aid,
            "type": "weekly_sync",
            "trigger": {"week": f"{week[0]}-W{week[1]:02d}"},
            "start": iso(start),
            "end": iso(end),
            "publish": iso(publish),
            "page": {
                "space": "DEL",
                "title": f"Delivery sync: {fmt_date(start)}",
                "labels": ["meeting-notes", "delivery-sync"],
                "author": author,
                "properties": props,
                "blocks": blocks,
            },
            "packet": pk.to_json(),
            "links": [],
            "implicit": [],
            "load": sp.get("load") if sp else None,
        }
        for t in tasks:
            add_link(ctx, art, t["key"], "informed by", publish)
        ctx.artifacts.append(art)


# ================================================================================================ leadership
def dso_at(b: Base, t: datetime) -> float | None:
    d = b.local(t).date()
    receivable = rev90 = 0.0
    for vs in b.records("invoice").values():
        cur = None
        for v in vs:
            if parse_time(v["ts"]) <= t:
                cur = v
        if not cur or cur["fields"].get("direction") != "receivable":
            continue
        f = cur["fields"]
        issued = date.fromisoformat(f["issued_at"])
        if issued > d:
            continue
        if f.get("status") != "paid":
            receivable += f["amount"]
        if (d - issued).days <= 90:
            rev90 += f["amount"]
    return round(receivable / (rev90 / 90), 0) if rev90 else None


def cash_at(b: Base, t: datetime) -> float | None:
    best = None
    for r in b.dataset("bank"):
        if parse_time(r["ts"]) <= t:
            best = float(r["balance_after"])
    return best


def arr_at(b: Base, d: date) -> tuple[float, int] | None:
    rows = [r for r in b.dataset("arr_daily") if r["date"] <= d.isoformat()]
    return (float(rows[-1]["arr"]), int(rows[-1]["active_clients"])) if rows else None


def contract_events(b: Base, kinds: tuple[str, ...], t0: datetime, t1: datetime) -> list[dict[str, Any]]:
    out = []
    for (ot, _), lines in b.sim.items():
        if ot != "contract":
            continue
        for ln in lines:
            p = ln["payload"]
            if p["kind"] in kinds and t0 <= parse_time(p["ts"]) < t1:
                out.append(p)
    return sorted(out, key=lambda p: p["ts"])


def nps_rows(b: Base, t0: datetime, t1: datetime) -> list[dict[str, Any]]:
    """NPS responses received in [t0, t1) (empty when the history has no survey)."""
    out = []
    for vs in b.records("nps_response").values():
        v = vs[0]
        ts = parse_time(v["ts"])
        if t0 <= ts < t1:
            f = v["fields"]
            out.append(
                {
                    "id": v["entity_id"],
                    "client": f.get("client"),
                    "contact": f.get("contact"),
                    "score": int(f["score"]),
                    "category": f.get("category"),
                    "survey": f.get("survey"),
                    "at": ts,
                }
            )
    return sorted(out, key=lambda r: r["at"])


def nps_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    pro = sum(1 for r in rows if r["score"] >= 9)
    det = sum(1 for r in rows if r["score"] <= 6)
    return {
        "responses": fmt_count(n, "response"),
        "promoters": pro,
        "detractors": det,
        "nps": round(100 * (pro - det) / n),
        "nps_meaning": "per cent promoters minus per cent detractors",
    }


def plan_leadership(ctx: Ctx) -> None:
    b = ctx.base
    ceo, fin, head = b.head("ceo"), b.head("finance"), b.head("delivery")
    months = []
    m = date(b.start.year, b.start.month, 1)
    while m < b.anchor:
        nxt = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
        last = nxt - timedelta(days=1)
        fri = last - timedelta(days=(last.weekday() - 4) % 7)
        day = b.workday_on_or_before(fri)
        if (day - b.start).days >= 3 and day < b.anchor:
            months.append({"month": m, "day": day, "decision": None})
        m = nxt
    decisions = b.records("decision")
    for did in sorted(decisions):
        f = decisions[did][0]["fields"]
        dd = b.local(parse_time(f["decided_at"])).date()
        near = min(months, key=lambda x: abs((x["day"] - dd).days), default=None)
        if near is not None and abs((near["day"] - dd).days) <= 14 and near["decision"] is None:
            near["decision"] = did
        else:
            months.append({"month": date(dd.year, dd.month, 1), "day": dd, "decision": did, "extra": True})
    for mo in sorted(months, key=lambda x: x["day"]):
        did = mo["decision"]
        if did:
            df = decisions[did][0]["fields"]
            dt = parse_time(df["decided_at"])
            end = dt - timedelta(minutes=10)
            start = end - timedelta(minutes=60)
            floor = b.at(b.local(dt).date(), 8, 30)
            if start < floor:
                start, end = floor, dt - timedelta(minutes=5)
            publish = dt + timedelta(minutes=35 + _h(did) % 40)
            d = b.local(start).date()
        else:
            d = mo["day"]
            for _ in range(5):
                if b.present(ceo, d):
                    break
                d = b.workday_on_or_before(d - timedelta(days=1))
            start = b.at(d, 16, 0)
            end = start + timedelta(minutes=60)
            publish = end + timedelta(minutes=30 + _h(d) % 30)
        if publish >= ctx.limit or d < b.start:
            continue
        attendees = present_list(b, [ceo, fin, head], d)
        if not attendees:
            continue
        author = fin if fin in attendees else attendees[0]
        label = f"{mo['month']:%B %Y}"
        aid = f"lr-{mo['month']:%Y-%m}" + ("-x" if mo.get("extra") else "")
        pk = Packet(
            aid,
            "monthly leadership review notes (management-only Confluence page)",
            f"Notes of the leadership review for {label} of {b.company}, held on {fmt_date(start)} "
            "by the CEO, "
            f"the Head of Finance and the Head of Delivery; written by {b.people[author].name}.",
            banned=list(ctx.banned),
        )
        for p in attendees:
            pk.person(b, p)
        p0 = max(datetime.combine(mo["month"], datetime.min.time(), b.tz), b.at(b.start, 0, 0))
        arr = arr_at(b, d)
        prev = arr_at(b, p0.date() - timedelta(days=1)) or arr_at(b, b.start)
        cash = cash_at(b, start)
        dso = dso_at(b, start)
        facts: dict[str, Any] = {
            "review": {
                "month": label,
                "date": fmt_date(start),
                "period": f"{fmt_date(p0)} to {fmt_date(start)}",
            }
        }
        fin_f: dict[str, Any] = {}
        if arr:
            fin_f["arr"] = pk.need("arr", fmt_money(arr[0]), money_forms(fmt_money(arr[0])))
            fin_f["active_clients"] = pk.need(
                "clients", fmt_count(arr[1], "active client"), count_forms(fmt_count(arr[1], "active client"))
            )
            if prev:
                fin_f["arr_at_period_start"] = fmt_money(prev[0])
                a1, a0 = fmt_money(arr[0]), fmt_money(prev[0])
                fin_f["arr_change"] = "flat" if a1 == a0 else "up" if arr[0] > prev[0] else "down"
        if cash is not None:
            fin_f["cash"] = pk.need("cash", fmt_money(cash), money_forms(fmt_money(cash)))
        if dso is not None:
            fin_f["dso"] = pk.need("dso", f"{int(dso)} days")
            fin_f["dso_meaning"] = (
                "days sales outstanding: open receivables over the average daily billing of 90 days"
            )
        facts["finance"] = fin_f
        notices = contract_events(b, ("notice_given",), p0, start)
        churn = [
            {
                "client": pk.org(b, n["fields"]["client"]),
                "service_ends": fmt_date(date.fromisoformat(n["fields"]["end_date"])),
            }
            for n in notices
        ]
        facts["churn_notices"] = {
            "count": pk.need(
                "notices",
                fmt_count(len(churn), "churn notice"),
                count_forms(fmt_count(len(churn), "churn notice")),
            ),
            "clients": churn[:6],
        }
        ups = contract_events(b, ("contract_amended_upsell",), p0, start)
        downs = contract_events(b, ("contract_amended_downgrade",), p0, start)
        signed = contract_events(b, ("contract_signed",), p0, start)
        facts["commercial"] = {
            "upsells": [
                {"client": pk.org(b, u["fields"]["client"]), "change": u["fields"].get("last_amendment")}
                for u in ups
            ][:4],
            "downgrades": [{"client": pk.org(b, u["fields"]["client"])} for u in downs][:4],
            "new_contracts": [{"client": pk.org(b, u["fields"]["client"])} for u in signed][:4],
        }
        inc_list = []
        for iid, vs in b.records("incident").items():
            v0 = vs[0]
            det = parse_time(v0["fields"]["detected_at"])
            if p0 <= det < start:
                inc_list.append((iid, v0["fields"]["severity"]))
        major = sorted(i for i, s in inc_list if s in ("SEV1", "SEV2"))
        facts["incidents"] = {
            "count": pk.need(
                "incidents",
                fmt_count(len(inc_list), "incident"),
                count_forms(fmt_count(len(inc_list), "incident")),
            ),
            "major_incidents": major,
        }
        nps = nps_rows(b, p0, start)
        if nps:
            facts["nps_survey"] = nps_summary(nps)
        hiring: dict[str, Any] = {"open_requisitions": [], "joined": [], "resigned": [], "left": []}
        for vs in b.records("requisition").values():
            v = None
            for x in vs:
                if parse_time(x["ts"]) <= start:
                    v = x
            if v and v["fields"].get("status") not in ("filled", "closed", "cancelled"):
                hiring["open_requisitions"].append(
                    {"role": v["fields"]["role"], "status": v["fields"]["status"]}
                )
        for vs in b.records("employee_event").values():
            for v in vs:
                ts = parse_time(v["ts"])
                if not (p0 <= ts < start):
                    continue
                ff = v["fields"]
                pid = ff.get("employee")
                if pid not in b.people:
                    continue
                if v["kind"] == "employee_joined":
                    hiring["joined"].append(
                        {
                            "person": pk.person(b, pid),
                            "full_name": b.people[pid].name,
                            "role": ff.get("role"),
                            "date": fmt_date(date.fromisoformat(ff["effective_date"])),
                        }
                    )
                elif v["kind"] == "resignation_received":
                    hiring["resigned"].append(
                        {
                            "person": pk.person(b, pid),
                            "full_name": b.people[pid].name,
                            "role": ff.get("role"),
                            "last_day": fmt_date(date.fromisoformat(ff["end_date"])),
                        }
                    )
                elif v["kind"] == "employee_left":
                    hiring["left"].append({"person": pk.person(b, pid), "full_name": b.people[pid].name})
        facts["people"] = hiring
        report = None
        for ln in b.pages:
            pl = ln["payload"]
            if parse_time(ln["at"]) <= start and any(x["name"] == "report" for x in pl["labels"]["results"]):
                report = ln
        if report:
            facts["based_on"] = report["payload"]["title"]
        sum_req = [k for k in ("arr", "cash", "clients") if k in pk.required]
        disc_req = [k for k in ("dso", "notices", "incidents") if k in pk.required]
        decision = None
        if did:
            df = decisions[did][-1]["fields"]
            facts["decision"] = {"statement": df["statement"], "owner": pk.person(b, df["owner"])}
            decision = {"record": did, "statement": df["statement"], "owner": df["owner"]}
        pk.facts = facts
        pk.slot(
            "summary", "The state of the company this month in two to four sentences.", (35, 120), sum_req
        )
        pk.slot(
            "discussion",
            "What the leaders discussed: receivables and DSO, churn notices, incidents, hiring.",
            (50, 190),
            disc_req,
        )
        pk.slot(
            "notes",
            "Things the leaders keep watching. These are notes, not decisions: say 'we keep "
            "monitoring' or 'we will look again', never 'we decided'.",
            (15, 80),
        )
        if decision:
            pk.need("statement", decision["statement"], [decision["statement"].rstrip(".")])
        tasks = []
        for key in sorted(b.issues, key=lambda k: b.issues[k].first_at):
            if len(tasks) >= 4:
                break
            it = b.issues[key]
            if not key.startswith("BIZ-") or it.first_at > start or key in ctx.linked:
                continue
            p = it.at(start)
            if p["fields"]["status"]["name"] in OPEN or not p["fields"]["summary"].startswith(LEADER_TICKETS):
                continue
            who = ctx.assignee_at(it, start)
            if not who or not b.employed(who, d):
                continue
            pk.person(b, who)
            tasks.append(
                {
                    "assignee": who,
                    "text": f"Follow up {key}: {p['fields']['summary']}",
                    "key": key,
                    "state": "TODO",
                }
            )
        pk.facts["open_business_tickets"] = [
            {"ticket": t["key"], "owner": pk.people[t["assignee"]]["first"]} for t in tasks
        ]
        props = [
            {"label": "Type", "text": "Leadership review"},
            {"label": "Date", "text": when_text(b.local(start), b.local(end))},
            {"label": "Attendees", "mentions": attendees},
            {"label": "Period", "text": label},
        ]
        if report:
            props.append(
                {
                    "label": "Based on",
                    "links": [
                        [report["payload"]["title"], b.page_url(report["container"], report["external_id"])]
                    ],
                }
            )
        rows = [
            ["ARR", fin_f.get("arr", "-")],
            ["Active clients", str(arr[1]) if arr else "-"],
            ["Cash", fin_f.get("cash", "-")],
            ["DSO", fin_f.get("dso", "-")],
            ["Churn notices", str(len(churn))],
            ["Incidents", str(len(inc_list))],
            ["Open requisitions", str(len(hiring["open_requisitions"]))],
        ]
        blocks = [
            {"h": "Summary"},
            {"slot": "summary"},
            {"h": "Metrics"},
            {"table": rows},
            {"h": "Discussion"},
            {"slot": "discussion"},
            {"h": "Notes"},
            {"slot": "notes"},
        ]
        if decision:
            blocks += [
                {"h": "Decisions"},
                {"decisions": [{"text": decision["statement"], "owner": decision["owner"]}]},
            ]
        if tasks:
            blocks += [{"h": "Action items"}, {"tasks": tasks}]
        art = {
            "id": aid,
            "type": "leadership_review",
            "trigger": {"month": f"{mo['month']:%Y-%m}", "decision": did},
            "start": iso(start),
            "end": iso(end),
            "publish": iso(publish),
            "page": {
                "space": "MGMT",
                "title": f"Leadership review: {label}",
                "labels": ["meeting-notes", "leadership-review"],
                "author": author,
                "properties": props,
                "blocks": blocks,
            },
            "packet": pk.to_json(),
            "links": [],
            "implicit": [],
            "decision": decision,
        }
        if decision:
            link_decision_tickets(ctx, art, did, parse_time(decisions[did][0]["fields"]["decided_at"]))
        for t in tasks:
            add_link(ctx, art, t["key"], "informed by", publish)
        ctx.artifacts.append(art)


def link_decision_tickets(ctx: Ctx, rv: dict[str, Any], did: str, dt: datetime) -> None:
    """Tickets that carry out a hiring decision: the hiring ticket and the onboarding of the hire."""
    b = ctx.base
    for r in b.records("requisition").values():
        if not any(c["type"] == "decision" and c["id"] == did for c in r[0].get("causes") or []):
            continue
        role = r[0]["fields"].get("role")
        rid = r[0].get("entity_id")
        hire = next(
            (
                e[0]["fields"].get("employee_name")
                for e in b.records("employee_event").values()
                if e[0]["kind"] == "employee_joined" and any(c["id"] == rid for c in e[0].get("causes") or [])
            ),
            None,
        )
        for key, it in sorted(b.issues.items()):
            s = it.last["fields"]["summary"]
            if (role and s == f"Hiring: {role}" and it.first_at >= dt) or (
                hire and s == f"Onboarding: {hire}"
            ):
                add_link(ctx, rv, key, "implements", parse_time(rv["publish"]), frag="dec-1")


# ================================================================================================ decisions
def plan_decisions(ctx: Ctx) -> None:
    """A rationale version of each base decision, after its leadership review notes."""
    b = ctx.base
    reviews = {
        a["decision"]["record"]: a
        for a in ctx.artifacts
        if a["type"] == "leadership_review" and a.get("decision")
    }
    syncs = sorted((a for a in ctx.artifacts if a["type"] == "weekly_sync"), key=lambda a: a["publish"])
    for did, vs in sorted(b.records("decision").items()):
        rv = reviews.get(did)
        if rv is None:
            continue
        f = vs[-1]["fields"]
        dt = parse_time(f["decided_at"])
        at = first_workday_time(b, parse_time(rv["publish"]) + timedelta(minutes=50 + _h(did) % 40))
        owner = f.get("owner")
        if owner and b.absent(owner, at.date()):
            continue
        if at >= ctx.limit:
            continue
        ee_id = next((c["id"] for c in vs[-1].get("causes") or [] if c["type"] == "life_event"), None)
        ee = b.record_at("employee_event", ee_id, dt) if ee_id else None
        le_id = next((c["id"] for c in (ee or {}).get("causes") or [] if c["type"] == "life_event"), None)
        le = b.record_at("life_event", le_id, dt) if le_id else None
        sync = None
        for a in syncs:
            if parse_time(a["publish"]) <= dt and a.get("load") is not None:
                sync = a
        pk = Packet(
            f"dr-{did}",
            "decision rationale (one paragraph in a business record)",
            f"The rationale field of the decision record {did} of {b.company}, written by the CEO on "
            f"{fmt_date(at)} after the leadership review of {fmt_date(parse_time(rv['start']))}.",
            banned=list(ctx.banned),
        )
        pk.voice = "the CEO, in the first person plural ('we')"
        facts: dict[str, Any] = {
            "decision": {
                "id": did,
                "statement": f["statement"],
                "decided": fmt_date(b.local(dt)),
                "owner": pk.person(b, owner) if owner else None,
            }
        }
        req = []
        pk.need(
            "meeting",
            fmt_date(b.local(parse_time(rv["start"]))),
            date_forms(b.local(parse_time(rv["start"]))),
        )
        facts["meeting"] = {"what": "leadership review", "date": pk.required["meeting"]["text"]}
        req.append("meeting")
        if ee:
            ef = ee["fields"]
            pid = ef.get("employee")
            facts["resignation"] = {
                "person": pk.person(b, pid) if pid in b.people else ef.get("employee_name"),
                "full_name": ef.get("employee_name"),
                "role": ef.get("role"),
                "notice_received": fmt_date(b.local(parse_time(ee["ts"]))),
                "last_day": pk.need(
                    "last_day",
                    fmt_date(date.fromisoformat(ef["end_date"])),
                    date_forms(date.fromisoformat(ef["end_date"])),
                ),
            }
            pk.need(
                "leaver",
                ef.get("employee_name"),
                [ef.get("employee_name"), pk.people.get(pid, {}).get("first", "")],
            )
            req += ["leaver", "last_day"]
        if le and le["fields"].get("load_recent") is not None:
            facts["resignation"]["recent_team_load"] = f"{le['fields']['load_recent']:.2f}"
        if sync:
            facts["load_evidence"] = {
                "source": f"weekly delivery sync of {sync['packet']['facts']['meeting']['date']}",
                "planned_sprint_load": pk.need("load", f"{sync['load']:.2f}"),
                "load_meaning": "planned work over capacity; 1.00 is full",
            }
            req.append("load")
        pk.facts = facts
        pk.slot(
            "rationale",
            "Why the company decided this: the resignation, the team load, and where it was "
            "decided. Plain, factual, first person plural.",
            (35, 110),
            req,
        )
        art = {
            "id": pk.id,
            "type": "decision_rationale",
            "trigger": {
                "decision": did,
                "review": rv["id"],
                "sync": sync["id"] if sync else None,
                "life_event": le_id,
            },
            "start": iso(at),
            "end": iso(at),
            "publish": iso(at),
            "record": {"id": did, "statement": f["statement"]},
            "packet": pk.to_json(),
            "links": [],
            "implicit": [],
        }
        ctx.artifacts.append(art)


# ================================================================================================ QBR
def quarter_of(d: date) -> tuple[date, date, str]:
    q = (d.month - 1) // 3
    s = date(d.year, 3 * q + 1, 1)
    e = date(d.year + (q == 3), (3 * q + 3) % 12 + 1, 1) - timedelta(days=1)
    return s, e, f"Q{q + 1} {d.year}"


def plan_qbrs(ctx: Ctx) -> None:
    b = ctx.base
    impact = b.dataset("incident_impact")
    sup_opened: list[tuple[str, datetime]] = []
    for (ot, _), lines in b.sim.items():
        if ot == "support_ticket":
            p = lines[0]["payload"]
            sup_opened.append(
                (p["fields"].get("client"), parse_time(p["fields"].get("opened_at") or p["ts"]))
            )
    clients = {c["id"]: c for c in b.dataset("clients")}
    contract_of: dict[str, str] = {}
    for (ot, rid), lines in sorted(b.sim.items()):
        if ot == "contract":
            contract_of.setdefault(lines[0]["payload"]["fields"]["client"], rid)
    head, pm = b.head("delivery"), b.head("pm")
    q = quarter_of(b.start)
    quarters = []
    while q[1] < b.anchor:
        quarters.append(q)
        q = quarter_of(q[1] + timedelta(days=1))
    for n, cid in enumerate(ctx.key_clients):
        am = clients[cid]["account_manager"]
        for qs, qe, ql in quarters:
            d = meeting_day(b, b.workday_on_or_after(qe + timedelta(days=9 + 2 * n)), [am])
            if d is None:
                continue
            start = b.at(d, 14, 0)
            end = start + timedelta(minutes=60)
            publish = end + timedelta(minutes=40 + _h(cid, ql) % 40)
            if publish >= ctx.limit:
                continue
            ctr_id = contract_of.get(cid)
            ctr = b.record_at("contract", ctr_id, start) if ctr_id else None
            if not ctr or ctr["fields"].get("status") not in ("active",) or ctr["fields"].get("end_date"):
                continue
            cf = ctr["fields"]
            p0 = max(datetime.combine(qs, datetime.min.time(), b.tz), b.at(b.start, 0, 0))
            p1 = datetime.combine(qe + timedelta(days=1), datetime.min.time(), b.tz)
            delivery = head if b.present(head, d) else pm
            contacts = b.contacts_of(cid)[:2]
            attendees = present_list(b, [am, delivery], d)
            aid = f"qbr-{cid}-{ql.replace(' ', '')}"
            pk = Packet(
                aid,
                "quarterly business review notes (internal Confluence page about a client meeting)",
                f"Notes of the quarterly business review of {ql} with the client {b.org_name(cid)}, held on "
                f"{fmt_date(start)}; written by {b.people[am].name}, the account manager, for colleagues.",
                banned=list(ctx.banned),
            )
            for p in attendees:
                pk.person(b, p)
            client = pk.org(b, cid)
            for c in contacts:
                pk.contact(b, c.id)
            pk.need("client", client)
            pk.need("quarter", ql)
            acv = pk.need("acv", fmt_money(float(cf["amount"])), money_forms(fmt_money(float(cf["amount"]))))
            sla = pk.need("sla", fmt_pct(float(cf.get("sla_target", 0.995))))
            facts: dict[str, Any] = {
                "client": client,
                "quarter": ql,
                "period": f"{fmt_date(p0)} to {fmt_date(qe)}",
                "meeting_date": fmt_date(start),
                "client_attendees": [f"{c.name} ({c.role})" for c in contacts],
                "contract": {
                    "number": cf.get("number"),
                    "annual_value": acv,
                    "billing": cf.get("billing_frequency"),
                    "sla_target": sla,
                    "term_end": fmt_date(date.fromisoformat(cf["term_end"])) if cf.get("term_end") else None,
                },
            }
            incs = [
                r for r in impact if r["client"] == cid and p0.date() <= date.fromisoformat(r["date"]) <= qe
            ]
            down = sum(float(r["downtime_minutes"]) for r in incs)
            minutes = (p1 - p0).total_seconds() / 60
            avail = 1 - down / minutes
            inc_rows = []
            for r in incs:
                v = b.record_at("incident", r["incident"], start)
                inc_rows.append(
                    {
                        "date": fmt_date(date.fromisoformat(r["date"])),
                        "severity": r["severity"],
                        "title": v["fields"]["title"] if v else "",
                        "downtime": fmt_minutes(float(r["downtime_minutes"])),
                    }
                )
            service: dict[str, Any] = {
                "incidents": pk.need(
                    "incidents",
                    fmt_count(len(incs), "incident") if incs else "no incidents",
                    count_forms(fmt_count(len(incs), "incident"))
                    if incs
                    else ["no incidents", "no incident"],
                ),
                "incident_list": inc_rows,
                "availability": pk.need("availability", fmt_pct(avail, 3 if avail < 1 else 1)),
                "sla_met": "yes" if avail >= float(cf.get("sla_target", 0.995)) else "no",
            }
            svc_req = ["incidents", "availability", "sla"]
            if down:
                service["total_downtime"] = pk.need(
                    "downtime", fmt_minutes(down), minute_forms(fmt_minutes(down))
                )
                svc_req.append("downtime")
            sup = sum(1 for c, t in sup_opened if c == cid and p0 <= t < p1)
            service["support_tickets_opened"] = fmt_count(sup, "support ticket")
            facts["service"] = service
            nps = [r for r in nps_rows(b, p0, start) if r["client"] == cid]
            if nps:
                facts["nps"] = [
                    {
                        "contact": pk.contact(b, r["contact"]) if r["contact"] in b.contacts else None,
                        "score": r["score"],
                        "category": r["category"],
                        "survey": r["survey"],
                    }
                    for r in nps
                ]
            com: dict[str, Any] = {}
            com_req: list[str] = []
            evs = contract_events(
                b,
                (
                    "contract_renewed",
                    "contract_amended_upsell",
                    "contract_amended_downgrade",
                    "implementation_completed",
                ),
                p0,
                min(p1, start),
            )
            evs = [e for e in evs if e["fields"]["client"] == cid]
            for e in evs:
                ef = e["fields"]
                if e["kind"] == "contract_renewed":
                    com["renewal"] = {
                        "renewed": pk.need(
                            "renewed",
                            fmt_date(date.fromisoformat(ef["renewed_at"])),
                            date_forms(date.fromisoformat(ef["renewed_at"])),
                        ),
                        "uplift": fmt_pct(float(ef.get("uplift", 0)), 1),
                        "new_term_end": fmt_date(date.fromisoformat(ef["term_end"])),
                    }
                    com_req.append("renewed")
                elif e["kind"] == "contract_amended_upsell":
                    com["upsell"] = {
                        "change": ef.get("last_amendment"),
                        "annual_value_after": fmt_money(float(ef["amount"])),
                    }
                    pk.need("upsell", ef.get("last_amendment", "").split(":")[-1].split(" from ")[0].strip())
                    com_req.append("upsell")
                elif e["kind"] == "contract_amended_downgrade":
                    com["downgrade"] = {
                        "change": ef.get("last_amendment"),
                        "annual_value_after": fmt_money(float(ef["amount"])),
                    }
                elif e["kind"] == "implementation_completed":
                    gl = date.fromisoformat(ef["go_live"])
                    com["go_live"] = {
                        "date": pk.need("go_live", fmt_date(gl), date_forms(gl)),
                        "late_days": int(ef.get("go_live_late_days") or 0),
                    }
                    com_req.append("go_live")
            epics = []
            for key, it in b.issues.items():
                if not key.startswith("IMPL-") or it.first_at > start:
                    continue
                p = it.at(start)
                if (
                    p["fields"]["issuetype"]["name"] != "Epic"
                    or p["fields"].get("customfield_10300") != client
                ):
                    continue
                st = p["fields"]["status"]["name"]
                if st in OPEN and it.done_at() and it.done_at() < p0:
                    continue
                epics.append({"epic": key, "summary": p["fields"]["summary"], "status": st})
            if epics:
                com["implementations"] = epics
            if not any(k in com for k in ("renewal", "upsell", "downgrade", "go_live")):
                com["contract_changes"] = "none in this quarter"
            facts["commercial"] = com
            tasks = []
            for key, it in sorted(b.issues.items(), key=lambda x: x[1].first_at):
                if len(tasks) >= 3:
                    break
                if key in ctx.linked or it.first_at > start:
                    continue
                p = it.at(start)
                f = p["fields"]
                if f["status"]["name"] in OPEN or f["issuetype"]["name"] == "Epic":
                    continue
                if not (f.get("customfield_10300") == client or client in f["summary"]):
                    continue
                who = ctx.assignee_at(it, start)
                if not who or not b.employed(who, d):
                    continue
                pk.person(b, who)
                tasks.append(
                    {"assignee": who, "text": f"Follow up {key}: {f['summary']}", "key": key, "state": "TODO"}
                )
            facts["open_tickets"] = [
                {"ticket": t["key"], "owner": pk.people[t["assignee"]]["first"]} for t in tasks
            ]
            pk.facts = facts
            pk.slot(
                "summary",
                "The purpose and the overall picture of the review.",
                (35, 120),
                ["client", "quarter", "acv"],
            )
            pk.slot(
                "service",
                "Service in the quarter: incidents that affected the client, downtime against the SLA, "
                "support load.",
                (30, 150),
                svc_req,
            )
            pk.slot(
                "commercial",
                "Contract and delivery: renewal, upsell or downgrade, implementation status, what the "
                "client asked for next. Only the facts given.",
                (20, 130),
                com_req[:3],
            )
            props = [
                {"label": "Type", "text": "Quarterly business review"},
                {"label": "Date", "text": when_text(b.local(start), b.local(end))},
                {
                    "label": "Attendees",
                    "mentions": attendees,
                    "extra": ", ".join(f"{c.name} ({client})" for c in contacts),
                },
                {"label": "Client", "text": client},
                {"label": "Period", "text": ql},
            ]
            blocks = [
                {"h": "Summary"},
                {"slot": "summary"},
                {"h": "Service"},
                {"slot": "service"},
                {"h": "Commercial"},
                {"slot": "commercial"},
            ]
            if tasks:
                blocks += [{"h": "Action items"}, {"tasks": tasks}]
            art = {
                "id": aid,
                "type": "qbr",
                "trigger": {"client": cid, "quarter": ql},
                "start": iso(start),
                "end": iso(end),
                "publish": iso(publish),
                "page": {
                    "space": "ACCT",
                    "title": f"QBR {ql}: {client}",
                    "labels": ["meeting-notes", "qbr"],
                    "author": am,
                    "properties": props,
                    "blocks": blocks,
                },
                "packet": pk.to_json(),
                "links": [],
                "implicit": [],
            }
            for t in tasks:
                add_link(ctx, art, t["key"], "informed by", publish)
            ctx.artifacts.append(art)


# ================================================================================================ escalations
def _send_time(b: Base, pid: str | None, after: datetime, hh: int, mm: int, gap_days: int = 0) -> datetime:
    """A working-hours time on or after ``after`` (+ gap days) when ``pid`` (an employee) is present."""
    d = b.local(after).date()
    if gap_days:
        d = b.next_workday(d, gap_days)
    t = max(b.at(b.workday_on_or_after(d), hh, mm), after + timedelta(minutes=20))
    t = first_workday_time(b, t, hh, mm)
    for _ in range(15):
        if pid is None or b.present(pid, t.date()):
            return t
        t = b.at(b.next_workday(t.date()), hh, mm)
    return t


def plan_escalations(ctx: Ctx) -> None:
    b = ctx.base
    head = b.head("delivery")
    pms = {a["trigger"]["incident"]: a for a in ctx.artifacts if a["type"] == "postmortem"}
    impact = defaultdict(dict)
    for r in b.dataset("incident_impact"):
        impact[r["incident"]][r["client"]] = float(r["downtime_minutes"])
    notices = [m for m in b.mails.values() if m["payload"]["text_slot"].endswith(":incident_notice")]
    for iid, vs in sorted(b.records("incident").items()):
        f = vs[-1]["fields"]
        if f.get("severity") not in ("SEV1", "SEV2"):
            continue
        started = b.local(parse_time(f["started_at"]))
        subject = f"Service incident on {started:%d %B}: resolved"
        pick = None
        for cid in f.get("affected_clients") or []:
            if cid not in ctx.key_clients:
                continue
            dom = (b.orgs.get(cid) or {}).get("domain")
            for m in notices:
                p = m["payload"]
                to = p["toRecipients"][0]["emailAddress"]["address"]
                if p["subject"] == subject and to.endswith("@" + str(dom)):
                    pick = (cid, m)
                    break
            if pick:
                break
        if not pick:
            continue
        cid, notice = pick
        np_ = notice["payload"]
        contact = next(
            c for c in b.contacts_of(cid) if c.email == np_["toRecipients"][0]["emailAddress"]["address"]
        )
        am_addr = np_["from"]["emailAddress"]["address"]
        am = next(pid for pid, p in b.people.items() if p.email == am_addr)
        sent0 = parse_time(np_["sentDateTime"])
        pm_art = pms.get(iid)
        down = impact[iid].get(cid, f.get("downtime_minutes", 0))
        aid = f"esc-{iid}-{cid}"
        pk = Packet(
            aid,
            "e-mail thread between a client and Larchwood Systems after a service incident",
            f"A client of {b.company} writes back after the incident notice of {fmt_date(b.local(sent0))}; "
            f"each field is one e-mail of the thread, in order.",
            banned=list(ctx.banned),
        )
        cname = pk.contact(b, contact.id)
        client = pk.org(b, cid)
        amf = pk.person(b, am)
        hf = pk.person(b, head)
        day_phrase = pk.need(
            "outage",
            f"the outage on {fmt_day(started)}",
            [f"the outage on {fmt_day(started)}", f"the outage on {started:%B} {started.day}"],
        )
        dmin = pk.need("client_downtime", fmt_minutes(down), minute_forms(fmt_minutes(down)))
        facts: dict[str, Any] = {
            "client": client,
            "client_contact": cname,
            "account_manager": amf,
            "head_of_delivery": hf,
            "incident": {
                "date": fmt_date(started),
                "what_users_saw": f["title"].split(": ", 1)[-1],
                "client_downtime": dmin,
                "time_to_restore": fmt_minutes(f.get("restore_minutes", 0)),
                "notice_subject": np_["subject"],
            },
            "sla_target": "99.5%",
        }
        msgs = []
        t1 = _send_time(b, None, sent0, 9, 40 + _h(aid) % 50, gap_days=1)
        msgs.append(
            {"slot": "m1", "from": ("contact", contact.id), "to": [("person", am)], "cc": [], "sent": t1}
        )
        t2 = _send_time(b, am, t1 + timedelta(hours=2, minutes=_h(aid, 2) % 90), 11, 0)
        msgs.append(
            {
                "slot": "m2",
                "from": ("person", am),
                "to": [("contact", contact.id)],
                "cc": [("person", head)],
                "sent": t2,
            }
        )
        pk.slot(
            "m1",
            f"{cname} (the client) replies to the incident notice: the impact on their users, and asks "
            "what caused it and what will stop it happening again. Firm but polite.",
            (40, 120),
            ["outage", "client_downtime"],
        )
        review_req = []
        if pm_art:
            ps = parse_time(pm_art["start"])
            rv = pk.need("review_date", fmt_date(b.local(ps)), date_forms(b.local(ps)))
            facts["post_incident_review"] = {
                "date": rv,
                "status_when_replying": "already held" if ps < t2 else "scheduled",
            }
            review_req = ["review_date"]
        pk.slot(
            "m2",
            f"{amf} (account manager) answers {cname}: apologises, confirms the facts, says what happens "
            "next with the review. No promises beyond the facts.",
            (40, 130),
            review_req,
        )
        if pm_art:
            cause = pm_art["packet"]["facts"]["root_cause"]
            t3 = _send_time(
                b,
                head,
                max(t2, parse_time(pm_art["publish"])) + timedelta(hours=1, minutes=_h(aid, 3) % 60),
                10,
                0,
            )
            if t3 < ctx.limit:
                rc = (
                    "a change we deployed to production"
                    if "deploy" in cause.get("kind", "")
                    else cause["kind"].replace("an outside cause: ", "")
                )
                facts["review_findings"] = {"cause_in_plain_words": pk.need("cause", rc)}
                if cause.get("rolled_back"):
                    facts["review_findings"]["change_rolled_back"] = "yes"
                bug = pm_art["trigger"].get("bug")
                if bug:
                    st = b.issues[bug].status_at(t3)
                    facts["review_findings"]["fix_status"] = (
                        "fixed and deployed" if st in OPEN else "fix in progress"
                    )
                msgs.append(
                    {
                        "slot": "m3",
                        "from": ("person", head),
                        "to": [("contact", contact.id)],
                        "cc": [("person", am)],
                        "sent": t3,
                    }
                )
                pk.slot(
                    "m3",
                    f"{hf} (Head of Delivery) shares the outcome of the post-incident review with {cname}: "
                    "the "
                    "cause in plain words and what the team is doing about it.",
                    (50, 150),
                    ["cause"] + review_req,
                )
                if f["severity"] == "SEV1" or down >= 60:
                    t4 = _send_time(b, None, t3, 10, 15 + _h(aid, 4) % 40, gap_days=1)
                    if t4 < ctx.limit:
                        msgs.append(
                            {
                                "slot": "m4",
                                "from": ("contact", contact.id),
                                "to": [("person", head)],
                                "cc": [("person", am)],
                                "sent": t4,
                            }
                        )
                        pk.slot(
                            "m4",
                            f"{cname} acknowledges the review briefly and says the client will watch the "
                            "service closely.",
                            (15, 70),
                        )
        if msgs[-1]["sent"] >= ctx.limit:
            continue
        pk.facts = facts
        art = {
            "id": aid,
            "type": "escalation",
            "trigger": {"incident": iid, "client": cid, "notice": np_["id"]},
            "start": iso(t1),
            "end": iso(msgs[-1]["sent"]),
            "publish": iso(t1),
            "thread": {
                "conversation": np_["conversationId"],
                "subject": f"Re: {np_['subject']}",
                "parent": np_["internetMessageId"],
                "messages": [
                    {
                        **m,
                        "sent": iso(m["sent"]),
                        "from": list(m["from"]),
                        "to": [list(x) for x in m["to"]],
                        "cc": [list(x) for x in m["cc"]],
                    }
                    for m in msgs
                ],
            },
            "packet": pk.to_json(),
            "links": [],
            "implicit": [
                {"message": 0, "cause": f"sim.{iid}", "predicate": "refers_to", "quote": day_phrase}
            ],
        }
        ctx.artifacts.append(art)


def _thread(conv: str | None, subject: str, parent: str | None, msgs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "conversation": conv,
        "subject": subject,
        "parent": parent,
        "messages": [
            {
                **m,
                "sent": iso(m["sent"]),
                "from": list(m["from"]),
                "to": [list(x) for x in m["to"]],
                "cc": [list(x) for x in m["cc"]],
            }
            for m in msgs
        ],
    }


def plan_late_escalations(ctx: Ctx) -> None:
    """A key client chases a late implementation: due date -> account manager -> go-live confirmation."""
    b = ctx.base
    clients = {c["id"]: c for c in b.dataset("clients")}
    head, pm = b.head("delivery"), b.head("pm")
    for (ot, rid), lines in sorted(b.sim.items()):
        if ot != "contract":
            continue
        for ln in lines:
            p = ln["payload"]
            f = p["fields"]
            late = int(f.get("go_live_late_days") or 0)
            cid = f.get("client")
            if p["kind"] != "implementation_completed" or late <= 0 or cid not in ctx.key_clients:
                continue
            gl = date.fromisoformat(f["go_live"])
            promised = gl - timedelta(days=late)
            live_at = parse_time(p["ts"])
            client = b.org_name(cid)
            epic = None
            for key, it in sorted(b.issues.items(), key=lambda x: x[1].first_at):
                ff = it.last["fields"]
                if (
                    key.startswith("IMPL-")
                    and ff["issuetype"]["name"] == "Epic"
                    and ff.get("customfield_10300") == client
                    and it.first_at <= b.at(promised, 9)
                    and (it.done_at() or live_at) <= live_at + timedelta(hours=1)
                ):
                    epic = key
            if not epic:
                continue
            am = clients[cid]["account_manager"]
            contact = b.contacts_of(cid)[0]
            aid = f"esc-late-{epic}-{cid}"
            t1 = (
                b.at(promised, 16, 20 + _h(aid) % 30)
                if b.cal.is_workday(promised)
                else b.at(b.next_workday(promised), 9, 40)
            )
            t2 = t1 + timedelta(minutes=40 + _h(aid, 2) % 30)
            if b.local(t2).hour >= 18 or not b.present(am, b.local(t2).date()):
                t2 = _send_time(b, am, t1, 9, 15 + _h(aid, 3) % 30, gap_days=1)
            if not (t1 < t2 < live_at - timedelta(minutes=30)):
                continue
            writer = pm if b.present(pm, b.local(live_at).date()) else head
            t3 = _send_time(b, writer, live_at + timedelta(minutes=30 + _h(aid, 4) % 60), 10, 0)
            if t3 >= ctx.limit:
                continue
            it = b.issues[epic]
            feature = it.last["fields"]["summary"].split(": ", 1)[-1].replace(" rollout", "")
            pk = Packet(
                aid,
                "e-mail thread between a client and Larchwood Systems about a late implementation",
                f"A key client of {b.company} chases an implementation that is past its due date; each field "
                f"is one e-mail of the thread, in order.",
                banned=list(ctx.banned),
            )
            cname = pk.contact(b, contact.id)
            pk.org(b, cid)
            amf = pk.person(b, am)
            wf = pk.person(b, writer)
            due = pk.need("due", fmt_date(promised), date_forms(promised))
            feat = pk.need("feature", feature)
            open_tasks = []
            for _key, x in sorted(b.issues.items()):
                px = x.at(t2)
                if (
                    px
                    and (px["fields"].get("parent") or {}).get("key") == epic
                    and px["fields"]["status"]["name"] not in OPEN
                ):
                    open_tasks.append(
                        {"task": px["fields"]["summary"], "status": px["fields"]["status"]["name"]}
                    )
            golive = pk.need("go_live", fmt_date(gl), date_forms(gl))
            pk.need("late", fmt_count(late, "day"), count_forms(fmt_count(late, "day")))
            pk.facts = {
                "client": client,
                "client_contact": cname,
                "account_manager": amf,
                "delivery": wf,
                "implementation": {"what": feat, "due_date": due, "epic": epic},
                "open_tasks_when_the_account_manager_replies": open_tasks[:4],
                "went_live": {"date": golive, "days_after_due_date": pk.required["late"]["text"]},
            }
            not_yet = date_forms(gl) if gl != promised else []
            pk.slot(
                "m1",
                f"{cname} (the client) writes to {amf}: the {feat} work was due on {due} and is not live; "
                "asks for the status. Polite but firm.",
                (35, 110),
                ["due", "feature"],
                banned=not_yet,
            )
            pk.slot(
                "m2",
                f"{amf} (account manager) answers {cname}: apologises, says what is still open, and that "
                f"{wf} will confirm when it is live. No new date.",
                (35, 120),
                [],
                banned=not_yet,
            )
            pk.slot(
                "m3",
                f"{wf} confirms to {cname} that the {feat} work went live, and how late it was.",
                (30, 110),
                ["go_live", "late"],
            )
            msgs = [
                {"slot": "m1", "from": ("contact", contact.id), "to": [("person", am)], "cc": [], "sent": t1},
                {
                    "slot": "m2",
                    "from": ("person", am),
                    "to": [("contact", contact.id)],
                    "cc": [("person", writer)],
                    "sent": t2,
                },
                {
                    "slot": "m3",
                    "from": ("person", writer),
                    "to": [("contact", contact.id)],
                    "cc": [("person", am)],
                    "sent": t3,
                },
            ]
            ctx.artifacts.append(
                {
                    "id": aid,
                    "type": "escalation",
                    "trigger": {"late_implementation": epic, "client": cid, "contract": rid},
                    "start": iso(t1),
                    "end": iso(t3),
                    "publish": iso(t1),
                    "thread": _thread(None, f"{feature}: past the due date", None, msgs),
                    "packet": pk.to_json(),
                    "links": [],
                    "implicit": [
                        {"message": 0, "cause": f"jira.{epic}", "predicate": "refers_to", "quote": feat}
                    ],
                }
            )


def plan_detractor_escalations(ctx: Ctx) -> None:
    """A key client's detractor score -> the account manager's follow-up thread (BIZ follow-up ticket)."""
    b = ctx.base
    head = b.head("delivery")
    impact = b.dataset("incident_impact")
    qbrs = {(a["trigger"]["client"], a["start"]): a for a in ctx.artifacts if a["type"] == "qbr"}
    for r in nps_rows(b, b.at(b.start, 0), ctx.limit):
        cid = r["client"]
        if r["score"] > 6 or cid not in ctx.key_clients or r["contact"] not in b.contacts:
            continue
        client = b.org_name(cid)
        ticket = next(
            (
                k
                for k, it in sorted(b.issues.items(), key=lambda x: x[1].first_at)
                if it.last["fields"]["summary"].startswith(f"NPS follow-up with {client}")
                and it.first_at >= r["at"]
            ),
            None,
        )
        if not ticket:
            continue
        it = b.issues[ticket]
        am = ctx.assignee_at(it, it.first_at + timedelta(minutes=5))
        if not am:
            continue
        aid = f"esc-nps-{r['id']}"
        t1 = _send_time(b, am, it.first_at + timedelta(minutes=50 + _h(aid) % 60), 10, 0)
        t2 = _send_time(b, None, t1, 10, 5 + _h(aid, 2) % 50, gap_days=1)
        t3 = _send_time(b, am, t2 + timedelta(hours=2, minutes=_h(aid, 3) % 60), 14, 0)
        if t3 >= ctx.limit:
            continue
        contact = b.contacts[r["contact"]]
        pk = Packet(
            aid,
            "e-mail thread between an account manager and a client after a low survey score",
            f"The account manager of {b.company} follows up a client's survey answer; each field is one "
            f"e-mail of the thread, in order.",
            banned=list(ctx.banned),
        )
        cname = pk.contact(b, contact.id)
        pk.org(b, cid)
        amf = pk.person(b, am)
        score = pk.need(
            "score", f"a score of {r['score']}", [f"a score of {r['score']}", f"score of {r['score']}"]
        )
        d0 = b.local(r["at"]).date() - timedelta(days=90)
        incs = [
            x
            for x in impact
            if x["client"] == cid and d0 <= date.fromisoformat(x["date"]) <= b.local(r["at"]).date()
        ]
        inc_rows = []
        for x in incs:
            v = b.record_at("incident", x["incident"], r["at"])
            inc_rows.append(
                {
                    "date": fmt_date(date.fromisoformat(x["date"])),
                    "what_users_saw": (v["fields"]["title"].split(": ", 1)[-1] if v else ""),
                    "downtime": fmt_minutes(float(x["downtime_minutes"])),
                }
            )
        facts: dict[str, Any] = {
            "client": client,
            "client_contact": cname,
            "account_manager": amf,
            "survey": {
                "name": r["survey"],
                "answered": fmt_date(b.local(r["at"])),
                "score": score,
                "scale": "0 to 10; 6 or less is a detractor",
            },
            "incidents_affecting_the_client_in_the_90_days_before": inc_rows or "none",
        }
        m2_req: list[str] = []
        if inc_rows:
            pk.need(
                "incidents",
                fmt_count(len(inc_rows), "incident"),
                count_forms(fmt_count(len(inc_rows), "incident")),
            )
            m2_req = ["incidents"]
        nxt = sorted((k[1], a) for k, a in qbrs.items() if k[0] == cid and k[1] > iso(t3))
        m3_req: list[str] = []
        if nxt:
            qd = b.local(parse_time(nxt[0][0]))
            facts["next_quarterly_review"] = pk.need("qbr", fmt_date(qd), date_forms(qd))
            m3_req = ["qbr"]
        pk.facts = facts
        pk.slot(
            "m1",
            f"{amf} thanks {cname} for answering the survey with {score} and asks what would make the "
            "service better. Short and sincere.",
            (30, 100),
            ["score"],
        )
        pk.slot(
            "m2",
            f"{cname} replies to {amf}: explains the score using only the facts given (the incidents, if "
            "any); no new complaints.",
            (30, 110),
            m2_req,
        )
        pk.slot(
            "m3",
            f"{amf} answers {cname}: thanks, takes the points to the delivery team, and names the next "
            "quarterly review if the facts give its date. No promises.",
            (30, 110),
            m3_req,
        )
        msgs = [
            {"slot": "m1", "from": ("person", am), "to": [("contact", contact.id)], "cc": [], "sent": t1},
            {"slot": "m2", "from": ("contact", contact.id), "to": [("person", am)], "cc": [], "sent": t2},
            {
                "slot": "m3",
                "from": ("person", am),
                "to": [("contact", contact.id)],
                "cc": [("person", head)],
                "sent": t3,
            },
        ]
        ctx.artifacts.append(
            {
                "id": aid,
                "type": "escalation",
                "trigger": {"nps_response": r["id"], "client": cid, "ticket": ticket},
                "start": iso(t1),
                "end": iso(t3),
                "publish": iso(t1),
                "thread": _thread(None, "Your feedback in our client survey", None, msgs),
                "packet": pk.to_json(),
                "links": [],
                "implicit": [
                    {"message": 0, "cause": f"sim.{r['id']}", "predicate": "refers_to", "quote": score}
                ],
            }
        )


# ================================================================================================ plan
def assign_ids(ctx: Ctx) -> None:
    """Page ids and mail ids in time order, after the base ids (base pages are 1000001..)."""
    b = ctx.base
    base_pages = {int(ln["external_id"]) for ln in b.pages}
    base_mails = set(b.mails)
    pages = sorted((a for a in ctx.artifacts if a.get("page")), key=lambda a: (a["publish"], a["id"]))
    for n, a in enumerate(pages, start=1):
        pid = PAGE_BASE + n
        assert pid not in base_pages
        a["page"]["id"] = str(pid)
    msgs = sorted(
        (
            (m["sent"], a["id"], k, m)
            for a in ctx.artifacts
            if a.get("thread")
            for k, m in enumerate(a["thread"]["messages"])
        ),
        key=lambda x: (x[0], x[1], x[2]),
    )
    for n, (_, _, _, m) in enumerate(msgs, start=1):
        mid = f"msg-{MAIL_BASE + n:06d}"
        assert mid not in base_mails
        m["id"] = mid
    new = sorted(
        (a for a in ctx.artifacts if a.get("thread") and not a["thread"]["conversation"]),
        key=lambda a: (a["start"], a["id"]),
    )
    for n, a in enumerate(new, start=1):
        a["thread"]["conversation"] = f"thr-{THREAD_BASE + n:06d}"


def plan(base: Base, *, types: tuple[str, ...] | None = None) -> dict[str, Any]:
    """All artifacts of a history, JSON-ready (``plan.json``)."""
    ctx = Ctx(base)
    plan_postmortems(ctx)
    plan_weekly_syncs(ctx)
    plan_leadership(ctx)
    plan_decisions(ctx)
    plan_qbrs(ctx)
    plan_escalations(ctx)
    plan_late_escalations(ctx)
    plan_detractor_escalations(ctx)
    assign_ids(ctx)
    arts = sorted(ctx.artifacts, key=lambda a: (a["publish"], a["id"]))
    if types:
        arts = [a for a in arts if a["type"] in types]
    counts: dict[str, int] = defaultdict(int)
    for a in arts:
        counts[a["type"]] += 1
    from variator.narrative.check import Names

    return {
        "history": str(base.root),
        "tenant": base.tenant,
        "company": base.company,
        "names": Names.from_people_yaml(base.people_yaml).to_json(),
        "anchor": base.anchor.isoformat(),
        "spaces": SPACES,
        "counts": dict(sorted(counts.items())),
        "links": sum(len(a["links"]) for a in arts),
        "artifacts": arts,
    }
