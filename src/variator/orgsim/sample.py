"""A tiny connected organisation, written through the whole chain: Event -> line -> delivery.

Six people, three Jira issues, one merge request and one e-mail thread in the two
weeks before T. All dates are relative to T, so the sample never looks old. It is the
smallest delivery that passes the structural checks of Plancy's ``kbsim.intake
validate``. It does not try to meet the density and coverage targets of §6-7: they
need a real company of 20+ people and 100+ days.
"""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from variator.orgsim.calendar import BusinessCalendar, RelativeDates
from variator.orgsim.delivery import write_delivery
from variator.orgsim.events import Cause, Event
from variator.orgsim.streams import Streams

TENANT = "wp3sample"
DOMAIN = "wp3sample.example"
TIMEZONE = "Europe/Berlin"
CLIENT_DOMAIN = "northwind.example"
LAG = timedelta(minutes=1)
# id, name, role, department, manager, login, groups
STAFF = [
    ("WP-E01", "Olivia Grant", "Chief Executive Officer", "Management", None, "o.grant", ["grp:management"]),
    ("WP-E02", "Sam Rivera", "Head of Sales", "Sales", "WP-E01", "s.rivera", ["grp:management", "grp:sales"]),
    (
        "WP-E03",
        "Maya Chen",
        "Head of Engineering",
        "Engineering",
        "WP-E01",
        "m.chen",
        ["grp:management", "grp:dev"],
    ),
    ("WP-E04", "Lena Park", "Backend Engineer", "Engineering", "WP-E03", "l.park", ["grp:dev"]),
    ("WP-E05", "Tom Novak", "Backend Engineer", "Engineering", "WP-E03", "t.novak", ["grp:dev"]),
    ("WP-E06", "Ravi Shah", "QA Engineer", "Engineering", "WP-E03", "r.shah", ["grp:dev"]),
]
CATEGORY = {"To Do": "new", "Done": "done"}  # every other status: indeterminate


def _src(kind: str, tenant: str) -> str:
    return f"{kind}-{tenant}"


def people_yaml(tenant: str = TENANT) -> dict[str, Any]:
    people = []
    for pid, name, role, dept, manager, login, groups in STAFF:
        people.append(
            {
                "id": pid,
                "name": name,
                "role": role,
                "department": dept,
                "manager": manager,
                "email": f"{login}@{DOMAIN}",
                "principal": f"user:{login}",
                "groups": ["grp:all-staff", *groups],
                "accounts": {
                    _src("jira", tenant): f"712020:{login}",
                    _src("gitlab", tenant): login.replace(".", ""),
                    _src("mail", tenant): f"{login}@{DOMAIN}",
                },
            }
        )
    return {
        "admins": ["user:admin"],
        "groups": {
            "grp:all-staff": {"name": "All staff"},
            "grp:management": {"name": "Management"},
            "grp:sales": {"name": "Sales"},
            "grp:dev": {"name": "Engineering"},
        },
        "people": people,
        "organizations": [
            {"id": "org-northwind", "name": "Northwind Foods", "kind": "client", "domain": CLIENT_DOMAIN}
        ],
        "contacts": [
            {
                "id": "CT-nw_ross",
                "name": "Daniel Ross",
                "role": "CTO, Northwind Foods",
                "org": "org-northwind",
                "email": f"d.ross@{CLIENT_DOMAIN}",
                "accounts": {_src("mail", tenant): f"d.ross@{CLIENT_DOMAIN}"},
            }
        ],
        "availability": [],
    }


