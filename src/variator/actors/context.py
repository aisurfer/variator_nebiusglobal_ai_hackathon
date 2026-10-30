"""Derived indices of a history shared by the estimators: presence, absences, sprints, source tags."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from variator.actors.history import ENGINEER_ROLES, History, PersonInfo, parse_date


@dataclass
class Spell:
    kind: str  # vacation | sick
    first: date
    last: date  # inclusive
    days: int  # working days
    evidence: list[str] = field(default_factory=list)
    booked_at: datetime | None = None


@dataclass
class Context:
    h: History
    absences: dict[str, dict[date, str]]  # pid -> day -> kind (days before T)
    spells: dict[str, list[Spell]]  # pid -> spells that started before T
    planned: dict[str, list[Spell]]  # pid -> leave booked before T that starts at or after T

    # ------------------------------------------------------------------ tags
    def tag(self, dataset: str) -> str:
        return f"data:{dataset}@{self.h.as_of.isoformat()}"

    @staticmethod
    def group(cohort: str) -> str:
        return f"group:{cohort}"

    # ------------------------------------------------------------------ people
    def employed_span(self, p: PersonInfo) -> tuple[date, date]:
        """[first, end) inside the history window: from max(start, history start) to the last day + 1 or T."""
        a = max(p.start, self.h.start)
        b = self.h.as_of
        if p.last_day is not None and p.last_day < b:
            b = p.last_day + timedelta(days=1)
        return a, max(a, b)

    def employed_days(self, p: PersonInfo) -> list[date]:
        a, b = self.employed_span(p)
        return self.h.workdays(a, b)

    def present_days(self, p: PersonInfo) -> list[date]:
        off = self.absences.get(p.id, {})
        return [d for d in self.employed_days(p) if d not in off]

    def engineers(self) -> list[PersonInfo]:
        return [p for p in self.h.people.values() if p.role in ENGINEER_ROLES]


def build_context(h: History) -> Context:
    """Absence spells from the employee events (vacation and sick leave) known before T."""
    absences: dict[str, dict[date, str]] = defaultdict(dict)
    spells: dict[str, list[Spell]] = defaultdict(list)
    planned: dict[str, list[Spell]] = defaultdict(list)
    by_id: dict[str, list[dict]] = defaultdict(list)
    for e in h.records.get("employee_event", []):
        by_id[e["id"]].append(e)
    for eid, vers in by_id.items():
        typ = vers[0].get("type")
        if typ not in ("vacation", "sick_leave"):
            continue
        pid = vers[0]["employee"]
        last = vers[-1]
        first = parse_date(last["effective_date"])
        kinds = {v["kind"] for v in vers}
        if first is None:
            continue
        kind = "vacation" if typ == "vacation" else "sick"
        if first >= h.as_of:
            if kind == "vacation" and "leave_booked" in kinds:
                end = parse_date(last.get("end_date")) or first
                planned[pid].append(
                    Spell(
                        kind,
                        first,
                        end,
                        len(h.workdays(first, end + timedelta(days=1))),
                        [h.kb("sim", eid)],
                        vers[0]["ts"],
                    )
                )
            continue
        if kind == "vacation" and "leave_started" not in kinds and first >= h.as_of:
            continue
        end = parse_date(last.get("end_date"))
        if end is None:  # sick and not back at T
            end = h.as_of - timedelta(days=1)
        end = min(end, h.as_of - timedelta(days=1))
        days = h.workdays(first, end + timedelta(days=1))
        for d in days:
            absences[pid].setdefault(d, kind)
        spells[pid].append(Spell(kind, first, end, len(days), [h.kb("sim", eid)], vers[0]["ts"]))
    for v in spells.values():
        v.sort(key=lambda s: s.first)
    return Context(h, dict(absences), dict(spells), dict(planned))
