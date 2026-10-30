"""Densification: the everyday work around the scenario, added to a finished world.

A 20-person company produces about a thousand Jira tickets and a thousand meetings in a hundred days; the scenario
alone yields the storylines, not this volume. Densification adds it the same way the simulator works: the engine
owns every fact (keys, people, times, statuses, links, numbers) and plans everything up front, deterministically;
the subagent swarm only writes the texts, in two parallel passes:

  plan        tickets with a full life cycle (To Do → In Progress → Review → Ready for QA → Done, reopenings,
              Won't Do, absences respected), epics, sub-tasks, "blocks" links, tickets spawned by planning meetings
              (remote link "informed by"), tickets implementing earlier decisions ("implements"), regressions caused by
              a merged MR ("caused by"), merge requests closing tickets, meeting series with attendees and the tickets
              they talk about, weekly product metrics with engine-owned numbers (the North Star);
  pass 1      ticket summaries and descriptions (dz_tickets requests);
  pass 2      transition and discussion comments, merge request texts and review notes, meeting notes, metric
              commentaries (dz_comments, dz_mrs, dz_meetings, dz_kpi) — they cite the ticket texts of pass 1;
  apply       every artefact through the World API on a copy of the world, then the whole journal re-sorted by time
              (the exporter replays in seq order), with a backup of the previous world.jsonl.

    cd kb && PYTHONPATH=sim:app python -m kbsim.densify company plan
    ... prepare 1 | list --phase 1 --shard 1/9 | check <id> | prepare 2 | apply | status

Writers via files: `prepare` writes request files sim/out/<t>/rounds/densify-<phase>/req-<id>.json (who writes, the
system text, the prompt, the response format, the response path, the checks); a writer (a Nemotron developer agent subagent) saves
the JSON answer to `response_path` and runs `check <id>` until it is ok; `apply` takes the checked answers.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import random
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from kbsim.tenant import SIM_ROOT, TenantState
from kbsim.world import INITIAL_TICKET_NO, World

DAY_START, DAY_END = time(9, 30), time(18, 30)
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
OPEN = ("In Progress", "Review", "Ready for QA", "Reopened")
KINDS = ("dz_tickets", "dz_comments", "dz_mrs", "dz_meetings", "dz_kpi")


# ================================================================================================ plan model
@dataclass
class Transition:
    at: str
    to: str
    actor: str
    comment: str | None = None  # start | review | qa_reopen | qa_done | done | wont_do | unblocked


@dataclass
class TicketStub:
    key: str
    project: str
    itype: str
    component: str
    created: str
    reporter: str
    assignee: str
    priority: str
    parent: str | None = None
    blocked_by: str | None = None
    origin: dict | None = None  # {"kind": "meeting"|"decision"|"regression", ...}
    transitions: list[Transition] = field(default_factory=list)
    discussion: list[dict] = field(default_factory=list)  # [{"at", "author"}]
    mr: int | None = None
    theme: str | None = None  # epics: the theme; sub-tasks: the parent's theme


@dataclass
class MRStub:
    n: int
    repo: str
    key: str
    author: str
    reviewer: str
    created: str
    merged_at: str | None
    notes: list[dict] = field(default_factory=list)  # [{"at", "author"}]


@dataclass
class MeetingStub:
    n: int
    series: str
    title: str
    kind: str
    space: str
    held_at: str
    minutes: int
    organizer: str
    attendees: list[str]
    externals: list[str]
    client: str | None = None
    refs: list[str] = field(default_factory=list)  # ticket keys to talk about
    spawned: list[str] = field(default_factory=list)  # ticket keys created from its action items
    context: list[str] = field(default_factory=list)  # recent letters / decisions (one line each)


@dataclass
class KpiStub:
    n: int
    published: str
    week_from: str
    week_to: str
    numbers: dict[str, Any]
    note: str | None = None


@dataclass
class Plan:
    tickets: list[TicketStub]
    mrs: list[MRStub]
    meetings: list[MeetingStub]
    kpis: list[KpiStub]
    components: dict[str, dict]
    version: int = 1

    def to_json(self) -> dict:
        return {**({"version": self.version} if self.version >= 2 else {}),
                "tickets": [asdict(t) for t in self.tickets], "mrs": [asdict(m) for m in self.mrs],
                "meetings": [asdict(m) for m in self.meetings], "kpis": [asdict(k) for k in self.kpis],
                "components": self.components}

    @classmethod
    def from_json(cls, d: dict) -> "Plan":
        return cls([TicketStub(**{**t, "transitions": [Transition(**x) for x in t["transitions"]]}) for t in d["tickets"]],
                   [MRStub(**m) for m in d["mrs"]], [MeetingStub(**m) for m in d["meetings"]],
                   [KpiStub(**k) for k in d["kpis"]], d["components"], int(d.get("version", 1)))


# ================================================================================================ calendar
class Calendar:
    def __init__(self, state: TenantState):
        self.d = state.d
        self.tz = self.d.tz
        self.days = state.company.company.days
        self.end = datetime.combine(self.d.date_of(self.days), DAY_END, tzinfo=self.tz)

    def day_of(self, t: datetime) -> int:
        return (t.astimezone(self.tz).date() - self.d.date_of(1)).days + 1

    def workdays(self) -> list[int]:
        return [d for d in range(1, self.days + 1) if self.d.is_workday(d)]

    def at(self, day: int, hhmm: str) -> datetime:
        h, m = map(int, hhmm.split(":"))
        return datetime.combine(self.d.date_of(day), time(h, m), tzinfo=self.tz)

    def is_work(self, t: datetime) -> bool:
        lt = t.astimezone(self.tz)
        return self.d.is_workday(self.day_of(lt)) and DAY_START <= lt.time() < DAY_END

    def add_hours(self, t: datetime, hours: float) -> datetime:
        """`hours` of working time after t (Mon–Fri 09:30–18:30 local, holidays skipped)."""
        left = timedelta(hours=hours)
        cur = self.next_work(t)
        while left > timedelta(0):
            day_end = datetime.combine(cur.astimezone(self.tz).date(), DAY_END, tzinfo=self.tz)
            if cur >= day_end:  # past the calendar's horizon (next_work stops there): no working days left to count
                return cur + left
            step = min(left, day_end - cur)
            cur += step
            left -= step
            if left > timedelta(0):
                cur = self.next_work(day_end)
        return cur

    def next_work(self, t: datetime) -> datetime:
        lt = t.astimezone(self.tz)
        while True:
            day = self.day_of(lt)
            if day > self.days + 30:
                return lt
            start = datetime.combine(lt.date(), DAY_START, tzinfo=self.tz)
            end = datetime.combine(lt.date(), DAY_END, tzinfo=self.tz)
            if self.d.is_workday(day) and lt < end:
                return max(lt, start)
            lt = datetime.combine(lt.date() + timedelta(days=1), DAY_START, tzinfo=self.tz)

    def available(self, pid: str, t: datetime) -> bool:
        return not self.d.absent(pid, self.day_of(t))

    def back_from(self, pid: str, t: datetime) -> datetime:
        """The first working moment at or after t when pid is not on leave."""
        cur = self.next_work(t)
        for _ in range(40):
            if self.available(pid, cur):
                return cur
            cur = self.next_work(datetime.combine(cur.astimezone(self.tz).date() + timedelta(days=1), DAY_START,
                                                  tzinfo=self.tz))
        return cur


def audience(w: World, tool: str, key: str) -> set[str]:
    """Employees who can read a container (a Jira project, a Confluence space)."""
    acl = set(w.container_acl(tool, key))
    return {pid for pid, p in w.people.items() if (set(p.groups) | {p.principal}) & acl}


def on_topic(comp: dict, text: str) -> bool:
    low = text.lower()
    return any(str(x).lower() in low for x in comp.get("topics") or [])


def iso(t: datetime) -> str:
    return t.isoformat()


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ================================================================================================ planner
PLAN_VERSION = 2  # 1: one random stream for the whole plan (older plan.json files); 2: a stream per part of the plan


def derived_rng(seed: Any, label: str) -> random.Random:
    """An independent stream per part of the plan: a new feature drawing from its own stream leaves the others."""
    return random.Random(int.from_bytes(hashlib.sha256(f"{seed}:{label}".encode()).digest()[:8], "big"))


class Planner:
    def __init__(self, state: TenantState, cfg: dict):
        self.s, self.cfg = state, cfg
        self.w = state.w
        self.cal = Calendar(state)
        self.seed = cfg.get("seed", 7)
        self.version = int(cfg.get("plan_version", 1))
        self.r = random.Random(self.seed)  # version 1: every part draws from this one stream, in plan order
        self._streams: dict[str, random.Random] = {}
        self.comps: dict[str, dict] = {}
        for proj, p in cfg["projects"].items():
            for c in p["components"]:
                self.comps[c["id"]] = {**c, "project": proj}
        self.leads: dict[str, str] = cfg.get("leads") or {}
        self.qa: str = cfg.get("qa", "qa")
        self.reviewers: list[str] = cfg.get("reviewers") or []
        self.bug_watcher: str | None = cfg.get("bug_watcher")
        self.counters = {proj: self.w.counters.get(f"ticket:{proj}") or INITIAL_TICKET_NO.get(proj, 10)
                         for proj in cfg["projects"]}
        self.tickets: list[TicketStub] = []
        self.mrs: list[MRStub] = []
        self.meetings: list[MeetingStub] = []
        self.epics: dict[str, list[TicketStub]] = {p: [] for p in cfg["projects"]}
        # when the scenario is in touch with each organisation: client work and vendor calls stay inside that window
        self.org_mail: dict[str, list[datetime]] = {}
        for e in sorted(self.w.emails.values(), key=lambda e: e.sent):
            for pid in {e.sender, *e.to, *e.cc}:
                if pid in self.w.contacts:
                    self.org_mail.setdefault(self.w.contacts[pid].org, []).append(e.sent)

    # -------------------------------------------------------------------------------------------- helpers
    def rng(self, label: str) -> random.Random:
        """The random stream of a part of the plan: tickets, epics, origins, lifecycle, mrs, discussion, meetings,
        kpi. Version 1 plans share one stream (kept for plan.json files made before streams existed)."""
        if self.version < 2:
            return self.r
        if label not in self._streams:
            self._streams[label] = derived_rng(self.seed, label)
        return self._streams[label]

    def pick(self, xs: list[str], t: datetime, exclude: tuple[str, ...] = (), r: random.Random | None = None) -> str:
        avail = [x for x in xs if x not in exclude and (x not in self.w.people or self.cal.available(x, t))]
        return (r or self.r).choice(avail or [x for x in xs if x not in exclude] or xs)

    def org_active(self, org: str | None, t: datetime, grace_days: int = 10) -> bool:
        ts = self.org_mail.get(org or "")
        if org is None:
            return True
        return bool(ts) and ts[0] <= t <= ts[-1] + timedelta(days=grace_days)

    def available_at(self, pid: str, t: datetime) -> datetime:
        return self.cal.back_from(pid, t) if pid in self.w.people else t

    def when(self, day: int, r: random.Random | None = None) -> datetime:
        mins = (r or self.r).randint(0, int((DAY_END.hour * 60 + DAY_END.minute) - (DAY_START.hour * 60 + DAY_START.minute)) - 20)
        return datetime.combine(self.cal.d.date_of(day), DAY_START, tzinfo=self.cal.tz) + timedelta(minutes=mins)

    def new_key(self, project: str) -> str:
        self.counters[project] += 1
        return f"{project}-{self.counters[project]}"

    def status_at(self, t: TicketStub, at: datetime) -> str | None:
        if dt(t.created) > at:
            return None
        st = "To Do"
        for x in t.transitions:
            if dt(x.at) <= at:
                st = x.to
        return st

    # -------------------------------------------------------------------------------------------- tickets
    def plan(self) -> Plan:
        days = self.cal.workdays()
        meetings_by_day = self.plan_meetings(days)
        for day in days:
            self.plan_epics(day)
            n = max(4, int(self.rng("tickets").gauss(self.cfg["tickets_per_workday"], 3)))
            for _ in range(n):
                self.new_ticket(day, meetings_by_day.get(day, []))
        # keys follow creation time inside each project (as Jira numbers them)
        self.renumber()
        self.attach_meeting_refs()
        kpis = self.plan_kpis(days)
        return Plan(self.tickets, self.mrs, self.meetings, kpis, self.comps, self.version)

    def plan_epics(self, day: int) -> None:
        d = self.cal.d.date_of(day)
        first = day == self.cal.workdays()[0] or (d.day <= 3 and d.weekday() == 0) or (d.day == 1 and d.weekday() < 5)
        if not first:
            return
        r = self.rng("epics")
        for proj, p in self.cfg["projects"].items():
            if any(dt(e.created).month == d.month for e in self.epics[proj]):
                continue
            for comp in r.sample(p["components"], k=min(len(p["components"]), p.get("epics_per_month", 1))):
                t0 = self.when(day, r)
                if not self.org_active(comp.get("client"), t0):
                    continue
                lead = self.pick([self.leads.get(proj, comp["assignees"][0])] + comp["reporters"], t0, r=r)
                e = TicketStub(key="", project=proj, itype="Epic", component=comp["id"], created=iso(t0), reporter=lead,
                               assignee=self.pick([lead] + comp["assignees"], t0, r=r), priority="High")
                start = self.available_at(e.assignee, self.cal.add_hours(t0, r.uniform(1, 12)))
                e.transitions.append(Transition(iso(start), "In Progress", e.assignee))
                done = self.available_at(e.assignee, self.cal.add_hours(start, r.uniform(140, 320)))
                if done < self.cal.end:
                    e.transitions.append(Transition(iso(done), "Done", e.assignee, "done"))
                self.epics[proj].append(e)
                self.tickets.append(e)

    def new_ticket(self, day: int, meetings: list[MeetingStub]) -> TicketStub:
        r = self.rng("tickets")
        t0 = self.when(day, r)
        live = {p: [c for c in v["components"] if self.org_active(c.get("client"), t0)] for p, v in self.cfg["projects"].items()}
        projs = [p for p in live if live[p]]
        proj = r.choices(projs, [self.cfg["projects"][p]["weight"] for p in projs])[0]
        comp = r.choice(live[proj])
        mix = self.cfg["issue_mix"] if proj in self.cfg["qa_projects"] else {"Task": 0.8, "Story": 0.1, "Bug": 0.1}
        itype = r.choices(list(mix), list(mix.values()))[0]
        origin = None
        spawning = [m for m in meetings if self.series(m.series).get("refs") == "spawn"
                    and proj in (self.series(m.series).get("projects") or [proj]) and dt(m.held_at) < t0]
        if spawning and r.random() < 0.55:
            m = r.choice(spawning)
            t0 = dt(m.held_at) + timedelta(minutes=m.minutes + r.randint(5, 90))
            origin = {"kind": "meeting", "meeting": m.n, "title": m.title, "held_at": m.held_at}
        reporter = origin and self.meetings[origin["meeting"]].organizer or self.pick(comp["reporters"], t0, r=r)
        assignee = self.pick(comp["assignees"], t0, exclude=(reporter,) if len(comp["assignees"]) > 1 else (), r=r)
        if itype == "Sub-task":
            parents = [x for x in self.tickets if x.component == comp["id"] and x.itype in ("Story", "Task")
                       and self.status_at(x, t0) not in (None, "Done", "Won't Do")]
            if not parents:
                itype = "Task"
        prio = r.choices(["Highest", "High", "Medium", "Low"], [5, 35, 50, 10] if itype == "Bug" else [2, 25, 58, 15])[0]
        t = TicketStub(key="", project=proj, itype=itype, component=comp["id"], created=iso(t0), reporter=reporter,
                       assignee=assignee, priority=prio, origin=origin)
        if itype == "Sub-task":
            p = r.choice(parents)
            t.parent, t.theme = p.key or f"#{self.tickets.index(p)}", p.theme
        elif itype in ("Story", "Task") and r.random() < 0.7:
            same = [e for e in self.epics[proj] if e.component == comp["id"] and dt(e.created) <= t0
                    and self.status_at(e, t0) != "Done"]
            if same:
                t.parent = f"#{self.tickets.index(r.choice(same))}"
        if origin is None:
            self.maybe_origin(t, t0)
        if itype in ("Task", "Story") and r.random() < 0.08:
            blockers = [x for x in self.tickets if x.component == comp["id"] and x.itype != "Epic"
                        and self.status_at(x, t0) in ("To Do", "In Progress")]
            if blockers:
                t.blocked_by = f"#{self.tickets.index(r.choice(blockers))}"
        self.tickets.append(t)
        self.lifecycle(t, comp)
        if origin and origin["kind"] == "meeting":
            self.meetings[origin["meeting"]].spawned.append(f"#{len(self.tickets) - 1}")
        return t

    def maybe_origin(self, t: TicketStub, t0: datetime) -> None:
        comp = self.comps[t.component]
        r = self.rng("origins")
        if t.itype == "Bug" and comp.get("repo") and r.random() < 0.25:
            mrs = [m for m in self.mrs if m.repo == comp["repo"] and m.merged_at and t0 - timedelta(days=10) < dt(m.merged_at) < t0]
            if mrs:
                m = r.choice(mrs)
                t.origin = {"kind": "regression", "mr": m.n, "key": m.key}
                return
        if r.random() < 0.2:
            decisions = [d for d in self.decisions_before(t0, 21, audience(self.w, "jira", t.project)) if on_topic(comp, d[1])]
            if decisions:
                handle, text, when = r.choice(decisions)
                t.origin = {"kind": "decision", "handle": handle, "text": text, "date": when}

    def decisions_before(self, t0: datetime, days: int, readers: set[str]) -> list[tuple[str, str, str]]:
        """Decisions of the last `days` that every reader of the new artefact may see (no leaks via the context)."""
        out = []
        for ev in self.w.events:
            if ev.kind != "page.created" or not ev.handle or not ev.data.get("decisions"):
                continue
            if t0 - timedelta(days=days) <= ev.ts < t0 and all(self.w.can_see(p, ev) for p in readers):
                for dtext in ev.data["decisions"][:3]:
                    out.append((ev.handle, dtext, ev.ts.date().isoformat()))
        return out

    def lifecycle(self, t: TicketStub, comp: dict) -> None:
        c0 = dt(t.created)
        r = self.rng("lifecycle")
        rm, rd = self.rng("mrs"), self.rng("discussion")
        if t.itype == "Epic":
            return
        if r.random() < 0.04:  # dropped before work started
            at = self.available_at(t.reporter, self.cal.add_hours(c0, r.uniform(8, 60)))
            if at < self.cal.end:
                t.transitions.append(Transition(iso(at), "Won't Do", t.reporter, "wont_do"))
            return
        delay = {"Highest": (0.2, 3), "High": (1, 14), "Medium": (6, 40), "Low": (20, 90)}[t.priority]
        start = self.cal.back_from(t.assignee, self.cal.add_hours(c0, r.uniform(*delay)))
        work = {"Bug": (3, 22), "Task": (6, 40), "Story": (14, 70), "Sub-task": (3, 16)}[t.itype]
        qa = t.project in self.cfg["qa_projects"] and comp.get("repo") is not None
        cur = start
        steps: list[Transition] = [Transition(iso(cur), "In Progress", t.assignee, "start" if r.random() < 0.25 else None)]
        rounds = 0
        while True:
            cur = self.cal.back_from(t.assignee, self.cal.add_hours(cur, r.uniform(*work) / (1 + rounds)))
            if qa:
                steps.append(Transition(iso(cur), "Review", t.assignee, "review" if r.random() < 0.35 else None))
                if t.mr is None:
                    reviewer = self.pick([x for x in self.reviewers if x != t.assignee] or self.reviewers, cur, r=rm)
                    t.mr = len(self.mrs)
                    self.mrs.append(MRStub(len(self.mrs), comp["repo"], "", t.assignee, reviewer, iso(cur), None))
                mr = self.mrs[t.mr]
                hours = r.uniform(1, 14)
                rev = self.available_at(mr.reviewer, self.cal.add_hours(cur, hours))
                if mr.merged_at is None:
                    for _ in range(rm.choice([0, 1, 1, 2, 3])):
                        at = self.cal.add_hours(cur, rm.uniform(0.1, hours))
                        who = rm.choice([mr.reviewer, mr.author])
                        if self.cal.available(who, at) and at < rev:
                            mr.notes.append({"at": iso(at), "author": who})
                    mr.notes.sort(key=lambda n: n["at"])
                cur = rev
                steps.append(Transition(iso(cur), "Ready for QA", mr.reviewer))
                if mr.merged_at is None:
                    mr.merged_at = iso(cur)
                qa_done = self.available_at(self.qa, self.cal.add_hours(cur, r.uniform(1, 14)))
                reopen = r.random() < (0.18 if t.itype == "Bug" else 0.08) and rounds < 2
                if reopen:
                    steps.append(Transition(iso(qa_done), "Reopened", self.qa, "qa_reopen"))
                    cur = self.cal.add_hours(qa_done, r.uniform(0.5, 6))
                    steps.append(Transition(iso(self.cal.back_from(t.assignee, cur)), "In Progress", t.assignee))
                    rounds += 1
                    continue
                steps.append(Transition(iso(qa_done), "Done", self.qa, "qa_done" if r.random() < 0.4 else None))
            else:
                if r.random() < 0.3:
                    steps.append(Transition(iso(cur), "Review", t.assignee))
                    cur = self.available_at(t.assignee, self.cal.add_hours(cur, r.uniform(1, 10)))
                steps.append(Transition(iso(cur), "Done", t.assignee, "done" if r.random() < 0.3 else None))
            break
        t.transitions = [x for x in steps if dt(x.at) < self.cal.end]
        if t.mr is not None and self.mrs[t.mr].merged_at and dt(self.mrs[t.mr].merged_at) >= self.cal.end:
            self.mrs[t.mr].merged_at = None
        if t.mr is not None:
            mr = self.mrs[t.mr]
            mr.notes = [n for n in mr.notes if dt(n["at"]) < self.cal.end]
            if dt(mr.created) >= self.cal.end:
                self.mrs.pop(t.mr)  # the last one: created by this ticket a moment ago
                t.mr = None
        end = dt(t.transitions[-1].at) if t.transitions and t.transitions[-1].to in ("Done", "Won't Do") else self.cal.end
        people = sorted({t.reporter, t.assignee, self.leads.get(t.project, t.reporter),
                         self.bug_watcher if t.itype == "Bug" and self.bug_watcher else t.reporter})
        for _ in range(rd.choice([0, 0, 1, 1, 2] if t.itype != "Bug" else [0, 1, 1, 2, 3])):
            at = self.cal.next_work(c0 + (end - c0) * rd.uniform(0.05, 0.9))
            who = rd.choice(people)
            if at < end and self.cal.available(who, at):
                t.discussion.append({"at": iso(at), "author": who})
        t.discussion.sort(key=lambda x: x["at"])

    def renumber(self) -> None:
        """Keys by creation time inside each project; '#i' references become keys."""
        order = sorted(range(len(self.tickets)), key=lambda i: self.tickets[i].created)
        for i in order:
            self.tickets[i].key = self.new_key(self.tickets[i].project)
        ref = lambda v: self.tickets[int(v[1:])].key if isinstance(v, str) and v.startswith("#") else v  # noqa: E731
        for t in self.tickets:
            t.parent, t.blocked_by = ref(t.parent), ref(t.blocked_by)
        for e in self.tickets:
            if e.itype == "Epic":
                e.theme = self.comps[e.component]["name"]
        for t in self.tickets:
            if t.itype == "Sub-task" and t.parent:
                t.theme = next((x.theme for x in self.tickets if x.key == t.parent), None)
        for m in self.mrs:
            m.key = next(t.key for t in self.tickets if t.mr == m.n)
        for t in self.tickets:
            if t.origin and t.origin.get("kind") == "regression":
                t.origin["key"] = self.mrs[t.origin["mr"]].key
        for m in self.meetings:
            m.spawned = [ref(x) for x in m.spawned]

    # -------------------------------------------------------------------------------------------- meetings
    def series(self, sid: str) -> dict:
        return next(s for s in self.cfg["meetings"] if s["id"] == sid)

    def plan_meetings(self, days: list[int]) -> dict[int, list[MeetingStub]]:
        out: dict[int, list[MeetingStub]] = {}
        rotation: dict[str, int] = {}
        week_no = lambda d: (self.cal.d.date_of(d) - self.cal.d.date_of(1)).days // 7  # noqa: E731
        for day in days:
            wd = WEEKDAYS[self.cal.d.date_of(day).weekday()]
            for s in self.cfg["meetings"]:
                cad = s["cadence"]
                if cad == "daily":
                    ok = True
                elif cad == "biweekly-fri":
                    ok = wd == "fri" and week_no(day) % 2 == 1
                else:
                    ok = wd in cad
                if not ok:
                    continue
                t0 = self.cal.at(day, s["time"])
                if not self.org_active(s.get("client"), t0):
                    continue
                skip = s.get("skip_if_title") or []
                skip = [x.lower() for x in ([skip] if isinstance(skip, str) else skip)]
                if skip and any(x in p.title.lower() and (p.held_at or p.created).astimezone(self.cal.tz).date() == t0.date()
                                for p in self.w.pages.values() for x in skip):
                    continue  # the scenario has its own notes of this meeting that day
                attendees = [a for a in s["attendees"] if a in self.w.people and self.cal.available(a, t0)]
                externals = [a for a in s["attendees"] if a in self.w.contacts]
                if s.get("rotate"):
                    i = rotation.get(s["id"], 0)
                    rotation[s["id"]] = i + 1
                    other = s["rotate"][i % len(s["rotate"])]
                    if not self.cal.available(other, t0):
                        continue
                    attendees.append(other)
                if s.get("rotate_external"):
                    live = [c for c in s["rotate_external"] if self.org_active(self.w.contacts[c].org, t0, grace_days=3)]
                    if not live:
                        continue
                    i = rotation.get(s["id"], 0)
                    rotation[s["id"]] = i + 1
                    externals = [live[i % len(live)]]
                organizer = s["organizer"] if s["organizer"] in attendees else (attendees[0] if attendees else None)
                if organizer is None or len(attendees) + len(externals) < 2 and s["kind"] not in ("demo", "discovery"):
                    continue
                m = MeetingStub(len(self.meetings), s["id"], s["title"], s["kind"], s["space"], iso(t0), s["minutes"],
                                organizer, attendees, externals, s.get("client"))
                self.meetings.append(m)
                out.setdefault(day, []).append(m)
        return out

    def attach_meeting_refs(self) -> None:
        for m in self.meetings:
            s = self.series(m.series)
            t0 = dt(m.held_at)
            pool = [t for t in self.tickets if t.itype != "Epic" and dt(t.created) < t0]
            if s.get("projects"):
                pool = [t for t in pool if t.project in s["projects"]]
            if s.get("components"):
                pool = [t for t in pool if t.component in s["components"]]
            refs: list[TicketStub] = []
            mode = s.get("refs", "none")
            st = lambda t: self.status_at(t, t0)  # noqa: E731
            if mode == "in_progress":
                refs = [t for t in pool if st(t) in OPEN and t.assignee in m.attendees]
            elif mode in ("open", "spawn"):
                refs = sorted([t for t in pool if st(t) not in ("Done", "Won't Do")], key=lambda t: t.created)[-12:]
            elif mode == "open_bugs":
                refs = [t for t in pool if t.itype == "Bug" and st(t) not in ("Done", "Won't Do")]
            elif mode == "support_bugs":
                refs = [t for t in pool if t.itype == "Bug" and t.reporter == self.bug_watcher and dt(t.created) > t0 - timedelta(days=7)]
            elif mode == "done":
                refs = [t for t in pool if any(x.to == "Done" and t0 - timedelta(days=14) < dt(x.at) < t0 for x in t.transitions)]
            elif mode == "client":
                refs = [t for t in pool if st(t) not in ("Won't Do",) and (st(t) != "Done" or any(
                    x.to == "Done" and dt(x.at) > t0 - timedelta(days=7) for x in t.transitions))]
            elif mode == "assignee":
                other = [a for a in m.attendees if a != m.organizer]
                refs = [t for t in pool if t.assignee in other and st(t) not in ("Done", "Won't Do")]
            self.rng("meetings").shuffle(refs)
            m.refs = [t.key for t in refs[:8]]
            if mode in ("client", "clients"):
                m.context = self.client_context(m, t0)
            elif m.kind == "management":
                m.context = [f"{d} - decision: {text}" for _, text, d in
                             self.decisions_before(t0, 14, audience(self.w, "confluence", m.space))[:5]]

    def client_context(self, m: MeetingStub, t0: datetime) -> list[str]:
        """Recent letters with the meeting's clients that every attendee could read; subjects with figures are left
        out (amounts and dates of a deal stay where they were written)."""
        lines = []
        by_id = {ev.artifact_id: ev for ev in self.w.events if ev.kind == "email.sent"}
        for e in self.w.emails.values():
            if not (t0 - timedelta(days=7) <= e.sent < t0) or re.search(r"\d", e.subject):
                continue
            if not all(self.w.can_see(p, by_id[e.id]) for p in m.attendees):
                continue
            parties = [e.sender, *e.to, *e.cc]
            orgs = {self.w.contacts[p].org for p in parties if p in self.w.contacts}
            if (m.client and m.client in orgs) or (not m.client and orgs):
                who = self.w.party_name(e.sender)
                lines.append(f"{e.sent.astimezone(self.cal.tz):%m/%d} e-mail \"{e.subject}\" from {who}")
        return lines[-6:]

    # -------------------------------------------------------------------------------------------- North Star
    def plan_kpis(self, days: list[int]) -> list[KpiStub]:
        k = self.cfg.get("kpi")
        if not k:
            return []
        wd = WEEKDAYS.index(k["weekday"])
        mondays = [d for d in days if self.cal.d.date_of(d).weekday() == wd]
        cur = dict(k["start"])
        out = []
        events = {e["date"]: e for e in k.get("events", [])}
        os_ = k["online_share"]
        for i, day in enumerate(mondays):
            d = self.cal.d.date_of(day)
            wk_from, wk_to = d - timedelta(days=7), d - timedelta(days=1)
            ev = next((e for e2, e in events.items() if wk_from < date.fromisoformat(str(e2)) <= d), None)
            new = self.rng("kpi").randint(*k["weekly_new"]) if i else 0
            churn = self.rng("kpi").randint(*k["weekly_churn"]) if i else 0
            if ev:
                new += ev.get("new", 0)
                churn += ev.get("churn", 0)
            paying = cur["paying"] + new - churn
            active = round(paying * self.rng("kpi").uniform(*k["active_share"]))
            orders = round(active * self.rng("kpi").uniform(*k["orders_per_active"]))
            share = (os_["before"] if d < date.fromisoformat(str(os_["pilot_from"])) else os_["pilot"]
                     if d < date.fromisoformat(str(os_["launch_from"])) else self.rng("kpi").uniform(*os_["launch"]))
            mrr = paying * k["mrr_per_paying"] + self.rng("kpi").randint(-15, 15) * 1000
            cur = {"paying": paying, "active": active, "orders": orders, "mrr": mrr}
            out.append(KpiStub(i, iso(self.cal.at(day, k["time"])), wk_from.isoformat(), wk_to.isoformat(),
                               {"paying": paying, "new": new, "churned": churn, "active": active, "orders": orders,
                                "online_share": round(share * 100, 1), "mrr": mrr},
                               ev.get("note") if ev else None))
        return out


# ================================================================================================ requests
FORMAT = {
    "dz_tickets": 'JSON: {"tickets": [{"key": "SAAS-512", "summary": "…", "description": "…"}]} - exactly one item per key of the list.',
    "dz_comments": 'JSON: {"comments": [{"slot": "c12", "body": "…"}]} - one per slot.',
    "dz_mrs": 'JSON: {"mrs": [{"slot": "m3", "title": "SAAS-512: …", "description": "…", "notes": ["…"]}]} - notes: exactly as many review notes as stated.',
    "dz_meetings": 'JSON: {"meetings": [{"slot": "s7", "title": "…", "body": "…markdown…", "decisions": ["…"], "action_items": ["dev_back: … SAAS-512"]}]}',
    "dz_kpi": 'JSON: {"reports": [{"slot": "w3", "commentary": "…"}]}',
}
STYLE = ("Write in English, the way people write in an IT company: concrete, with domain details, no filler and no "
         "notes about the simulation. Do not invent major decisions, amounts or clients that are not in the context. "
         "No URLs.")


class Densifier:
    def __init__(self, tenant: str, root: Path | None = None):
        self.tenant = tenant
        self.root = root or SIM_ROOT
        self.s = TenantState(tenant, self.root)
        self.cfg = yaml.safe_load((self.root / "tenants" / tenant / "densify.yaml").read_text(encoding="utf-8"))
        self.dir = self.s.out / "densify"
        self.plan_path = self.dir / "plan.json"

    # -------------------------------------------------------------------------------------------- plan
    def make_plan(self) -> dict:
        if self.plan_path.exists():
            raise SystemExit(f"plan exists: {self.plan_path} (delete it to plan again)")
        plan = Planner(self.s, self.cfg).plan()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.plan_path.write_text(json.dumps(plan.to_json(), ensure_ascii=False), encoding="utf-8")
        return self.summary(plan)

    def load(self) -> Plan:
        return Plan.from_json(json.loads(self.plan_path.read_text(encoding="utf-8")))

    def summary(self, plan: Plan) -> dict:
        from collections import Counter

        return {"tickets": len(plan.tickets), "by_project": dict(Counter(t.project for t in plan.tickets)),
                "by_type": dict(Counter(t.itype for t in plan.tickets)),
                "transitions": sum(len(t.transitions) for t in plan.tickets),
                "reopened": sum(any(x.to == "Reopened" for x in t.transitions) for t in plan.tickets),
                "open_at_end": sum(1 for t in plan.tickets if not t.transitions or t.transitions[-1].to not in ("Done", "Won't Do")),
                "comments": sum(sum(1 for x in t.transitions if x.comment) + len(t.discussion) for t in plan.tickets),
                "mrs": len(plan.mrs), "merged": sum(1 for m in plan.mrs if m.merged_at), "mr_notes": sum(len(m.notes) for m in plan.mrs),
                "meetings": len(plan.meetings), "meetings_by_kind": dict(Counter(m.kind for m in plan.meetings)),
                "spawned": sum(len(m.spawned) for m in plan.meetings),
                "origins": dict(Counter((t.origin or {}).get("kind", "-") for t in plan.tickets)),
                "with_parent": sum(1 for t in plan.tickets if t.parent), "blocked": sum(1 for t in plan.tickets if t.blocked_by),
                "kpi_weeks": len(plan.kpis)}

    # -------------------------------------------------------------------------------------------- shared texts
    def who(self, pid: str) -> str:
        if pid in self.s.w.people:
            p = self.s.w.people[pid]
            return f"{p.name} ({pid}, {p.role})"
        if pid in self.s.w.contacts:
            c = self.s.w.contacts[pid]
            return f"{c.name} ({pid}, {c.role})"
        return pid

    def voices(self, pids: set[str]) -> str:
        lines = []
        for pid in sorted(pids):
            p = self.s.w.people.get(pid)
            if p:
                lines.append(f"- {p.name} ({pid}, {p.role}): {p.writing_style}")
        return "How the participants write:\n" + "\n".join(lines)

    def local(self, s: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
        return dt(s).astimezone(self.s.d.tz).strftime(fmt)

    def history(self, comp: dict, before: datetime, readers: set[str], n: int = 6) -> list[str]:
        """Scenario artefacts on the component's topics before a moment that every reader of the ticket may see
        (neither future facts nor restricted ones reach the writer)."""
        out = []
        for ev in self.s.w.events:
            if ev.ts >= before or ev.kind not in ("ticket.created", "email.sent", "page.created"):
                continue
            if not all(self.s.w.can_see(p, ev) for p in readers):
                continue
            title = ev.data.get("summary") or ev.data.get("subject") or ev.data.get("title") or ""
            if on_topic(comp, title):
                out.append(f"{ev.ts.astimezone(self.s.d.tz):%m/%d} {ev.kind.split('.')[0]}: {title}")
        return out[-n:]

    def write_request(self, phase: int, rid: str, kind: str, system: str, prompt: str, checks: dict) -> dict:
        rdir = self.s.out / "rounds" / f"densify-{phase}"
        rdir.mkdir(parents=True, exist_ok=True)
        req = {"id": rid, "kind": kind, "who": "", "who_name": f"the team of {self.s.company.company.name}",
               "handbook_path": str(self.s.handbook_path), "system": system, "prompt": prompt,
               "response_path": str(rdir / f"resp-{rid}.json"), "response_format": FORMAT[kind],
               "checks": {**checks, "banned": self.s.banned}}
        path = rdir / f"req-{rid}.json"
        path.write_text(json.dumps(req, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"id": rid, "kind": kind, "request_path": str(path)}

    # -------------------------------------------------------------------------------------------- pass 1
    def prepare1(self) -> dict:
        plan = self.load()
        groups: dict[tuple, list[TicketStub]] = {}
        for t in plan.tickets:
            half = dt(t.created).strftime("%Y-%m")
            groups.setdefault((t.project, t.component, half), []).append(t)
        reqs = []
        by_key = {t.key: t for t in plan.tickets}
        for (proj, comp, half), ts in sorted(groups.items()):
            for i in range(0, len(ts), 12):
                chunk = ts[i:i + 12]
                c = plan.components[comp]
                first = dt(chunk[0].created)
                lines = []
                for t in chunk:
                    line = (f"- {t.key} · {t.itype} · priority {t.priority} · created by {self.who(t.reporter)} "
                            f"{self.local(t.created)} · assignee {self.who(t.assignee)}")
                    if t.parent:
                        p = by_key.get(t.parent)
                        line += f" · parent {t.parent}" + (f" (epic \"{p.theme}\")" if p and p.itype == "Epic" else "")
                    if t.blocked_by:
                        line += f" · blocked by {t.blocked_by}"
                    o = t.origin or {}
                    if o.get("kind") == "meeting":
                        line += f" · created as a follow-up of the meeting \"{o['title']}\" {self.local(o['held_at'], '%Y-%m-%d')}"
                    elif o.get("kind") == "decision":
                        line += f" · implements the decision of {o['date']}: \"{o['text']}\""
                    elif o.get("kind") == "regression":
                        line += f" · regression after the changes for {o['key']} (the MR is already merged)"
                    if t.itype == "Epic":
                        line += f" · epic theme: {t.theme}"
                    lines.append(line)
                ctx = self.history(c, first, audience(self.s.w, "jira", proj))
                prompt = (f"Jira project {proj}, component \"{c['name']}\"" + (f", client {c['client']}" if c.get("client") else "")
                          + f". Period: {self.local(chunk[0].created, '%Y-%m-%d')}.\n\n"
                          + ("What happened on the topic before (for plausibility, do not retell it):\n" + "\n".join(ctx) + "\n\n" if ctx else "")
                          + "For every ticket write a summary (as in Jira: short and to the point, without the key) and a description:\n"
                          "- Bug: what broke, where, steps to reproduce, expected and actual result, environment;\n"
                          "- Task/Story/Sub-task: context, what to do, acceptance criteria (as a list);\n"
                          "- Epic: goal, scope of work, acceptance criteria of the epic.\n"
                          "The tickets differ in meaning; where an origin is given (meeting, decision, regression), build the "
                          "description on it. 3-12 sentences or bullet points per description.\n\nTickets:\n" + "\n".join(lines))
                reqs.append(self.write_request(1, f"dz1-{proj.lower()}-{comp}-{half}-{i // 12}", "dz_tickets",
                                               STYLE + "\n\n" + self.voices({t.reporter for t in chunk} | {t.assignee for t in chunk}),
                                               prompt, {"keys": [t.key for t in chunk], "types": {t.key: t.itype for t in chunk}}))
        return {"phase": 1, "requests": len(reqs)}

    def texts(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for p in (self.s.out / "rounds" / "densify-1").glob("resp-*.json"):
            for t in json.loads(p.read_text(encoding="utf-8")).get("tickets", []):
                out[t["key"]] = t
        return out

    # -------------------------------------------------------------------------------------------- pass 2
    def prepare2(self) -> dict:
        plan = self.load()
        tx = self.texts()
        missing = [t.key for t in plan.tickets if t.key not in tx]
        if missing:
            raise SystemExit(f"pass 1 incomplete: {len(missing)} tickets without text, e.g. {missing[:5]}")
        short = lambda k: f"{k} \"{tx[k]['summary']}\"" if k in tx else k  # noqa: E731
        by_key = {t.key: t for t in plan.tickets}
        planner = Planner(self.s, self.cfg)  # meeting context is recomputed from the world, not stored in the plan
        for m in plan.meetings:
            mode = planner.series(m.series).get("refs")
            if mode in ("client", "clients"):
                m.context = planner.client_context(m, dt(m.held_at))

        def state(k: str, at: str) -> str:
            t = by_key.get(k)
            if not t:
                return ""
            st = "To Do"
            for x in t.transitions:
                if x.at <= at:
                    st = x.to
            return f" - status at the time of the meeting: {st}, assignee {self.s.w.party_name(t.assignee)}"
        reqs = []
        # comments: transitions with a comment kind + discussion comments, by project and half-month
        slots: dict[tuple, list[dict]] = {}
        for t in plan.tickets:
            seq = 0
            for x in t.transitions:
                if x.comment:
                    seq += 1
                    slots.setdefault((t.project, dt(x.at).strftime("%Y-%m-%d")[:7]), []).append(
                        {"slot": f"{t.key}#t{seq}", "key": t.key, "at": x.at, "author": x.actor, "kind": x.comment, "to": x.to})
            for j, dsc in enumerate(t.discussion):
                slots.setdefault((t.project, dsc["at"][:7]), []).append(
                    {"slot": f"{t.key}#d{j + 1}", "key": t.key, "at": dsc["at"], "author": dsc["author"], "kind": "discussion"})
        hint = {"start": "takes the ticket into work: plan, question or risk", "review": "hands over to review: what is done, what to check",
                "qa_reopen": "QA sends the ticket back: what is wrong, steps, environment", "qa_done": "QA accepts: what was checked",
                "done": "closes the ticket: outcome", "wont_do": "closes as Won't Do: why we do not do it",
                "discussion": "discussion along the way: clarification, question, answer, status, disagreement"}
        for (proj, month), ss in sorted(slots.items()):
            for i in range(0, len(ss), 24):
                chunk = ss[i:i + 24]
                lines = [f"- [{c['slot']}] {short(c['key'])} · {self.local(c['at'])} · author {self.who(c['author'])} · "
                         f"{hint[c['kind']]}" + (f" (status -> {c['to']})" if c.get("to") else "")
                         + f"\n  ticket description: {tx[c['key']]['description'][:260].replace(chr(10), ' ')}" for c in chunk]
                reqs.append(self.write_request(2, f"dz2c-{proj.lower()}-{month}-{i // 24}", "dz_comments",
                                               STYLE + "\n\n" + self.voices({c["author"] for c in chunk if c["author"] in self.s.w.people}),
                                               f"Jira comments of project {proj}. Write one comment per slot, as its author, "
                                               "1-5 sentences, on the substance of the ticket; other tickets may be referred to by key.\n\n"
                                               + "\n".join(lines), {"slots": [c["slot"] for c in chunk]}))
        # merge requests
        for i in range(0, len(plan.mrs), 10):
            chunk = plan.mrs[i:i + 10]
            lines = [f"- [m{m.n}] repository {m.repo} · closes {short(m.key)} · author {self.who(m.author)} · reviewer "
                     f"{self.who(m.reviewer)} · review notes: {len(m.notes)} (authors in order: "
                     f"{', '.join(self.s.w.party_name(n['author']) for n in m.notes) or '-'})"
                     f"\n  ticket: {tx[m.key]['description'][:300].replace(chr(10), ' ')}" for m in chunk]
            reqs.append(self.write_request(2, f"dz2m-{i // 10}", "dz_mrs", STYLE,
                                           "GitLab merge requests. For each: the title starts with the ticket key; the description says "
                                           "what changed, how to verify it and \"Closes <key>\"; notes are the review notes and the author's "
                                           "answers in order (1-3 sentences each, about the code: names of functions, tests, fields).\n\n"
                                           + "\n".join(lines),
                                           {"slots": [f"m{m.n}" for m in chunk], "keys": {f"m{m.n}": m.key for m in chunk},
                                            "notes": {f"m{m.n}": len(m.notes) for m in chunk}}))
        # meetings, by day
        by_day: dict[str, list[MeetingStub]] = {}
        for m in plan.meetings:
            by_day.setdefault(m.held_at[:10], []).append(m)
        guide = {"standup": "per participant: yesterday / today / blockers, with ticket keys", "planning": "what we take, estimates, priorities, who does what",
                 "sync": "statuses, questions, agreements", "triage": "per bug: priority, owner, what we do",
                 "review": "what was shown, remarks, what to rework", "retro": "what went well, what went badly, what we change",
                 "management": "money, clients, people, risks - briefly, item by item", "client": "status for the client, the client's questions, agreements",
                 "prospect": "the prospect's interest, questions, next step of the deal", "demo": "the prospect (invent a plausible customer), what was shown, questions, next step",
                 "discovery": "the customer (plausible), how they work today, pains, what the product could help with",
                 "onboarding": "the customer (plausible), what was set up, the owner's questions, what is left",
                 "one_on_one": "work questions, workload, agreements (no personal assessments)", "vendor": "terms, problems, agreements"}
        for d, ms in sorted(by_day.items()):
            for i in range(0, len(ms), 7):
                chunk = ms[i:i + 7]
                blocks = []
                for m in chunk:
                    b = [f"### [s{m.n}] \"{m.title}\" - {self.local(m.held_at)}, {m.minutes} min, space {m.space}",
                         "Attendees: " + "; ".join(self.who(a) for a in m.attendees + m.externals),
                         f"Organizer and author of the notes: {self.who(m.organizer)}. Format: {guide.get(m.kind, 'discussion and outcomes')}."]
                    if m.refs:
                        b.append("Tickets to discuss (the statuses are facts, do not change them):\n"
                                 + "\n".join(f"  - {short(k)}{state(k, m.held_at)}" for k in m.refs))
                    if m.spawned:
                        b.append("Tickets created as a result of the meeting - each one MUST be a separate action item with its key:\n"
                                 + "\n".join(f"  - {short(k)} -> assignee {self.who(next(t.assignee for t in plan.tickets if t.key == k))}"
                                             for k in m.spawned))
                    if m.context:
                        b.append("Context:\n" + "\n".join(f"  - {c}" for c in m.context))
                    blocks.append("\n".join(b))
                atts = {a for m in chunk for a in m.attendees}
                reqs.append(self.write_request(2, f"dz2s-{d}-{i // 7}", "dz_meetings", STYLE + "\n\n" + self.voices(atts),
                                               "Meeting notes in Confluence. For each meeting: title (name and date), body in markdown "
                                               "(sections \"Discussion\" and, if needed, \"Risks\"), decisions - 0-2 working decisions of "
                                               "this meeting (a priority, a due date or an assignee of a ticket, an agreement with a client; "
                                               "usually empty for a standup; never cancel or change company decisions from the context), "
                                               "action_items - \"employee_id: text\" (ids of the attendees). Standup 80-200 words, others "
                                               "120-350.\n\n"
                                               + "\n\n".join(blocks),
                                               {"slots": [f"s{m.n}" for m in chunk],
                                                "spawned": {f"s{m.n}": m.spawned for m in chunk},
                                                "refs": {f"s{m.n}": m.refs for m in chunk},
                                                "attendees": {f"s{m.n}": m.attendees for m in chunk},
                                                "kinds": {f"s{m.n}": m.kind for m in chunk}}))
        # weekly product metrics
        if plan.kpis:
            lines = []
            for k in plan.kpis:
                n = k.numbers
                lines.append(f"- [w{k.n}] week {k.week_from}..{k.week_to}: active customers (with orders) {n['active']}, paying {n['paying']} "
                             f"(+{n['new']} / -{n['churned']}), orders {n['orders']}, paid online {n['online_share']}%, "
                             f"MRR {n['mrr'] // 1000}k" + (f" Event of the week: {k.note}." if k.note else ""))
            reqs.append(self.write_request(2, "dz2k-0", "dz_kpi", STYLE + "\n\n" + self.voices({self.cfg["kpi"]["author"]}),
                                           f"The product manager's weekly commentary on the product metrics of {self.s.company.company.name} "
                                           "(the engine renders the table with the numbers). For each week 3-6 sentences: what grew or fell "
                                           "and why (from the events of the week and the company context), what we do next. Always state "
                                           "the number of active customers of that week.\n\n" + "\n".join(lines),
                                           {"slots": [f"w{k.n}" for k in plan.kpis], "active": {f"w{k.n}": k.numbers["active"] for k in plan.kpis}}))
        return {"phase": 2, "requests": len(reqs)}

    # -------------------------------------------------------------------------------------------- listing
    def listing(self, phase: int, shard: str | None) -> list[dict]:
        rdir = self.s.out / "rounds" / f"densify-{phase}"
        reqs = sorted(rdir.glob("req-*.json"))
        if shard:  # a fixed share of all requests, so writers started at different times never overlap
            k, n = map(int, shard.split("/"))
            reqs = reqs[k - 1::n]
        out = []
        for p in reqs:
            rid = p.name[4:-5]
            if (rdir / f"resp-{rid}.json").exists() and check_request(json.loads(p.read_text(encoding="utf-8")))["ok"]:
                continue
            out.append({"id": rid, "request_path": str(p)})
        return out

    def status(self) -> dict:
        out = {}
        for phase in (1, 2):
            rdir = self.s.out / "rounds" / f"densify-{phase}"
            reqs = list(rdir.glob("req-*.json"))
            ok = sum(1 for p in reqs if (rdir / f"resp-{p.name[4:-5]}.json").exists()
                     and check_request(json.loads(p.read_text(encoding="utf-8")))["ok"])
            out[f"phase{phase}"] = {"requests": len(reqs), "ok": ok}
        return out

    # -------------------------------------------------------------------------------------------- apply
    def apply(self) -> dict:
        plan = self.load()
        tx = self.texts()
        resp2: dict[str, dict] = {}
        for p in (self.s.out / "rounds" / "densify-2").glob("resp-*.json"):
            d = json.loads(p.read_text(encoding="utf-8"))
            for c in d.get("comments", []):
                resp2[c["slot"]] = c
            for m in d.get("mrs", []):
                resp2[m["slot"]] = m
            for m in d.get("meetings", []):
                resp2[m["slot"]] = m
            for k in d.get("reports", []):
                resp2[k["slot"]] = k
        problems = self.status()
        if problems["phase1"]["ok"] < problems["phase1"]["requests"] or problems["phase2"]["ok"] < problems["phase2"]["requests"]:
            raise SystemExit(f"responses incomplete: {problems}")
        src = self.s.out / "world.jsonl"
        backup = self.s.out / "world.before-densify.jsonl"
        if not backup.exists():
            shutil.copy(src, backup)
        tmp = self.dir / "work"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        shutil.copy(backup, tmp / "world.jsonl")
        w = World(self.s.company, tmp)
        w.load()
        before = len(w.events)
        cal = Calendar(self.s)
        ops: list[tuple[str, int, Any]] = []  # (ts, order, callable)

        def add(ts: str, fn) -> None:
            ops.append((ts, len(ops), fn))

        mr_ids: dict[int, str] = {}
        meet_handle = lambda n: f"dz:meeting:{n}"  # noqa: E731
        for t in plan.tickets:
            text = tx[t.key]
            remote = []
            o = t.origin or {}
            if o.get("kind") == "meeting":
                remote.append({"relationship": "informed by", "target": meet_handle(o["meeting"]), "title": o["title"]})
            elif o.get("kind") == "decision":
                remote.append({"relationship": "implements", "target": o["handle"], "title": o["text"][:120]})
            elif o.get("kind") == "regression":
                remote.append({"relationship": "caused by", "target": f"dz:mr:{o['mr']}", "title": f"MR for {o['key']}"})
            links = [{"type": "is blocked by", "key": t.blocked_by}] if t.blocked_by else []

            def create(t=t, text=text, remote=remote, links=links):
                key = w.create_ticket(ts=dt(t.created), day=cal.day_of(dt(t.created)), actor=t.reporter, project=t.project,
                                      issue_type=t.itype, summary=text["summary"], description=text["description"],
                                      assignee=t.assignee, priority=t.priority,
                                      labels=[t.component] + (["regression"] if (t.origin or {}).get("kind") == "regression" else []),
                                      parent=t.parent, links=links, remote_links=remote, handle=f"dz:ticket:{t.key}")
                assert key == t.key, f"key drift: planned {t.key}, world gave {key}"
            add(t.created, create)
            seq = 0
            for x in t.transitions:
                body = None
                if x.comment:
                    seq += 1
                    body = resp2[f"{t.key}#t{seq}"]["body"]
                add(x.at, lambda t=t, x=x, body=body: w.update_ticket(ts=dt(x.at), day=cal.day_of(dt(x.at)), actor=x.actor, key=t.key,
                                                                     changes={"status": x.to}, comment=body))
            for j, dsc in enumerate(t.discussion):
                body = resp2[f"{t.key}#d{j + 1}"]["body"]
                add(dsc["at"], lambda t=t, dsc=dsc, body=body: w.comment_ticket(ts=dt(dsc["at"]), day=cal.day_of(dt(dsc["at"])),
                                                                              actor=dsc["author"], key=t.key, body=body))
        for m in plan.mrs:
            r = resp2[f"m{m.n}"]

            def create_mr(m=m, r=r):
                mr_ids[m.n] = w.create_mr(ts=dt(m.created), day=cal.day_of(dt(m.created)), actor=m.author, repo=m.repo,
                                          title=r["title"], description=r["description"], reviewers=[m.reviewer], closes=[m.key],
                                          handle=f"dz:mr:{m.n}")
            add(m.created, create_mr)
            for note, text in zip(m.notes, r.get("notes") or []):
                add(note["at"], lambda m=m, note=note, text=text: w.comment_mr(ts=dt(note["at"]), day=cal.day_of(dt(note["at"])),
                                                                             actor=note["author"], mr_id=mr_ids[m.n], body=text))
            if m.merged_at:
                add(m.merged_at, lambda m=m: w.merge_mr(ts=dt(m.merged_at), day=cal.day_of(dt(m.merged_at)), actor=m.reviewer,
                                                        mr_id=mr_ids[m.n]))
        for m in plan.meetings:
            r = resp2[f"s{m.n}"]
            items = []
            for a in r.get("action_items") or []:
                pid, _, text = a.partition(":")
                pid = pid.strip()
                items.append({"assignee": pid if pid in w.people else m.organizer, "text": (text or a).strip()})
            props = {"Date": self.local(m.held_at, "%Y-%m-%d"), "Attendees": ", ".join(w.party_name(a) for a in m.attendees + m.externals)}
            add(m.held_at, lambda m=m, r=r, items=items, props=props: w.create_page(
                ts=dt(m.held_at) + timedelta(minutes=m.minutes + 5), day=cal.day_of(dt(m.held_at)), actor=m.organizer, space=m.space,
                title=r["title"], body=r["body"], kind="meeting_notes" if m.kind != "retro" else "retro", attendees=m.attendees,
                decisions=list(r.get("decisions") or []), action_items=items, properties=props, held_at=dt(m.held_at),
                handle=meet_handle(m.n)))
        kc = self.cfg.get("kpi") or {}
        for k in plan.kpis:
            n = k.numbers
            body = (f"# {kc.get('title', 'Metrics')}: week {date.fromisoformat(k.week_from):%m/%d}-{date.fromisoformat(k.week_to):%m/%d}\n\n"
                    "| Metric | Value |\n|---|---|\n"
                    f"| Active customers (at least one order in the week) - North Star | {n['active']} |\n"
                    f"| Paying customers | {n['paying']} |\n| New this week | {n['new']} |\n| Churned this week | {n['churned']} |\n"
                    f"| Orders | {n['orders']} |\n| Share paid online | {n['online_share']}% |\n| MRR, thousands | {n['mrr'] // 1000} |\n\n"
                    f"## Commentary\n\n{resp2[f'w{k.n}']['commentary']}\n")
            add(k.published, lambda k=k, n=n, body=body: w.create_page(
                ts=dt(k.published), day=cal.day_of(dt(k.published)), actor=kc.get("author", "product"), space=kc.get("space", "PROD"),
                title=f"{kc.get('title', 'Metrics')}: week {date.fromisoformat(k.week_from):%m/%d}-{date.fromisoformat(k.week_to):%m/%d/%Y}",
                body=body, kind="report", attendees=[], decisions=[], action_items=[],
                properties={"Period": f"{k.week_from} - {k.week_to}", "Type": "weekly metrics report",
                            "Active customers": str(n["active"]), "Paying customers": str(n["paying"]), "New": str(n["new"]),
                            "Churned": str(n["churned"]), "Orders": str(n["orders"]),
                            "Paid online, %": str(n["online_share"]), "MRR, thousands": str(n["mrr"] // 1000)}, held_at=None,
                handle=f"dz:kpi:{k.n}"))
        for _, _, fn in sorted(ops, key=lambda x: (x[0], x[1])):
            fn()
        added = len(w.events) - before
        # one journal in time order: the exporter and the knowledge base replay by seq
        lines = [json.loads(line) for line in (tmp / "world.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        lines.sort(key=lambda e: (dt(e["ts"]), e["seq"]))
        for i, e in enumerate(lines, 1):
            e["seq"] = i
        out = self.s.out / "world.jsonl.tmp"
        out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in lines) + "\n", encoding="utf-8")
        out.replace(src)
        return {"added_events": added, "world_events": len(lines), "backup": str(backup)}


# ================================================================================================ checks
def _load_resp(req: dict) -> tuple[dict | None, list[str]]:
    path = Path(req["response_path"])
    if not path.exists():
        return None, [f"no response file {path}"]
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"not JSON: {exc}"]
    return (d, []) if isinstance(d, dict) else (None, ["the response must be a JSON object"])


def _banned(text: str, patterns: list[str]) -> list[str]:
    return [f"banned topic \"{m.group(0)}\"" for p in patterns for m in [re.search(p, text, re.I)] if m]


def check_request(req: dict) -> dict:
    d, problems = _load_resp(req)
    if d is None:
        return {"ok": False, "problems": problems}
    ch = req["checks"]
    kind = req["kind"]
    banned = ch.get("banned") or []
    if kind == "dz_tickets":
        items = {t.get("key"): t for t in d.get("tickets") or [] if isinstance(t, dict)}
        for k in ch["keys"]:
            t = items.get(k)
            if not t:
                problems.append(f"no ticket {k}")
                continue
            s, desc = str(t.get("summary") or ""), str(t.get("description") or "")
            if not 10 <= len(s) <= 160 or "\n" in s:
                problems.append(f"{k}: the summary must be one line of 10-160 characters")
            if len(desc) < (80 if ch["types"].get(k) == "Epic" else 100):
                problems.append(f"{k}: the description is too short ({len(desc)} characters)")
            problems += [f"{k}: {p}" for p in _banned(s + " " + desc, banned)]
        extra = set(items) - set(ch["keys"])
        if extra:
            problems.append(f"unexpected keys: {sorted(extra)[:5]}")
    elif kind == "dz_comments":
        items = {c.get("slot"): c for c in d.get("comments") or [] if isinstance(c, dict)}
        for sl in ch["slots"]:
            body = str((items.get(sl) or {}).get("body") or "")
            if len(body) < 15:
                problems.append(f"no comment, or it is shorter than 15 characters: {sl}")
            problems += [f"{sl}: {p}" for p in _banned(body, banned)]
    elif kind == "dz_mrs":
        items = {m.get("slot"): m for m in d.get("mrs") or [] if isinstance(m, dict)}
        for sl in ch["slots"]:
            m = items.get(sl)
            if not m:
                problems.append(f"no MR {sl}")
                continue
            if ch["keys"][sl] not in str(m.get("title") or ""):
                problems.append(f"{sl}: the title must start with the key {ch['keys'][sl]}")
            if len(str(m.get("description") or "")) < 60:
                problems.append(f"{sl}: the description is shorter than 60 characters")
            notes = m.get("notes") or []
            if len(notes) != ch["notes"][sl] or any(len(str(x)) < 8 for x in notes):
                problems.append(f"{sl}: exactly {ch['notes'][sl]} non-empty review notes are needed")
    elif kind == "dz_meetings":
        items = {m.get("slot"): m for m in d.get("meetings") or [] if isinstance(m, dict)}
        for sl in ch["slots"]:
            m = items.get(sl)
            if not m:
                problems.append(f"no meeting {sl}")
                continue
            body = str(m.get("body") or "")
            need = 300 if ch["kinds"][sl] == "standup" else 450
            if len(body) < need:
                problems.append(f"{sl}: the notes are shorter than {need} characters")
            if not str(m.get("title") or "").strip():
                problems.append(f"{sl}: no title")
            ai = " ".join(str(x) for x in m.get("action_items") or [])
            for k in ch["spawned"].get(sl) or []:
                if k not in ai:
                    problems.append(f"{sl}: an action item with the key {k} is required (in action_items)")
            for a in m.get("action_items") or []:
                pid = str(a).split(":", 1)[0].strip()
                if pid not in ch["attendees"][sl]:
                    problems.append(f"{sl}: action item \"{str(a)[:40]}\" - the assignee id must be one of the attendees {ch['attendees'][sl]}")
                    break
            refs = ch["refs"].get(sl) or []
            if refs and ch["kinds"][sl] in ("standup", "triage") and sum(k in body for k in refs) < min(2, len(refs)):
                problems.append(f"{sl}: mention at least {min(2, len(refs))} tickets of the list by key in the notes")
            problems += [f"{sl}: {p}" for p in _banned(body + " " + ai, banned)]
    elif kind == "dz_kpi":
        items = {k.get("slot"): k for k in d.get("reports") or [] if isinstance(k, dict)}
        for sl in ch["slots"]:
            c = str((items.get(sl) or {}).get("commentary") or "")
            if len(c) < 120:
                problems.append(f"{sl}: the commentary is shorter than 120 characters")
            elif str(ch["active"][sl]) not in c.replace(" ", "").replace(" ", ""):
                problems.append(f"{sl}: state the number of active customers {ch['active'][sl]}")
    return {"ok": not problems, "problems": problems[:12]}


# ================================================================================================ CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="kbsim.densify")
    ap.add_argument("tenant")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    p = sub.add_parser("prepare")
    p.add_argument("phase", type=int, choices=[1, 2])
    p = sub.add_parser("list")
    p.add_argument("--phase", type=int, required=True)
    p.add_argument("--shard", default=None)
    p = sub.add_parser("check")
    p.add_argument("id")
    sub.add_parser("apply")
    sub.add_parser("status")
    sub.add_parser("summary")
    a = ap.parse_args(argv)
    dz = Densifier(a.tenant)
    if a.cmd == "plan":
        out: Any = dz.make_plan()
    elif a.cmd == "summary":
        out = dz.summary(dz.load())
    elif a.cmd == "prepare":
        out = dz.prepare1() if a.phase == 1 else dz.prepare2()
    elif a.cmd == "list":
        out = dz.listing(a.phase, a.shard)
    elif a.cmd == "check":
        path = next((dz.s.out / "rounds").rglob(f"req-{a.id}.json"), None)
        if path is None:
            raise SystemExit(f"unknown request {a.id}")
        out = check_request(json.loads(path.read_text(encoding="utf-8")))
    elif a.cmd == "apply":
        out = dz.apply()
    else:
        out = dz.status()
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