def sources_yaml(start: datetime, tenant: str = TENANT) -> dict[str, Any]:
    return {
        "tenant": {
            "id": tenant,
            "name": "WP3 Sample Co",
            "timezone": TIMEZONE,
            "people_at": start.isoformat(),
        },
        "sources": [
            {
                "id": _src("jira", tenant),
                "provider": "jira",
                "name": "Jira",
                "base_url": f"https://{tenant}.atlassian.net",
                "containers": {
                    "ENG": {"name": "Engineering", "acl": ["grp:all-staff"]},
                    "HR": {"name": "People ops", "acl": ["grp:management"]},
                },
            },
            {
                "id": _src("gitlab", tenant),
                "provider": "gitlab",
                "name": "GitLab",
                "base_url": f"https://git.{DOMAIN}",
                "jira_source": _src("jira", tenant),
                "jira_keys": ["ENG"],
                "containers": {f"{tenant}/api": {"name": "API", "acl": ["grp:dev"]}},
            },
            {
                "id": _src("mail", tenant),
                "provider": "email",
                "name": "Mail",
                "mail_domain": DOMAIN,
                "group_mailboxes": {},
            },
            {
                "id": _src("directory", tenant),
                "provider": "directory",
                "name": "Directory",
                "containers": {"people": {"name": "HR directory"}},
            },
        ],
    }


def _person(login: str) -> dict[str, Any]:
    name = next(s[1] for s in STAFF if s[5] == login)
    return {"accountId": f"712020:{login}", "displayName": name, "emailAddress": f"{login}@{DOMAIN}"}


