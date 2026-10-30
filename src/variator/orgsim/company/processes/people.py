"""P9 people: leave, sickness, growth, salary reviews, rotation, resignations and hiring.

Life events of a person (09 §2.4):

- sickness: spells from a counting process with the person's rate; holidays: the main
  two-week block in the person's month and short breaks;
- growth: the pace rises and so does the salary expectation; a quarterly review grants a
  raise or not; an unmet expectation raises the resignation hazard;
- rotation: a developer moves to another component and is slower for a month;
- resignation: a one-time event with the hazard ``attrition`` + the burnout rule (sprint load
  above 1.05) + the unmet expectation. The leaver gives a month of notice; the CEO decides to
  backfill; a requisition runs for about ``hiring_lead`` months; the new hire (a slot actor
  ``hire-slot-n`` from the role cohort) ramps up.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import TYPE_CHECKING

import numpy as np

from variator.orgsim.company import templates as T
from variator.orgsim.company.habits import NamePool, person_habits
from variator.orgsim.company.model import FirstPassage, PersonState, Record, World, annual_hazard
from variator.orgsim.company.spec import Person
from variator.orgsim.streams import slot_key

if TYPE_CHECKING:
    from variator.orgsim.company.engine import Engine

TEAMS = {
    "api": "Platform API",
    "billing-engine": "Billing",
    "data-pipeline": "Data",
    "ml-inference": "ML Platform",
    "integrations": "Integrations",
    "auth": "Identity",
    "web-app": "Web",
    "infrastructure": "Infrastructure",
}
BASE_REASONS = ["an external offer", "relocation", "a career change", "personal reasons"]
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def month_label(d: date) -> str:
    return f"{MONTHS[d.month - 1]} {d.year}"


class People:
    def __init__(self, w: World, eng: Engine):
        self.w, self.eng = w, eng
        self.names = NamePool(w.streams, {p.name for p in w.spec.people})
        self.spells: dict[str, list[tuple[str, date, date]]] = {}  # pid -> (kind, first, last)
        self.resign: dict[str, FirstPassage] = {}
        self.growth: dict[str, object] = {}
        self.rotation: dict[str, object] = {}
        self.biz_clock: dict[str, object] = {}
        self.pending: list[tuple[date, str, object]] = []  # (day, action, payload)
        self.hires = 0
        self.next_id = len(w.spec.people) + 1
        self.vacancies: dict[str, Record] = {}  # seat -> requisition
        self.availability: list[dict[str, str]] = []
        self._leave_rec: dict[tuple[str, date], Record] = {}
        self._cands: dict[str, str] = {}

    # ================================================================================== setup
    def setup(self) -> None:
        w = self.w
        for p in w.spec.people:
            h = person_habits(w.spec, p, w.streams)
            start = w.start - timedelta(days=int(30.4 * p.tenure_months))
            st = PersonState(p, h, start, p.id, p.salary, velocity=h.velocity, team=self._team(p))
            w.people[p.id] = st
            self._init_actor(st, w.start)

    def _team(self, p: Person) -> str:
        if p.role == "qa":
            return "QA"
        return TEAMS.get(p.skills[0], "Platform") if p.skills else p.department

    def _init_actor(self, st: PersonState, since: date) -> None:
        pid = st.id
        self.resign[pid] = FirstPassage(
            self.w.rng("people", st.seat if st.hire_slot is None else st.hire_slot, f"resign-{pid}")
        )
        self.growth[pid] = self.w.clock("people", pid, "growth")
        self.rotation[pid] = self.w.clock("people", pid, "rotation")
        self.biz_clock[pid] = self.w.clock("people", pid, "biz")
        self._plan_absences(st, since)

    def _plan_absences(self, st: PersonState, since: date) -> None:
        w, pid, h = self.w, st.id, st.habits
        r = w.rng("people", pid, "leave")
        days = [d for d in w.days if d >= since]
        taken: dict[date, str] = w.absences.setdefault(pid, {})
        spells: list[tuple[str, date, date]] = []

        def book(kind: str, first: int, n: int) -> None:
            block = days[first : first + n]
            if not block or any(x in taken for x in block):
                return
            for x in block:
                taken[x] = kind
            spells.append((kind, block[0], block[-1]))

        months = sorted({(d.year, d.month) for d in days})
        for y, m in months:
            idx = [k for k, d in enumerate(days) if (d.year, d.month) == (y, m)]
            if len(idx) < 8:
                continue
            if m == h.vacation_month:
                n = 5 if m == 12 else 10
                book("vacation", idx[int(r.integers(0, max(len(idx) - n // 2, 1)))], n)
            elif r.random() < 0.22:
                book("vacation", idx[int(r.integers(0, len(idx)))], int(r.integers(2, 4)))
        sick = w.clock("people", pid, "sick")
        k = 0
        while k < len(days):
            if sick.tick(h.sick_spells / 252.0) and days[k] not in taken:
                n = 1 + int(min(r.geometric(0.45) - 1, 7))
                book("sick", k, n)
                k += n
            else:
                k += 1
        spells.sort(key=lambda s: s[1])
        self.spells[pid] = spells
        for kind, a, b in spells:
            self.availability.append(
                {"person": pid, "from": a.isoformat(), "to": b.isoformat(), "kind": kind}
            )
            if kind == "vacation":
                book_day = w.bday(a, -int(r.integers(10, 40)))
                self.pending.append((max(book_day, since), "book_leave", (pid, a, b)))
            self.pending.append((a, f"{kind}_start", (pid, a, b)))
            self.pending.append((w.bday(b, 1), f"{kind}_end", (pid, a, b)))

    # ================================================================================== the day
    def step(self, i: int, d: date) -> None:
        w = self.w
        due = [x for x in self.pending if x[0] <= d]
        self.pending = [x for x in self.pending if x[0] > d]
        for _, action, payload in sorted(due, key=lambda x: (x[0], x[1], str(x[2])[:40])):
            getattr(self, "_" + action)(d, payload)
        if d.month in (1, 4, 7, 10) and (i == 0 or w.days[i - 1].month != d.month):
            self._salary_review(d)
        t = w.spec.truth
        for pid in sorted(w.people):
            p = w.people[pid]
            if not p.employed(d) or p.person.executive:
                continue
            if p.notice is None:
                burnout = self.burnout(p)
                a = (
                    p.habits.attrition
                    + (t.burnout_attrition_add if burnout else 0.0)
                    + (t.unmet_expectation_attrition_add if p.unmet else 0.0)
                )
                if self.resign[pid].tick(annual_hazard(a)):
                    self._resign(pid, d, burnout)
                    continue
            if self.growth[pid].tick(p.habits.growth_rate / 252.0) and p.notice is None:
                self._grow(pid, d)
            if self.rotation[pid].tick(p.habits.rotation_rate / 252.0) and p.notice is None:
                self._rotate(pid, d)

    def burnout(self, p: PersonState) -> bool:
        recent = p.loads[-2:]
        return bool(recent) and float(np.mean(recent)) > self.w.spec.truth.burnout_load

    # ------------------------------------------------------------------ leave and sickness
    def _ee(
        self,
        ts,
        kind: str,
        pid: str,
        typ: str,
        effective: date,
        causes: tuple,
        *,
        actor: str | None = None,
        rec: Record | None = None,
        **fields,
    ) -> Record:
        p = self.w.people[pid]
        base = dict(
            employee=pid,
            type=typ,
            effective_date=effective.isoformat(),
            role=p.person.title,
            department=p.person.department,
            **fields,
        )
        if actor:
            base["actor"] = actor
        if rec is None:
            return self.w.record("employee_event", "people", ts, "people", kind, causes, prefix="EE", **base)
        if not actor:
            rec.state.pop("actor", None)
        return rec.update(ts, "people", kind, causes, **base)

    def _book_leave(self, d: date, payload) -> None:
        pid, a, b = payload
        day = self.w.next_present_day(pid, d) or d
        if day >= a or day >= self.w.anchor:
            day = d
        actor = pid if self.w.present(pid, day) else None
        ts = self.w.stamp(pid, day, hi=0.8) if actor else self.w.at(day, "08:00", 30, ("people", pid))
        rec = self._ee(
            ts,
            "leave_booked",
            pid,
            "vacation",
            a,
            (self.eng.rule("RULE-LEAVE"),),
            actor=actor,
            end_date=b.isoformat(),
            status="approved",
            owner=self.eng.dept_head(self.w.people[pid].person.role),
        )
        self._leave_rec[(pid, a)] = rec

    def _vacation_start(self, d: date, payload) -> None:
        pid, a, b = payload
        rec = self._leave_rec.get((pid, a))
        ts = self.w.at(d, "07:00", 50, ("people", pid))
        rec = self._ee(
            ts,
            "leave_started",
            pid,
            "vacation",
            a,
            (self.eng.rule("RULE-LEAVE"),),
            rec=rec,
            end_date=b.isoformat(),
            status="on_leave",
        )
        self._leave_rec[(pid, a)] = rec

    def _vacation_end(self, d: date, payload) -> None:
        pid, a, b = payload
        rec = self._leave_rec.pop((pid, a), None)
        if rec is None or not self.w.people[pid].employed(d):
            return
        ts = self.w.at(d, "07:00", 50, ("people", pid))
        days = len(self.w.cal.workdays(a, b + timedelta(days=1)))
        self._ee(
            ts,
            "leave_ended",
            pid,
            "vacation",
            a,
            (),
            rec=rec,
            end_date=b.isoformat(),
            status="completed",
            days=days,
        )

    def _sick_start(self, d: date, payload) -> None:
        pid, a, b = payload
        if not self.w.people[pid].employed(d):
            return
        ts = self.w.at(d, "08:05", 40, ("people", pid))
        self._leave_rec[(pid, a)] = self._ee(
            ts, "sickness_reported", pid, "sick_leave", a, (self.eng.rule("RULE-SICK"),), status="on_leave"
        )

    def _sick_end(self, d: date, payload) -> None:
        pid, a, b = payload
        rec = self._leave_rec.pop((pid, a), None)
        if rec is None or not self.w.people[pid].employed(d):
            return
        actor = pid if self.w.present(pid, d) else None
        ts = self.w.stamp(pid, d, hi=0.1) if actor else self.w.at(d, "09:00", 30, ("people", pid))
        days = len(self.w.cal.workdays(a, b + timedelta(days=1)))
        self._ee(
            ts,
            "returned_to_work",
            pid,
            "sick_leave",
            a,
            (),
            rec=rec,
            actor=actor,
            end_date=b.isoformat(),
            status="completed",
            days=days,
        )

    # ------------------------------------------------------------------ growth, reviews, rotation
    def _grow(self, pid: str, d: date) -> None:
        w = self.w
        p = w.people[pid]
        r = w.rng("people", pid, "grow")
        area = str(T.pick(r, p.person.skills or [p.team or "the team"]))
        expect = round(p.salary * (1.08 + 0.07 * float(r.random())), -2)
        ts = w.at(d, "08:30", 60, ("people", pid))
        rec = self._ee(
            ts,
            "growth_recognised",
            pid,
            "growth",
            d,
            (),
            status="recognised",
            description=f"{p.person.name} took ownership of {area} and leads more of the work there.",
            salary=p.salary,
            salary_expectation=expect,
            currency="USD",
            owner=self.eng.dept_head(p.person.role),
        )
        p.expectation = max(p.expectation, expect)
        p.velocity *= 1.12
        p.growth_event = rec  # type: ignore[attr-defined]

    def _salary_review(self, d: date) -> None:
        w = self.w
        cfo = self.eng.head("finance")
        for pid in sorted(w.people):
            p = w.people[pid]
            if not p.employed(d) or p.notice is not None or p.expectation <= p.salary:
                continue
            growth = getattr(p, "growth_event", None)
            r = w.rng("people", pid, "review")
            grant = r.random() < (w.spec.truth.raise_grant if p.unmet is None else 0.5)
            day = w.next_present_day(cfo, d) or d
            actor = cfo if w.present(cfo, day) else None
            ts = w.stamp(cfo, day, hi=0.9) if actor else w.at(day, "11:00", 60, ("people", pid))
            causes = (self.eng.rule("RULE-SALARY-REVIEW"),) + ((("life_event", growth),) if growth else ())
            if grant:
                eff = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
                self._ee(
                    ts,
                    "salary_changed",
                    pid,
                    "salary_change",
                    eff,
                    causes,
                    actor=actor,
                    status="approved",
                    salary=p.expectation,
                    previous_salary=p.salary,
                    currency="USD",
                    owner=cfo,
                )
                p.salary, p.unmet = p.expectation, None
                p.salary_hist.append((eff, p.salary))
            else:
                rec = self._ee(
                    ts,
                    "salary_review_declined",
                    pid,
                    "salary_review",
                    d,
                    causes,
                    actor=actor,
                    status="declined",
                    salary=p.salary,
                    salary_expectation=p.expectation,
                    currency="USD",
                    owner=cfo,
                )
                p.unmet = rec

    def _rotate(self, pid: str, d: date) -> None:
        w = self.w
        p = w.people[pid]
        r = w.rng("people", pid, "rotate")
        options = [c for c in w.spec.components if c not in p.person.skills and c != "infrastructure"]
        if not options:
            return
        new = str(T.pick(r, options))
        old_team = p.team
        p.person = p.person.model_copy(update={"skills": [new] + p.person.skills[:1]})
        p.team = TEAMS.get(new, old_team)
        p.slow_until = w.bday(d, 20)
        ts = w.at(d, "09:15", 60, ("people", pid))
        self._ee(
            ts,
            "rotation_started",
            pid,
            "rotation",
            d,
            (),
            status="active",
            from_team=old_team,
            to_team=p.team,
            owner=self.eng.head("delivery"),
        )

    # ------------------------------------------------------------------ resignation and hiring
    def _resign(self, pid: str, d: date, burnout: bool) -> None:
        w, t = self.w, self.w.spec.truth
        p = w.people[pid]
        r = w.rng("people", pid, "reason")
        parts = [("base", p.habits.attrition)]
        if burnout:
            parts.append(("burnout", t.burnout_attrition_add))
        if p.unmet:
            parts.append(("unmet", t.unmet_expectation_attrition_add))
        total = sum(x for _, x in parts)
        u, acc, why = float(r.random()) * total, 0.0, "base"
        for name, x in parts:
            acc += x
            if u <= acc:
                why = name
                break
        reason = {
            "burnout": "burnout after months over capacity",
            "unmet": "the salary expectation after the growth was not met",
        }.get(why) or str(T.pick(r, BASE_REASONS))
        causes: tuple = ()
        if why == "burnout":
            causes = (self.eng.rule("RULE-BURNOUT"),)
        elif why == "unmet" and p.unmet is not None:
            causes = (("life_event", p.unmet),)
        le = w.record(
            "life_event",
            "people",
            w.at(d, "07:10", 40, ("people", pid)),
            "people",
            "life_event_happened",
            causes,
            prefix="LE",
            title=f"{p.person.name} decides to leave",
            statement=f"{p.person.name} decides to leave the company: {reason}.",
            employee=pid,
            effective_date=d.isoformat(),
            reason=why,
            burnout=burnout,
            load_recent=round(float(np.mean(p.loads[-2:])) if p.loads else 0.0, 3),
        )
        day = w.next_present_day(pid, d) or d
        last = w.bday(day, t.notice_bdays)
        here = w.present(pid, day)
        ts = w.stamp(pid, day, hi=0.5) if here else w.at(day, "09:00", 30, ("people", pid))
        letter = (
            w.mail(
                ts,
                w.addr(pid),
                [w.addr(self.eng.head("finance"))],
                f"Resignation - {p.person.name}",
                f"Dear Morgan, I am writing to resign from my role as {p.person.title}. "
                f"My last working day will be {last:%d %B %Y}.",
                process="people",
                kind="resignation_letter",
                cc=[w.addr(p.person.manager)] if p.person.manager and p.person.manager != pid else [],
                slot_kind="resignation_letter",
            )
            if here
            else None
        )
        ee = self._ee(
            ts + timedelta(minutes=20),
            "resignation_received",
            pid,
            "resignation",
            day,
            (("life_event", le), self.eng.rule("RULE-NOTICE")),
            actor=pid if here else None,
            status="notice",
            end_date=last.isoformat(),
            related=[letter] if letter else [],
            owner=self.eng.head("finance"),
        )
        p.notice, p.last_day = ee, last
        self.pending.append((w.bday(last, 1), "exit", (pid, ee)))
        self.pending.append((w.bday(day, 1), "decide_backfill", (pid, ee)))
        cfo = self.eng.head("finance")
        self.eng.work.biz_ticket(
            cfo,
            day,
            w.index.get(day, 0),
            f"Offboarding: {p.person.name}",
            days=(t.notice_bdays - 5, t.notice_bdays),
            after=ts,
        )

    def _exit(self, d: date, payload) -> None:
        pid, ee = payload
        w = self.w
        p = w.people[pid]
        ts = w.at(d, "07:30", 30, ("people", pid))
        self._ee(
            ts,
            "employee_left",
            pid,
            "exit",
            p.last_day,
            (("life_event", ee),),
            status="left",
            owner=self.eng.head("finance"),
        )

    def _decide_backfill(self, d: date, payload) -> None:
        pid, ee = payload
        w, t = self.w, self.w.spec.truth
        p = w.people[pid]
        ceo, cfo = self.eng.head("ceo"), self.eng.head("finance")
        day = w.next_present_day(ceo, d) or d
        here = w.present(ceo, day)
        ts = w.stamp(ceo, day, hi=0.6) if here else w.at(day, "10:00", 60, ("people", pid))
        title = self._base_title(p.person.title)
        mail = (
            w.mail(
                ts,
                w.addr(ceo),
                [w.addr(cfo)],
                f"Approved: backfill {title}",
                f"Morgan, please open a requisition for a {title} to replace {p.person.name}.",
                process="people",
                kind="hiring_approval",
                cc=[w.addr(self.eng.head("delivery"))],
                slot_kind="hiring_approval",
            )
            if here
            else None
        )
        dec = w.record(
            "decision",
            "people",
            ts + timedelta(minutes=2),
            "people",
            "decision_made",
            (("life_event", ee),),
            prefix="DEC",
            title=f"Backfill the {title} role",
            statement=f"Hire a {title} to replace {p.person.name}.",
            status="accepted",
            owner=ceo,
            decided_at=ts.isoformat(),
            related=[mail] if mail else [],
            actor=ceo if here else None,
        )
        r = w.rng("people", p.seat, f"lead-{pid}")
        months = t.hiring_lead * math.exp(r.normal(0, 0.25))
        start = w.cal.roll_forward(day + timedelta(days=int(30.4 * months)))
        if start.weekday() != 0:  # people start on a Monday
            start = w.cal.roll_forward(start + timedelta(days=(7 - start.weekday()) % 7))
        self.hires += 1
        slot = slot_key("hire", self.hires)
        req = w.record(
            "requisition",
            "people",
            ts + timedelta(minutes=40),
            "people",
            "requisition_opened",
            (("decision", dec), self.eng.proc("PROC-HIRING")),
            prefix="REQ",
            role=title,
            department=p.person.department,
            status="open",
            owner=cfo,
            opened_at=day.isoformat(),
            target_start=start.isoformat(),
            replaces=pid,
            hiring_manager=p.person.manager or ceo,
            salary_budget=round(p.salary * 1.05, -3),
            currency="USD",
        )
        self.vacancies[p.seat] = req
        steps = [
            (w.bday(day, 5), "sourcing"),
            (w.bday(day, 20), "interviewing"),
            (w.bday(start, -18), "offer_made"),
            (w.bday(start, -12), "offer_accepted"),
        ]
        for when, status in steps:
            if when > day:
                self.pending.append((when, "req_status", (req, status, slot, pid)))
        self.pending.append((start, "hire", (req, slot, pid)))
        self.eng.work.biz_ticket(
            cfo,
            day,
            w.index.get(day, 0),
            f"Hiring: {title}",
            after=ts,
            days=(max(int(months * 21) - 5, 5), int(months * 21)),
        )

    @staticmethod
    def _base_title(title: str) -> str:
        for prefix in ("Senior ", "Lead "):
            if title.startswith(prefix):
                return title[len(prefix) :]
        return title

    def _req_status(self, d: date, payload) -> None:
        req, status, slot, pid = payload
        w = self.w
        ts = w.at(d, "10:00", 240, ("people", slot))
        extra = {}
        if status == "offer_accepted":
            extra = {"candidate": self._candidate(slot)}
        req.update(ts, "people", f"requisition_{status}", status=status, **extra)

    def _candidate(self, slot: str) -> str:
        if slot not in self._cands:
            self._cands[slot] = self.names.person(slot)
        return self._cands[slot]

    def _hire(self, d: date, payload) -> None:
        req, slot, old = payload
        w = self.w
        seat = w.people[old].seat
        prev = w.people[old].person
        pid = f"LW-E{self.next_id:02d}"
        self.next_id += 1
        name = self._candidate(slot)
        first, last = name.lower().split(" ", 1)
        login = f"{first}.{last.replace(' ', '').replace(chr(39), '')}"
        r = w.rng("people", slot, "salary")
        person = Person(
            id=pid,
            name=name,
            title=self._base_title(prev.title),
            role=prev.role,
            department=prev.department,
            manager=prev.manager if prev.manager != old else None,
            login=login,
            salary=round(prev.salary * float(np.exp(r.normal(0.02, 0.08))), -3),
            tenure_months=0,
            skills=list(prev.skills),
        )
        probe = person.model_copy(update={"id": slot})
        h = person_habits(w.spec, probe, w.streams).model_copy(update={"person_id": pid})
        st = PersonState(
            person, h, d, seat, person.salary, velocity=h.velocity, team=self._team(person), hire_slot=slot
        )
        st.ramp_until = [(w.bday(d, 20), 0.5), (w.bday(d, 40), 0.75)]
        w.people[pid] = st
        self._init_actor(st, w.bday(d, 1))
        ts = w.at(d, "08:45", 20, ("people", pid))
        req.update(ts, "people", "requisition_filled", status="filled", filled_at=d.isoformat(), employee=pid)
        self._ee(
            ts + timedelta(minutes=5),
            "employee_joined",
            pid,
            "hire",
            d,
            (("process", req),),
            status="active",
            salary=person.salary,
            currency="USD",
            owner=self.eng.head("finance"),
        )
        self.vacancies.pop(seat, None)
        cfo = self.eng.head("finance")
        self.eng.work.biz_ticket(cfo, d, w.index.get(d, 0), f"Onboarding: {name}", days=(3, 8), after=ts)

    # ================================================================================== business tickets
    def biz(self, i: int, d: date) -> None:
        w = self.w
        first_of_month = i == 0 or w.days[i - 1].month != d.month
        prev = w.days[i - 1] if i else d - timedelta(days=28)
        quarter = f"Q{(d.month - 1) // 3 + 1} {d.year}"
        if first_of_month:
            recurring = [
                ("finance", f"Month-end close for {month_label(prev)}"),
                ("marketing", f"Newsletter for {month_label(d)}"),
                ("ceo", f"Board pack for {month_label(prev)}"),
            ]
            if d.month in (1, 4, 7, 10):
                recurring.append(("delivery", f"Capacity plan for {quarter}"))
            for role, summary in recurring:
                for pid in self.eng.role_ids(role, d)[:1]:
                    self.eng.work.biz_ticket(pid, d, i, summary, days=(2, 6))
        clients = [c for c in w.clients.values() if c.profile.key and c.active_on(d)]
        for pid in sorted(w.people):
            p = w.people[pid]
            role = p.person.role
            if role in ("backend", "frontend", "devops", "qa") or not w.present(pid, d):
                continue
            n = self.biz_clock[pid].tick(p.habits.ticket_rate)
            r = w.rng("people", pid, "biz_text")
            for _ in range(n):
                client = clients[int(r.integers(len(clients)))].profile.name if clients else "a client"
                summary = T.biz_summary(r, role, month_label(d), quarter, client)
                if role == "analyst_designer" and r.random() < 0.6:
                    comp = str(T.pick(r, w.spec.components))
                    summary = str(T.pick(r, T.DESIGN_TASKS)).format(f=T.feature(r, comp))
                    self.eng.work.biz_ticket(pid, d, i, summary, project="PLAT", component=comp, days=(2, 7))
                else:
                    self.eng.work.biz_ticket(pid, d, i, summary)
