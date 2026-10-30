"""P8 support: client tickets, first responses, escalations to engineering, CSAT.

Each client opens tickets as a counting process with its habit ``support_per_month``. Two
couplings add tickets: an upsell or a new implementation adds ``upsell_support_mult`` - 1
times the base rate for 60 working days (these tickets are caused by the deal), and an
incident makes some affected clients open a ticket. A share of the bug tickets goes to
engineering as a Jira bug; the support ticket closes after the bug is done. Each resolved
ticket (its CSAT, first response and resolution time) goes to the client's NPS (``nps``).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

from variator.orgsim.company import templates as T
from variator.orgsim.company.model import ClientState, Issue, Record, World

if TYPE_CHECKING:
    from variator.orgsim.company.engine import Engine

CATEGORIES = ("how_to", "bug", "access", "data_request", "billing", "feature_request")
CATEGORY_P = (0.33, 0.25, 0.15, 0.10, 0.06, 0.11)
RESOLVE_HOURS = {
    "how_to": (1, 6),
    "bug": (6, 30),
    "access": (0.5, 4),
    "data_request": (6, 24),
    "billing": (4, 16),
    "feature_request": (2, 8),
}
INCIDENT_TICKET_P = {"SEV1": 0.5, "SEV2": 0.3, "SEV3": 0.15}


class Support:
    def __init__(self, w: World, eng: Engine):
        self.w, self.eng = w, eng
        self.pending: list[tuple[datetime, int, str, Record, dict]] = []
        self.open_count: dict[str, int] = {}
        self.n = 0

    def rng(self, entity: str, purpose: str) -> np.random.Generator:
        return self.w.rng("support", entity, purpose)

    def step(self, i: int, d: date) -> None:
        w, t = self.w, self.w.spec.truth
        for cid in sorted(w.clients):
            c = w.clients[cid]
            if not c.active_on(d) and not (c.status == "signed" and c.new):
                continue
            base = c.habits.support_per_month * 12 / 252.0
            if "support" not in c.clocks:
                c.clocks["support"] = w.clock("support", cid, "arrivals")
                c.clocks["support_extra"] = w.clock("support", cid, "extra")
            for _ in range(c.clocks["support"].tick(base if c.active_on(d) else 0.0)):
                self._ticket(c, d, None, None)
            extra = base * (t.upsell_support_mult - 1) if c.upsell_until and d < c.upsell_until else 0.0
            for _ in range(c.clocks["support_extra"].tick(extra)):
                self._ticket(c, d, None, c.upsell_deal)
        self._run_pending(d)

    # ------------------------------------------------------------------ tickets
    def _assignee(self, d: date) -> str:
        sup = self.eng.role_ids("support", d)
        here = [p for p in sup if self.w.present(p, d)]
        pool = here or sup
        if not pool:
            return self.eng.head("delivery")
        return min(pool, key=lambda p: (self.open_count.get(p, 0), p))

    def _ticket(
        self,
        c: ClientState,
        d: date,
        incident: Record | None,
        deal: Record | None,
        opened: datetime | None = None,
    ) -> None:
        w = self.w
        self.n += 1
        r = self.rng(c.id, f"t{self.n}")
        cat = "incident" if incident is not None else str(r.choice(CATEGORIES, p=CATEGORY_P))
        subject = str(T.pick(r, T.SUPPORT_SUBJECTS[cat]))
        if opened is None:
            opened = w.at(d, "08:00", 600, ("support", c.id))
        channel = "email" if r.random() < 0.65 else "portal"
        contact = 1 if cat == "billing" else 0
        assignee = self._assignee(d)
        prio = (
            "P1"
            if incident is not None and incident.state["severity"] == "SEV1"
            else "P2"
            if incident is not None or cat == "bug" and r.random() < 0.3
            else str(r.choice(["P3", "P4"]))
        )
        comps = w.spec.components
        component = incident.state["component"] if incident is not None else str(T.pick(r, comps))
        causes: tuple = (self.eng.proc("PROC-SUPPORT"),)
        if incident is not None:
            causes += (("process", incident),)
        if deal is not None:
            causes += (("process", deal),)
        fields = dict(
            title=f"[{c.profile.name}] {subject}",
            client=c.id,
            status="open",
            priority=prio,
            reporter=f"CNT-{c.id}-{contact + 1}",
            assignee=assignee,
            opened_at=opened.isoformat(),
            category=cat,
            channel=channel,
            contract=c.contract,
            component=component,
        )
        if incident is not None:
            fields["incident"] = incident
        rec = w.record(
            "support_ticket",
            "support",
            opened + timedelta(minutes=1),
            "support",
            "ticket_opened",
            causes,
            prefix="SUP",
            **fields,
        )
        self.open_count[assignee] = self.open_count.get(assignee, 0) + 1
        mail = None
        if channel == "email":
            ct = c.profile.contacts[contact]
            mail = w.mail(
                opened,
                (ct.name, self.eng.clients.contact_email(c, contact)),
                [w.mailbox("support")],
                f"[{c.profile.name}] {subject}",
                f"Hello, {subject.lower().rstrip('?.')}. Could you please help? Thanks, {ct.name.split()[0]}",
                process="support",
                kind="support_request",
                slot_kind="support_request",
            )
        # first response in the assignee's working hours
        hours = self.w.people[assignee].habits.response_hours * math.exp(r.normal(0, 0.5))
        if prio == "P1":
            hours = min(hours, 0.3)
        first = self._work_time(assignee, opened, hours)
        self.pending.append(
            (
                first,
                rec.uid,
                "first_response",
                rec,
                {
                    "mail": mail,
                    "cat": cat,
                    "assignee": assignee,
                    "client": c,
                    "incident": incident,
                    "r_hours": RESOLVE_HOURS.get(cat, (1, 6)),
                    "subject": subject,
                    "component": component,
                    "opened": opened,
                    "first": first,
                },
            )
        )

    def _work_time(self, pid: str, after: datetime, hours: float) -> datetime:
        """``hours`` of work time after ``after``, moved to a day when ``pid`` is at work."""
        w = self.w
        t = w.cal.add_work_hours(after, max(hours, 0.05))
        day = w.next_present_day(pid, t.date()) if pid in w.people else None
        if day is None:
            return t
        if day != t.date():
            t = w.stamp(pid, day, hi=0.3, purpose="support")
        return t

    def incident_tickets(
        self, incident: Record, clients: list[ClientState], d: date, detected: datetime
    ) -> None:
        sev = incident.state["severity"]
        for c in clients:
            r = self.rng(c.id, f"inc-{incident.uid}")
            if r.random() < INCIDENT_TICKET_P[sev]:
                self._ticket(
                    c, d, incident, None, opened=detected + timedelta(minutes=int(r.integers(4, 45)))
                )

    # ------------------------------------------------------------------ scheduled steps
    def _run_pending(self, d: date) -> None:
        end = self.w.cal.at(d, "23:59")
        due = sorted((x for x in self.pending if x[0] <= end), key=lambda x: (x[0], x[1]))
        self.pending = [x for x in self.pending if x[0] > end]
        for ts, _, action, rec, ctx in due:
            getattr(self, "_" + action)(ts, rec, ctx)

    def _first_response(self, ts: datetime, rec: Record, ctx: dict) -> None:
        w, t = self.w, self.w.spec.truth
        pid = ctx["assignee"]
        c = ctx["client"]
        actor = pid if w.present(pid, ts.date()) else None
        rec.update(
            ts,
            "support",
            "first_response",
            status="in_progress",
            first_response_at=ts.isoformat(),
            actor=actor,
        )
        if ctx["mail"] is not None and actor:
            ctx["reply"] = w.mail(
                ts + timedelta(minutes=1),
                w.addr(pid),
                [ctx["mail"].sender],
                f"Re: {ctx['mail'].subject}",
                "Thank you for the report. We are looking into it and will update you shortly.",
                process="support",
                kind="support_reply",
                cc=[w.mailbox("support")],
                parent=ctx["mail"],
                slot_kind="support_reply",
            )
        r = self.rng(c.id, f"res-{rec.uid}")
        cat = ctx["cat"]
        if cat == "incident":
            inc = ctx["incident"]
            done = datetime.fromisoformat(inc.state.get("resolved_at") or inc.state["started_at"])
            self.pending.append(
                (max(done, ts) + timedelta(minutes=int(r.integers(10, 60))), rec.uid, "resolve", rec, ctx)
            )
            return
        if cat == "bug" and actor and r.random() < min(t.support_escalation / CATEGORY_P[1], 0.95):
            self._escalate(ts, rec, ctx, r)
            return
        if cat == "feature_request" and actor and r.random() < 0.3:
            work = self.eng.work
            story = work.new_issue(
                "PLAT",
                "Story",
                T.story_summary(r, ctx["component"]),
                ts + timedelta(minutes=5),
                pid,
                component=ctx["component"],
                points=int(r.choice([2, 3, 5])),
                labels=("customer-request",),
                client=c.id,
                causes=(("process", rec),),
                description=f"Requested by {c.profile.name} through support.",
            )
            work.commit(story)
            ctx["story"] = story
        lo, hi = ctx["r_hours"]
        hours = lo + (hi - lo) * float(r.random())
        self.pending.append((self._work_time(pid, ts, hours), rec.uid, "resolve", rec, ctx))

    def _escalate(self, ts: datetime, rec: Record, ctx: dict, r: np.random.Generator) -> None:
        w, work = self.w, self.eng.work
        pid, comp, d = ctx["assignee"], ctx["component"], ts.date()
        owner = work.owner(comp, d)
        if owner is None:
            return
        bug = work.new_issue(
            "PLAT",
            "Bug",
            f"{ctx['subject'].rstrip('?.')} ({ctx['client'].profile.name})",
            ts + timedelta(minutes=10),
            pid,
            assignee=owner,
            component=comp,
            points=int(r.choice([1, 2, 3, 5])),
            priority="High",
            labels=("support",),
            client=ctx["client"].id,
            causes=(("process", rec),),
            description=f"Escalated from support by {w.person_name(pid)}.",
        )
        work.add_unplanned(bug, owner, ts + timedelta(minutes=10))
        rec.update(
            ts + timedelta(minutes=12),
            "support",
            "ticket_escalated",
            status="escalated",
            related=[bug],
            actor=pid,
        )
        bug.on_done.append(lambda issue, when, rec=rec, ctx=ctx: self._bug_done(issue, when, rec, ctx))

    def _bug_done(self, issue: Issue, when: datetime, rec: Record, ctx: dict) -> None:
        self.pending.append((self._work_time(ctx["assignee"], when, 1.0), rec.uid, "resolve", rec, ctx))

    def _resolve(self, ts: datetime, rec: Record, ctx: dict) -> None:
        w, t = self.w, self.w.spec.truth
        pid = ctx["assignee"]
        if not w.people[pid].employed(ts.date()):
            pid = self._assignee(ts.date())
        actor = pid if w.present(pid, ts.date()) else None
        r = self.rng(ctx["client"].id, f"csat-{rec.uid}")
        first_h = (ctx["first"] - ctx["opened"]).total_seconds() / 3600
        total_d = (ts - ctx["opened"]).total_seconds() / 86400
        score = (
            4.7
            - 0.4 * (first_h > 4)
            - 0.5 * (total_d > 3)
            + 2.0 * (t.support_quality - 0.8)
            + r.normal(0, 0.6)
        )
        csat = int(min(max(round(score), 1), 5)) if r.random() < 0.6 else None
        rec.update(
            ts,
            "support",
            "ticket_resolved",
            status="resolved",
            resolved_at=ts.isoformat(),
            csat=csat,
            assignee=pid,
            actor=actor,
        )
        self.open_count[ctx["assignee"]] = max(self.open_count.get(ctx["assignee"], 1) - 1, 0)
        self.eng.nps.ticket_resolved(ctx["client"], ts, csat, first_h, total_d, rec)
        parent = ctx.get("reply") or ctx.get("mail")
        if parent is not None and actor and r.random() < 0.7:
            w.mail(
                ts + timedelta(minutes=2),
                w.addr(pid),
                [ctx["mail"].sender],
                f"Re: {ctx['mail'].subject}",
                "This is now resolved on our side. Please let us know if you see it again.",
                process="support",
                kind="support_resolved",
                cc=[w.mailbox("support")],
                parent=parent,
                slot_kind="support_resolved",
            )