def _jira_time(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def _graph_time(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Builder:
    """Collects events and their timeline lines; ``seq`` counts the events."""

    def __init__(self, tenant: str, rel: RelativeDates):
        self.tenant, self.rel = tenant, rel
        self.events: list[Event] = []
        self.lines: list[dict[str, Any]] = []
        self.issues: dict[str, dict[str, Any]] = {}

    def emit(
        self, t: datetime, process: str, kind: str, entity: str, causes: list[Cause], **line: Any
    ) -> Event:
        ev = Event(
            run="sample",
            option="base",
            scenario="actual",
            ts=t,
            process=process,
            kind=kind,
            entity_id=entity,
            causes=tuple(causes),
            seq=len(self.events),
            fields={"source": line["source"], "object_type": line["object_type"]},
        )
        self.events.append(ev)
        self.lines.append(ev.to_timeline_line(tenant=self.tenant, observe_lag=LAG, **line))
        return ev

    def issue(
        self,
        key: str,
        t: datetime,
        *,
        summary: str,
        kind: str,
        assignee: str,
        reporter: str,
        causes: list[Cause],
    ) -> None:
        self.issues[key] = {
            "key": key,
            "id": str(10000 + len(self.issues) + 1),
            "fields": {
                "summary": summary,
                "description": summary + ".",
                "issuetype": {"name": kind},
                "project": {"key": "ENG", "name": "Engineering"},
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "resolution": None,
                "priority": {"name": "Medium"},
                "assignee": _person(assignee),
                "reporter": {"accountId": f"712020:{reporter}"},
                "creator": {"accountId": f"712020:{reporter}"},
                "created": _jira_time(t),
                "updated": _jira_time(t),
                "labels": [],
                "issuelinks": [],
                "comment": {"comments": []},
            },
            "names": {},
            "changelog": {"histories": []},
            "remotelinks": [],
        }
        self._issue_line(key, t, "issue_created", causes)

    def move(self, key: str, t: datetime, to: str, author: str, causes: list[Cause]) -> None:
        p = self.issues[key]
        f = p["fields"]
        p["changelog"]["histories"].append(
            {
                "id": str(50000 + sum(len(i["changelog"]["histories"]) for i in self.issues.values())),
                "author": {"accountId": f"712020:{author}"},
                "created": _jira_time(t),
                "items": [{"field": "status", "fromString": f["status"]["name"], "toString": to}],
            }
        )
        f["status"] = {"name": to, "statusCategory": {"key": CATEGORY.get(to, "indeterminate")}}
        f["resolution"] = {"name": "Done"} if to == "Done" else None
        f["updated"] = _jira_time(t)
        self._issue_line(key, t, "status_changed", causes)

    def comment(self, key: str, t: datetime, author: str, text: str, causes: list[Cause]) -> None:
        f = self.issues[key]["fields"]
        f["comment"]["comments"].append(
            {
                "id": str(20000 + len(f["comment"]["comments"]) + 1),
                "author": {"accountId": f"712020:{author}"},
                "body": text,
                "created": _jira_time(t),
                "updated": _jira_time(t),
            }
        )
        f["updated"] = _jira_time(t)
        self._issue_line(key, t, "comment_added", causes)

    def _issue_line(self, key: str, t: datetime, kind: str, causes: list[Cause]) -> None:
        self.emit(
            t,
            "delivery",
            kind,
            key,
            causes,
            source=_src("jira", self.tenant),
            object_type="issue",
            external_id=key,
            container="ENG",
            payload=copy.deepcopy(self.issues[key]),
        )


def build_sample(anchor: date, *, seed: int = 7, tenant: str = TENANT) -> dict[str, Any]:
    """Directory, timeline lines, cases and gold links of the sample, all relative to ``anchor``."""
    cal = BusinessCalendar(timezone=TIMEZONE)
    rel = RelativeDates(anchor, cal)
    b = _Builder(tenant, rel)
    jitter = Streams(seed).rng("sample", "minutes").integers(0, 20, size=32)  # a few minutes of noise
    times = iter(jitter.tolist())

    def at(n: int, hhmm: str) -> datetime:
        return rel.at(n, hhmm) + timedelta(minutes=next(times))

    proc = [Cause(type="process", id="delivery")]
    fix = [Cause(type="decision", id="fix-billing-retries"), *proc]
    # ENG-1: done, closed by a merge request
    b.issue(
        "ENG-1",
        at(-9, "10:00"),
        summary="Retry failed card payments in the billing job",
        kind="Task",
        assignee="l.park",
        reporter="m.chen",
        causes=fix,
    )
    b.move("ENG-1", at(-8, "09:30"), "In Progress", "l.park", proc)
    mr_open = at(-6, "11:00")
    mr = {
        "iid": 1,
        "title": "Retry failed card payments",
        "description": "Closes ENG-1. Adds a retry with backoff.",
        "state": "opened",
        "created_at": mr_open.isoformat(),
        "updated_at": mr_open.isoformat(),
        "merged_at": None,
        "closed_at": None,
        "author": {"username": "lpark", "name": "Lena Park"},
        "assignees": [],
        "reviewers": [{"username": "tnovak", "name": "Tom Novak"}],
        "source_branch": "eng-1-retry",
        "target_branch": "main",
        "notes": [],
    }
    mr_line = {
        "source": _src("gitlab", tenant),
        "object_type": "merge_request",
        "external_id": f"{tenant}/api!1",
        "container": f"{tenant}/api",
    }
    b.emit(mr_open, "delivery", "mr_opened", "mr-1", fix, payload=copy.deepcopy(mr), **mr_line)
    b.move("ENG-1", at(-6, "12:00"), "Review", "l.park", proc)
    note_t = at(-6, "15:00")
    mr["notes"].append(
        {
            "id": 1,
            "author": {"username": "tnovak"},
            "body": "Please add a test for the timeout case.",
            "created_at": note_t.isoformat(),
            "system": False,
        }
    )
    mr["updated_at"] = note_t.isoformat()
    b.emit(note_t, "delivery", "mr_reviewed", "mr-1", proc, payload=copy.deepcopy(mr), **mr_line)
    merged = at(-5, "10:00")
    mr.update(
        state="merged",
        merged_at=merged.isoformat(),
        updated_at=merged.isoformat(),
        merge_commit_sha="9f2c4e1b7a3d5f60812c4e9b0a7d3f5e1c2b4a68",
        merged_by={"username": "mchen"},
    )
    b.emit(merged, "delivery", "mr_merged", "mr-1", proc, payload=copy.deepcopy(mr), **mr_line)
    b.move("ENG-1", at(-5, "11:00"), "Done", "m.chen", proc)
    # ENG-2: a bug that comes back from QA and stays open
    b.issue(
        "ENG-2",
        at(-7, "10:00"),
        summary="Invoice PDF shows the wrong VAT rate",
        kind="Bug",
        assignee="t.novak",
        reporter="r.shah",
        causes=[Cause(type="life_event", id="vat-change"), *proc],
    )
    b.move("ENG-2", at(-7, "13:00"), "In Progress", "t.novak", proc)
    b.move("ENG-2", at(-4, "10:00"), "Review", "t.novak", proc)
    b.comment("ENG-2", at(-3, "11:00"), "r.shah", "The rate is still wrong for Austrian customers.", proc)
    b.move(
        "ENG-2", at(-3, "11:30"), "Reopened", "r.shah", [Cause(type="rule", id="qa-before-release"), *proc]
    )
    b.move("ENG-2", at(-2, "09:30"), "In Progress", "t.novak", proc)
    # ENG-3: new and open
    b.issue(
        "ENG-3",
        at(-1, "14:00"),
        summary="Export the monthly usage report as CSV",
        kind="Story",
        assignee="l.park",
        reporter="s.rivera",
        causes=[Cause(type="hypothesis", id="usage-export-demand"), *proc],
    )
    # an e-mail thread with the client; the reply refers to ENG-1 without its key
    mail = _src("mail", tenant)
    sent = at(-4, "08:00")
    first = {
        "id": "msg-000001",
        "internetMessageId": f"<{sent:%Y%m%d}.000001@{CLIENT_DOMAIN}>",
        "conversationId": "thr-000001",
        "subject": "Failed card payments last week",
        "from": {"emailAddress": {"name": "Daniel Ross", "address": f"d.ross@{CLIENT_DOMAIN}"}},
        "toRecipients": [{"emailAddress": {"name": "Sam Rivera", "address": f"s.rivera@{DOMAIN}"}}],
        "ccRecipients": [],
        "sentDateTime": _graph_time(sent),
        "receivedDateTime": _graph_time(sent + LAG),
        "inReplyTo": None,
        "references": [],
        "importance": "normal",
        "body": {
            "contentType": "text",
            "content": "Hi Sam, several card payments failed again last week. "
            "Can your team look into it? Daniel",
        },
        "hasAttachments": False,
        "attachments": [],
    }
    b.emit(
        sent,
        "sales",
        "email_received",
        "thr-000001",
        [Cause(type="habit", id="client-escalates-by-mail")],
        source=mail,
        object_type="message",
        external_id=first["internetMessageId"],
        acl=["user:s.rivera"],
        payload=first,
    )
    replied = at(-4, "10:30")
    quote = "the retry problem that Maya's team fixed"
    reply = {
        **first,
        "id": "msg-000002",
        "internetMessageId": f"<{replied:%Y%m%d}.000002@{DOMAIN}>",
        "subject": "Re: Failed card payments last week",
        "from": {"emailAddress": {"name": "Sam Rivera", "address": f"s.rivera@{DOMAIN}"}},
        "toRecipients": [{"emailAddress": {"name": "Daniel Ross", "address": f"d.ross@{CLIENT_DOMAIN}"}}],
        "ccRecipients": [{"emailAddress": {"name": "Maya Chen", "address": f"m.chen@{DOMAIN}"}}],
        "sentDateTime": _graph_time(replied),
        "receivedDateTime": _graph_time(replied + LAG),
        "inReplyTo": first["internetMessageId"],
        "references": [first["internetMessageId"]],
        "body": {
            "contentType": "text",
            "content": f"Hi Daniel, {quote} covers these payments. "
            "We will watch the next run and confirm. Sam",
        },
    }
    b.emit(
        replied,
        "sales",
        "email_sent",
        "thr-000001",
        [Cause(type="habit", id="reply-within-day")],
        source=mail,
        object_type="message",
        external_id=reply["internetMessageId"],
        acl=["user:m.chen", "user:s.rivera"],
        payload=reply,
    )

    since = {ln["external_id"]: ln["at"] for ln in reversed(b.lines)}  # the first line of each object
    gold = [
        {
            "effect": f"gitlab.{tenant}-api.mr-1",
            "cause": "jira.ENG-1",
            "predicate": "closes",
            "expressed_as": "mr_text",
            "since": since[f"{tenant}/api!1"],
            "explicit": True,
        },
        {
            "effect": "mail.msg-000002",
            "cause": "mail.msg-000001",
            "predicate": "replies_to",
            "expressed_as": "reply",
            "since": since[reply["internetMessageId"]],
            "explicit": True,
        },
        {
            "effect": "mail.msg-000002",
            "cause": "jira.ENG-1",
            "predicate": "refers_to",
            "expressed_as": "text",
            "since": since[reply["internetMessageId"]],
            "explicit": False,
            "quote": quote,
        },
    ]
    cases = [
        {
            "id": "S001",
            "category": "lookup",
            "difficulty": "trivial",
            "language": "en",
            "split": "dev",
            "question": "Who worked on the retry of failed card payments?",
            "principal": "user:o.grant",
            "known_at": None,
            "evidence": [{"entity_id": "jira.ENG-1", "why": "the task"}],
            "roots": [],
            "forbidden": [],
            "answer_facts": [{"key": "assignee", "any": ["Lena Park"]}],
            "expect_not_found": False,
            "rationale": "The assignee of the task.",
        },
        {
            "id": "S002",
            "category": "why",
            "difficulty": "tricky",
            "language": "en",
            "split": "holdout",
            "question": "Why did Sam tell the client that the failed payments are covered?",
            "principal": "user:s.rivera",
            "known_at": None,
            "evidence": [{"entity_id": "mail.msg-000002", "why": "the reply"}],
            "roots": [{"entity_id": "jira.ENG-1", "why": "the fix"}],
            "forbidden": [],
            "answer_facts": [{"key": "cause", "any": ["retry problem"]}],
            "expect_not_found": False,
            "rationale": "The reply refers to the retry task without its key.",
        },
    ]
    roles = {
        "ceo": ("CEO", "WP-E01"),
        "sales": ("Head of sales", "WP-E02"),
        "engineering": ("Head of engineering", "WP-E03"),
    }
    staff = {s[0]: s for s in STAFF}
    dashboard = {
        "id": tenant,
        "tenant": tenant,
        "name": "WP3 Sample Co",
        "sector": "Software",
        "headcount": len(STAFF),
        "timezone": TIMEZONE,
        "ownDomain": DOMAIN,
        "language": "en",
        "currency": "EUR",
        "engineeringDepartment": "Engineering",
        "roles": [
            {
                "id": rid,
                "label": label,
                "title": staff[pid][2],
                "name": staff[pid][1],
                "personId": f"person.{pid.lower()}",
                "principal": f"user:{staff[pid][5]}",
                **({"metadata": True} if rid != "engineering" else {}),
            }
            for rid, (label, pid) in roles.items()
        ],
    }
    start = rel.at(-10, "09:00")
    return {
        "sources": sources_yaml(start, tenant),
        "people": people_yaml(tenant),
        "lines": b.lines,
        "events": b.events,
        "cases": cases,
        "gold": gold,
        "dashboard": dashboard,
    }


def write_sample_delivery(
    root: str | Path, *, anchor: date, seed: int = 7, tenant: str = TENANT
) -> dict[str, Any]:
    """Write the sample delivery to ``root``; return its manifest."""
    s = build_sample(anchor, seed=seed, tenant=tenant)
    return write_delivery(
        root,
        tenant=tenant,
        sources=s["sources"],
        people=s["people"],
        lines=s["lines"],
        seed=seed,
        language="en",
        dashboard=s["dashboard"],
        cases=s["cases"],
        gold_links=s["gold"],
        sample_of=tenant,
    )
