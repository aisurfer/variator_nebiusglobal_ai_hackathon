"""P5 work: Jira issues in two-week sprints, merge requests, reviews, QA, blockers.

One working day at a time:

1. Tickets of people who left go to a teammate.
2. On the first day of a sprint the planner fills each engineer up to ``team_load`` x the
   capacity (velocity x known availability), after the carried-over and committed work.
3. Each engineer at work reviews merge requests, tests tickets (QA) and moves the active
   ticket: effort falls by the day's pace; a blocker stops it for some days; at zero effort
   a merge request opens. Absent people do nothing.
4. Business tickets of the non-engineers follow To Do -> In Progress -> Done.

Couplings: unplanned bugs (incidents, support) and committed implementation work (new
clients, upsells) raise the load; the load of each sprint feeds the burnout rule (people)
and the incident rate (operations).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np

from variator.orgsim.company import templates as T
from variator.orgsim.company.model import Issue, IssueUpdate, MergeRequest, World

if TYPE_CHECKING:
    from variator.orgsim.company.engine import Engine

POINTS = (1, 2, 3, 5, 8)
POINT_P = (0.30, 0.35, 0.22, 0.10, 0.03)
SPRINT_DAYS = 10
SPRINT_BASE = 40
EPIC_THEMES = [
    "reliability and scale",
    "faster onboarding",
    "self-service",
    "cost efficiency",
    "enterprise readiness",
    "better insights",
]
NO_QA_ROLES = ("devops", "qa")


@dataclass
class Sprint:
    n: int
    start: date
    end: date  # exclusive
    first_i: int
    last_i: int  # inclusive

    @property
    def name(self) -> str:
        return f"PLAT Sprint {SPRINT_BASE + self.n}"

    @property
    def jira_id(self) -> int:
        return 300 + self.n


@dataclass
class Simple:
    """A ticket outside the code flow: business tasks, designs, workshops."""

    issue: Issue
    start_i: int
    done_i: int
    reviewer: str | None = None
    review_i: int = -1


class Work:
    def __init__(self, w: World, eng: Engine):
        self.w, self.eng = w, eng
        self.sprints = self._sprints()
        self.sprint_of = {}
        for s in self.sprints:
            for i in range(s.first_i, s.last_i + 1):
                self.sprint_of[i] = s
        self.queue: dict[str, list[Issue]] = {}
        self.active: dict[str, Issue | None] = {}
        self.reviews: list[MergeRequest] = []
        self.fixes: list[MergeRequest] = []
        self.qa: list[tuple[Issue, int]] = []
        self.backlog: list[Issue] = []  # committed work waiting for a sprint (implementations)
        self.simple: list[Simple] = []
        self.epics: dict[tuple[str, str], Issue] = {}
        self.assigned: dict[str, float] = {}
        self.capacity: dict[str, float] = {}
        self.merged: list[MergeRequest] = []  # merged, waiting for a deploy
        self.biz_clocks: dict[str, Any] = {}
        self.loads: list[dict[str, Any]] = []
        self.unplanned_now: dict[str, float] = {}
        self.unplanned_hist: dict[str, list[float]] = {}

    # ================================================================================== sprints
    def _sprints(self) -> list[Sprint]:
        days = self.w.days
        first_monday = next((i for i, d in enumerate(days) if d.weekday() == 0), 0)
        starts = (
            [0] + list(range(first_monday, len(days), SPRINT_DAYS))
            if first_monday
            else list(range(0, len(days), SPRINT_DAYS))
        )
        starts = sorted(set(starts))
        out = []
        for n, i in enumerate(starts):
            j = starts[n + 1] - 1 if n + 1 < len(starts) else len(days) - 1
            end = days[j + 1] if j + 1 < len(days) else self.w.bday(days[j], 1)
            if n + 1 == len(starts):  # the last sprint ends after T: its nominal end
                end = self.w.bday(days[i], SPRINT_DAYS)
            out.append(Sprint(n, days[i], end, i, j))
        return out

    def sprint_payload(self, s: Sprint, at: datetime) -> dict[str, Any]:
        start = self.w.cal.at(s.start, "09:00")
        end = self.w.cal.at(s.end, "09:00")
        state = "closed" if end <= at else "active" if start <= at else "future"
        return {
            "id": s.jira_id,
            "name": s.name,
            "state": state,
            "boardId": 7,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        }

    # ================================================================================== Jira operations
    def _push(
        self,
        issue: Issue,
        ts: datetime,
        actor: str,
        kind: str,
        data: dict[str, Any],
        causes: tuple = (),
        process: str = "work",
    ) -> datetime:
        if issue.updates and ts <= issue.updates[-1].ts:
            ts = issue.updates[-1].ts + timedelta(minutes=3)
        issue.updates.append(IssueUpdate(ts, actor, kind, data, tuple(causes), self.w.order(), process))
        return ts

    def new_issue(
        self,
        project: str,
        itype: str,
        summary: str,
        ts: datetime,
        reporter: str,
        *,
        assignee: str | None = None,
        component: str | None = None,
        points: int = 0,
        priority: str = "Medium",
        parent: Issue | None = None,
        labels: tuple[str, ...] = (),
        description: str = "",
        client: str | None = None,
        causes: tuple = (),
        slot_kind: str = "issue",
        process: str = "work",
    ) -> Issue:
        issue = Issue(
            self.w.uid(),
            project,
            itype,
            summary,
            component,
            points,
            priority,
            reporter,
            assignee,
            ts,
            parent,
            list(labels),
            description or f"{summary}.",
            client,
            slot_kind,
        )
        if parent is not None:
            parent.children.append(issue)
        s = self.sprint_of.get(self.w.day_of(ts.date()))
        if project in ("PLAT", "IMPL") and itype != "Epic" and s is not None:
            issue.sprints.append((ts, s.n))
        self._push(issue, ts, reporter, "created", {"assignee": assignee}, causes, process)
        self.w.issues.append(issue)
        return issue

    def transition(
        self, issue: Issue, ts: datetime, actor: str, to: str, causes: tuple = (), process: str = "work"
    ) -> datetime:
        old = issue.status
        issue.status = to
        ts = self._push(issue, ts, actor, "transition", {"from": old, "to": to}, causes, process)
        if to == "In Progress" and issue.parent is not None and issue.parent.status == "To Do":
            self.transition(issue.parent, ts + timedelta(minutes=1), actor, "In Progress")
        if to in ("Done", "Won't Do"):
            issue.done_at = ts
            for fn in list(issue.on_done):
                fn(issue, ts)
        return ts

    def comment(
        self,
        issue: Issue,
        ts: datetime,
        author: str,
        body: str,
        slot_kind: str = "comment",
        causes: tuple = (),
    ) -> datetime:
        return self._push(issue, ts, author, "comment", {"body": body, "slot_kind": slot_kind}, causes)

    def reassign(self, issue: Issue, ts: datetime, actor: str, to: str, note: str) -> datetime:
        old, issue.assignee = issue.assignee, to
        ts = self._push(issue, ts, actor, "assign", {"to": to, "from": old})
        return self.comment(issue, ts + timedelta(minutes=1), actor, note, "reassign_note")

    def block_link(self, issue: Issue, ts: datetime, actor: str, blocker: Issue) -> datetime:
        return self._push(issue, ts, actor, "link", {"blocked_by": blocker})

    def remote_link(self, issue: Issue, ts: datetime, actor: str, mr: MergeRequest) -> datetime:
        return self._push(issue, ts, actor, "remote_link", {"caused_by": mr})

    # ================================================================================== helpers
    def rng(self, entity: str, purpose: str) -> np.random.Generator:
        return self.w.rng("work", entity, purpose)

    def engineers(self, d: date) -> list[str]:
        return self.w.employed_ids(d, ("backend", "frontend", "devops", "qa"))

    def points(self, r: np.random.Generator) -> int:
        return int(r.choice(POINTS, p=POINT_P))

    def effort(self, pid: str, points: float) -> float:
        """Working days for ``points``; the velocity already includes the time lost to blockers."""
        p = self.w.people[pid]
        v = max(p.velocity, 4.0)
        sd = 0.3
        noise = float(np.exp(self.rng(pid, "effort").normal(-sd * sd / 2, sd)))
        return points * SPRINT_DAYS / v * (1 - self.w.spec.truth.blockers) * noise

    def owner(self, component: str | None, d: date, exclude: tuple[str, ...] = ()) -> str | None:
        """The engineer with the least load among those who work on ``component`` (else any developer)."""
        ids = [i for i in self.engineers(d) if i not in exclude and not self._leaving(i, d)]
        if not ids:
            return None
        skilled = [
            i
            for i in ids
            if component in self.w.people[i].person.skills
            and self.w.people[i].person.role in ("backend", "frontend", "devops")
        ]
        devs = [i for i in ids if self.w.people[i].person.role in ("backend", "frontend")]
        pool = skilled or devs or ids
        return min(pool, key=lambda i: (self.assigned.get(i, 0) / max(self.capacity.get(i, 1), 1), i))

    def _leaving(self, pid: str, d: date) -> bool:
        p = self.w.people[pid]
        return p.last_day is not None and (p.last_day - d).days < 10

    def lead(self, d: date) -> str:
        """The tech lead, else the head of delivery (for epics and reassignments)."""
        for pid in self.w.employed_ids(d, ("backend",)):
            if self.w.people[pid].person.key:
                return pid
        return self.eng.head("delivery")

    def present_or(self, candidates: list[str], d: date) -> str | None:
        return self.w.first_present([c for c in candidates if c], d)

    # ================================================================================== epics
    def quarter(self, d: date) -> str:
        return f"Q{(d.month - 1) // 3 + 1} {d.year}"

    def epic(self, component: str, d: date, ts: datetime) -> Issue | None:
        key = (component, self.quarter(d))
        e = self.epics.get(key)
        if e is not None:
            return e
        who = self.present_or(
            [
                self.lead(d),
                self.eng.head("delivery"),
                self.eng.role_ids("pm", d)[0] if self.eng.role_ids("pm", d) else "",
            ],
            d,
        )
        if who is None:
            return None
        r = self.rng(component, "epic")
        title = f"{key[1]}: {T.pick(r, EPIC_THEMES)} for {component}"
        e = self.new_issue(
            "PLAT",
            "Epic",
            title,
            ts - timedelta(minutes=3),
            who,
            assignee=who,
            component=component,
            priority="Medium",
            slot_kind="epic",
        )
        self.epics[key] = e
        return e

    def close_epics(self, d: date) -> None:
        for e in [x for x in self.w.issues if x.itype == "Epic" and x.status == "In Progress"]:
            if not e.children or not all(c.closed for c in e.children):
                continue
            if e.project == "PLAT" and self.quarter(d) == self.quarter(e.created.date()):
                continue  # a roadmap epic stays open until its quarter ends
            who = self.present_or([e.assignee or "", e.reporter, self.eng.head("delivery")], d)
            if who:
                last = max(c.done_at for c in e.children if c.done_at)
                self.transition(e, self.w.stamp(who, d, after=last), who, "Done")

    # ================================================================================== planning
    def plan(self, s: Sprint) -> None:
        w, d = self.w, s.start
        # sprint end: record the realised load of the previous sprint
        for pid, cap in self.capacity.items():
            if cap > 0 and pid in w.people:
                w.people[pid].loads.append(self.assigned.get(pid, 0.0) / cap)
        if self.capacity:
            self.loads.append({"sprint": s.n - 1, "load": round(self.team_load_now(), 4)})
        for pid, pts in self.unplanned_now.items():
            self.unplanned_hist.setdefault(pid, []).append(pts)
        self.assigned, self.capacity, self.unplanned_now = {}, {}, {}
        planner = self.present_or(self.eng.role_ids("pm", d) + [self.eng.head("delivery"), self.lead(d)], d)
        if planner is None:
            planner = self.eng.head("delivery")
        t0 = w.stamp(planner, d, lo=0.0, hi=0.15) if w.present(planner, d) else w.at(d, "09:30")
        engineers = [e for e in self.engineers(d) if not self._leaving(e, d)]
        days = [w.days[i] for i in range(s.first_i, s.last_i + 1)]
        for pid in engineers:
            p = w.people[pid]
            avail = sum(1 for x in days if p.employed(x) and w.absences.get(pid, {}).get(x) != "vacation")
            self.capacity[pid] = p.velocity * p.speed(d) * avail / SPRINT_DAYS
            carry = 0.0
            for issue in self.queue.get(pid, []) + ([self.active[pid]] if self.active.get(pid) else []):
                if issue.closed:
                    continue
                left = issue.effort_left / max(issue.effort_total, 1e-9) * issue.points
                carry += left
                if not any(n == s.n for _, n in issue.sprints):
                    issue.sprints.append((t0, s.n))
            self.assigned[pid] = carry
        self._drop_stale(s, d, planner, t0)
        # committed implementation work first
        for issue in list(self.backlog):
            if issue.created.date() > d:
                continue
            owner = self.owner(issue.component, d)
            if owner is None:
                continue
            self.backlog.remove(issue)
            self.give(issue, owner, t0, planner, planned=True)
            self._select(issue, planner, d)
        # the roadmap fills the rest up to the target load, minus the usual unplanned work (bugs)
        for pid in engineers:
            hist = self.unplanned_hist.get(pid, [])[-3:]
            buffer = float(np.mean(hist)) if hist else 0.1 * self.capacity[pid]
            need = self.w.spec.truth.team_load * self.capacity[pid] - self.assigned[pid] - buffer
            r = self.rng(pid, "roadmap")
            while need > 0.5:
                pts = self.points(r)
                issue = self._roadmap_ticket(pid, d, t0, pts, r)
                if issue is None:
                    break
                self.give(issue, pid, issue.created, None, planned=True)
                self._select(issue, planner, d)
                need -= pts

    def _select(self, issue: Issue, planner: str, d: date) -> None:
        """The planner moves a ticket of the sprint to Selected for Development."""
        if issue.status == "To Do" and self.w.present(planner, d):
            self.transition(
                issue, issue.updates[-1].ts + timedelta(minutes=2), planner, "Selected for Development"
            )

    def _drop_stale(self, s: Sprint, d: date, planner: str, t0: datetime) -> None:
        """Tickets never started after two sprints may be closed as Won't Do by the planner."""
        if not self.w.present(planner, d):
            return
        r = self.rng(planner, "stale")
        for pid, q in self.queue.items():
            for issue in list(q):
                if issue.started or issue.itype == "Bug" or issue.project != "PLAT" or len(issue.sprints) < 3:
                    continue
                if r.random() < 0.3:
                    q.remove(issue)
                    ts = self.comment(
                        issue,
                        t0 + timedelta(minutes=5),
                        planner,
                        "Out of scope for now; closing.",
                        "wont_do_note",
                    )
                    self.transition(issue, ts + timedelta(minutes=1), planner, "Won't Do")
                    self.assigned[pid] = max(self.assigned.get(pid, 0) - issue.points, 0)

    def _roadmap_ticket(
        self, pid: str, d: date, t0: datetime, pts: int, r: np.random.Generator
    ) -> Issue | None:
        p = self.w.people[pid]
        role = p.person.role
        comp = str(T.pick(r, p.person.skills or ["api"]))
        u = float(r.random())
        if role in ("backend", "frontend"):
            itype = "Story" if u < 0.65 else "Task" if u < 0.85 else "Bug"
        elif role == "devops":
            itype = "Task" if u < 0.7 else "Story" if u < 0.9 else "Bug"
        else:
            itype = "Task" if u < 0.8 else "Bug"
        if itype == "Story":
            summary = T.story_summary(r, comp)
        elif itype == "Bug":
            summary = T.bug_summary(r, comp)
        else:
            summary = T.task_summary(r, comp, role)
        analyst = self.eng.role_ids("analyst_designer", d)
        pms = self.eng.role_ids("pm", d)
        qa = self.eng.role_ids("qa", d)
        if itype == "Story":
            cands = analyst + pms + [self.lead(d)]
        elif itype == "Bug":
            cands = qa + [self.lead(d)]
        else:
            cands = [pid, self.lead(d)]
        rep = self.present_or(cands, d) or self.present_or([pid], d)
        if rep is None:
            return None
        ts = min(self.w.stamp(rep, d, after=t0, hi=0.25), t0 + timedelta(minutes=50))
        parent = None
        u = float(r.random())
        if itype == "Story" or (itype == "Task" and u < 0.75) or (itype == "Bug" and u < 0.3):
            parent = self.epic(comp, d, ts)
            if parent is not None and parent.created >= ts:
                ts = parent.created + timedelta(minutes=1)  # a child never predates its epic
        prio = "High" if itype == "Bug" else str(T.pick(r, ["Medium", "Medium", "Low", "High"]))
        issue = self.new_issue(
            "PLAT",
            itype,
            summary,
            ts,
            rep,
            assignee=pid,
            component=comp,
            points=pts,
            priority=prio,
            parent=parent,
            causes=(self.eng.proc("PROC-ROADMAP"),),
            slot_kind="issue",
        )
        if itype == "Bug" and r.random() < 0.5:
            # a defect found in a recent change: the bug "is caused by" that merge request
            recent = [
                m
                for m in self.w.mrs
                if m.merged_at
                and m.issue.component == comp
                and timedelta(0) < ts - m.merged_at < timedelta(days=30)
            ]
            if recent:
                mr = recent[int(r.integers(len(recent)))]
                self.remote_link(issue, ts + timedelta(minutes=1), rep, mr)
                issue.description = f"Found after {mr.issue.summary.lower()}."
        return issue

    def give(
        self, issue: Issue, pid: str, ts: datetime, actor: str | None, planned: bool, front: bool = False
    ) -> None:
        """Put ``issue`` on the queue of ``pid`` (an assignment line when the assignee changes)."""
        if issue.assignee != pid and actor is not None:
            if issue.updates and ts <= issue.updates[-1].ts:
                ts = issue.updates[-1].ts + timedelta(minutes=2)
            old, issue.assignee = issue.assignee, pid
            self._push(issue, ts, actor, "assign", {"to": pid, "from": old})
        issue.assignee = pid
        role = self.w.people[pid].person.role
        issue.needs_mr = issue.project in ("PLAT", "IMPL") and issue.itype != "Epic"
        issue.needs_qa = issue.needs_mr and role not in NO_QA_ROLES and issue.itype in ("Story", "Bug")
        if issue.effort_total == 0:
            issue.effort_total = issue.effort_left = self.effort(pid, issue.points)
        q = self.queue.setdefault(pid, [])
        q.insert(0, issue) if front else q.append(issue)
        self.assigned[pid] = self.assigned.get(pid, 0.0) + issue.points
        if not planned:
            issue.unplanned = True
            self.unplanned_now[pid] = self.unplanned_now.get(pid, 0.0) + issue.points

    def add_unplanned(self, issue: Issue, owner: str, ts: datetime) -> None:
        """A bug from an incident or a support ticket: to the front of the owner's queue."""
        self.give(issue, owner, ts, None, planned=False, front=True)

    def commit(self, issue: Issue) -> None:
        """Committed work (implementations) for the next sprint planning."""
        self.backlog.append(issue)

    def finish(self) -> None:
        """Record the load of the sprint that runs at T (not finished)."""
        if self.capacity:
            self.loads.append({"sprint": self.sprints[-1].n, "load": round(self.team_load_now(), 4)})

    def team_load_now(self) -> float:
        cap = sum(self.capacity.values())
        return sum(self.assigned.values()) / cap if cap else self.w.spec.truth.team_load

    # ================================================================================== the day
    def step(self, i: int, d: date) -> None:
        self._handover(d)
        s = self.sprint_of.get(i)
        if s is not None and s.first_i == i and (s.n > 0 or i == 0):
            self.plan(s)
        engineers = sorted(self.engineers(d))
        for pid in engineers:
            if not self.w.present(pid, d):
                continue
            self._fix_reviews(pid, i, d)
            self._review(pid, i, d)
            if self.w.people[pid].person.role == "qa":
                self._test(pid, i, d)
            self._develop(pid, i, d)
        self._simple(i, d)
        self.close_epics(d)
        self.w.team_load = self.team_load_now()

    def _handover(self, d: date) -> None:
        for pid in list(self.queue):
            p = self.w.people[pid]
            if p.last_day is None or d <= p.last_day:
                continue
            items = [x for x in ([self.active.get(pid)] + self.queue[pid]) if x and not x.closed]
            self.queue[pid], self.active[pid] = [], None
            actor = self.present_or([self.lead(d), self.eng.head("delivery")] + self.eng.role_ids("pm", d), d)
            for issue in items:
                to = self.owner(issue.component, d, exclude=(pid,))
                if to is None or actor is None:
                    continue
                ts = self.w.stamp(actor, d, hi=0.3)
                self.reassign(
                    issue, ts, actor, to, f"Reassigned from {p.person.name}, who has left the company."
                )
                issue.effort_total = issue.effort_left = max(issue.effort_left, 0.5)
                if issue.status in ("In Progress", "Blocked"):
                    self.transition(issue, ts + timedelta(minutes=2), actor, "To Do")
                issue.started = False
                self.give(issue, to, ts, None, planned=False)
            for mr in self.reviews:
                if mr.reviewer == pid:
                    mr.reviewer = self.reviewer_for(mr.issue, mr.author, d)
            for mr in self.fixes:
                if mr.author == pid:
                    mr.author = mr.issue.assignee or mr.author
        for s in list(self.simple):
            p = self.w.people.get(s.issue.assignee or "")
            if p and p.last_day and d > p.last_day and not s.issue.closed:
                head = self.eng.dept_head(p.person.role)
                actor = self.present_or([head, self.eng.head("ceo")], d)
                if actor and head != s.issue.assignee:
                    ts = self.w.stamp(actor, d, hi=0.3)
                    self.reassign(s.issue, ts, actor, head, f"Reassigned from {p.person.name}, who has left.")

    def reviewer_for(self, issue: Issue, author: str, d: date) -> str:
        role = self.w.people[author].person.role
        same = [
            i
            for i in self.engineers(d)
            if i != author and self.w.people[i].person.role == role and not self._leaving(i, d)
        ]
        r = self.rng(author, "reviewer")
        if same and r.random() < 0.7:
            return same[int(r.integers(len(same)))]
        lead = self.lead(d)
        if lead != author and lead in self.engineers(d):
            return lead
        others = [
            i
            for i in self.engineers(d)
            if i != author and self.w.people[i].person.role in ("backend", "frontend", "devops")
        ]
        return others[int(r.integers(len(others)))] if others else author

    def _develop(self, pid: str, i: int, d: date) -> None:
        """One day of development: the day's pace goes to the active ticket, then to the next ones."""
        w = self.w
        p = w.people[pid]
        r = self.rng(pid, "develop")
        budget = p.speed(d)
        last: datetime | None = None
        for _ in range(4):
            issue = self.active.get(pid)
            if issue is None or issue.closed or issue.status in ("In Review", "In QA"):
                issue = self._next(pid, d, after=last)
                if issue is None:
                    return
            if issue.status == "Blocked":
                if i < issue.blocked_until:
                    return
                ts = w.stamp(pid, d, after=last, hi=0.3)
                last = self.transition(issue, ts, pid, "In Progress")
                if r.random() < 0.5 * p.habits.comment_rate:
                    last = self.comment(
                        issue, last + timedelta(minutes=4), pid, "Unblocked, continuing.", "unblock_note"
                    )
            elif r.random() < self.w.spec.truth.blockers * p.habits.block_rate / 2.5 * budget:
                self._block(pid, issue, i, d, r)
                return
            use = min(budget, issue.effort_left)
            issue.effort_left -= use
            budget -= use
            if issue.effort_left > 1e-9:
                if r.random() < 0.06 * p.habits.comment_rate * min(issue.points, 5):
                    ts = w.stamp(pid, d, after=last, lo=0.5)
                    self.comment(
                        issue,
                        ts,
                        pid,
                        "Progress update: the main part works; edge cases left.",
                        "progress_note",
                    )
                return
            frac = 1.0 - budget / max(p.speed(d), 1e-9)
            after = max(x for x in (last, issue.updates[-1].ts) if x is not None)
            ts = w.stamp(pid, d, after=after, lo=min(0.9 * frac, 0.85), hi=min(frac + 0.1, 1))
            if issue.needs_mr:
                last = self._open_mr(pid, issue, i, d, ts)
            else:
                last = self.transition(issue, ts, pid, "Done")
            self.active[pid] = None
            if budget <= 0.05:
                self._next(pid, d, after=last)
                return

    def _next(self, pid: str, d: date, after: datetime | None = None) -> Issue | None:
        q = self.queue.get(pid, [])
        while q and q[0].closed:
            q.pop(0)
        if not q:
            self.active[pid] = None
            return None
        issue = q.pop(0)
        after = max([x for x in (after, issue.updates[-1].ts) if x is not None])
        ts = self.w.stamp(pid, d, after=after, lo=0.05)
        if issue.status != "In Progress":
            ts = self.transition(issue, ts, pid, "In Progress")
            r = self.rng(pid, "start_note")
            if r.random() < 0.3 * self.w.people[pid].habits.comment_rate:
                self.comment(
                    issue,
                    ts + timedelta(minutes=3),
                    pid,
                    "Picked this up; plan to finish it this sprint.",
                    "start_note",
                )
            elif issue.reporter != pid and r.random() < 0.35 and self.w.present(issue.reporter, d):
                ts = self.comment(
                    issue,
                    ts + timedelta(minutes=5),
                    pid,
                    "Question: should this also cover the existing data?",
                    "question",
                )
                self.comment(
                    issue,
                    self.w.stamp(issue.reporter, d, after=ts),
                    issue.reporter,
                    "Yes, please include the existing data.",
                    "answer",
                )
        issue.started = True
        self.active[pid] = issue
        return issue

    def _block(self, pid: str, issue: Issue, i: int, d: date, r: np.random.Generator) -> None:
        w = self.w
        ts = w.stamp(pid, d, after=issue.updates[-1].ts, lo=0.1, hi=0.6)
        ts = self.transition(issue, ts, pid, "Blocked")
        issue.blocked_until = i + 1 + int(r.geometric(0.45))
        blockers = [
            x
            for q in self.queue.values()
            for x in q
            if x is not issue
            and not x.closed
            and x.project == issue.project
            and x.assignee != pid
            and x.created < ts
        ]
        if blockers and r.random() < 0.6:
            other = blockers[int(r.integers(len(blockers)))]
            ts = self.block_link(issue, ts + timedelta(minutes=1), pid, other)
            self.comment(
                issue,
                ts + timedelta(minutes=1),
                pid,
                f"Blocked by {other.summary.lower()}; waiting for it.",
                "block_note",
            )
            if other.assignee in self.queue and other in self.queue[other.assignee]:
                self.queue[other.assignee].remove(other)
                self.queue[other.assignee].insert(0, other)
        else:
            reason = str(T.pick(r, T.BLOCK_REASONS))
            self.comment(issue, ts + timedelta(minutes=1), pid, f"Blocked: {reason}.", "block_note")

    def _open_mr(self, pid: str, issue: Issue, i: int, d: date, ts: datetime) -> datetime:
        reviewer = self.reviewer_for(issue, pid, d)
        ts = max(ts, issue.updates[-1].ts + timedelta(minutes=1))
        mr = MergeRequest(self.w.uid(), issue, pid, reviewer, ts)
        mr.updates.append(IssueUpdate(ts, pid, "opened", {}, (("process", issue),), self.w.order()))
        mr.review_day = i + max(int(round(self.w.people[reviewer].habits.review_days)), 0)
        self.w.mrs.append(mr)
        issue.mr = mr
        self.reviews.append(mr)
        t2 = self.transition(issue, ts + timedelta(minutes=2), pid, "In Review")
        if self.rng(pid, "mr_comment").random() < 0.7 * self.w.people[pid].habits.comment_rate:
            t2 = self.comment(
                issue,
                t2 + timedelta(minutes=1),
                pid,
                "Ready for review.",
                "review_request",
                (("process", mr),),
            )
        return t2

    def _review(self, pid: str, i: int, d: date) -> None:
        for mr in [
            m for m in self.reviews if m.reviewer == pid and m.review_day <= i and m.state == "opened"
        ]:
            r = self.rng(pid, "review")
            after = mr.updates[-1].ts
            ts = self.w.stamp(pid, d, after=after, lo=0.05)
            if not mr.changes_requested and r.random() < 0.25:
                mr.changes_requested = True
                mr.notes.append((ts, pid, "Please cover the error path with a test.", "review_note"))
                mr.updates.append(IssueUpdate(ts, pid, "note", {}, (), self.w.order()))
                self.reviews.remove(mr)
                self.fixes.append(mr)
                if r.random() < 0.5 and not mr.issue.closed:
                    self.comment(
                        mr.issue,
                        ts + timedelta(minutes=2),
                        pid,
                        "Changes requested on the merge request.",
                        "review_feedback",
                        (("process", mr),),
                    )
                continue
            mr.notes.append((ts, pid, "Looks good to me.", "review_note"))
            mr.updates.append(IssueUpdate(ts, pid, "note", {}, (), self.w.order()))
            self._merge(mr, pid, ts + timedelta(minutes=int(r.integers(2, 20))), i)

    def _fix_reviews(self, pid: str, i: int, d: date) -> None:
        for mr in [m for m in self.fixes if m.author == pid]:
            ts = self.w.stamp(pid, d, after=mr.updates[-1].ts, lo=0.05, hi=0.5)
            mr.notes.append((ts, pid, "Added the test; ready for another look.", "review_reply"))
            mr.updates.append(IssueUpdate(ts, pid, "note", {}, (), self.w.order()))
            self.fixes.remove(mr)
            mr.review_day = i + 1
            self.reviews.append(mr)

    def _merge(self, mr: MergeRequest, pid: str, ts: datetime, i: int) -> None:
        mr.state, mr.merged_at, mr.merged_by = "merged", ts, pid
        mr.updates.append(IssueUpdate(ts, pid, "merged", {}, (), self.w.order()))
        if mr in self.reviews:
            self.reviews.remove(mr)
        self.merged.append(mr)
        issue = mr.issue
        if issue.closed:
            return
        if issue.needs_qa:
            self.transition(issue, ts + timedelta(minutes=2), pid, "In QA", (("process", mr),))
            self.qa.append((issue, i))
        else:
            self.transition(issue, ts + timedelta(minutes=2), pid, "Done", (("process", mr),))

    def _test(self, pid: str, i: int, d: date) -> None:
        r = self.rng(pid, "qa")
        budget = 1.0
        for item in [x for x in self.qa if x[1] <= i and x[0].assignee != pid]:
            issue, _ = item
            cost = 0.25 + 0.15 * min(issue.points, 5) * float(r.random())
            if cost > budget:
                break
            budget -= cost
            self.qa.remove(item)
            ts = self.w.stamp(pid, d, after=issue.updates[-1].ts, lo=0.1)
            if r.random() < self.w.spec.truth.qa_reopen:
                ts = self.comment(
                    issue,
                    ts,
                    pid,
                    f"QA: the fix fails on staging for {issue.component}; reopening.",
                    "qa_reject",
                )
                self.transition(issue, ts + timedelta(minutes=1), pid, "Reopened")
                issue.effort_total = issue.effort_left = 0.3 * max(issue.effort_total, 1.0)
                issue.started = False
                owner = (
                    issue.assignee
                    if issue.assignee and self.w.people[issue.assignee].employed(d)
                    else self.owner(issue.component, d)
                )
                if owner:
                    self.queue.setdefault(owner, []).insert(0, issue)
                    issue.assignee = owner
                continue
            if r.random() < 0.65 * self.w.people[pid].habits.comment_rate:
                ts = self.comment(issue, ts, pid, "Tested on staging; works as expected.", "qa_pass")
            self.transition(issue, ts + timedelta(minutes=1), pid, "Done")

    # ================================================================================== simple flow
    def add_simple(self, issue: Issue, start_i: int, done_i: int) -> None:
        self.simple.append(Simple(issue, start_i, max(done_i, start_i)))

    def _simple(self, i: int, d: date) -> None:
        for s in list(self.simple):
            issue = s.issue
            if issue.closed:
                self.simple.remove(s)
                continue
            pid = issue.assignee or issue.reporter
            if not self.w.present(pid, d):
                continue
            r = self.rng(pid, "simple")
            if issue.status in ("To Do", "Reopened") and i >= s.start_i:
                ts = self.w.stamp(pid, d, after=issue.updates[-1].ts, hi=0.6)
                self.transition(issue, ts, pid, "In Progress")
                if r.random() < 0.55 * self.w.people[pid].habits.comment_rate:
                    self.comment(
                        issue, ts + timedelta(minutes=30), pid, "Started; first draft today.", "progress_note"
                    )
            elif issue.status == "In Review" and s.reviewer:
                rev = s.reviewer if self.w.people[s.reviewer].employed(d) else pid
                if i < s.review_i or not self.w.present(rev, d):
                    continue
                ts = self.w.stamp(rev, d, after=issue.updates[-1].ts, lo=0.1)
                if r.random() < 0.5:
                    ts = self.comment(issue, ts, rev, "Reviewed; approved.", "review_note")
                self.transition(issue, ts + timedelta(minutes=2), rev, "Done")
                self.simple.remove(s)
            elif issue.status == "In Progress" and i >= s.done_i:
                ts = self.w.stamp(pid, d, after=issue.updates[-1].ts, lo=0.3)
                if r.random() < 0.65 * self.w.people[pid].habits.comment_rate:
                    ts = self.comment(
                        issue, ts, pid, "Done; details are in the linked documents.", "done_note"
                    )
                head = self.eng.dept_head(self.w.people[pid].person.role)
                if head != pid and r.random() < 0.4:
                    self.transition(issue, ts + timedelta(minutes=2), pid, "In Review")
                    s.reviewer, s.review_i = head, i + 1 + int(r.integers(0, 2))
                    continue
                if issue.reporter != pid and r.random() < 0.4 and self.w.present(issue.reporter, d):
                    ts = self.comment(
                        issue,
                        self.w.stamp(issue.reporter, d, after=ts),
                        issue.reporter,
                        "Thanks, looks good.",
                        "thanks_note",
                    )
                self.transition(issue, ts + timedelta(minutes=2), pid, "Done")
                self.simple.remove(s)

    def biz_ticket(
        self,
        pid: str,
        d: date,
        i: int,
        summary: str,
        *,
        project: str = "BIZ",
        itype: str = "Task",
        reporter: str | None = None,
        causes: tuple = (),
        labels: tuple[str, ...] = (),
        client: str | None = None,
        days: tuple[int, int] = (1, 6),
        process: str = "work",
        component: str | None = None,
        parent: Issue | None = None,
        after: datetime | None = None,
        priority: str = "Medium",
    ) -> Issue | None:
        rep = reporter or pid
        if not self.w.present(rep, d):
            rep = pid if self.w.present(pid, d) else None
        if rep is None:
            return None
        r = self.rng(pid, "biz_days")
        ts = self.w.stamp(rep, d, after=after, hi=0.7)
        issue = self.new_issue(
            project,
            itype,
            summary,
            ts,
            rep,
            assignee=pid,
            causes=causes,
            labels=labels,
            client=client,
            process=process,
            component=component,
            parent=parent,
            priority=priority,
            slot_kind="biz_issue",
        )
        start = i + int(r.integers(0, 3))
        self.add_simple(issue, start, start + int(r.integers(days[0], days[1] + 1)))
        return issue
