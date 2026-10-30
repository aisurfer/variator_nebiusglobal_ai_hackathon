"""The world of one history run: objects with versions, actors, clocks and time stamps.

Processes change the world day by day. They create objects and add versions to them:

- ``Record``: a business record of the ``sim`` provider (contract, invoice, incident...).
  Each version is a full snapshot of its fields.
- ``Issue``, ``MergeRequest``, ``Mail``, ``Page``: Jira, GitLab, e-mail and Confluence
  objects; ``render`` turns their updates into provider payloads.

External ids (``INV-00042``, ``PLAT-17``) are given at the end, in time order, by
``render.assign_ids``. Until then objects refer to each other directly; a field value or a
cause that is an object is written as its id. Money that the ARR calibration scales is a
``Money`` value (``generator`` sets ``Money.scale``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np

from variator.orgsim.calendar import BusinessCalendar
from variator.orgsim.company.habits import ClientHabits, ClientProfile, PersonHabits
from variator.orgsim.company.spec import CompanySpec, Person
from variator.orgsim.streams import Streams

SIM_TYPES = (
    "contract",
    "invoice",
    "payment",
    "bank_entry",
    "deal",
    "employee_event",
    "requisition",
    "payroll",
    "incident",
    "deploy",
    "support_ticket",
    "cloud_bill",
    "tax_payment",
    "decision",
    "nps_response",
    # the cause catalog of the KB sim adapter
    "life_event",
    "process",
    "rule",
)
BDAYS_PER_YEAR = 252.0


# ---------------------------------------------------------------------------------------------- money
@dataclass(frozen=True)
class Money:
    """An amount the ARR calibration scales; ``step`` is the rounding unit after scaling."""

    raw: float
    step: float = 0.01

    def value(self, scale: float) -> float:
        v = round(self.raw * scale / self.step) * self.step
        return round(v, 2)


def amount(x: Any, scale: float) -> float:
    return x.value(scale) if isinstance(x, Money) else float(x)


@dataclass(frozen=True)
class TextSlot:
    """A field value that marks a text for a later writer: ``render`` writes ``sim:<record id>:<kind>``."""

    kind: str


# ---------------------------------------------------------------------------------------------- clocks
class ArrivalClock:
    """Arrivals of a counting process by time change (04 §1.6, ``streams.arrival_counts``).

    Arrival k happens in the period where the cumulative hazard reaches E_1 + ... + E_k,
    E_i ~ Exp(1) drawn in order from the clock's own stream.
    """

    def __init__(self, rng: np.random.Generator):
        self.rng = rng
        self.cum = 0.0
        self.next = float(rng.standard_exponential())

    def tick(self, hazard: float) -> int:
        self.cum += max(float(hazard), 0.0)
        n = 0
        while self.cum >= self.next:
            n += 1
            self.next += float(self.rng.standard_exponential())
        return n


class FirstPassage:
    """A one-time event: it fires in the first period where the cumulative hazard reaches E ~ Exp(1)."""

    def __init__(self, rng: np.random.Generator):
        self.e = float(rng.standard_exponential())
        self.cum = 0.0
        self.fired = False

    def tick(self, hazard: float) -> bool:
        if self.fired:
            return False
        self.cum += max(float(hazard), 0.0)
        if self.cum >= self.e:
            self.fired = True
            return True
        return False


def annual_hazard(p: float) -> float:
    """Hazard per working day from an annual probability."""
    p = min(max(p, 0.0), 0.999)
    return -math.log1p(-p) / BDAYS_PER_YEAR


# ---------------------------------------------------------------------------------------------- objects
Cause = tuple[str, Any]  # (cause type, the object that is the cause: a record, issue, MR, mail or page)


class Obj:
    otype = "object"

    def __init__(self, uid: int):
        self.uid = uid
        self.ext: str | None = None

    def ref(self) -> str:
        """The knowledge-base entity id of the object (a cause id or a reference field value)."""
        return str(self.ext)

    @property
    def first_ts(self) -> datetime:
        raise NotImplementedError


@dataclass
class Version:
    ts: datetime
    process: str
    kind: str
    snapshot: dict[str, Any]
    causes: tuple[Cause, ...]
    order: int


class Record(Obj):
    """A business record of the sim provider.

    Each version holds the full state of the record and every cause that still holds
    (the KB adapter closes a causal link when its cause is missing from a later version).
    """

    def __init__(self, uid: int, otype: str, container: str, world: World, prefix: str):
        super().__init__(uid)
        self.otype = otype
        self.container = container
        self.prefix = prefix
        self.state: dict[str, Any] = {}
        self.causes: list[Cause] = []
        self.versions: list[Version] = []
        self._world = world

    def ref(self) -> str:
        return str(self.ext)

    @property
    def first_ts(self) -> datetime:
        return self.versions[0].ts

    @property
    def last_ts(self) -> datetime:
        return self.versions[-1].ts

    def update(
        self,
        ts: datetime,
        process: str,
        kind: str,
        causes: tuple[Cause, ...] = (),
        drop: tuple[Cause, ...] = (),
        **changes: Any,
    ) -> Record:
        if self.versions and ts <= self.versions[-1].ts:
            ts = self.versions[-1].ts + timedelta(seconds=30)
        for c in causes:
            if not any(c[0] == x[0] and c[1] is x[1] for x in self.causes):
                self.causes.append(c)
        self.causes = [c for c in self.causes if not any(c[0] == x[0] and c[1] is x[1] for x in drop)]
        self.state.update(changes)
        self.versions.append(
            Version(ts, process, kind, dict(self.state), tuple(self.causes), self._world.order())
        )
        return self


@dataclass
class IssueUpdate:
    ts: datetime
    actor: str
    kind: str  # created | transition | comment | assign | link | remote_link
    data: dict[str, Any]
    causes: tuple[Cause, ...]
    order: int
    process: str = "work"


class Issue(Obj):
    otype = "jira"

    def __init__(
        self,
        uid: int,
        project: str,
        itype: str,
        summary: str,
        component: str | None,
        points: int,
        priority: str,
        reporter: str,
        assignee: str | None,
        created: datetime,
        parent: Issue | None,
        labels: list[str],
        description: str,
        client: str | None,
        slot_kind: str,
    ):
        super().__init__(uid)
        self.project, self.itype, self.summary, self.component = project, itype, summary, component
        self.points, self.priority, self.reporter, self.assignee = points, priority, reporter, assignee
        self.created, self.parent, self.labels, self.description = created, parent, labels, description
        self.client, self.slot_kind = client, slot_kind
        self.status = "To Do"
        self.sprints: list[tuple[datetime, int]] = []
        self.updates: list[IssueUpdate] = []
        self.children: list[Issue] = []
        # work state (processes.work)
        self.effort_left = 0.0
        self.effort_total = 0.0
        self.needs_mr = False
        self.needs_qa = False
        self.mr: MergeRequest | None = None
        self.done_at: datetime | None = None
        self.started = False
        self.blocked_until = -1
        self.next_day = 0
        self.unplanned = False
        self.on_done: list[Any] = []

    @property
    def first_ts(self) -> datetime:
        return self.created

    def ref(self) -> str:
        return f"jira.{self.ext}"

    @property
    def key(self) -> str:
        return self.ext or f"?{self.uid}"

    @property
    def closed(self) -> bool:
        return self.status in ("Done", "Won't Do")


class MergeRequest(Obj):
    otype = "gitlab"

    def __init__(self, uid: int, issue: Issue, author: str, reviewer: str, created: datetime):
        super().__init__(uid)
        self.issue, self.author, self.reviewer, self.created = issue, author, reviewer, created
        self.updates: list[IssueUpdate] = []
        self.notes: list[tuple[datetime, str, str, str]] = []  # ts, author, body, slot
        self.state = "opened"
        self.merged_at: datetime | None = None
        self.merged_by: str | None = None
        self.review_day = -1
        self.changes_requested = False
        self.deployed_in: Record | None = None
        self.iid = 0
        self.entity = ""

    def ref(self) -> str:
        return self.entity

    @property
    def first_ts(self) -> datetime:
        return self.created


@dataclass
class Mail:
    uid: int
    sent: datetime
    sender: tuple[str, str]  # name, address
    to: list[tuple[str, str]]
    cc: list[tuple[str, str]]
    subject: str
    body: str
    process: str
    kind: str
    causes: tuple[Cause, ...]
    parent: Mail | None
    thread: int
    slot_kind: str
    order: int
    ext: str | None = None  # payload id msg-000123
    message_id: str | None = None
    otype: str = "mail"

    def ref(self) -> str:
        return f"mail.{self.ext}"

    @property
    def first_ts(self) -> datetime:
        return self.sent


@dataclass
class Page:
    uid: int
    ts: datetime
    author: str
    space: str
    title: str
    properties: list[tuple[str, str]]
    body: str
    labels: list[str]
    process: str
    kind: str
    causes: tuple[Cause, ...]
    order: int
    ext: str | None = None
    otype: str = "confluence"

    def ref(self) -> str:
        return f"confluence.page-{self.ext}"

    @property
    def first_ts(self) -> datetime:
        return self.ts


# ---------------------------------------------------------------------------------------------- actors
@dataclass
class PersonState:
    person: Person
    habits: PersonHabits
    start: date  # first working day (before the history for the initial staff)
    seat: str
    salary: float
    last_day: date | None = None
    notice: Record | None = None
    velocity: float = 0.0
    ramp_until: list[tuple[date, float]] = field(default_factory=list)
    loads: list[float] = field(default_factory=list)
    unmet: Record | None = None  # the declined salary review after a growth event
    growth_event: Record | None = None
    expectation: float = 0.0
    slow_until: date | None = None
    team: str = ""
    hire_slot: str | None = None
    salary_hist: list[tuple[date, float]] = field(default_factory=list)

    def salary_on(self, d: date) -> float:
        v = self.person.salary
        for since, s in self.salary_hist:
            if since <= d:
                v = s
        return v

    @property
    def id(self) -> str:
        return self.person.id

    def employed(self, d: date) -> bool:
        return self.start <= d and (self.last_day is None or d <= self.last_day)

    def speed(self, d: date) -> float:
        """Share of the normal pace on day ``d`` (ramp-up of a new hire, a rotation)."""
        f = 1.0
        for until, share in self.ramp_until:
            if d < until:
                f = min(f, share)
        if self.slow_until and d < self.slow_until:
            f = min(f, 0.85)
        return f


@dataclass
class ClientState:
    profile: ClientProfile
    habits: ClientHabits
    contract: Record | None = None
    status: str = "active"  # active | notice | churned | prospect
    start: date | None = None
    end: date | None = None  # exclusive: the first day without the service
    term_end: date | None = None
    acv: float = 0.0  # raw ACV in force (before the calibration)
    schedule: list[tuple[date, float]] = field(default_factory=list)  # (effective from, raw ACV)
    bumps: list[tuple[int, float, Record]] = field(default_factory=list)  # (day index, weight, incident)
    upsell_until: date | None = None
    upsell_deal: Record | None = None  # the deal that adds support load until upsell_until
    upsell_busy: bool = False
    late_spells: list[tuple[date, date, float, Record]] = field(default_factory=list)
    implementation: Issue | None = None
    promised: date | None = None
    overdue: bool = False
    churn_clock: FirstPassage | None = None
    clocks: dict[str, ArrivalClock] = field(default_factory=dict)
    new: bool = False
    won_deal: Record | None = None

    @property
    def id(self) -> str:
        return self.profile.id

    def active_on(self, d: date) -> bool:
        return self.start is not None and self.start <= d and (self.end is None or d < self.end)

    def acv_on(self, d: date) -> float:
        v = 0.0
        for since, acv in self.schedule:
            if since <= d:
                v = acv
        return v


# ---------------------------------------------------------------------------------------------- world
class World:
    """State of one run: spec, calendar, actors, objects and keyed streams."""

    def __init__(self, spec: CompanySpec, anchor: date, seed: int, calendar: BusinessCalendar, start: date):
        self.spec, self.anchor, self.seed, self.cal, self.start = spec, anchor, seed, calendar, start
        self.streams = Streams(seed, ("history",))
        self.days: list[date] = calendar.workdays(start, anchor)
        self.index = {d: i for i, d in enumerate(self.days)}
        self.end = datetime.combine(anchor, time(0, 0), tzinfo=calendar.tz)
        self._rngs: dict[str, np.random.Generator] = {}
        self._uid = 0
        self._order = 0
        self.records: dict[str, list[Record]] = {t: [] for t in SIM_TYPES}
        self.issues: list[Issue] = []
        self.mrs: list[MergeRequest] = []
        self.mails: list[Mail] = []
        self.pages: list[Page] = []
        self.people: dict[str, PersonState] = {}
        self.clients: dict[str, ClientState] = {}
        self.absences: dict[str, dict[date, str]] = {}
        self.datasets: dict[str, list[dict[str, Any]]] = {}
        self.team_load = spec.truth.team_load
        self.day = start
        self.day_i = 0
        self._threads = 0

    # ------------------------------------------------------------------ randomness
    def rng(self, process: str, entity: str, purpose: str) -> np.random.Generator:
        """The keyed stream ``history:<process>:<entity>:<purpose>`` (one generator per key)."""
        label = self.streams.label(process, entity, purpose)
        g = self._rngs.get(label)
        if g is None:
            g = self._rngs[label] = self.streams.rng(process, entity, purpose)
        return g

    def clock(self, process: str, entity: str, purpose: str) -> ArrivalClock:
        return ArrivalClock(self.rng(process, entity, purpose))

    # ------------------------------------------------------------------ counters
    def uid(self) -> int:
        self._uid += 1
        return self._uid

    def order(self) -> int:
        self._order += 1
        return self._order

    def thread(self) -> int:
        self._threads += 1
        return self._threads

    # ------------------------------------------------------------------ objects
    def record(
        self,
        otype: str,
        container: str,
        ts: datetime,
        process: str,
        kind: str,
        causes: tuple[Cause, ...] = (),
        *,
        prefix: str,
        ext: str | None = None,
        **state: Any,
    ) -> Record:
        """A new sim record. ``ext`` fixes its id; else ``render`` numbers ``prefix``-NNNN in time order."""
        if otype not in SIM_TYPES:
            raise ValueError(f"unknown sim object type {otype!r}")
        r = Record(self.uid(), otype, container, self, prefix)
        r.ext = ext
        r.update(ts, process, kind, causes, **state)
        self.records[otype].append(r)
        return r

    def mail(
        self,
        sent: datetime,
        sender: tuple[str, str],
        to: list[tuple[str, str]],
        subject: str,
        body: str,
        *,
        process: str,
        kind: str,
        causes: tuple[Cause, ...] = (),
        cc: list[tuple[str, str]] | None = None,
        parent: Mail | None = None,
        slot_kind: str = "mail",
    ) -> Mail:
        if parent is not None and sent <= parent.sent:
            sent = parent.sent + timedelta(minutes=7)
        m = Mail(
            self.uid(),
            sent,
            sender,
            to,
            cc or [],
            subject,
            body,
            process,
            kind,
            tuple(causes),
            parent,
            parent.thread if parent else self.thread(),
            slot_kind,
            self.order(),
        )
        self.mails.append(m)
        return m

    def page(
        self,
        ts: datetime,
        author: str,
        space: str,
        title: str,
        properties: list[tuple[str, str]],
        body: str,
        labels: list[str],
        *,
        process: str,
        kind: str,
        causes: tuple[Cause, ...] = (),
    ) -> Page:
        p = Page(
            self.uid(),
            ts,
            author,
            space,
            title,
            properties,
            body,
            labels,
            process,
            kind,
            tuple(causes),
            self.order(),
        )
        self.pages.append(p)
        return p

    def row(self, dataset: str, **values: Any) -> None:
        self.datasets.setdefault(dataset, []).append(values)

    # ------------------------------------------------------------------ people and time
    def person_name(self, pid: str) -> str:
        return self.people[pid].person.name

    def email(self, pid: str) -> str:
        return f"{self.people[pid].person.login}@{self.spec.company.domain}"

    def addr(self, pid: str) -> tuple[str, str]:
        return (self.person_name(pid), self.email(pid))

    def mailbox(self, box: str) -> tuple[str, str]:
        names = {"support": "Larchwood Support", "billing": "Larchwood Billing"}
        return (names.get(box, box.title()), f"{box}@{self.spec.company.domain}")

    def present(self, pid: str, d: date) -> bool:
        p = self.people.get(pid)
        return bool(p and p.employed(d) and d not in self.absences.get(pid, {}) and self.cal.is_workday(d))

    def employed_ids(self, d: date, roles: tuple[str, ...] | None = None) -> list[str]:
        return [
            pid
            for pid, p in self.people.items()
            if p.employed(d) and (roles is None or p.person.role in roles)
        ]

    def present_ids(self, d: date, roles: tuple[str, ...] | None = None) -> list[str]:
        return [pid for pid in self.employed_ids(d, roles) if self.present(pid, d)]

    def first_present(self, candidates: list[str], d: date) -> str | None:
        return next((c for c in candidates if self.present(c, d)), None)

    def next_present_day(self, pid: str, d: date, limit: int = 60) -> date | None:
        """``d`` or the next working day when ``pid`` is at work; None if not within ``limit`` days."""
        x = self.cal.roll_forward(d)
        for _ in range(limit):
            if self.present(pid, x):
                return x
            p = self.people[pid]
            if p.last_day and x > p.last_day:
                return None
            x = self.cal.add_business_days(x, 1)
        return None

    def stamp(
        self,
        pid: str,
        d: date,
        after: datetime | None = None,
        lo: float = 0.0,
        hi: float = 1.0,
        purpose: str = "clock",
    ) -> datetime:
        """A moment in the working hours of ``pid`` on day ``d``, later than ``after`` when given."""
        h = self.people[pid].habits
        span = 8.5 * 60
        r = self.rng("clock", pid, purpose)
        minute = h.start_min + span * (lo + (hi - lo) * float(r.random()))
        t = self.cal.at(d, time(int(minute // 60), int(minute % 60), int(r.integers(0, 60))))
        if after is not None and t <= after:
            t = after + timedelta(minutes=int(r.integers(2, 25)), seconds=int(r.integers(0, 60)))
        return t

    def at(
        self, d: date, hhmm: str, jitter_min: int = 0, purpose: tuple[str, str] = ("clock", "system")
    ) -> datetime:
        """A fixed local time on day ``d`` plus up to ``jitter_min`` minutes of noise."""
        t = self.cal.at(d, hhmm)
        if jitter_min:
            r = self.rng(purpose[0], purpose[1], "jitter")
            t += timedelta(minutes=int(r.integers(0, jitter_min)), seconds=int(r.integers(0, 60)))
        return t

    def bday(self, d: date, n: int) -> date:
        return self.cal.add_business_days(d, n)

    def day_of(self, d: date) -> int:
        """Index of the working day ``d`` (or of the next one)."""
        x = self.cal.roll_forward(d)
        return self.index.get(x, len(self.days) if x >= self.anchor else 0)
