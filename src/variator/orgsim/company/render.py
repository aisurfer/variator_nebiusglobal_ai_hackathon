"""Turn the world of a run into the files of a corpus delivery.

1. ``assign_ids``: external ids in time order (INV-00042, PLAT-17, !12, msg-000123).
2. ``cut``: drop every version at or after T (nothing after T is delivered).
3. ``lines``: one timeline line per version. ``sim`` lines carry the event envelope with the
   full state of the record and every cause that still holds (KB adapter ``kb.connectors.sim``).
   Jira, GitLab, mail and Confluence lines carry provider payloads; each generated text has
   a ``text_slot`` marker so writer agents can replace it later.
4. ``gold``: the declared links as gold links, and the implicit references of the client updates.
5. ``directory``: ``people.yaml``, ``sources.yaml`` and ``dashboard.json``.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from variator.orgsim.company import templates as T
from variator.orgsim.company.model import Issue, Mail, MergeRequest, Money, Page, Record, TextSlot, World
from variator.orgsim.events import CAUSE_PREDICATES, Cause, Event

LAG = timedelta(minutes=1)
RUN, OPTION, SCENARIO = "history", "base", "actual"
PERSON_FIELDS = ("owner", "assignee", "author", "reporter", "employee", "actor", "contact")
ORG_FIELDS = ("client", "vendor", "counterparty", "authority")
RESERVED = ("event", "event_at", "process", "simulation", "causes")
CATEGORY = {"To Do": "new", "Selected for Development": "new", "Done": "done", "Won't Do": "done"}
GROUPS = {
    "grp:all-staff": "All staff",
    "grp:management": "Management",
    "grp:finance": "Finance and HR",
    "grp:delivery": "Delivery",
    "grp:engineering": "Engineering",
    "grp:support": "Support",
    "grp:sales": "Sales",
    "grp:marketing": "Marketing",
}
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
SIM_CONTAINERS = {
    "company": ("Company policies and processes", ["grp:all-staff"]),
    "finance": ("Finance", ["grp:management", "grp:finance"]),
    "sales": ("Sales and contracts", ["grp:management", "grp:sales", "grp:finance"]),
    "people": ("People", ["grp:management", "grp:finance"]),
    "engineering": ("Engineering operations", ["grp:engineering", "grp:delivery", "grp:management"]),
    "support": ("Support", ["grp:all-staff"]),
    "feedback": ("Client feedback (NPS)", ["grp:management", "grp:sales", "grp:support", "grp:delivery"]),
}


def _jira_time(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def _graph_time(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Pending:
    ts: datetime
    order: int
    kind: str  # sim | jira | gitlab | mail | page
    obj: Any
    k: int  # index of the version / update


class Renderer:
    def __init__(self, w: World, eng: Any, scale: float, implicit: list[dict[str, Any]]):
        self.w, self.eng, self.scale, self.implicit = w, eng, scale, implicit
        co = w.spec.company
        self.tenant = co.tenant
        self.src = {k: f"{k}-{co.tenant}" for k in ("jira", "confluence", "gitlab", "mail", "directory")}
        self.src["sim"] = "sim"
        self.project = co.gitlab_project
        self.slug = co.gitlab_project.replace("/", "-")
        self.end = w.end
        self.names: dict[str, str] = {}
        self.dropped: Counter = Counter()
        self.first_at: dict[str, str] = {}

    # ================================================================================== ids and cut
    def assign_ids(self) -> None:
        w = self.w
        groups: dict[str, list[Record]] = {}
        for recs in w.records.values():
            for r in recs:
                if r.ext is None:
                    groups.setdefault(r.prefix, []).append(r)
        for prefix, recs in groups.items():
            recs.sort(key=lambda r: (r.first_ts, r.uid))
            width = max(4, len(str(len(recs))))
            for n, r in enumerate(recs, start=1):
                r.ext = f"{prefix}-{n:0{width}d}"
        by_project: dict[str, list[Issue]] = {}
        for i in w.issues:
            by_project.setdefault(i.project, []).append(i)
        for project, issues in by_project.items():
            issues.sort(key=lambda i: (i.created, i.uid))
            for n, i in enumerate(issues, start=1):
                i.ext = f"{project}-{n}"
        for n, m in enumerate(sorted(w.mrs, key=lambda m: (m.created, m.uid)), start=1):
            m.iid, m.ext = n, f"{self.project}!{n}"
            m.entity = f"gitlab.{self.slug}.mr-{n}"
        for n, m in enumerate(sorted(w.mails, key=lambda m: (m.sent, m.uid)), start=1):
            m.ext = f"msg-{n:06d}"
            dom = m.sender[1].rpartition("@")[2]
            m.message_id = f"<{m.sent.astimezone(UTC):%Y%m%d%H%M%S}.{n:06d}@{dom}>"
        for n, p in enumerate(sorted(w.pages, key=lambda p: (p.ts, p.uid)), start=1):
            p.ext = str(1_000_000 + n)

    def cut(self) -> None:
        """Drop every version at or after T; an object without versions is not delivered."""
        w, end = self.w, self.end - LAG
        for t, recs in w.records.items():
            for r in recs:
                r.versions = [v for v in r.versions if v.ts < end]
            w.records[t] = [r for r in recs if r.versions]
        for i in w.issues:
            i.updates = [u for u in i.updates if u.ts < end]
        w.issues = [i for i in w.issues if i.updates]
        for m in w.mrs:
            m.updates = [u for u in m.updates if u.ts < end]
            m.notes = [n for n in m.notes if n[0] < end]
        w.mrs = [m for m in w.mrs if m.updates]
        w.mails = [m for m in w.mails if m.sent < end]
        w.pages = [p for p in w.pages if p.ts < end]
        self.delivered = {id(x) for recs in w.records.values() for x in recs}
        self.delivered |= {id(x) for x in w.issues} | {id(x) for x in w.mrs} | {id(x) for x in w.mails}
        self.delivered |= {id(x) for x in w.pages}
        self.first_ts: dict[int, datetime] = {}
        for recs in w.records.values():
            for r in recs:
                self.first_ts[id(r)] = r.versions[0].ts
        for x in w.issues:
            self.first_ts[id(x)] = x.updates[0].ts
        for x in w.mrs:
            self.first_ts[id(x)] = x.updates[0].ts
        for x in w.mails:
            self.first_ts[id(x)] = x.sent
        for x in w.pages:
            self.first_ts[id(x)] = x.ts

    # ================================================================================== values
    def is_obj(self, x: Any) -> bool:
        return isinstance(x, (Record, Issue, MergeRequest, Mail, Page))

    def value(self, x: Any, where: str) -> Any:
        if isinstance(x, Money):
            return x.value(self.scale)
        if self.is_obj(x):
            if id(x) not in self.delivered:
                self.dropped[where] += 1
                return None
            return x.ref()
        if isinstance(x, list):
            out = [self.value(y, where) for y in x]
            return [y for y in out if y is not None]
        if isinstance(x, dict):
            return {k: v for k, v in ((k, self.value(v, where)) for k, v in x.items()) if v is not None}
        if isinstance(x, (date, datetime)):
            return x.isoformat()
        if isinstance(x, float):
            return round(x, 4)
        return x

    def sim_fields(self, r: Record, snapshot: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in snapshot.items():
            if k in RESERVED:
                raise ValueError(f"{r.otype}: the field name {k!r} is reserved by the KB sim adapter")
            if isinstance(v, TextSlot):
                out[k] = f"sim:{r.ext}:{v.kind}"
                continue
            val = self.value(v, f"{r.otype}.{k}")
            if val is None or val == [] or val == {}:
                continue
            out[k] = val
        if r.otype in ("contract", "invoice", "payment") and "number" not in out:
            out["number"] = r.ext
        for k in PERSON_FIELDS + ORG_FIELDS:
            v = out.get(k)
            if isinstance(v, str) and v in self.names:
                out[f"{k}_name"] = self.names[v]
            elif isinstance(v, list):
                names = [self.names.get(x) for x in v if isinstance(x, str)]
                if names and all(names):
                    out[f"{k}_name"] = ", ".join(names)  # type: ignore[arg-type]
        return out

    def causes(self, causes: tuple, at: datetime, where: str) -> tuple[Cause, ...]:
        out = []
        for ctype, target in causes:
            if id(target) not in self.delivered:
                self.dropped[f"cause:{where}"] += 1
                continue
            if self.first_ts[id(target)] > at:
                self.dropped[f"late_cause:{where}"] += 1
            out.append(Cause(type=ctype, id=target.ref()))
        return tuple(out)

    # ================================================================================== people
    def person(self, pid: str) -> dict[str, Any]:
        p = self.w.people[pid].person
        return {
            "accountId": f"712020:{p.login}",
            "displayName": p.name,
            "emailAddress": f"{p.login}@{self.w.spec.company.domain}",
        }

    def git_user(self, pid: str) -> dict[str, Any]:
        p = self.w.people[pid].person
        return {"username": p.login.replace(".", ""), "name": p.name}

    # ================================================================================== Jira
    def jira_payload(self, issue: Issue, k: int) -> dict[str, Any]:
        w = self.w
        ups = issue.updates[: k + 1]
        at = ups[-1].ts
        status, resolution = "To Do", None
        histories, comments, links, remotes = [], [], [], []
        current_assignee = issue.updates[0].data.get("assignee")
        for n, u in enumerate(ups):
            if u.kind == "transition":
                status = u.data["to"]
                resolution = (
                    {"name": "Done"}
                    if status == "Done"
                    else {"name": "Won't Do"}
                    if status == "Won't Do"
                    else None
                )
                histories.append(
                    {
                        "id": str(issue.uid * 100 + n),
                        "author": {"accountId": self.person(u.actor)["accountId"]},
                        "created": _jira_time(u.ts),
                        "items": [
                            {"field": "status", "fromString": u.data["from"], "toString": u.data["to"]}
                        ],
                    }
                )
            elif u.kind == "assign":
                prev = current_assignee
                current_assignee = u.data["to"]
                histories.append(
                    {
                        "id": str(issue.uid * 100 + n),
                        "author": {"accountId": self.person(u.actor)["accountId"]},
                        "created": _jira_time(u.ts),
                        "items": [
                            {
                                "field": "assignee",
                                "fromString": w.person_name(prev) if prev else None,
                                "toString": w.person_name(current_assignee),
                            }
                        ],
                    }
                )
            elif u.kind == "comment":
                comments.append(
                    {
                        "id": str(issue.uid * 100 + n),
                        "author": self.person(u.actor),
                        "body": u.data["body"],
                        "created": _jira_time(u.ts),
                        "updated": _jira_time(u.ts),
                        "text_slot": f"jira:{issue.ext}:comment-{len(comments) + 1}:{u.data['slot_kind']}",
                    }
                )
            elif u.kind == "link":
                other: Issue = u.data["blocked_by"]
                if id(other) in self.delivered:
                    links.append(
                        {
                            "id": str(issue.uid * 100 + n),
                            "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                            "inwardIssue": {"key": other.ext, "fields": {"summary": other.summary}},
                        }
                    )
            elif u.kind == "remote_link":
                mr: MergeRequest = u.data["caused_by"]
                if id(mr) in self.delivered:
                    remotes.append(
                        {
                            "id": issue.uid * 100 + n,
                            "relationship": "is caused by",
                            "object": {
                                "url": self.mr_url(mr),
                                "title": f"!{mr.iid} {mr.issue.ext}: {mr.issue.summary}",
                            },
                        }
                    )
        sprints = [
            self.eng.work.sprint_payload(self.eng.work.sprints[n], at) for ts, n in issue.sprints if ts <= at
        ]
        fields: dict[str, Any] = {
            "summary": issue.summary,
            "description": issue.description,
            "issuetype": {"name": issue.itype},
            "project": {"key": issue.project, "name": PROJECTS[issue.project]},
            "status": {"name": status, "statusCategory": {"key": CATEGORY.get(status, "indeterminate")}},
            "resolution": resolution,
            "priority": {"name": issue.priority},
            "assignee": self.person(current_assignee) if current_assignee else None,
            "reporter": self.person(issue.reporter),
            "creator": self.person(issue.reporter),
            "created": _jira_time(issue.created),
            "updated": _jira_time(at),
            "labels": sorted(issue.labels),
            "components": [{"name": issue.component}] if issue.component else [],
            "issuelinks": links,
            "comment": {"comments": comments, "total": len(comments)},
            "customfield_10016": issue.points or None,
            "customfield_10020": sprints or None,
        }
        if issue.parent is not None:
            fields["parent"] = {"key": issue.parent.ext, "fields": {"summary": issue.parent.summary}}
        if issue.client:
            fields["customfield_10300"] = self.names.get(issue.client, issue.client)
        if status == "Done" or status == "Won't Do":
            fields["resolutiondate"] = (
                _jira_time(issue.done_at) if issue.done_at and issue.done_at <= at else _jira_time(at)
            )
        return {
            "key": issue.ext,
            "id": str(10000 + issue.uid),
            "self": f"{self.w.spec.company.jira_base_url}/rest/api/2/issue/{10000 + issue.uid}",
            "fields": fields,
            "names": {
                "customfield_10016": "Story Points",
                "customfield_10020": "Sprint",
                "customfield_10300": "Client",
            },
            "changelog": {"histories": histories},
            "remotelinks": remotes,
            "text_slot": f"jira:{issue.ext}:{issue.slot_kind}",
        }

    # ================================================================================== GitLab
    def mr_url(self, mr: MergeRequest) -> str:
        return f"{self.w.spec.company.gitlab_base_url}/{self.project}/-/merge_requests/{mr.iid}"

    def mr_payload(self, mr: MergeRequest, k: int) -> dict[str, Any]:
        ups = mr.updates[: k + 1]
        at = ups[-1].ts
        merged = any(u.kind == "merged" for u in ups)
        notes = [n for n in mr.notes if n[0] <= at]
        key, summary = mr.issue.ext, mr.issue.summary
        branch = f"{key.lower()}-" + "-".join(summary.lower().split()[:4]).replace("'", "")
        branch = "".join(ch for ch in branch if ch.isalnum() or ch == "-")
        sha = hashlib.sha1(f"{self.tenant}:{mr.uid}:merge".encode()).hexdigest()
        return {
            "id": 70000 + mr.iid,
            "iid": mr.iid,
            "project_id": 7,
            "title": f"{key}: {summary}",
            "description": T.mr_description(key, summary),
            "state": "merged" if merged else "opened",
            "created_at": mr.created.isoformat(),
            "updated_at": at.isoformat(),
            "merged_at": mr.merged_at.isoformat() if merged and mr.merged_at else None,
            "closed_at": None,
            "author": self.git_user(mr.author),
            "assignees": [self.git_user(mr.author)],
            "reviewers": [self.git_user(mr.reviewer)],
            "source_branch": branch,
            "target_branch": "main",
            "web_url": self.mr_url(mr),
            "labels": [mr.issue.component] if mr.issue.component else [],
            "notes": [
                {
                    "id": mr.iid * 100 + j,
                    "author": {"username": self.git_user(a)["username"]},
                    "body": body,
                    "created_at": t.isoformat(),
                    "system": False,
                    "text_slot": f"gitlab:{mr.iid}:note-{j}:{slot}",
                }
                for j, (t, a, body, slot) in enumerate(notes, start=1)
            ],
            **(
                {"merge_commit_sha": sha, "merged_by": self.git_user(mr.merged_by)}
                if merged and mr.merged_by
                else {}
            ),
            "text_slot": f"gitlab:{mr.iid}:description",
        }

    # ================================================================================== mail
    def mail_payload(self, m: Mail) -> dict[str, Any]:
        def addr(x: tuple[str, str]) -> dict[str, Any]:
            return {"emailAddress": {"name": x[0], "address": x[1]}}

        refs, p = [], m.parent
        while p is not None:
            refs.insert(0, p.message_id)
            p = p.parent
        return {
            "id": m.ext,
            "internetMessageId": m.message_id,
            "conversationId": f"thr-{m.thread:06d}",
            "subject": m.subject,
            "from": addr(m.sender),
            "toRecipients": [addr(x) for x in m.to],
            "ccRecipients": [addr(x) for x in m.cc],
            "sentDateTime": _graph_time(m.sent),
            "receivedDateTime": _graph_time(m.sent + timedelta(seconds=40)),
            "inReplyTo": m.parent.message_id if m.parent else None,
            "references": refs,
            "importance": "normal",
            "body": {"contentType": "text", "content": m.body},
            "hasAttachments": False,
            "attachments": [],
            "text_slot": f"mail:{m.ext}:{m.slot_kind}",
        }

    def mail_acl(self, m: Mail) -> list[str]:
        co = self.w.spec.company
        principals = {
            f"{p.person.login}@{co.domain}": f"user:{p.person.login}" for p in self.w.people.values()
        }
        boxes = {f"support@{co.domain}": ["grp:support"], f"billing@{co.domain}": ["grp:finance"]}
        acl: set[str] = set()
        for _, a in [m.sender, *m.to, *m.cc]:
            if a in principals:
                acl.add(principals[a])
            acl.update(boxes.get(a, []))
        return sorted(acl)

    # ================================================================================== Confluence
    def page_payload(self, p: Page) -> dict[str, Any]:
        import json

        def tx(s: str) -> dict[str, Any]:
            return {"type": "text", "text": s}

        def cell(kind: str, s: str) -> dict[str, Any]:
            return {"type": kind, "attrs": {}, "content": [{"type": "paragraph", "content": [tx(s)]}]}

        table = {
            "type": "table",
            "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
            "content": [
                {"type": "tableRow", "content": [cell("tableHeader", k), cell("tableCell", v)]}
                for k, v in p.properties
            ],
        }
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "bodiedExtension",
                    "attrs": {
                        "extensionType": "com.atlassian.confluence.macro.core",
                        "extensionKey": "details",
                        "parameters": {"macroParams": {}},
                    },
                    "content": [table],
                },
                {"type": "paragraph", "content": [tx(p.body)]},
                {"type": "heading", "attrs": {"level": 2}, "content": [tx("Commentary")]},
                {"type": "paragraph", "content": [tx("No unusual changes this week.")]},
            ],
        }
        acc = self.person(p.author)["accountId"]
        base = self.w.spec.company.confluence_base_url
        return {
            "id": p.ext,
            "status": "current",
            "title": p.title,
            "spaceId": "98001",
            "space": {"id": "98001", "key": p.space, "name": "Management"},
            "parentId": "980010",
            "parentType": "page",
            "authorId": acc,
            "ownerId": acc,
            "createdAt": _graph_time(p.ts),
            "version": {
                "number": 1,
                "message": "",
                "minorEdit": False,
                "authorId": acc,
                "createdAt": _graph_time(p.ts),
            },
            "labels": {
                "results": [
                    {
                        "id": str(int(hashlib.sha1(x.encode()).hexdigest()[:6], 16)),
                        "name": x,
                        "prefix": "global",
                    }
                    for x in p.labels
                ]
            },
            "body": {
                "atlas_doc_format": {
                    "value": json.dumps(doc, ensure_ascii=False),
                    "representation": "atlas_doc_format",
                }
            },
            "_links": {"webui": f"/spaces/{p.space}/pages/{p.ext}/{p.title.replace(' ', '+')}", "base": base},
            "text_slot": f"confluence:{p.ext}:commentary",
        }

    # ================================================================================== lines
    def pending(self) -> list[Pending]:
        w = self.w
        out: list[Pending] = []
        for recs in w.records.values():
            for r in recs:
                for k, v in enumerate(r.versions):
                    out.append(Pending(v.ts, v.order, "sim", r, k))
        for i in w.issues:
            for k, u in enumerate(i.updates):
                out.append(Pending(u.ts, u.order, "jira", i, k))
        for m in w.mrs:
            for k, u in enumerate(m.updates):
                out.append(Pending(u.ts, u.order, "gitlab", m, k))
        for m in w.mails:
            out.append(Pending(m.sent, m.order, "mail", m, 0))
        for p in w.pages:
            out.append(Pending(p.ts, p.order, "page", p, 0))
        out.sort(key=lambda x: (x.ts.astimezone(UTC), x.order))
        return out

    def lines(self) -> tuple[list[dict[str, Any]], list[Event]]:
        lines, events = [], []
        for seq, it in enumerate(self.pending()):
            line, ev = self._line(it, seq)
            lines.append(line)
            events.append(ev)
            self.first_at.setdefault(self._entity(it), line["at"])
        return lines, events

    def _entity(self, it: Pending) -> str:
        if it.kind == "sim":
            return f"sim.{it.obj.ext}"
        return it.obj.ref()

    def _line(self, it: Pending, seq: int) -> tuple[dict[str, Any], Event]:
        meta = {"run": RUN, "option": OPTION, "scenario": SCENARIO, "seq": seq}
        if it.kind == "sim":
            r: Record = it.obj
            v = r.versions[it.k]
            ev = Event(
                **meta,
                ts=v.ts,
                process=v.process,
                kind=v.kind,
                entity_id=r.ext,
                fields=self.sim_fields(r, v.snapshot),
                causes=self.causes(v.causes, v.ts, r.otype),
            )
            line = ev.to_timeline_line(
                tenant=self.tenant,
                source=self.src["sim"],
                object_type=r.otype,
                external_id=r.ext,
                container=r.container,
                acl=SIM_CONTAINERS[r.container][1],
                observe_lag=LAG,
            )
            return line, ev
        if it.kind == "jira":
            i: Issue = it.obj
            u = i.updates[it.k]
            ev = Event(
                **meta,
                ts=u.ts,
                process=u.process,
                kind=f"issue_{u.kind}",
                entity_id=i.ref(),
                fields={"source": self.src["jira"], "object_type": "issue", "external_id": i.ext},
                causes=self.causes(u.causes, u.ts, "jira"),
            )
            line = ev.to_timeline_line(
                tenant=self.tenant,
                source=self.src["jira"],
                object_type="issue",
                external_id=i.ext,
                container=i.project,
                payload=self.jira_payload(i, it.k),
                observe_lag=LAG,
            )
            return line, ev
        if it.kind == "gitlab":
            m: MergeRequest = it.obj
            u = m.updates[it.k]
            ev = Event(
                **meta,
                ts=u.ts,
                process="work",
                kind=f"mr_{u.kind}",
                entity_id=m.ref(),
                fields={"source": self.src["gitlab"], "object_type": "merge_request", "external_id": m.ext},
                causes=self.causes(u.causes, u.ts, "gitlab"),
            )
            line = ev.to_timeline_line(
                tenant=self.tenant,
                source=self.src["gitlab"],
                object_type="merge_request",
                external_id=m.ext,
                container=self.project,
                payload=self.mr_payload(m, it.k),
                observe_lag=LAG,
            )
            return line, ev
        if it.kind == "mail":
            ml: Mail = it.obj
            ev = Event(
                **meta,
                ts=ml.sent,
                process=ml.process,
                kind=ml.kind,
                entity_id=ml.ref(),
                fields={"source": self.src["mail"], "object_type": "message", "external_id": ml.message_id},
                causes=self.causes(ml.causes, ml.sent, "mail"),
            )
            line = ev.to_timeline_line(
                tenant=self.tenant,
                source=self.src["mail"],
                object_type="message",
                external_id=ml.message_id,
                acl=self.mail_acl(ml),
                payload=self.mail_payload(ml),
                observe_lag=LAG,
            )
            return line, ev
        p: Page = it.obj
        ev = Event(
            **meta,
            ts=p.ts,
            process=p.process,
            kind=p.kind,
            entity_id=p.ref(),
            fields={"source": self.src["confluence"], "object_type": "page", "external_id": p.ext},
            causes=self.causes(p.causes, p.ts, "page"),
        )
        line = ev.to_timeline_line(
            tenant=self.tenant,
            source=self.src["confluence"],
            object_type="page",
            external_id=p.ext,
            container=p.space,
            payload=self.page_payload(p),
            observe_lag=LAG,
        )
        return line, ev

    # ================================================================================== gold links
    def gold(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: dict[tuple[str, str, str], dict[str, Any]] = {}

        def add(effect: str, pred: str, cause: str, how: str, since: str, **extra: Any) -> None:
            key = (effect, pred, cause)
            if key not in out:
                out[key] = {
                    "effect": effect,
                    "cause": cause,
                    "predicate": pred,
                    "expressed_as": how,
                    "since": since,
                    "explicit": not extra,
                    **extra,
                }

        for ln in lines:
            p = ln.get("payload") or {}
            src, at = ln["source"], ln["at"]
            if src == self.src["sim"]:
                eff = f"sim.{ln['external_id']}"
                for c in p.get("causes") or []:
                    cid = c["id"]
                    add(
                        eff, CAUSE_PREDICATES[c["type"]], cid if "." in cid else f"sim.{cid}", "sim_cause", at
                    )
            elif src == self.src["jira"]:
                eff = f"jira.{ln['external_id']}"
                f = p["fields"]
                if f.get("parent"):
                    add(eff, "part_of", f"jira.{f['parent']['key']}", "parent", at)
                for lk in f.get("issuelinks") or []:
                    add(eff, "blocked_by", f"jira.{lk['inwardIssue']['key']}", "issue_link", at)
                for rl in p.get("remotelinks") or []:
                    iid = rl["object"]["url"].rsplit("/", 1)[1]
                    add(eff, "caused_by", f"gitlab.{self.slug}.mr-{iid}", "remote_link", at)
            elif src == self.src["gitlab"]:
                add(
                    f"gitlab.{self.slug}.mr-{p['iid']}",
                    "closes",
                    f"jira.{p['title'].split(':')[0]}",
                    "mr_text",
                    at,
                )
            elif src == self.src["mail"] and p.get("inReplyTo"):
                parent = self._mail_by_mid.get(p["inReplyTo"])
                if parent:
                    add(f"mail.{p['id']}", "replies_to", f"mail.{parent}", "reply", at)
        rows = list(out.values())
        for x in self.implicit:
            m, i = x["mail"], x["issue"]
            if id(m) in self.delivered and id(i) in self.delivered:
                rows.append(
                    {
                        "effect": m.ref(),
                        "cause": i.ref(),
                        "predicate": "refers_to",
                        "expressed_as": "text",
                        "since": self.first_at.get(m.ref()),
                        "explicit": False,
                        "quote": x["quote"],
                    }
                )
        return rows

    @property
    def _mail_by_mid(self) -> dict[str, str]:
        if not hasattr(self, "_mids"):
            self._mids = {m.message_id: m.ext for m in self.w.mails}
        return self._mids

    # ================================================================================== directory
    def directory(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        w = self.w
        co = w.spec.company
        people = []
        for pid in sorted(w.people):
            st = w.people[pid]
            p = st.person
            people.append(
                {
                    "id": pid,
                    "name": p.name,
                    "role": p.title,
                    "department": p.department,
                    "manager": p.manager,
                    "email": f"{p.login}@{co.domain}",
                    "principal": f"user:{p.login}",
                    "groups": ["grp:all-staff", *ROLE_GROUPS[p.role]],
                    "accounts": {
                        self.src["jira"]: f"712020:{p.login}",
                        self.src["confluence"]: f"712020:{p.login}",
                        self.src["gitlab"]: p.login.replace(".", ""),
                        self.src["mail"]: f"{p.login}@{co.domain}",
                    },
                    "start_date": max(st.start, w.start).isoformat()
                    if st.hire_slot
                    else st.start.isoformat(),
                    "end_date": st.last_day.isoformat() if st.last_day and st.last_day < w.anchor else None,
                }
            )
        orgs = [{"id": "LW", "name": co.name, "kind": "other", "domain": co.domain}]
        contacts = []
        for cid in sorted(w.clients):
            c = w.clients[cid]
            kind = "client"
            orgs.append(
                {
                    "id": cid,
                    "name": c.profile.name,
                    "kind": kind,
                    "domain": c.profile.domain,
                    "description": f"{c.profile.industry} client",
                }
            )
            for k, ct in enumerate(c.profile.contacts):
                email = self.eng.clients.contact_email(c, k)
                contacts.append(
                    {
                        "id": f"CNT-{cid}-{k + 1}",
                        "name": ct.name,
                        "role": f"{ct.title}, {c.profile.name}",
                        "org": cid,
                        "email": email,
                        "accounts": {self.src["mail"]: email},
                    }
                )
        for deal in self.eng.sales.all:
            pr = deal.prospect
            if pr is None or pr.id in w.clients:
                continue
            orgs.append(
                {
                    "id": pr.id,
                    "name": pr.name,
                    "kind": "prospect",
                    "domain": pr.domain,
                    "description": f"{pr.industry} prospect",
                }
            )
            for k, ct in enumerate(pr.contacts):
                email = self.eng.sales.contact_email(pr, k)
                contacts.append(
                    {
                        "id": f"CNT-{pr.id}-{k + 1}",
                        "name": ct.name,
                        "role": f"{ct.title}, {pr.name}",
                        "org": pr.id,
                        "email": email,
                        "accounts": {self.src["mail"]: email},
                    }
                )
        for v in w.spec.vendors:
            orgs.append(
                {
                    "id": v.id,
                    "name": v.name,
                    "kind": "bank" if v.kind == "bank" else "vendor",
                    "domain": v.domain,
                    "description": f"{v.kind} vendor",
                }
            )
            email = f"billing@{v.domain}"
            contacts.append(
                {
                    "id": f"CNT-{v.id}-1",
                    "name": v.contact_name,
                    "role": f"{v.contact_title}, {v.name}",
                    "org": v.id,
                    "email": email,
                    "accounts": {self.src["mail"]: email},
                }
            )
        ta = w.spec.tax_authority
        orgs.append({"id": ta.id, "name": ta.name, "kind": "government"})
        avail = sorted(
            (
                {"person": a["person"], "from": a["from"], "to": a["to"]}
                for a in self.eng.people.availability
                if a["from"] < w.anchor.isoformat()
            ),
            key=lambda a: (a["person"], a["from"]),
        )
        people_yaml = {
            "admins": ["user:admin"],
            "groups": {g: {"name": n} for g, n in GROUPS.items()},
            "people": people,
            "organizations": orgs,
            "contacts": contacts,
            "availability": avail,
        }
        self.names = (
            {p["id"]: p["name"] for p in people}
            | {o["id"]: o["name"] for o in orgs}
            | {c["id"]: c["name"] for c in contacts}
        )
        people_at = w.cal.at(w.start, "05:00") - timedelta(days=1)
        sources = {
            "tenant": {
                "id": self.tenant,
                "name": co.name,
                "timezone": co.timezone,
                "language": co.language,
                "people_at": people_at.isoformat(),
            },
            "sources": [
                {
                    "id": self.src["jira"],
                    "provider": "jira",
                    "name": "Jira",
                    "base_url": co.jira_base_url,
                    "containers": {k: {"name": PROJECTS[k], "acl": acl} for k, acl in JIRA_ACL.items()},
                },
                {
                    "id": self.src["confluence"],
                    "provider": "confluence",
                    "name": "Confluence",
                    "base_url": co.confluence_base_url,
                    "containers": {"MGMT": {"name": "Management", "acl": ["grp:management"]}},
                },
                {
                    "id": self.src["gitlab"],
                    "provider": "gitlab",
                    "name": "GitLab",
                    "base_url": co.gitlab_base_url,
                    "jira_source": self.src["jira"],
                    "jira_keys": ["PLAT", "IMPL"],
                    "containers": {
                        self.project: {"name": "Platform", "acl": ["grp:engineering", "grp:delivery"]}
                    },
                },
                {
                    "id": self.src["mail"],
                    "provider": "email",
                    "name": "Mail",
                    "mail_domain": co.domain,
                    "group_mailboxes": {
                        f"support@{co.domain}": ["grp:support"],
                        f"billing@{co.domain}": ["grp:finance"],
                    },
                },
                {
                    "id": self.src["directory"],
                    "provider": "directory",
                    "name": "Directory",
                    "containers": {"people": {"name": "HR directory"}},
                },
                {
                    "id": self.src["sim"],
                    "provider": "sim",
                    "name": "Business records (VARIATOR orgsim)",
                    "containers": {k: {"name": n, "acl": acl} for k, (n, acl) in SIM_CONTAINERS.items()},
                },
            ],
        }
        return sources, people_yaml, self.dashboard()

    def dashboard(self) -> dict[str, Any]:
        co = self.w.spec.company
        roles = []
        for rid, label in (("ceo", "CEO"), ("finance", "Head of Finance"), ("delivery", "Head of Delivery")):
            p = next(x for x in self.w.spec.people if x.role_id == rid)
            roles.append(
                {
                    "id": rid,
                    "label": label,
                    "title": p.title,
                    "name": p.name,
                    "personId": f"person.{p.id.lower()}",
                    "principal": "user:admin",
                    "metadata": True,
                }
            )
        sales = next(x for x in self.w.spec.people if x.role == "sales")
        lead = self.w.spec.person(DELIVERY_HEAD)
        roles.append(
            {
                "id": "sales",
                "label": "Sales",
                "title": sales.title,
                "name": sales.name,
                "personId": f"person.{sales.id.lower()}",
                "principal": "user:admin",
            }
        )
        roles.append(
            {
                "id": "engineering",
                "label": "Engineering",
                "title": lead.title,
                "name": lead.name,
                "personId": f"person.{lead.id.lower()}",
                "principal": "user:admin",
            }
        )
        return {
            "id": self.tenant,
            "tenant": self.tenant,
            "name": co.name,
            "sector": co.sector,
            "headcount": len(self.w.spec.people),
            "timezone": co.timezone,
            "ownDomain": co.domain,
            "language": co.language,
            "currency": co.currency,
            "engineeringDepartment": co.engineering_department,
            "northStar": {
                "title": "Annual recurring revenue",
                "reportLabel": "report",
                "property": "ARR, $k",
                "period": "Period",
                "unit": "$k of ARR",
                "field": "arrK",
                "target": co.north_star_target_k,
                "by": co.north_star_by,
                "secondary": [
                    {"property": "Active clients", "label": "active clients", "field": "activeClients"},
                    {"property": "New ARR, $k", "label": "new ARR, $k", "field": "newArrK"},
                    {
                        "property": "Churned ARR, $k",
                        "label": "churned ARR, $k",
                        "field": "churnedArrK",
                        "summary": False,
                    },
                    {"property": "Cash, $k", "label": "cash, $k", "field": "cashK", "summary": False},
                ],
                "question": "Is recurring revenue growing fast enough to reach the goal?",
                "definition": "Annual recurring revenue in thousands of dollars, as published in the weekly "
                "company metrics report.",
                "headline": "ARR ${value}k",
            },
            "roles": roles,
        }


PROJECTS = {"PLAT": "Platform", "IMPL": "Client implementations", "BIZ": "Business operations"}
JIRA_ACL = {
    "PLAT": ["grp:all-staff"],
    "IMPL": ["grp:all-staff"],
    "BIZ": ["grp:management", "grp:finance", "grp:sales", "grp:marketing", "grp:delivery"],
}
DELIVERY_HEAD = "LW-E03"
