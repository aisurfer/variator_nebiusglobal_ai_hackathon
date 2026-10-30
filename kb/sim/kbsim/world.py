"""The simulated world: deterministic state + append-only event log (ground truth, OrgForge-style).

Writers never mutate state: a generator turns their validated texts into WorldEvents through the mutation API below
and applies them here. The same `apply` path rebuilds the state from world.jsonl when a run is resumed. Ids, keys and
times are assigned here. (Plancy's prompt views of events for LLM agents are not part of this copy.)
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from kbsim.models import (
    Attachment,
    Commit,
    Company,
    Email,
    MergeRequest,
    MRComment,
    Page,
    Ticket,
    TicketComment,
    WikiPage,
    WorldEvent,
)

INITIAL_TICKET_NO = {"KD": 214, "SAAS": 387, "OPS": 96, "HR": 31}


class World:
    def __init__(self, company: Company, out_dir: Path):
        self.company = company
        self.out_dir = out_dir
        self.log_path = out_dir / "world.jsonl"
        self.people = {p.id: p for p in company.people}
        self.contacts = {c.id: c for c in company.contacts}
        self.orgs = {o.id: o for o in company.organizations}
        self.by_email = {p.email.lower(): p.id for p in company.people} | {c.email.lower(): c.id for c in company.contacts}
        self.tickets: dict[str, Ticket] = {}
        self.emails: dict[str, Email] = {}
        self.pages: dict[str, Page] = {}
        self.wikis: dict[str, WikiPage] = {}
        self.mrs: dict[str, MergeRequest] = {}
        self.commits: dict[str, Commit] = {}
        self.handles: dict[str, tuple[str, str]] = {}  # handle -> (artifact_type, id)
        self.beat_events: dict[str, list[int]] = defaultdict(list)  # beat id -> event seqs
        self.events: list[WorldEvent] = []
        self.counters: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------ persistence
    def load(self) -> int:
        if not self.log_path.exists():
            return 0
        with self.log_path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    self._apply(WorldEvent.model_validate_json(line))
        return len(self.events)

    def _append(self, ev: WorldEvent) -> WorldEvent:
        self._apply(ev)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(ev.model_dump_json() + "\n")
        return ev

    def _new_event(self, *, ts: datetime, day: int, actor: str, kind: str, artifact_type: str, artifact_id: str,
                   data: dict[str, Any], beat_id: str | None, handle: str | None) -> WorldEvent:
        return WorldEvent(seq=len(self.events) + 1, ts=ts, day=day, actor=actor, kind=kind,
                          artifact_type=artifact_type, artifact_id=artifact_id, data=data, beat_id=beat_id,
                          handle=handle)

    # ------------------------------------------------------------------ the single state transition
    def _apply(self, ev: WorldEvent) -> None:
        d = ev.data
        if ev.kind == "ticket.created":
            t = Ticket.model_validate(d)
            self.tickets[t.key] = t
            proj, _, no = t.key.partition("-")
            self.counters[f"ticket:{proj}"] = max(self.counters[f"ticket:{proj}"], int(no))
        elif ev.kind == "ticket.commented":
            t = self.tickets[ev.artifact_id]
            t.comments.append(TicketComment.model_validate(d["comment"]))
            t.updated = ev.ts
        elif ev.kind == "ticket.updated":
            t = self.tickets[ev.artifact_id]
            for k, v in (d.get("changes") or {}).items():
                setattr(t, k, v)
            if d.get("comment"):
                t.comments.append(TicketComment.model_validate(d["comment"]))
            t.updated = ev.ts
        elif ev.kind == "email.sent":
            e = Email.model_validate(d)
            self.emails[e.id] = e
            for a in e.attachments:
                self.handles.setdefault(a.meta.handle, ("doc", f"{e.id}#{a.doc_id}"))
        elif ev.kind == "page.created":
            p = Page.model_validate(d)
            self.pages[p.id] = p
        elif ev.kind == "page.updated":
            p = self.pages[ev.artifact_id]
            for k in ("body", "title", "properties", "decisions", "action_items", "attendees"):
                if d.get(k) is not None:
                    setattr(p, k, d[k])
            p.version += 1
            p.updated = ev.ts
        elif ev.kind == "wiki.created":
            w = WikiPage.model_validate(d)
            self.wikis[w.id] = w
        elif ev.kind == "wiki.updated":
            w = self.wikis[ev.artifact_id]
            for k in ("body", "title", "objects"):
                if d.get(k) is not None:
                    setattr(w, k, d[k])
            w.version += 1
            w.updated = ev.ts
        elif ev.kind == "mr.created":
            m = MergeRequest.model_validate(d)
            self.mrs[m.id] = m
            self.counters[f"mr:{m.repo}"] = max(self.counters[f"mr:{m.repo}"], m.iid)
        elif ev.kind == "mr.commented":
            m = self.mrs[ev.artifact_id]
            m.comments.append(MRComment.model_validate(d["comment"]))
            m.updated = ev.ts
        elif ev.kind == "mr.merged":
            m = self.mrs[ev.artifact_id]
            m.state, m.merged_at, m.merge_commit, m.updated = "merged", ev.ts, d.get("merge_commit"), ev.ts
        elif ev.kind == "commit.pushed":
            c = Commit.model_validate(d)
            self.commits[c.sha] = c
        for ident in self._ids_in(ev):
            kind, _, num = ident.rpartition("-")
            if kind in ("msg", "thr", "page", "comment", "note") and num.isdigit():
                self.counters[kind] = max(self.counters[kind], int(num))
        if ev.handle:
            self.handles[ev.handle] = (ev.artifact_type, ev.artifact_id)
        if ev.beat_id:
            self.beat_events[ev.beat_id].append(ev.seq)
        self.events.append(ev)

    @staticmethod
    def _ids_in(ev: WorldEvent) -> list[str]:
        """Ids allocated by next_id() that this event carries (restored into the counters on load)."""
        d = ev.data
        out = [ev.artifact_id]
        if ev.kind == "email.sent":
            out += [d.get("id", ""), d.get("thread_id", "")]
        c = d.get("comment") if isinstance(d.get("comment"), dict) else None
        if c:
            out.append(c.get("id", ""))
        return out

    # ------------------------------------------------------------------ id helpers
    def next_ticket_key(self, project: str) -> str:
        k = f"ticket:{project}"
        if self.counters[k] == 0:
            self.counters[k] = INITIAL_TICKET_NO.get(project, 10)
        self.counters[k] += 1
        return f"{project}-{self.counters[k]}"

    def next_id(self, kind: str, width: int = 6) -> str:
        self.counters[kind] += 1
        return f"{kind}-{self.counters[kind]:0{width}d}"

    def next_mr_iid(self, repo: str) -> int:
        k = f"mr:{repo}"
        if self.counters[k] == 0:
            self.counters[k] = 40
        self.counters[k] += 1
        return self.counters[k]

    def resolve_party(self, ref: str | None) -> str | None:
        """Persona id, contact id, e-mail address or a name -> persona/contact id (None if unknown)."""
        if not ref:
            return None
        r = ref.strip().strip("<>").strip()
        if r in self.people or r in self.contacts:
            return r
        if r.lower() in self.by_email:
            return self.by_email[r.lower()]
        low = r.lower()
        for pid, p in self.people.items():
            if p.name.lower() == low or p.principal.lower() == low:
                return pid
        for cid, c in self.contacts.items():
            if c.name.lower() == low:
                return cid
        return None

    def party_email(self, pid: str) -> str:
        if pid in self.people:
            return self.people[pid].email
        if pid in self.contacts:
            return self.contacts[pid].email
        return pid

    def party_name(self, pid: str) -> str:
        if pid in self.people:
            return self.people[pid].name
        if pid in self.contacts:
            return self.contacts[pid].name
        return pid

    # ------------------------------------------------------------------ mutations (engine API)
    def create_ticket(self, *, ts: datetime, day: int, actor: str, project: str, issue_type: str, summary: str,
                      description: str, assignee: str | None, priority: str, labels: list[str],
                      parent: str | None = None, links: list[dict] | None = None, remote_links: list[dict] | None = None,
                      beat_id: str | None = None, handle: str | None = None) -> str:
        key = self.next_ticket_key(project)
        t = Ticket(key=key, project=project, issue_type=issue_type, summary=summary, description=description,
                   status="To Do", priority=priority, reporter=actor, assignee=assignee, labels=labels, parent=parent,
                   links=links or [], remote_links=remote_links or [], created=ts, updated=ts)
        self._append(self._new_event(ts=ts, day=day, actor=actor, kind="ticket.created", artifact_type="ticket",
                                     artifact_id=key, data=t.model_dump(mode="json"), beat_id=beat_id, handle=handle))
        return key

    def comment_ticket(self, *, ts: datetime, day: int, actor: str, key: str, body: str, beat_id: str | None = None,
                       handle: str | None = None) -> str:
        cid = self.next_id("comment")
        c = TicketComment(id=cid, author=actor, body=body, created=ts)
        self._append(self._new_event(ts=ts, day=day, actor=actor, kind="ticket.commented", artifact_type="ticket",
                                     artifact_id=key, data={"comment": c.model_dump(mode="json")}, beat_id=beat_id,
                                     handle=handle))
        return cid

    def update_ticket(self, *, ts: datetime, day: int, actor: str, key: str, changes: dict[str, Any],
                      comment: str | None = None, beat_id: str | None = None, handle: str | None = None) -> None:
        data: dict[str, Any] = {"changes": changes}
        if comment:
            data["comment"] = TicketComment(id=self.next_id("comment"), author=actor, body=comment,
                                            created=ts).model_dump(mode="json")
        self._append(self._new_event(ts=ts, day=day, actor=actor, kind="ticket.updated", artifact_type="ticket",
                                     artifact_id=key, data=data, beat_id=beat_id, handle=handle))

    def send_email(self, *, ts: datetime, day: int, sender: str, to: list[str], cc: list[str], subject: str,
                   body: str, reply_to: str | None = None, attachments: list[Attachment] | None = None,
                   acl: list[str] | None = None, beat_id: str | None = None, handle: str | None = None) -> str:
        eid = self.next_id("msg")
        parent = self.emails.get(reply_to) if reply_to else None
        thread = parent.thread_id if parent else self.next_id("thr")
        domain = self.contacts[sender].email.split("@")[-1] if sender in self.contacts else self.company.tools.mail_domain
        msg_id = f"<{ts.strftime('%Y%m%d.%H%M%S')}.{eid.split('-')[1]}@{domain}>"
        e = Email(id=eid, message_id=msg_id, thread_id=thread, in_reply_to=parent.message_id if parent else None,
                  sender=sender, to=to, cc=cc, subject=subject, body=body, sent=ts, attachments=attachments or [],
                  mailbox_acl=acl or [])
        self._append(self._new_event(ts=ts, day=day, actor=sender, kind="email.sent", artifact_type="email",
                                     artifact_id=eid, data=e.model_dump(mode="json"), beat_id=beat_id, handle=handle))
        return eid

    def create_page(self, *, ts: datetime, day: int, actor: str, space: str, title: str, body: str, kind: str,
                    attendees: list[str], decisions: list[str], action_items: list[dict], properties: dict[str, str],
                    held_at: datetime | None, beat_id: str | None = None, handle: str | None = None) -> str:
        pid = self.next_id("page", 4)
        p = Page(id=pid, space=space, title=title, kind=kind, body=body, author=actor, attendees=attendees,
                 held_at=held_at, decisions=decisions, action_items=action_items, properties=properties, created=ts,
                 updated=ts)
        self._append(self._new_event(ts=ts, day=day, actor=actor, kind="page.created", artifact_type="page",
                                     artifact_id=pid, data=p.model_dump(mode="json"), beat_id=beat_id, handle=handle))
        return pid

    def update_page(self, *, ts: datetime, day: int, actor: str, page_id: str, body: str | None, title: str | None,
                    decisions: list[str] | None = None, action_items: list[dict] | None = None,
                    properties: dict[str, str] | None = None, beat_id: str | None = None,
                    handle: str | None = None) -> None:
        data = {"body": body, "title": title, "decisions": decisions, "action_items": action_items,
                "properties": properties}
        self._append(self._new_event(ts=ts, day=day, actor=actor, kind="page.updated", artifact_type="page",
                                     artifact_id=page_id, data=data, beat_id=beat_id, handle=handle))

    def upsert_wiki(self, *, ts: datetime, day: int, actor: str, space: str, page: str, title: str, body: str,
                    objects: dict[str, str] | None = None, beat_id: str | None = None, handle: str | None = None) -> str:
        wid = page if "." in page else f"{space}.{page}"
        if wid in self.wikis:
            self._append(self._new_event(ts=ts, day=day, actor=actor, kind="wiki.updated", artifact_type="wiki",
                                         artifact_id=wid, data={"body": body, "title": title, "objects": objects},
                                         beat_id=beat_id, handle=handle))
        else:
            w = WikiPage(id=wid, space=wid.split(".")[0], title=title, body=body, author=actor,
                         objects=objects or {}, created=ts, updated=ts)
            self._append(self._new_event(ts=ts, day=day, actor=actor, kind="wiki.created", artifact_type="wiki",
                                         artifact_id=wid, data=w.model_dump(mode="json"), beat_id=beat_id,
                                         handle=handle))
        return wid

    def create_mr(self, *, ts: datetime, day: int, actor: str, repo: str, title: str, description: str,
                  reviewers: list[str], closes: list[str], beat_id: str | None = None, handle: str | None = None) -> str:
        iid = self.next_mr_iid(repo)
        mid = f"{repo}!{iid}"
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or f"mr-{iid}"
        m = MergeRequest(id=mid, repo=repo, iid=iid, title=title, description=description, author=actor,
                         reviewers=reviewers, source_branch=f"feature/{slug}", closes=closes, created=ts, updated=ts)
        self._append(self._new_event(ts=ts, day=day, actor=actor, kind="mr.created", artifact_type="mr",
                                     artifact_id=mid, data=m.model_dump(mode="json"), beat_id=beat_id, handle=handle))
        return mid

    def comment_mr(self, *, ts: datetime, day: int, actor: str, mr_id: str, body: str, beat_id: str | None = None,
                   handle: str | None = None) -> None:
        c = MRComment(id=self.next_id("note"), author=actor, body=body, created=ts)
        self._append(self._new_event(ts=ts, day=day, actor=actor, kind="mr.commented", artifact_type="mr",
                                     artifact_id=mr_id, data={"comment": c.model_dump(mode="json")}, beat_id=beat_id,
                                     handle=handle))

    def merge_mr(self, *, ts: datetime, day: int, actor: str, mr_id: str, beat_id: str | None = None,
                 handle: str | None = None) -> str:
        import hashlib

        sha = hashlib.sha1(f"{mr_id}{ts.isoformat()}".encode()).hexdigest()
        self._append(self._new_event(ts=ts, day=day, actor=actor, kind="mr.merged", artifact_type="mr",
                                     artifact_id=mr_id, data={"merge_commit": sha}, beat_id=beat_id, handle=handle))
        m = self.mrs[mr_id]
        c = Commit(sha=sha, repo=m.repo, author=m.author, message=f"Merge branch '{m.source_branch}' into 'main'\n\n"
                   f"{m.title}\n\nSee {m.id}", created=ts)
        self._append(self._new_event(ts=ts, day=day, actor=actor, kind="commit.pushed", artifact_type="commit",
                                     artifact_id=sha, data=c.model_dump(mode="json"), beat_id=None, handle=None))
        return sha

    # ------------------------------------------------------------------ visibility and inboxes
    def container_acl(self, tool: str, key: str | None) -> list[str]:
        cont = getattr(self.company.tools, tool, {}) or {}
        c = cont.get(key) if key else None
        return list(c.acl) if c else ["grp:all-staff"]

    def can_read_email(self, persona_id: str, e: Email) -> bool:
        p = self.people.get(persona_id)
        if p is None:
            return False
        return persona_id in {e.sender, *e.to, *e.cc} or bool((set(p.groups) | {p.principal}) & set(e.mailbox_acl))

    def can_see(self, persona_id: str, ev: WorldEvent) -> bool:
        p = self.people.get(persona_id)
        if p is None:
            return False
        groups = set(p.groups) | {p.principal}
        if ev.artifact_type == "email":
            e = self.emails[ev.artifact_id]
            if persona_id in {e.sender, *e.to, *e.cc}:
                return True
            return bool(groups & set(e.mailbox_acl))
        if ev.artifact_type == "ticket":
            return bool(groups & set(self.container_acl("jira", self.tickets[ev.artifact_id].project)))
        if ev.artifact_type == "page":
            return bool(groups & set(self.container_acl("confluence", self.pages[ev.artifact_id].space)))
        if ev.artifact_type == "wiki":
            return bool(groups & set(self.container_acl("xwiki", self.wikis[ev.artifact_id].space)))
        if ev.artifact_type in ("mr", "commit"):
            repo = self.mrs[ev.artifact_id].repo if ev.artifact_type == "mr" else self.commits[ev.artifact_id].repo
            return bool(groups & set(self.container_acl("gitlab", repo)))
        return False

    def _mentions(self, persona_id: str, text: str) -> bool:
        p = self.people[persona_id]
        first = p.name.split()[0]
        return first in text or p.name in text

    def relevant(self, persona_id: str, ev: WorldEvent) -> bool:
        """Is this event something the persona would notice (their mail, their tickets, mentions, reviews)?"""
        if ev.actor == persona_id or not self.can_see(persona_id, ev):
            return False
        if ev.artifact_type == "email":
            e = self.emails[ev.artifact_id]
            if persona_id in e.to or persona_id in e.cc:
                return True
            boxes = {k.lower(): v for k, v in self.company.tools.group_mailboxes.items()}
            box_groups = {g for r in [*e.to, *e.cc] for g in boxes.get(str(r).lower(), [])}
            return bool(set(self.people[persona_id].groups) & box_groups)
        if ev.artifact_type == "ticket":
            t = self.tickets[ev.artifact_id]
            commenters = {c.author for c in t.comments}
            text = json.dumps(ev.data, ensure_ascii=False)
            return persona_id in {t.reporter, t.assignee} or persona_id in commenters or self._mentions(persona_id, text)
        if ev.artifact_type == "mr":
            m = self.mrs[ev.artifact_id]
            return persona_id in {m.author, *m.reviewers} or self._mentions(persona_id, json.dumps(ev.data, ensure_ascii=False))
        if ev.artifact_type == "page":
            p = self.pages[ev.artifact_id]
            return persona_id in p.attendees or persona_id == p.author or self._mentions(persona_id, p.body)
        if ev.artifact_type == "wiki":
            return self._mentions(persona_id, json.dumps(ev.data, ensure_ascii=False))
        return False

    def events_since(self, seq: int) -> Iterable[WorldEvent]:
        return self.events[seq:]
