"""The forward day loop: the company after T, one working day at a time, for one fork and scenario.

Start: ``StateAtT`` (who works there, the clients and their contracts, open invoices, cash at T).
Behaviour: ``Params`` (learned habits, each with a source tag). Decisions: ``Fork`` (dated levers).
Randomness: keyed streams ``s<k>:<process>:<entity>:<purpose>`` of the scenario k (no fork in the key,
so the forks of one scenario share their random numbers and differ only by their interventions).

Processes per working day, in a fixed order: fork actions, people (leave, sickness, resignations,
hiring), sales (deals), clients (activation, end of service, churn, upsell, downgrade, late payment,
renewal, escalated tickets), operations (deploys, incidents, GPU shortages, post-incident reviews),
NPS surveys, capacity and load, finance (invoices, payments, payroll, taxes, vendor and cloud bills,
bank), then the day metrics. At each month end a signal record explains a rise or a fall of the
incidents with declared causes; a leadership review follows each full month.

Output: ``Book`` (records with versions and causes), ``days`` (metrics per working day), ``lane``
(events for the case page), ``notable`` (facts of the texts that writers write), ``directory`` changes
(new hires, new prospects and their contacts).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from variator.orgsim.calendar import add_months
from variator.orgsim.company.forward.forks import Fork
from variator.orgsim.company.forward.params import SERVICES, SEVERITIES, Params, annual_to_daily
from variator.orgsim.company.forward.records import Book, Ids, Rec
from variator.orgsim.company.forward.state import StateAtT
from variator.orgsim.company.habits import NamePool, domain_of
from variator.orgsim.company.model import ArrivalClock, FirstPassage
from variator.orgsim.streams import Streams, scenario_part

ENGINEERS = ("backend", "frontend", "devops", "qa")
BASE_REASONS = ["an external offer", "relocation", "a career change", "personal reasons"]
CHURN_REASONS = [
    "budget cuts",
    "a move to an in-house team",
    "a lower price from a competitor",
    "a merger with another company",
    "a change of strategy",
]
OUTSIDE = ["third-party API outage", "traffic spike", "certificate expiry"]
SYMPTOMS = [
    "elevated error rate",
    "slow responses",
    "failed logins",
    "stuck background jobs",
    "timeouts on the API",
    "missing data in reports",
]
COMPONENTS = (
    "api",
    "web-app",
    "auth",
    "integrations",
    "billing-engine",
    "data-pipeline",
    "ml-inference",
    "infrastructure",
)
UPSELLS = [
    "advanced analytics",
    "an extra region",
    "the data export module",
    "premium support",
    "the integrations pack",
    "GPU inference capacity",
]
GPU_DEPENDENTS = {"ml-inference": 1.0, "api": 0.35, "web-app": 0.2}
SERVICE_LABEL = {
    "api": "the API",
    "web-app": "the web app",
    "auth": "login (auth)",
    "integrations": "the integrations",
    "billing-engine": "the billing engine",
    "data-pipeline": "the data pipeline",
    "ml-inference": "ML inference",
    "infrastructure": "the infrastructure",
}
SEV_WEIGHT = {"SEV1": 1.0, "SEV2": 0.5, "SEV3": 0.15}
TITLES = {
    "backend": "Backend Engineer",
    "frontend": "Frontend Engineer",
    "devops": "DevOps Engineer",
    "qa": "QA Engineer",
}
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


def boundary_after(d: date, billing: str) -> date:
    m = add_months(d.replace(day=1), 1)
    if billing == "quarterly":
        while m.month not in (1, 4, 7, 10):
            m = add_months(m, 1)
    return m


@dataclass
class Person:
    id: str
    name: str
    title: str
    role: str
    department: str
    manager: str | None
    seat: str
    salary: float
    executive: bool
    login: str
    start: date
    weight: float = 1.0  # throughput relative to the engineers' median
    last_day: date | None = None
    ramp: list[tuple[date, float]] = field(default_factory=list)
    absent: dict[date, str] = field(default_factory=dict)
    sick_rate: float = 2.0
    vacation_days: float = 20.0
    notice: str | None = None
    new: bool = False

    @property
    def engineer(self) -> bool:
        return self.role in ENGINEERS

    def employed(self, d: date) -> bool:
        return self.start <= d and (self.last_day is None or d <= self.last_day)

    def speed(self, d: date) -> float:
        f = 1.0
        for until, share in self.ramp:
            if d < until:
                f = min(f, share)
        return f


@dataclass
class Client:
    id: str
    name: str
    key: bool
    billing: str
    status: str  # active | notice | signed | churned
    acv: float
    schedule: list[tuple[date, float]]
    start: date | None
    end: date | None
    term_end: date | None
    term_months: int
    account_manager: str
    contract: Rec | None
    h: dict[str, float]
    bumps: list[tuple[date, float, str]] = field(default_factory=list)
    late_until: date | None = None
    late_extra: float = 0.0
    late_event: str | None = None
    nps: list[tuple[date, int, str]] = field(default_factory=list)  # (date, score, response id)
    support_mult_until: date | None = None
    new: bool = False

    def active_on(self, d: date) -> bool:
        return (
            self.start is not None
            and self.start <= d
            and (self.end is None or d < self.end)
            and self.status != "signed"
        )

    def acv_on(self, d: date) -> float:
        v = 0.0
        for since, a in self.schedule:
            if since <= d:
                v = a
        return v


class ForwardRun:
    def __init__(
        self,
        st: StateAtT,
        params: Params,
        fork: Fork,
        scenario: int,
        seed: int,
        months: int = 6,
        *,
        tenant: str = "company",
        path: list[dict[str, Any]] | None = None,
    ):
        self.st, self.P, self.fork, self.k, self.seed = st, params, fork, scenario, seed
        self.tenant = tenant
        self.path = path  # key events of a week-tree leaf (WP18): forced into their week
        self.path_log: list[dict[str, Any]] = []
        self.streams = Streams(seed, (scenario_part(scenario),))
        self._rngs: dict[str, np.random.Generator] = {}
        self.start = st.anchor
        self.end = add_months(st.anchor, months)
        self.months = months
        self.days = st.cal.workdays(self.start, self.end)
        self.book = Book()
        self.ids = Ids(st.id_top)
        self.lane: list[dict[str, Any]] = []
        self.series: list[dict[str, Any]] = []
        self.notable: list[dict[str, Any]] = []
        self.new_people: list[Person] = []
        self.new_orgs: list[dict[str, Any]] = []
        self.new_contacts: list[dict[str, Any]] = []
        self.left_people: dict[str, date] = {}
        self.cash = st.cash_at_t
        self.bank_items: list[tuple[datetime, float, str, str, str, tuple, str]] = []
        self.pending: list[tuple[date, int, str, Any]] = []
        self._pk = 0
        self.downtime: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.gpu: list[tuple[date, float, str]] = []  # (start, hit, life event id)
        self.loads: list[tuple[date, float]] = []
        self.unplanned: dict[date, float] = defaultdict(float)
        self.committed: list[tuple[date, date, float, str]] = []  # (from, to, FTE per day, why)
        self.incidents: list[Rec] = []
        self.deploys: list[Rec] = []
        self.month_stats: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
        self.names = NamePool(self.streams, set(st.names.values()))
        self.local_names: dict[str, str] = {}
        self.decision_id: str | None = None
        self.version = self._last_version()
        self._setup()

    # ================================================================================== basics
    def rng(self, process: str, entity: str, purpose: str) -> np.random.Generator:
        label = self.streams.label(process, entity, purpose)
        g = self._rngs.get(label)
        if g is None:
            g = self._rngs[label] = self.streams.rng(process, entity, purpose)
        return g

    def clock(self, process: str, entity: str, purpose: str) -> ArrivalClock:
        return ArrivalClock(self.rng(process, entity, purpose))

    def at(self, d: date, hhmm: str, jitter: int = 0, key: tuple[str, str] = ("clock", "system")) -> datetime:
        t = self.st.cal.at(d, hhmm)
        if jitter:
            r = self.rng(key[0], key[1], "jitter")
            t += timedelta(minutes=int(r.integers(0, jitter)), seconds=int(r.integers(0, 60)))
        return t

    def bday(self, d: date, n: int) -> date:
        return self.st.cal.add_business_days(d, n)

    def roll(self, d: date) -> date:
        return self.st.cal.roll_forward(d)

    def roll_back(self, d: date) -> date:
        return self.st.cal.roll_backward(d)

    def later(self, d: date, action: str, payload: Any) -> None:
        self._pk += 1
        self.pending.append((d, self._pk, action, payload))

    def contact(self, cid: str, kind: int = 0) -> dict[str, Any] | None:
        """Contact ``kind`` (0 main, 1 billing) of a client of the history or of a client won after T."""
        own = [c for c in self.new_contacts if c["org"] == cid]
        if own:
            return own[kind] if len(own) > kind else own[0]
        return self.st.contact(cid, kind)

    def name(self, x: str) -> str:
        if x in self.people:
            return self.people[x].name
        if x in self.clients:
            return self.clients[x].name
        return self.local_names.get(x) or self.st.names.get(x, x)

    def event(
        self,
        d: date,
        typ: str,
        text: str,
        entity: str | None = None,
        person: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        """One event of the lane: a plain text for people, the record it opens, the raw fields."""
        ev: dict[str, Any] = {"date": d.isoformat(), "type": typ, "text": text}
        if raw:
            ev["raw"] = raw
        if entity:
            ev["entityId"] = (
                entity
                if "." in entity.split("-")[0] or entity.startswith(("person.", "org."))
                else f"sim.{entity}"
            )
        if person:
            ev["person"] = person
            if person in self.people and self.people[person].new:  # a hire of the run: not in actors.json
                ev["personName"] = self.people[person].name
        self.lane.append(ev)

    def new(
        self,
        prefix: str,
        otype: str,
        container: str,
        ts: datetime,
        process: str,
        kind: str,
        causes: tuple = (),
        *,
        rid: str | None = None,
        width: int = 4,
        **state: Any,
    ) -> Rec:
        rid = rid or self.ids.next(prefix, width)
        return self.book.new(rid, otype, container, ts, process, kind, causes, **state)

    def upd(
        self, r: Rec, ts: datetime, process: str, kind: str, causes: tuple = (), drop: tuple = (), **ch: Any
    ) -> Rec:
        return self.book.update(r, ts, process, kind, causes, drop, **ch)

    def _last_version(self) -> list[int]:
        best = (4, 0, 0)
        for rid, ln in self.st.sim_last.items():
            if not rid.startswith("DEP-"):
                continue
            v = str(ln["payload"]["fields"].get("version", "v4.0.0"))[1:].split(".")
            try:
                t = tuple(int(x) for x in v)
            except ValueError:
                continue
            best = max(best, t)  # type: ignore[assignment]
        return list(best)

    # ================================================================================== setup
    def _setup(self) -> None:
        st, P = self.st, self.P
        self.people: dict[str, Person] = {}
        thr = [
            float(p.habits["throughput_points_per_sprint"]["point"])
            for p in st.people.values()
            if p.role in ENGINEERS and "throughput_points_per_sprint" in p.habits
        ]
        self.thr_median = float(np.median(thr)) if thr else 12.0
        for pid, p in sorted(st.people.items()):
            h = p.habits
            w = (
                float(h["throughput_points_per_sprint"]["point"]) / self.thr_median
                if "throughput_points_per_sprint" in h
                else 1.0
            )
            self.people[pid] = Person(
                pid,
                p.name,
                p.title,
                p.role,
                p.department,
                p.manager,
                p.seat,
                p.salary,
                p.executive,
                p.login,
                p.start,
                weight=w,
                sick_rate=float(h.get("sick_spells_per_year", {}).get("point", 1.9)),
                vacation_days=min(float(h.get("vacation_days_per_year", {}).get("point", 20.0)), 30.0),
            )
        for p in self.people.values():
            self._plan_leave(p, self.start)
        self.resign = {pid: FirstPassage(self.rng("people", pid, "resign")) for pid in self.people}
        self.sick = {pid: self.clock("people", pid, "sick") for pid in self.people}
        # clients
        self.clients: dict[str, Client] = {}
        for cid, c in sorted(st.clients.items()):
            f = c.contract_fields
            sched = [(date.fromisoformat(x["from"]), float(x["acv"])) for x in f.get("acv_schedule") or []]
            if not sched:
                sched = [(c.start or self.start, c.acv)]
            h = {k: float(v["point"]) for k, v in c.habits.items() if isinstance(v, dict) and "point" in v}
            cl = Client(
                cid,
                c.name,
                c.key,
                c.billing,
                c.status,
                c.acv,
                sched,
                c.start,
                c.end,
                date.fromisoformat(f["term_end"]) if f.get("term_end") else None,
                int(f.get("term_months") or 12),
                c.account_manager,
                None,
                h,
            )
            cl.contract = self._adopt(c.contract, st.snapshot_line(c.contract))
            self.clients[cid] = cl
        self.churn = {cid: FirstPassage(self.rng("clients", cid, "churn")) for cid in self.clients}
        self.cclocks = {
            (cid, k): self.clock("clients", cid, k)
            for cid in self.clients
            for k in ("upsell", "downgrade", "late", "tickets")
        }
        # open records of the history the run continues
        self.adopted: dict[str, Rec] = {}
        for oi in st.open_invoices:
            self.adopted[oi.id] = self._adopt(oi.id, st.snapshot_line(oi.id))
        for did in st.open_deals:
            self.adopted[did] = self._adopt(did, st.snapshot_line(did))
        if st.last_payroll:
            self.adopted[st.last_payroll] = self._adopt(st.last_payroll, st.snapshot_line(st.last_payroll))
        for v in st.vendors.values():
            if v.agreement in st.snapshot_ids():
                self.adopted[v.agreement] = self._adopt(v.agreement, st.snapshot_line(v.agreement))
        self.deal_clock = self.clock("sales", "pipeline", "arrivals")
        self.deploy_clock = self.clock("ops", "deploys", "arrivals")
        self.incident_clock = self.clock("ops", "incidents", "arrivals")
        self.deals: list[dict[str, Any]] = []
        for did in st.open_deals:
            f = st.fields(did)
            close = max(
                date.fromisoformat(f.get("expected_close") or self.start.isoformat()),
                self.bday(self.start, 3),
            )
            self.deals.append(
                {
                    "rec": self.adopted[did],
                    "close": self.roll(close),
                    "value": float(f["amount"]),
                    "client": f["client"],
                    "slot": did,
                    "new": f.get("deal_type") == "new",
                }
            )
        self.prospect_n = st.prospect_top
        self.person_n = st.person_top
        self.hire_slots = 0
        self.sprint_plan: dict[str, Any] = {}
        self.planned_load = P["team_load"]
        self.milestones: list[str] = []  # milestone records of the fork's levers
        self.prices_live = False  # the fork's new list prices are in the contract records
        self.monitoring: list[dict[str, Any]] = []  # rows of monitoring_daily, per working day and service
        self.last_load_signal: str | None = None
        self.forced: dict[date, list[dict[str, Any]]] = defaultdict(list)
        self.joins: dict[str, date] = {}  # person name -> the join day of the backfill (path)
        if self.path is not None:
            self._plan_path()

    # ================================================================================== path (WP18 leaf)
    def week_days(self, w: int) -> list[date]:
        a = self.start + timedelta(days=7 * (w - 1))
        return [x for x in self.days if a <= x < a + timedelta(days=7)]

    def _plan_path(self) -> None:
        """Place each key event of the path on a working day of its week (keyed draw per event)."""
        weeks = (self.end - self.start).days // 7
        for n, e in enumerate(self.path or []):
            w = int(e["week"])
            if w < 1 or w > weeks:
                continue
            days = self.week_days(w)
            if not days:
                continue
            if e["type"] == "people" and e["text"].startswith("A backfill for "):
                who = e["text"][len("A backfill for ") :].rsplit(" joins", 1)[0]
                self.joins[who] = days[0]
                continue
            r = self.rng("path", f"w{w}", f"e{n}")
            d = days[int(r.integers(len(days)))]
            self.forced[d].append(e)

    def _path_day(self, d: date) -> None:
        events = self.forced.get(d, [])
        hits = [e["entityId"][4:] for e in events if e["type"] == "client" and " hit by a SEV" in e["text"]]
        for e in events:
            typ, text, ent = e["type"], e["text"], e.get("entityId") or ""
            done = True
            if typ == "incident":
                self._incident(
                    d, e.get("severity"), e.get("hours"), hits if e.get("severity") == "SEV1" else None
                )
            elif typ == "client" and " gives notice" in text:
                c = self.clients.get(ent[4:])
                done = c is not None and c.status == "active"
                if done:
                    self._notice(c, d, *self._churn_parts(c, d))
            elif typ == "client" and " buys more" in text:
                c = self.clients.get(ent[4:])
                done = c is not None and c.status == "active"
                if done:
                    self._upsell(c, d, value=_k_amount(text))
            elif typ == "client" and " reduces its scope" in text:
                c = self.clients.get(ent[4:])
                done = c is not None and c.status == "active"
                if done:
                    self._downgrade(c, d)
            elif typ == "client" and text.startswith("Deal won"):
                self._deal_won(d, _k_amount(text), ent[4:] if ent.startswith("org.") else None)
            elif typ == "payment" and "pay late" in text:
                c = self.clients.get(ent[4:])
                done = c is not None and c.status == "active"
                if done:
                    self._late(c, d)
            elif typ == "people" and "hands in notice" in text:
                pid = ent.replace("person.", "").upper()
                p = self.people.get(pid)
                done = p is not None and p.employed(d) and p.notice is None
                if done:
                    self._resign(p, d)
            elif typ == "shock":
                self._shock(d, e)
            elif typ == "client" and " hit by a SEV" in text:
                done = True  # the SEV1 incident of the same day takes the client first
            else:
                done = False
            self.path_log.append(
                {"week": e["week"], "date": d.isoformat(), "type": typ, "text": text, "applied": done}
            )

    def _churn_parts(self, c: Client, d: date) -> tuple[float, float, float]:
        P = self.P
        base = min(0.95, c.h.get("churn_hazard", 0.17) * P["churn_scale"] + self.fork.churn_add(d))
        bump = sum(
            P["churn_incident_k"] * w * math.exp(-(d - t0).days / P["incident_churn_tau_days"])
            for t0, w, _ in c.bumps
        )
        nps_add = 0.0
        if c.nps and c.term_end and (c.term_end - d).days <= 180:
            nps_add = 0.01 * max(0.0, 30.0 - self._nps_signal(c))
        return base, bump, nps_add

    def _shock(self, d: date, e: dict[str, Any]) -> None:
        sid = e.get("id", "shock")
        size = _size(e["text"])
        if sid == "gpu_shortage_event" and self.st.cloud is not None:
            self._gpu_shortage(d, self.at(d, "09:00"), hit=abs(size) or None)
            return
        title = e["text"].split(":", 1)[0]
        ts = self.at(d, "08:00", 60, ("path", sid))
        le = self.new(
            "LE",
            "life_event",
            "company",
            ts,
            "market",
            "life_event_happened",
            (),
            title=title,
            statement=f"{e['text']}.",
            effective_date=d.isoformat(),
            event_type="market",
            polarity="negative",
            size=round(size, 4),
        )
        if sid == "fx_event":
            self.fx_from, self.fx_size, self.fx_event = d, abs(size), le.id
        pct = round(abs(size) * 100)
        text = {
            "competitor_event": f"A competitor cuts prices by about {pct}%",
            "macro_event": f"A market downturn lowers demand by about {pct}%",
            "fx_event": f"A currency move raises offshore costs by about {pct}%",
        }.get(sid, title)
        self.event(d, "shock", text, le.id, raw={"id": sid, "size": round(size, 4), "path_text": e["text"]})

    def _adopt(self, rid: str, ln: dict[str, Any]) -> Rec:
        """A history record the run continues from its snapshot line (later versions are new lines)."""
        p = ln["payload"]
        r = Rec(rid, ln["object_type"], ln["container"] or "finance")
        r.state = dict(p["fields"])
        r.causes = [(c["type"], c["id"]) for c in p["causes"]]
        r.adopted = True  # type: ignore[attr-defined]
        r.base_ts = datetime.fromisoformat(p["ts"])  # type: ignore[attr-defined]
        self.book.records[rid] = r
        return r

    def _plan_leave(self, p: Person, since: date) -> None:
        r = self.rng("people", p.id, "leave")
        shares = self.P["vacation_month_share"]
        m = since.replace(day=1)
        while m < self.end:
            nxt = add_months(m, 1)
            days = [d for d in self.days if m <= d < nxt and d >= since]
            if len(days) >= 6:
                n = int(r.poisson(p.vacation_days * shares[m.month]))
                n = min(n, 10, len(days) - 1)
                if n > 0:
                    k0 = int(r.integers(0, len(days) - n + 1))
                    block = days[k0 : k0 + n]
                    if not any(x in p.absent for x in block):
                        for x in block:
                            p.absent[x] = "vacation"
                        self.later(block[0], "leave_start", (p.id, block[0], block[-1]))
            m = nxt

    # ================================================================================== the loop
    def run(self) -> ForwardRun:
        self._fork_start()
        self._opening_finance()
        for i, d in enumerate(self.days):
            self.i, self.d = i, d
            due = sorted((x for x in self.pending if x[0] <= d), key=lambda x: (x[0], x[1]))
            self.pending = [x for x in self.pending if x[0] > d]
            for _, _, action, payload in due:
                getattr(self, "_" + action)(d, payload)
            if self.path is not None:
                self._path_day(d)
            self._people(d)
            self._sales(d)
            self._clients(d)
            self._ops(d)
            self._nps(d)
            self._load(d)
            self._finance_day(d)
            self._month_end(d)
            self._metrics(d)
        return self

    # ================================================================================== fork
    def _fork_start(self) -> None:
        fk, d0 = self.fork, self.days[0]
        ceo = "LW-E01"
        if fk.lever("lever_debt") > 0:
            d = self.roll(max(fk.dates["debt_start"], d0))
            share = fk.vars["debt_share"]
            pct = f"{round(share * 100)}%"
            ts = self.at(d, "09:40", 30, ("fork", "debt"))
            dec = self.new(
                "DEC",
                "decision",
                "company",
                ts,
                "decision",
                "decision_made",
                (),
                title=f"Use {pct} of engineering capacity for technical debt",
                statement=f"From {d:%d %B %Y} the delivery team spends {pct} of its capacity on technical "
                "debt: "
                f"the services with the most incidents first.",
                status="accepted",
                owner=ceo,
                actor=ceo,
                decided_at=ts.isoformat(),
                lever="lever_debt",
                effect_expected=fk.dates["debt_ready"].isoformat(),
            )
            self.decision_id = dec.id
            self.event(
                d, "decision", f"Decision: {pct} of engineering capacity goes to technical debt", dec.id, ceo
            )
            self._note("decision_rationale", dec, d, {"lever": "debt"})
            self.later(self.roll(max(fk.dates["debt_ready"], d0)), "milestone", ("debt_ready", dec.id))
        if fk.lever("lever_hire") > 0:
            d = self.roll(max(fk.dates["hire_start"], d0))
            n = int(round(fk.vars.get("hire_count", 5)))
            ts = self.at(d, "09:40", 30, ("fork", "hire"))
            dec = self.new(
                "DEC",
                "decision",
                "company",
                ts,
                "decision",
                "decision_made",
                (),
                title=f"Hire {n} engineers",
                statement=f"Open {n} engineering roles on {d:%d %B %Y} to add delivery capacity.",
                status="accepted",
                owner=ceo,
                actor=ceo,
                decided_at=ts.isoformat(),
                lever="lever_hire",
            )
            self.decision_id = dec.id
            self.event(d, "decision", f"Decision: hire {n} engineers", dec.id, ceo)
            self._note("decision_rationale", dec, d, {"lever": "hire", "count": n})
            roles = self.P["hire_roles"]
            for j in range(n):
                self._open_requisition(d, roles[j % len(roles)], ("decision", dec.id), None)
        if fk.lever("lever_gpu") > 0:
            d = self.roll(max(fk.dates["gpu_start"], d0))
            ts = self.at(d, "09:40", 30, ("fork", "gpu"))
            cost = fk.vars["gpu_reserve_cost"]
            dec = self.new(
                "DEC",
                "decision",
                "company",
                ts,
                "decision",
                "decision_made",
                (),
                title="Reserve GPU capacity on a second cloud",
                statement=f"Reserve GPU capacity on a second cloud for ${cost:,.0f} a year "
                f"from {d:%d %B %Y}.",
                status="accepted",
                owner=ceo,
                actor=ceo,
                decided_at=ts.isoformat(),
                lever="lever_gpu",
            )
            self.decision_id = dec.id
            self.event(d, "decision", "Decision: reserve GPU capacity on a second cloud", dec.id, ceo)
            self._note("decision_rationale", dec, d, {"lever": "gpu"})
            self.later(self.roll(max(fk.dates["gpu_ready"], d0)), "milestone", ("gpu_ready", dec.id))
        if fk.lever("price_change") != 0:
            d = self.roll(max(fk.dates["price_start"], d0))
            pc = fk.lever("price_change")
            ts = self.at(d, "09:40", 30, ("fork", "price"))
            dec = self.new(
                "DEC",
                "decision",
                "company",
                ts,
                "decision",
                "decision_made",
                (),
                title=f"Raise list prices by {round(pc * 100)}%",
                statement=f"Raise list prices by {round(pc * 100)}% from {fk.dates['price_live']:%d %B %Y}; "
                "clients get the notice now.",
                status="accepted",
                owner=ceo,
                actor=ceo,
                decided_at=ts.isoformat(),
                lever="price_change",
            )
            self.decision_id = dec.id
            self.event(d, "decision", f"Decision: raise list prices by {round(pc * 100)}%", dec.id, ceo)
            self._note("decision_rationale", dec, d, {"lever": "price"})
            self.later(self.roll(max(fk.dates["price_live"], d0)), "milestone", ("price_live", dec.id))

    def _milestone(self, d: date, payload: tuple[str, str]) -> None:
        what, dec = payload
        text = {
            "debt_ready": (
                "Debt work shows an effect",
                "The first debt fixes are in production: fewer incidents and "
                "lower latency are expected from now on.",
            ),
            "gpu_ready": (
                "GPU capacity on the second cloud ready",
                "The reserved GPU capacity on the second cloud "
                "is ready; a shortage at the main cloud now hits less.",
            ),
            "price_live": ("New prices live", "The new list prices apply to every invoice from now on."),
            "hires_ready": (
                "New engineers at full pace",
                "The engineers hired for the delivery team now work at full pace.",
            ),
        }[what]
        ts = self.at(d, "08:30", 20, ("fork", what))
        le = self.new(
            "LE",
            "life_event",
            "company",
            ts,
            "decision",
            "milestone_reached",
            (("decision", dec),),
            title=text[0],
            statement=text[1],
            effective_date=d.isoformat(),
            event_type="milestone",
            polarity="positive",
        )
        self.milestones.append(le.id)
        self.event(d, "milestone", text[0], le.id)
        if what == "price_live":
            self._prices_live(ts, le.id, dec)

    def _prices_live(self, ts: datetime, le: str, dec: str) -> None:
        """The new list prices in every client contract that has not ended: an amendment, price step."""
        self.prices_live = True
        live, pc = self.fork.dates["price_live"], self.fork.lever("price_change")
        for cid in sorted(self.clients):
            c = self.clients[cid]
            if c.contract is None or (c.end is not None and c.end <= live):
                continue
            self.upd(
                c.contract,
                ts + timedelta(minutes=5),
                "clients",
                "contract_amended_price",
                (("life_event", le), ("decision", dec)),
                amount=self._billed(c.acv),
                acv_schedule=self._acv_schedule(c),
                last_amendment=f"list prices {pc * 100:+.0f}% from {live.isoformat()}",
            )

    def _billed(self, acv: float) -> float:
        """An ACV of the price list in the contract records: with the fork's price change once it is live."""
        return round(acv * (1 + self.fork.lever("price_change")), 2) if self.prices_live else acv

    def _acv_schedule(self, c: Client) -> list[dict[str, Any]]:
        """The contract's ACV steps as billed (the run bills ``acv_on(d) x price_factor(d)``): once the new
        prices are live, the steps from that date carry the price change and a step at that date carries it
        for the ACV in force then."""
        steps = list(c.schedule)
        if self.prices_live:
            live = self.fork.dates["price_live"]
            before = [i for i, (s, _) in enumerate(steps) if s < live]
            if before and not any(s == live for s, _ in steps):
                steps.insert(before[-1] + 1, (live, c.acv_on(live)))
            steps = [(s, round(a * self.fork.price_factor(s), 2)) for s, a in steps]
        return [{"from": s.isoformat(), "acv": a} for s, a in steps]

    # ================================================================================== people
    def _people(self, d: date) -> None:
        P = self.P
        for pid in sorted(self.people):
            p = self.people[pid]
            if not p.employed(d):
                continue
            if d not in p.absent and self.sick[pid].tick(p.sick_rate / 252.0):
                n = 1 + int(
                    min(
                        self.rng("people", pid, "sick-len").geometric(1 / max(P["sick_days_per_spell"], 1.01))
                        - 1,
                        9,
                    )
                )
                block = [x for x in self.days if x >= d][:n]
                for x in block:
                    p.absent.setdefault(x, "sick")
                ts = self.at(d, "08:10", 50, ("people", pid))
                ee = self.new(
                    "EE",
                    "employee_event",
                    "people",
                    ts,
                    "people",
                    "sickness_reported",
                    (("rule", "RULE-SICK"),),
                    employee=pid,
                    type="sick_leave",
                    status="on_leave",
                    effective_date=d.isoformat(),
                    role=p.title,
                    department=p.department,
                    owner=p.manager or "LW-E02",
                )
                self.later(self.bday(block[-1], 1), "sick_end", (pid, ee.id, len(block)))
            if p.executive or p.notice or not self.resign.get(pid) or self.path is not None:
                continue
            a = P["resignation_hazard"] + (P["burnout_add"] if self._burnout(p) else 0.0)
            if self.resign[pid].tick(annual_to_daily(a)):
                self._resign(p, d)

    def _burnout(self, p: Person) -> bool:
        if not p.engineer or len(self.loads) < 10:
            return False
        recent = [x for _, x in self.loads[-20:]]
        return float(np.mean(recent)) > self.P["burnout_load"]

    def _leave_start(self, d: date, payload: tuple) -> None:
        pid, a, b = payload
        p = self.people[pid]
        if not p.employed(d):
            return
        ts = self.at(d, "08:00", 40, ("people", pid))
        ee = self.new(
            "EE",
            "employee_event",
            "people",
            ts,
            "people",
            "leave_started",
            (("rule", "RULE-LEAVE"),),
            employee=pid,
            type="vacation",
            status="on_leave",
            effective_date=a.isoformat(),
            end_date=b.isoformat(),
            role=p.title,
            department=p.department,
            owner=p.manager or "LW-E02",
        )
        self.later(self.bday(b, 1), "leave_end", (pid, ee.id, len([x for x in self.days if a <= x <= b])))

    def _leave_end(self, d: date, payload: tuple) -> None:
        pid, rid, n = payload
        r = self.book.records[rid]
        self.upd(
            r, self.at(d, "08:05", 30, ("people", pid)), "people", "leave_ended", status="completed", days=n
        )

    def _sick_end(self, d: date, payload: tuple) -> None:
        pid, rid, n = payload
        r = self.book.records[rid]
        self.upd(
            r,
            self.at(d, "08:20", 30, ("people", pid)),
            "people",
            "returned_to_work",
            status="completed",
            days=n,
            end_date=self.bday(d, -1).isoformat(),
        )

    def _resign(self, p: Person, d: date) -> None:
        r = self.rng("people", p.id, "reason")
        burnout = self._burnout(p)
        reason = (
            "burnout after months over capacity"
            if burnout
            else str(BASE_REASONS[int(r.integers(len(BASE_REASONS)))])
        )
        causes: tuple = (("rule", "RULE-BURNOUT"),) if burnout else ()
        ts = self.at(d, "07:10", 40, ("people", p.id))
        if burnout and self.last_load_signal:
            causes += (("life_event", self.last_load_signal),)
        if not burnout:  # the reason from outside the company is a declared cause of its own
            ext = self.new(
                "EXT", "life_event", "people", ts - timedelta(minutes=30), "people",
                "life_event_happened", (), title=f"{p.name}: {reason}",
                statement=f"Reason for {p.name} to leave: {reason}.", employee=p.id,
                effective_date=d.isoformat(), event_type="external", polarity="negative",
            )  # fmt: skip
            causes = (("life_event", ext.id),)
        le = self.new(
            "LE",
            "life_event",
            "people",
            ts,
            "people",
            "life_event_happened",
            causes,
            title=f"{p.name} decides to leave",
            statement=f"{p.name} decides to leave the company: {reason}.",
            employee=p.id,
            effective_date=d.isoformat(),
            event_type="resignation",
            reason="burnout" if burnout else "base",
            burnout=burnout,
            load_recent=round(float(np.mean([x for _, x in self.loads[-20:]])) if self.loads else 0.0, 3),
        )
        last = self.bday(d, 20)
        ee = self.new(
            "EE",
            "employee_event",
            "people",
            ts + timedelta(minutes=25),
            "people",
            "resignation_received",
            (("life_event", le.id), ("rule", "RULE-NOTICE")),
            employee=p.id,
            type="resignation",
            status="notice",
            effective_date=d.isoformat(),
            end_date=last.isoformat(),
            role=p.title,
            department=p.department,
            owner="LW-E02",
            actor=p.id,
        )
        p.notice, p.last_day = ee.id, last
        self.left_people[p.id] = last
        self.event(d, "people", f"{p.name} ({p.title}) resigns; last day {last:%d %b}", le.id, p.id)
        self._note("resignation", le, d, {"person": p.id, "reason": reason, "last_day": last})
        self.later(self.bday(last, 1), "exit", (p.id, ee.id))
        self.later(self.bday(d, 1), "backfill", (p.id, ee.id))

    def _exit(self, d: date, payload: tuple) -> None:
        pid, ee = payload
        p = self.people[pid]
        ts = self.at(d, "07:30", 30, ("people", pid))
        self.upd(
            self.book.records[ee],
            ts,
            "people",
            "employee_left",
            status="left",
            type="exit",
            effective_date=p.last_day.isoformat() if p.last_day else d.isoformat(),
        )
        self.event(d, "people", f"{p.name} leaves the company", ee, pid)

    def _backfill(self, d: date, payload: tuple) -> None:
        pid, ee = payload
        p = self.people[pid]
        ceo = "LW-E01"
        ts = self.at(d, "10:00", 90, ("people", f"backfill-{pid}"))
        title = p.title.replace("Senior ", "").replace("Lead ", "")
        dec = self.new(
            "DEC",
            "decision",
            "people",
            ts,
            "people",
            "decision_made",
            (("life_event", ee),),
            title=f"Backfill the {title} role",
            statement=f"Hire a {title} to replace {p.name}.",
            status="accepted",
            owner=ceo,
            actor=ceo,
            decided_at=ts.isoformat(),
        )
        self.event(d, "decision", f"Decision: backfill the {title} role", dec.id, ceo)
        self._open_requisition(d, p.role, ("decision", dec.id), p)

    def _open_requisition(self, d: date, role: str, cause: tuple[str, str], replaces: Person | None) -> None:
        self.hire_slots += 1
        slot = f"hire-slot-{self.hire_slots}"
        r = self.rng("people", slot, "lead")
        months = math.exp(self.P["hiring_lead_log_mu"] + self.P["hiring_lead_log_sd"] * r.normal())
        start = self.roll(d + timedelta(days=int(30.4 * months)))
        while start.weekday() != 0:
            start = self.roll(start + timedelta(days=1))
        if self.path is not None and replaces is not None:
            # the path says when the backfill joins; without a join in the window it starts after the window
            start = self.joins.get(replaces.name) or max(start, self.roll(self.end + timedelta(days=7)))
        title = (
            replaces.title.replace("Senior ", "").replace("Lead ", "")
            if replaces
            else TITLES.get(role, "Engineer")
        )
        dept = replaces.department if replaces else "Engineering"
        manager = (replaces.manager if replaces else None) or "LW-E03"
        salary = replaces.salary if replaces else self._role_salary(role)
        ts = self.at(d, "11:00", 60, ("people", slot))
        req = self.new(
            "REQ",
            "requisition",
            "people",
            ts,
            "people",
            "requisition_opened",
            (cause, ("process", "PROC-HIRING")),
            role=title,
            department=dept,
            status="open",
            owner="LW-E02",
            opened_at=d.isoformat(),
            target_start=start.isoformat(),
            hiring_manager=manager,
            salary_budget=round(salary * 1.05, -3),
            currency="USD",
            replaces=replaces.id if replaces else None,
        )
        for when, status in (
            (self.bday(d, 5), "sourcing"),
            (self.bday(d, 20), "interviewing"),
            (self.bday(start, -12), "offer_accepted"),
        ):
            if d < when < start:
                self.later(when, "req_status", (req.id, status, slot))
        self.later(start, "hire", (req.id, slot, role, title, dept, manager, salary, cause))

    def _role_salary(self, role: str) -> float:
        xs = sorted(p.salary for p in self.people.values() if p.role == role and not p.new)
        return float(np.median(xs)) if xs else 100_000.0

    def _req_status(self, d: date, payload: tuple) -> None:
        rid, status, slot = payload
        extra = {"candidate": self._candidate(slot)} if status == "offer_accepted" else {}
        self.upd(
            self.book.records[rid],
            self.at(d, "10:00", 240, ("people", slot)),
            "people",
            f"requisition_{status}",
            status=status,
            **extra,
        )

    def _candidate(self, slot: str) -> str:
        if not hasattr(self, "_cands"):
            self._cands: dict[str, str] = {}
        if slot not in self._cands:
            self._cands[slot] = self.names.person(f"{scenario_part(self.k)}-{slot}")
        return self._cands[slot]

    def _hire(self, d: date, payload: tuple) -> None:
        rid, slot, role, title, dept, manager, salary, cause = payload
        self.person_n += 1
        pid = f"LW-E{self.person_n:02d}"
        name = self._candidate(slot)
        first, last = name.lower().split(" ", 1)
        login = f"{first}.{last.replace(' ', '').replace(chr(39), '')}"
        sal = round(salary * float(np.exp(self.rng("people", slot, "salary").normal(0.02, 0.05))), -3)
        ramp = [(self.bday(d, n), s) for n, s in self.P["ramp"]]
        p = Person(
            pid,
            name,
            title,
            role,
            dept,
            manager,
            pid,
            sal,
            False,
            login,
            d,
            weight=1.0,
            ramp=ramp,
            sick_rate=1.9,
            vacation_days=20.0,
            new=True,
        )
        self.people[pid] = p
        self.new_people.append(p)
        self._plan_leave(p, self.bday(d, 5))
        self.resign[pid] = FirstPassage(self.rng("people", slot, "resign"))
        self.sick[pid] = self.clock("people", slot, "sick")
        ts = self.at(d, "08:45", 20, ("people", pid))
        self.upd(
            self.book.records[rid],
            ts,
            "people",
            "requisition_filled",
            status="filled",
            filled_at=d.isoformat(),
            employee=pid,
            candidate=name,
        )
        ee = self.new(
            "EE",
            "employee_event",
            "people",
            ts + timedelta(minutes=5),
            "people",
            "employee_joined",
            (("process", rid),),
            employee=pid,
            type="hire",
            status="active",
            effective_date=d.isoformat(),
            role=title,
            department=dept,
            salary=sal,
            currency="USD",
            owner="LW-E02",
        )
        self.event(d, "people", f"{name} joins as {title}", ee.id, pid)
        if cause[0] == "decision" and cause[1] == self.decision_id and self.fork.lever("lever_hire") > 0:
            self._note("hire", ee, d, {"person": pid})
            n = int(round(self.fork.vars.get("hire_count", 5)))
            lever_hires = [x for x in self.new_people if x.id in self._lever_hires(pid)]
            if len(lever_hires) == n:  # the last hire of the lever: the team is at full pace after its ramp
                self.later(ramp[-1][0], "milestone", ("hires_ready", self.decision_id))

    def _lever_hires(self, pid: str) -> set[str]:
        if not hasattr(self, "_lh"):
            self._lh: set[str] = set()
        self._lh.add(pid)
        return self._lh

    # ================================================================================== sales
    def _sales(self, d: date) -> None:
        n = self.deal_clock.tick(self.P["deals_per_month"] * 12 / 252.0)
        for _ in range(n if self.path is None else 0):
            self._new_deal(d)
        for deal in [x for x in self.deals if x["close"] <= d]:
            self.deals.remove(deal)
            self._close(deal, d)

    def _new_deal(self, d: date, value: float | None = None, created: date | None = None) -> None:
        P = self.P
        self.prospect_n += 1
        pid = f"PR-{self.prospect_n:03d}"
        r = self.rng("sales", pid, "deal")
        industry = [
            "logistics",
            "healthcare",
            "retail",
            "energy",
            "insurance",
            "media",
            "manufacturing",
            "education",
            "fintech",
            "travel",
            "public sector",
            "real estate",
        ][int(r.integers(12))]
        name = self.names.company(f"{scenario_part(self.k)}-{pid}", industry)
        drawn = round(P["deal_value_median"] * math.exp(P["deal_value_log_sd"] * r.normal()) / 1200) * 1200
        value = round(value / 1200) * 1200 if value else drawn
        cycle = math.exp(P["sales_cycle_log_mu"] + P["sales_cycle_log_sd"] * r.normal())
        if created is not None:  # a path's deal: it closes on day d
            cycle = (d - self.roll(created)).days
            d = self.roll(created)
        reps = sorted(x.id for x in self.people.values() if x.role == "sales" and x.employed(d)) or ["LW-E01"]
        owner = reps[int(r.integers(len(reps)))]
        dom = domain_of(name)
        org = {"id": pid, "name": name, "kind": "client", "domain": dom, "description": f"{industry} client"}
        self.new_orgs.append(org)
        for j, kind in enumerate(("main", "billing")):
            cn = self.names.person(f"{scenario_part(self.k)}-{pid}-{kind}")
            first, last = cn.split(" ", 1)
            email = f"{first[0].lower()}.{last.lower().replace(' ', '').replace(chr(39), '')}@{dom}"
            role = "Head of IT" if kind == "main" else "Finance Manager"
            self.new_contacts.append(
                {
                    "id": f"CNT-{pid}-{j + 1}",
                    "name": cn,
                    "role": f"{role}, {name}",
                    "org": pid,
                    "email": email,
                    "accounts": {"mail-company": email},
                }
            )
        self.local_names[pid] = name
        for c in self.new_contacts[-2:]:
            self.local_names[c["id"]] = c["name"]
        ts = self.at(d, "11:00", 300, ("sales", pid))
        close = self.roll(d + timedelta(days=int(cycle)))
        rec = self.new(
            "DEAL",
            "deal",
            "sales",
            ts,
            "sales",
            "deal_created",
            (("process", "PROC-SALES"),),
            title=f"New business: {name}",
            client=pid,
            owner=owner,
            actor=owner,
            amount=value,
            currency="USD",
            deal_type="new",
            status="lead",
            industry=industry,
            probability=0.25,
            expected_close=close.isoformat(),
            source="outbound",
        )
        q = self.bday(d, int(r.integers(5, 15)))
        if q < close:
            self.later(q, "deal_stage", (rec.id, "qualified"))
        pr = self.bday(close, -int(r.integers(5, 12)))
        if q < pr < close:
            self.later(pr, "deal_stage", (rec.id, "proposal"))
        self.deals.append(
            {"rec": rec, "close": close, "value": value, "client": pid, "slot": pid, "new": True}
        )

    def _deal_stage(self, d: date, payload: tuple) -> None:
        rid, stage = payload
        r = self.book.records[rid]
        if r.state.get("status") in ("won", "lost"):
            return
        self.upd(r, self.at(d, "14:00", 180, ("sales", rid)), "sales", f"deal_{stage}", status=stage)

    def _deal_won(self, d: date, value: float | None, prospect: str | None) -> None:
        """A path's won deal: an open deal of that prospect, or a new prospect and its deal."""
        deal = next((x for x in self.deals if x["client"] == prospect), None) if prospect else None
        if deal is None:
            self._new_deal(d, value=value, created=max(self.start, d - timedelta(days=45)))
            deal = self.deals[-1]
        if value:
            deal["value"] = round(value / 1200) * 1200
        self.deals.remove(deal)
        self._close(deal, d, won=True)

    def _close(self, deal: dict[str, Any], d: date, won: bool | None = None) -> None:
        r = deal["rec"]
        if not deal["new"]:
            return
        u = float(self.rng("sales", deal["slot"], "outcome").random())
        if won is None:
            won = u < self.P["win_rate"] * self.fork.win_factor(d) and self.path is None
        ts = self.at(d, "15:00", 120, ("sales", deal["slot"]))
        owner = r.state.get("owner") or "LW-E18"
        if not won:
            reason = ["timing", "price", "chose a competitor", "no budget"][
                int(self.rng("sales", deal["slot"], "lost").integers(4))
            ]
            self.upd(
                r,
                ts,
                "sales",
                "deal_lost",
                status="lost",
                closed_at=d.isoformat(),
                lost_reason=reason,
                probability=0.0,
                actor=owner,
            )
            return
        cid = deal["client"]
        start = boundary_after(self.bday(d, 8), "monthly")
        value = deal["value"]
        ctr_id = self.ids.next("CTR")
        term = 12 if float(self.rng("sales", deal["slot"], "term").random()) < 0.75 else 24
        cl = Client(
            cid,
            self.name(cid),
            False,
            "monthly",
            "signed",
            value,
            [(start, value)],
            start,
            None,
            add_months(start, term),
            term,
            owner,
            None,
            self._cohort_habits(),
            new=True,
        )
        ctr = self.new(
            "CTR",
            "contract",
            "sales",
            ts + timedelta(minutes=1),
            "sales",
            "contract_signed",
            (("process", r.id), ("process", "PROC-SALES")),
            rid=ctr_id,
            title=f"{cl.name}: platform services agreement",
            client=cid,
            status="signed",
            amount=self._billed(value),
            currency="USD",
            signed_at=d.isoformat(),
            start_date=start.isoformat(),
            term_end=cl.term_end.isoformat(),
            term_months=term,
            billing_frequency="monthly",
            payment_terms_days=30,
            notice_days=int(cl.h.get("notice_days", 60)),
            sla_target=0.995,
            owner=owner,
            acv_schedule=self._acv_schedule(cl),
            key_account=False,
            number=ctr_id,
            deal=r.id,
        )
        cl.contract = ctr
        self.clients[cid] = cl
        self.churn[cid] = FirstPassage(self.rng("clients", cid, "churn"))
        for k in ("upsell", "downgrade", "late", "tickets"):
            self.cclocks[(cid, k)] = self.clock("clients", cid, k)
        self.upd(
            r,
            ts,
            "sales",
            "deal_won",
            status="won",
            closed_at=d.isoformat(),
            probability=1.0,
            contract=ctr.id,
            actor=owner,
        )
        self.committed.append(
            (start, self.bday(start, 30), self.P["impl_fte_days_new"] / 30.0, f"onboarding {cid}")
        )
        self.event(d, "client", f"New client {cl.name} signs: +${value / 1000:,.0f}k of ARR", ctr.id,
                   raw={"client": cid, "acv": value})  # fmt: skip
        self.month_stats[d.strftime("%Y-%m")]["new_arr"] += value

    def _cohort_habits(self) -> dict[str, float]:
        co = self.st.cohorts.get("clients:standard", {}).get("params", {})
        h = {k: float(v["point"]) for k, v in co.items() if isinstance(v, dict) and "point" in v}
        base = self.st.habits["client_base"]
        h.setdefault("churn_hazard", float(base["churn_hazard_base"]["point"]))
        h.setdefault("pay_delay_days", float(base["pay_delay_days"]["point"]))
        h.setdefault("pay_delay_sd_days", float(base["pay_delay_sd_days"]["point"]))
        h.setdefault("tickets_per_month", 1.0)
        h.setdefault("upsell_rate", 0.13)
        h.setdefault("downgrade_rate", 0.012)
        h.setdefault("late_payment_rate", 0.14)
        h.setdefault("notice_days", 60.0)
        h.setdefault("nps_level", 30.0)
        return h

    # ================================================================================== clients
    def _clients(self, d: date) -> None:
        P, fk = self.P, self.fork
        tau = P["incident_churn_tau_days"]
        for cid in sorted(self.clients):
            c = self.clients[cid]
            if c.status == "signed" and c.start and d >= c.start:
                c.status = "active"
                self.upd(
                    c.contract,
                    self.at(d, "06:50", 20, ("clients", cid)),
                    "clients",
                    "contract_activated",
                    status="active",
                )
            if c.status == "notice" and c.end and d >= c.end:
                c.status = "churned"
                self.upd(
                    c.contract,
                    self.at(d, "06:55", 20, ("clients", cid)),
                    "clients",
                    "contract_terminated",
                    status="terminated",
                )
                self.event(d, "client", f"{c.name} leaves: the service ends", c.contract.id,
                           raw={"client": cid, "acv": c.acv})  # fmt: skip
                continue
            if c.status != "active":
                continue
            # renewal at the end of the term
            if c.term_end and d >= c.term_end:
                up = round(
                    P["renewal_uplift"]
                    * float(self.rng("clients", cid, f"uplift-{c.term_end}").uniform(0.5, 1.5)),
                    4,
                )
                new_acv = round(c.acv * (1 + up) / 1200) * 1200
                c.schedule.append((c.term_end, new_acv))
                c.acv = new_acv
                c.term_end = add_months(c.term_end, c.term_months)
                self.upd(
                    c.contract,
                    self.at(d, "07:00", 30, ("clients", cid)),
                    "clients",
                    "contract_renewed",
                    (("process", "PROC-RENEWAL"),),
                    amount=self._billed(new_acv),
                    term_end=c.term_end.isoformat(),
                    renewed_at=d.isoformat(),
                    uplift=up,
                    acv_schedule=self._acv_schedule(c),
                )
            # churn: the client's hazard, incident bumps that decay, NPS in the renewal window, price change
            base = min(0.95, c.h.get("churn_hazard", 0.17) * P["churn_scale"] + fk.churn_add(d))
            bump = sum(P["churn_incident_k"] * w * math.exp(-(d - t0).days / tau) for t0, w, _ in c.bumps)
            nps_add = 0.0
            if c.nps and c.term_end and (c.term_end - d).days <= 180:
                sig = self._nps_signal(c)
                nps_add = 0.01 * max(0.0, 30.0 - sig)
            h = annual_to_daily(min(0.95, base * (1 + bump) + nps_add))
            own = self.path is None  # with a path, these life events come from the path
            if self.churn[cid].tick(h) and own:
                self._notice(c, d, base, bump, nps_add)
                continue
            if self.cclocks[(cid, "upsell")].tick(c.h.get("upsell_rate", 0.13) / 252.0) and own:
                self._upsell(c, d)
            if self.cclocks[(cid, "downgrade")].tick(c.h.get("downgrade_rate", 0.012) / 252.0) and own:
                self._downgrade(c, d)
            if (
                (c.late_until is None or d > c.late_until)
                and self.cclocks[(cid, "late")].tick(c.h.get("late_payment_rate", 0.14) / 252.0)
                and own
            ):
                self._late(c, d)
            mult = P["support_mult_after_deal"] if c.support_mult_until and d <= c.support_mult_until else 1.0
            n = self.cclocks[(cid, "tickets")].tick(c.h.get("tickets_per_month", 1.0) * 12 / 252.0 * mult)
            self.month_stats[d.strftime("%Y-%m")]["tickets"] += n
            for j in range(n):
                if float(self.rng("support", cid, f"esc-{d}-{j}").random()) < P["support_escalation_share"]:
                    self._escalation(c, d, j)

    def _nps_signal(self, c: Client) -> float:
        """The client's NPS signal: its responses shrunk to the company level (prior weight 3)."""
        prior, w = self.P["nps_level_typical"], 3.0
        pts = [100.0 if s >= 9 else (-100.0 if s <= 6 else 0.0) for _, s, _ in c.nps]
        return (prior * w + sum(pts)) / (w + len(pts))

    def _notice(self, c: Client, d: date, base: float, bump: float, nps_add: float) -> None:
        r = self.rng("clients", c.id, "reason")
        causes: list[tuple[str, str]] = []
        parts = {"base": base, "incidents": base * bump, "nps": nps_add}
        if self.fork.churn_add(d) > 0:
            parts["price"] = self.fork.churn_add(d)
        total = sum(parts.values())
        u, acc, why = float(r.random()) * total, 0.0, "base"
        for k, x in parts.items():
            acc += x
            if u <= acc:
                why = k
                break
        recent = sorted({i for t0, w, i in c.bumps if (d - t0).days <= 120})
        if why == "incidents" and recent:
            reason = "repeated service incidents"
        elif why == "nps":
            reason = "low satisfaction with the service"
        elif why == "price":
            reason = "the price increase"
        else:
            reason = str(CHURN_REASONS[int(r.integers(len(CHURN_REASONS)))])
        causes += [("process", i) for i in recent]
        detractor = [rid for _, s, rid in c.nps if s <= 6]
        if detractor:
            causes.append(("process", detractor[-1]))
        if why == "nps" and not detractor and c.nps:
            causes.append(("process", c.nps[-1][2]))
        if why == "price" and self.decision_id:
            causes.append(("decision", self.decision_id))
        notice_days = int(c.h.get("notice_days", 60))
        end = boundary_after(d + timedelta(days=notice_days), c.billing)
        ts = self.at(d, "10:30", 300, ("clients", c.id))
        if why == "base" or (why == "incidents" and not recent):
            # the client's own reason (budget cuts, a merger...) is a declared cause of its own
            ext = self.new(
                "EXT", "life_event", "sales", ts - timedelta(minutes=50), "clients",
                "life_event_happened", (), title=f"{c.name}: {reason}",
                statement=f"Reason given by {c.name}: {reason}.", client=c.id,
                effective_date=d.isoformat(), event_type="external", polarity="negative",
            )  # fmt: skip
            causes.append(("life_event", ext.id))
        le = self.new(
            "LE",
            "life_event",
            "sales",
            ts - timedelta(minutes=30),
            "clients",
            "life_event_happened",
            tuple(causes),
            title=f"{c.name} gives notice",
            statement=f"{c.name} decides to end the contract: {reason}.",
            client=c.id,
            effective_date=d.isoformat(),
            event_type="churn",
            polarity="negative",
            reason=why,
            incidents_recent=len(recent),
        )
        c.status, c.end = "notice", end
        self.upd(
            c.contract,
            ts,
            "clients",
            "notice_given",
            (("life_event", le.id), ("rule", "RULE-CLIENT-NOTICE")),
            status="notice",
            notice_at=d.isoformat(),
            end_date=end.isoformat(),
        )
        self.event(
            d, "client", f"{c.name} gives notice: ${c.acv / 1000:,.0f}k of ARR leaves ({reason})", le.id,
            raw={"client": c.id, "acv": c.acv, "reason": reason, "service_ends": end.isoformat()},
        )  # fmt: skip
        self.month_stats[d.strftime("%Y-%m")]["notices"] += 1
        self.month_stats[d.strftime("%Y-%m")]["churned_arr"] += c.acv
        self._note(
            "client_notice",
            le,
            d,
            {
                "client": c.id,
                "reason": reason,
                "end": end,
                "acv": c.acv,
                "incidents": recent,
                "detractor": detractor[-1:] if detractor else [],
            },
        )

    def _upsell(self, c: Client, d: date, value: float | None = None) -> None:
        """An upsell request; with ``value`` (a path) the deal is won in the same week at that value."""
        P = self.P
        r = self.rng("clients", c.id, f"upsell-{d}")
        what = UPSELLS[int(r.integers(len(UPSELLS)))]
        share = round(P["upsell_size_share"] * float(r.uniform(0.6, 1.4)), 3)
        if value:
            share = round(value / max(c.acv, 1.0), 3)
        ts = self.at(d, "11:30", 240, ("clients", c.id))
        le = self.new(
            "LE",
            "life_event",
            "sales",
            ts,
            "clients",
            "life_event_happened",
            (),
            title=f"{c.name} asks for {what}",
            statement=f"{c.name} asks for {what}.",
            client=c.id,
            effective_date=d.isoformat(),
            event_type="upsell_request",
            polarity="positive",
            size_share=share,
        )
        value = round((value or c.acv * share) / 1200) * 1200
        deal = self.new(
            "DEAL",
            "deal",
            "sales",
            ts + timedelta(minutes=40),
            "sales",
            "deal_qualified",
            (("life_event", le.id), ("process", "PROC-SALES")),
            title=f"Upsell: {what} for {c.name}",
            client=c.id,
            owner=c.account_manager,
            amount=value,
            currency="USD",
            deal_type="upsell",
            status="qualified",
            probability=0.85,
            contract=c.contract.id,
        )
        close = self.bday(d, int(r.integers(10, 30)))
        forced = self.path is not None
        if forced:
            close = max([x for x in self.days if x >= d and (x - d).days < 5][-1:] or [d])
        self.later(close, "upsell_close", (c.id, deal.id, le.id, value, what, forced))

    def _upsell_close(self, d: date, payload: tuple) -> None:
        cid, did, le, value, what, forced = payload
        c = self.clients[cid]
        deal = self.book.records[did]
        ts = self.at(d, "15:30", 90, ("clients", cid))
        lost = float(self.rng("clients", cid, f"upsell-win-{did}").random()) >= self.P["upsell_win_share"]
        if c.status != "active" or (lost and not forced):
            self.upd(deal, ts, "sales", "deal_lost", status="lost", closed_at=d.isoformat(), probability=0.0)
            return
        start = boundary_after(d, "monthly")
        c.acv = c.acv + value
        c.schedule.append((start, c.acv))
        c.support_mult_until = self.bday(start, 60)
        self.upd(deal, ts, "sales", "deal_won", status="won", closed_at=d.isoformat(), probability=1.0)
        self.upd(
            c.contract,
            ts + timedelta(minutes=2),
            "clients",
            "contract_amended_upsell",
            (("life_event", le), ("process", did)),
            amount=self._billed(c.acv),
            acv_schedule=self._acv_schedule(c),
            last_amendment=f"upsell: {what} from {start.isoformat()}",
            deal=did,
        )
        self.committed.append(
            (start, self.bday(start, 20), self.P["impl_fte_days_upsell"] / 20.0, f"upsell {cid}")
        )
        self.event(d, "client", f"{c.name} buys {what}: +${value / 1000:,.0f}k of ARR", did,
                   raw={"client": cid, "acv_added": value})  # fmt: skip
        self.month_stats[d.strftime("%Y-%m")]["new_arr"] += value

    def _downgrade(self, c: Client, d: date) -> None:
        r = self.rng("clients", c.id, f"downgrade-{d}")
        share = round(self.P["downgrade_size_share"] * float(r.uniform(0.6, 1.4)), 3)
        start = boundary_after(d, "monthly")
        ts = self.at(d, "12:00", 240, ("clients", c.id))
        le = self.new(
            "LE",
            "life_event",
            "sales",
            ts,
            "clients",
            "life_event_happened",
            (),
            title=f"{c.name} reduces its contract",
            statement=f"{c.name} asks to use fewer seats and modules.",
            client=c.id,
            effective_date=d.isoformat(),
            event_type="downgrade",
            polarity="negative",
            size_share=share,
        )
        c.acv = round(c.acv * (1 - share) / 1200) * 1200
        c.schedule.append((start, c.acv))
        self.upd(
            c.contract,
            ts + timedelta(minutes=30),
            "clients",
            "contract_amended_downgrade",
            (("life_event", le.id),),
            amount=self._billed(c.acv),
            acv_schedule=self._acv_schedule(c),
            last_amendment=f"downgrade from {start.isoformat()}",
        )
        self.event(d, "client", f"{c.name} reduces its contract by {round(share * 100)}%", le.id)

    def _late(self, c: Client, d: date) -> None:
        P = self.P
        r = self.rng("clients", c.id, f"late-{d}")
        days = int(P["late_spell_days"] * float(r.uniform(0.7, 1.3)))
        extra = float(P["late_extra_days"] * float(r.uniform(0.6, 1.3)))
        ts = self.at(d, "09:00", 300, ("clients", c.id))
        le = self.new(
            "LE",
            "life_event",
            "finance",
            ts,
            "clients",
            "life_event_happened",
            (),
            title=f"{c.name} pays late",
            statement=f"{c.name} has a cash squeeze and pays its invoices late.",
            client=c.id,
            effective_date=d.isoformat(),
            event_type="late_payment",
            polarity="negative",
            spell_days=days,
            extra_days=round(extra),
        )
        c.late_until, c.late_extra, c.late_event = d + timedelta(days=days), extra, le.id
        self.event(d, "client", f"{c.name} starts to pay late", le.id)

    def _escalation(self, c: Client, d: date, j: int) -> None:
        r = self.rng("support", c.id, f"ticket-{d}-{j}")
        comp = COMPONENTS[int(r.integers(len(COMPONENTS)))]
        what = [
            "Page does not load",
            "Export fails",
            "Login error",
            "Wrong totals in a report",
            "Slow search",
        ][int(r.integers(5))]
        opened = self.at(d, "09:00", 480, ("support", c.id))
        agents = sorted(x.id for x in self.people.values() if x.role == "support" and x.employed(d)) or [
            "LW-E03"
        ]
        who = agents[int(r.integers(len(agents)))]
        contact = self.contact(c.id, 0)
        rec = self.new(
            "SUP",
            "support_ticket",
            "support",
            opened,
            "support",
            "ticket_escalated",
            (("process", "PROC-SUPPORT"),),
            title=f"[{c.name}] {what}",
            client=c.id,
            contract=c.contract.id,
            status="escalated",
            priority="P2",
            category="bug",
            channel="email",
            component=comp,
            opened_at=opened.isoformat(),
            assignee=who,
            reporter=contact["id"] if contact else None,
        )
        hours = self.P["support_resolve_hours"] * float(r.lognormal(0.0, 0.5))
        done = opened + timedelta(hours=hours)
        self.unplanned[d] += 0.5
        self.later(self.roll(done.date()), "ticket_resolved", (rec.id, done))

    def _ticket_resolved(self, d: date, payload: tuple) -> None:
        rid, done = payload
        r = self.book.records[rid]
        ts = max(done, self.at(d, "09:00"))
        self.upd(
            r,
            ts,
            "support",
            "ticket_resolved",
            status="resolved",
            resolved_at=ts.isoformat(),
            csat=int(
                min(5, max(1, round(self.P["csat"] + float(self.rng("support", rid, "csat").normal(0, 0.7)))))
            ),
        )

    # ================================================================================== operations
    def _ops(self, d: date) -> None:
        P, fk = self.P, self.fork
        n = self.deploy_clock.tick(P["deploys_per_week"] / 5.0)
        if n:
            times = sorted(float(x) for x in self.rng("ops", "deploys", f"times-{d}").uniform(0.15, 0.95, n))
            for x in times:
                self._deploy(d, x)
        recent = [x for _, x in self.loads[-5:]] or [P["team_load_now"]]
        load_f = max(0.5, float(np.mean(recent)) / P["team_load"])
        rate = P["incidents_per_month"] * 12 / 252.0 * fk.incident_factor(d) * load_f
        n = self.incident_clock.tick(rate)
        for _ in range(n if self.path is None else 0):
            self._incident(d)

    def _oncall(self, d: date) -> str:
        devops = sorted(x.id for x in self.people.values() if x.role == "devops" and x.employed(d))
        week = d.isocalendar()[1]
        order = devops[week % len(devops) :] + devops[: week % len(devops)] if devops else []
        for pid in order:
            if d not in self.people[pid].absent:
                return pid
        return "LW-E03"

    def _deploy(self, d: date, frac: float) -> None:
        who = self._oncall(d)
        ts = self.at(d, "08:00") + timedelta(minutes=int(frac * 600))
        self.version[2] += 1
        if self.version[2] > 9:
            self.version[1], self.version[2] = self.version[1] + 1, 0
        v = "v" + ".".join(str(x) for x in self.version)
        comp = COMPONENTS[int(self.rng("ops", "deploys", f"comp-{v}").integers(len(COMPONENTS)))]
        rec = self.new(
            "DEP",
            "deploy",
            "engineering",
            ts,
            "operations",
            "deployed",
            (("process", "PROC-DEPLOY"),),
            title=f"Deploy {v}",
            component=comp,
            version=v,
            environment="production",
            status="succeeded",
            author=who,
            actor=who,
            deployed_at=ts.isoformat(),
            changes=int(self.rng("ops", "deploys", f"changes-{v}").integers(1, 9)),
        )
        self.deploys.append(rec)

    def _incident(
        self, d: date, sev: str | None = None, restore_h: float | None = None, prefer: list[str] | None = None
    ) -> None:
        """An incident on day ``d``; a path gives its severity, restore hours and clients hit (``prefer``)."""
        P = self.P
        n = len(self.incidents) + 1
        r = self.rng("ops", "incidents", f"n{n}")
        start = self.at(d, "07:00") + timedelta(minutes=int(r.integers(0, 14 * 60)))
        drawn = str(r.choice(SEVERITIES, p=P["severity_mix"]))
        sev = sev or drawn
        u = float(r.random())
        recent = [x for x in self.deploys if x.first_ts < start and (start - x.first_ts) < timedelta(days=3)]
        causes: list[tuple[str, str]] = []
        related: list[str] = []
        cloud = self.st.cloud
        if u < P["incident_deploy_share"] and recent:
            dep = recent[-1]
            kind = "deploy"
            comp = dep.state["component"]
            causes.append(("process", dep.id))
            related.append(dep.id)
        elif u < P["incident_deploy_share"] + P["incident_vendor_share"] and cloud is not None:
            kind = "cloud provider capacity"
            comp = "ml-inference"
            le = self._gpu_shortage(d, start)
            causes.append(("life_event", le))
        else:
            kind = OUTSIDE[int(r.integers(len(OUTSIDE)))]
            comp = COMPONENTS[int(r.integers(len(COMPONENTS)))]
            le = self.new(
                "LE",
                "life_event",
                "engineering",
                start - timedelta(minutes=2),
                "operations",
                "life_event_happened",
                (),
                title=f"{kind.capitalize()} affects {comp}",
                statement=f"A {kind} affects the {comp} service.",
                effective_date=d.isoformat(),
                event_type="outside",
                polarity="negative",
            )
            causes.append(("life_event", le.id))
            self.event(d, "shock", f"A {kind} hits {SERVICE_LABEL.get(comp, comp)}", le.id,
                       raw={"cause": kind, "component": comp})  # fmt: skip
        active = sorted((c for c in self.clients.values() if c.active_on(d)), key=lambda c: c.id)
        k = max(1, int(r.poisson(P["affected_clients"][sev])))
        k = min(k, len(active))
        wts = np.array([math.sqrt(max(c.acv, 1.0)) for c in active])
        idx = r.choice(len(active), size=k, replace=False, p=wts / wts.sum()) if active else []
        hit = [active[int(j)] for j in idx]
        for cid in prefer or []:
            c = self.clients.get(cid)
            if c is not None and c.active_on(d) and c not in hit:
                hit = [c] + hit[: max(len(hit) - 1, 0)]
        hit = sorted(hit, key=lambda c: -c.acv)
        noise = math.exp(r.normal(0, P["restore_log_sd"]) - P["restore_log_sd"] ** 2 / 2)
        restore = max(10.0, 60 * restore_h if restore_h is not None else 60 * P["restore_hours"][sev] * noise)
        down = restore * P["downtime_share"][sev]
        symptom = SYMPTOMS[int(r.integers(len(SYMPTOMS)))]
        detected = start + timedelta(minutes=int(r.integers(3, 16)))
        resolved = start + timedelta(minutes=int(restore))
        responder = self._oncall(d)
        services = [comp] if comp in SERVICES else ["api"]
        rec = self.new(
            "INC",
            "incident",
            "engineering",
            detected,
            "operations",
            "incident_opened",
            tuple(causes),
            title=f"{comp}: {symptom}",
            severity=sev,
            status="open",
            component=comp,
            services=services,
            started_at=start.isoformat(),
            detected_at=detected.isoformat(),
            client=hit[0].id if hit else None,
            affected_clients=[c.id for c in hit],
            affected_count=len(hit),
            cause_kind=kind,
            owner="LW-E03",
            assignee=responder,
            project="platform",
            related=related or None,
        )
        self.upd(
            rec,
            resolved,
            "operations",
            "incident_resolved",
            status="resolved",
            resolved_at=resolved.isoformat(),
            restore_minutes=int(restore),
            downtime_minutes=int(down),
        )
        self.incidents.append(rec)
        for s in services:
            self.downtime[d][s] += down
        for c in hit:
            c.bumps.append((d, SEV_WEIGHT[sev], rec.id))
        self.unplanned[d] += P["incident_fte_days"][sev] / 2
        self.unplanned[self.bday(d, 1)] += P["incident_fte_days"][sev] / 2
        ms = self.month_stats[d.strftime("%Y-%m")]
        ms["incidents"] += 1
        ms[f"cause:{kind if kind in ('deploy', 'cloud provider capacity') else 'outside'}"] += 1
        ms[sev] += 1
        on_call = self.people[responder].name if responder in self.people else responder
        self.event(
            d,
            "incident",
            f"{sev} incident on {SERVICE_LABEL.get(comp, comp)}, restored in {restore / 60:.1f} h "
            f"(on call: {on_call})",
            rec.id,
            responder,
            raw={"severity": sev, "component": comp, "symptom": symptom, "clients": len(hit),
                 "restore_minutes": int(restore), "cause": kind},
        )  # fmt: skip
        if kind == "deploy" and sev == "SEV1":
            dep = self.book.records[related[0]]
            self.upd(
                dep,
                start + timedelta(minutes=20),
                "operations",
                "deploy_rolled_back",
                (("process", rec.id),),
                status="rolled_back",
            )
        if sev in ("SEV1", "SEV2"):
            day = self.bday(d, int(r.integers(2, 5)))
            self.later(day, "postmortem", (rec.id,))

    def _gpu_shortage(self, d: date, start: datetime, hit: float | None = None) -> str:
        """The active GPU shortage at the cloud vendor, or a new one (a path gives its size)."""
        for t0, _, le in reversed(self.gpu):
            if (d - t0).days <= self.P["gpu_tau_days"]:
                return le
        cloud = self.st.cloud
        hit = hit or self.P["gpu_hit"]
        le = self.new(
            "LE",
            "life_event",
            "engineering",
            start - timedelta(minutes=1),
            "operations",
            "life_event_happened",
            (),
            title=f"{cloud.name} capacity shortage",
            statement=f"{cloud.name} runs short of GPU capacity in the region of the platform.",
            vendor=cloud.id,
            effective_date=d.isoformat(),
            event_type="vendor_capacity",
            polarity="negative",
        )
        self.gpu.append((d, hit, le.id))
        self.event(d, "shock", f"GPU capacity shortage at {cloud.name}", le.id)
        return le.id

    def _postmortem(self, d: date, payload: tuple) -> None:
        (iid,) = payload
        inc = self.book.records[iid]
        head = "LW-E03"
        ts = self.at(d, "14:00", 120, ("ops", f"pir-{iid}"))
        after = ""
        dep = next((c[1] for c in inc.causes if c[1].startswith("DEP-")), None)
        if dep:
            after = f" It started after deploy {self.book.records[dep].state['version']}."
        pir = self.new(
            "PIR",
            "decision",
            "engineering",
            ts,
            "operations",
            "review_published",
            (("process", iid), ("process", "PROC-INCIDENT")),
            title=f"Post-incident review: {iid} {inc.state['title']}",
            statement=f"Review of {iid} ({inc.state['severity']}): {inc.state['title']}; restored in "
            f"{inc.state['restore_minutes']} minutes; {inc.state['affected_count']} clients affected.{after}",
            status="accepted",
            owner=head,
            actor=head,
            incident=iid,
            decided_at=ts.isoformat(),
        )
        self.upd(
            inc,
            ts + timedelta(minutes=5),
            "operations",
            "incident_followed_up",
            related=list(inc.state.get("related") or []) + [pir.id],
        )
        self._note("postmortem", pir, d, {"incident": iid})

    # ================================================================================== NPS
    def _nps(self, d: date) -> None:
        if d.month not in (1, 4, 7, 10):
            return
        first_tue = d.replace(day=1)
        while first_tue.weekday() != 1:
            first_tue += timedelta(days=1)
        survey_day = self.roll(first_tue + timedelta(days=7))
        if d != survey_day:
            return
        survey = f"{d.year}-Q{(d.month - 1) // 3 + 1}"
        P = self.P
        scores = []
        for cid in sorted(self.clients):
            c = self.clients[cid]
            if not c.active_on(d):
                continue
            gap = sum(
                SEV_WEIGHT[self.book.records[i].state["severity"]]
                for t0, _, i in c.bumps
                if (d - t0).days <= 91
            )
            level = c.h.get("nps_level", P["nps_level_typical"]) - P["nps_sla_points"] * gap
            mu = _mu_for_nps(level, P["nps_score_sd_latent"])
            for j, kind in enumerate(("main", "billing")):
                r = self.rng("nps", cid, f"{survey}-{kind}")
                if float(r.random()) >= P["nps_response_rate"]:
                    continue
                score = int(
                    min(
                        10,
                        max(
                            0,
                            round(
                                mu
                                + (-0.1 if kind == "billing" else 0.0)
                                + P["nps_score_sd_latent"] * r.normal()
                            ),
                        ),
                    )
                )
                when = self.bday(d, int(r.integers(1, 6)))
                contact = self.contact(cid, j) or {}
                self.later(when, "nps_response", (cid, contact.get("id"), survey, score, d))
                scores.append(score)
        self.event(d, "client", f"NPS survey {survey} sent to the clients")

    def _nps_response(self, d: date, payload: tuple) -> None:
        cid, contact, survey, score, sent = payload
        c = self.clients[cid]
        ts = self.at(d, "09:00", 480, ("nps", cid))
        cat = "promoter" if score >= 9 else ("detractor" if score <= 6 else "passive")
        rec = self.new(
            "NPS",
            "nps_response",
            "feedback",
            ts,
            "nps",
            "nps_responded",
            (("process", "PROC-NPS"),),
            client=cid,
            contact=contact,
            survey=survey,
            score=score,
            category=cat,
            sent_at=sent.isoformat(),
            responded_at=ts.isoformat(),
        )
        c.nps.append((d, score, rec.id))
        ms = self.month_stats[sent.strftime("%Y-%m")]
        ms["nps_n"] += 1
        ms["nps_pro"] += score >= 9
        ms["nps_det"] += score <= 6

    # ================================================================================== load
    def _load(self, d: date) -> None:
        P, fk = self.P, self.fork
        eng = [p for p in self.people.values() if p.engineer and p.employed(d)]
        full = sum(p.weight * p.speed(d) for p in eng)
        here = sum(p.weight * p.speed(d) for p in eng if d not in p.absent)
        sprint = (self.i // 10) if self.i >= 0 else 0
        if self.sprint_plan.get("n") != sprint:
            days = self.days[sprint * 10 : sprint * 10 + 10]
            cap = sum(
                p.weight
                * p.speed(d)
                * sum(1 for x in days if p.employed(x) and p.absent.get(x) != "vacation")
                / max(len(days), 1)
                for p in eng
            )
            committed = sum(f for a, b, f, _ in self.committed if a <= d < b)
            target = P["team_load"] * self._hire_factor(d)
            debt = fk.debt_share(d) * cap
            # the planner keeps room for the usual unplanned work (incidents, escalations, sickness)
            past = [self.unplanned.get(x, 0.0) for x in self.days[max(0, self.i - 20) : self.i]]
            buffer = float(np.mean(past)) if past else 0.04 * cap
            buffer += cap * 0.019  # sick days: about 4.7 a year of 252 (staff sick_days_per_year)
            planned = max(target * cap - buffer, committed + debt + 0.5 * target * cap)
            self.sprint_plan = {"n": sprint, "planned": planned, "cap": cap}
        planned = self.sprint_plan["planned"]
        load = (planned * (full / max(self.sprint_plan["cap"], 1e-6)) + self.unplanned.get(d, 0.0)) / max(
            here, 0.5
        )
        load = min(load, 2.0)
        self.loads.append((d, round(load, 4)))
        self.today_load = load

    def _hire_factor(self, d: date) -> float:
        """Case formula load x (1 - 0.012 x hire_count x hire_effect): productive hires lower the plan."""
        if self.fork.lever("lever_hire") <= 0:
            return 1.0
        hired = [p for p in self.new_people if p.employed(d) and p.speed(d) >= 1.0]
        n = int(round(self.fork.vars.get("hire_count", 5)))
        return 1 - 0.012 * n * min(1.0, len(hired) / max(n, 1))

    # ================================================================================== finance
    def _opening_finance(self) -> None:
        """Payments and taxes of the records open at T."""
        st = self.st
        for oi in st.open_invoices:
            f = oi.fields
            issued = date.fromisoformat(f["issued_at"])
            if oi.direction == "receivable":
                c = self.clients.get(f["client"])
                h = c.h if c else self._cohort_habits()
                r = self.rng("finance", oi.id, "pay")
                delay = max(5.0, r.normal(h.get("pay_delay_days", 62.0), h.get("pay_delay_sd_days", 7.0)))
                paid = issued + timedelta(days=int(delay))
                if paid <= self.start:
                    paid = self.start + timedelta(days=int(r.integers(1, 21)))
                self.later(self.roll(paid), "receive", (oi.id,))
            else:
                due = date.fromisoformat(f["due_date"])
                self.later(self.roll(max(due, self.start)), "pay_bill", (oi.id,))
        if st.last_payroll:
            pr = st.fields(st.last_payroll)
            period = date.fromisoformat(pr["period"] + "-01")
            amount = round(float(pr.get("deductions", 0)) + float(pr.get("employer_tax", 0)), 2)
            self._paye(period, amount, st.last_payroll)

    def _paye(self, period: date, amount: float, payroll: str) -> None:
        nxt = add_months(period, 1)
        due = self.roll_back(nxt.replace(day=22))
        tid = f"TAX-PAYE-{period:%Y-%m}"
        ts = self.at(
            max(self.start, self.roll_back(period.replace(day=25)))
            if period >= self.start.replace(day=1)
            else self.start,
            "16:00",
        )
        if ts.date() < self.start:
            ts = self.at(self.start, "07:00")
        rec = self.new(
            "TAX",
            "tax_payment",
            "finance",
            ts,
            "finance",
            "tax_assessed",
            (("process", payroll), ("rule", "RULE-PAYE")),
            rid=tid,
            tax="PAYE and NIC",
            period=f"{period:%Y-%m}",
            amount=amount,
            currency="USD",
            authority="TA-UK",
            due_date=due.isoformat(),
            status="scheduled",
            owner="LW-E02",
            title=f"PAYE and NIC for {month_label(period)}",
        )
        if due < self.end:
            self.later(due, "pay_tax", (rec.id,))

    def _pay_tax(self, d: date, payload: tuple) -> None:
        (tid,) = payload
        r = self.book.records[tid]
        ts = self.at(d, "09:30", 60, ("finance", tid))
        self.upd(r, ts, "finance", "tax_paid", status="paid", paid_at=d.isoformat())
        self._bank(ts, -float(r.state["amount"]), "TA-UK", tid, r.state["title"], (("process", tid),), "tax")
        if float(r.state["amount"]) >= 150_000:
            self.event(
                d, "payment", f"{r.state['title']} paid: ${float(r.state['amount']) / 1000:,.0f}k", tid
            )

    def _receive(self, d: date, payload: tuple) -> None:
        (iid,) = payload
        inv = self.book.records[iid]
        if inv.state.get("status") == "paid":
            return
        cid = inv.state["client"]
        amount = float(inv.state["amount"])
        ts = self.at(d, "10:00", 300, ("finance", cid))
        pid = self.ids.next("PAY", 5)
        causes: tuple = (("process", iid),)
        c = self.clients.get(cid)
        if c and c.late_event and c.late_until and d <= c.late_until + timedelta(days=c.late_extra):
            causes += (("life_event", c.late_event),)
        pay = self.new(
            "PAY",
            "payment",
            "finance",
            ts,
            "finance",
            "payment_received",
            causes,
            rid=pid,
            number=pid,
            amount=amount,
            currency="USD",
            direction="incoming",
            settles=iid,
            paid_at=d.isoformat(),
            method="bank transfer",
            status="completed",
            client=cid,
            title=f"Payment {pid}: {iid}",
        )
        self.upd(
            inv,
            ts + timedelta(minutes=30),
            "finance",
            "invoice_paid",
            status="paid",
            paid_at=d.isoformat(),
            related=[pay.id],
        )
        self._bank(ts, amount, cid, pay.id, iid, (("process", "PROC-BILLING"),), "incoming")
        if amount >= 100_000:
            self.event(d, "payment", f"{self.name(cid)} pays {iid}: ${amount / 1000:,.0f}k", pay.id)

    def _pay_bill(self, d: date, payload: tuple) -> None:
        (bid,) = payload
        bill = self.book.records[bid]
        if bill.state.get("status") == "paid":
            return
        vid = bill.state["vendor"]
        amount = float(bill.state["amount"])
        ts = self.at(d, "11:00", 120, ("finance", vid))
        pid = self.ids.next("PAY", 5)
        pay = self.new(
            "PAY",
            "payment",
            "finance",
            ts,
            "finance",
            "payment_sent",
            (("process", bid),),
            rid=pid,
            number=pid,
            amount=amount,
            currency="USD",
            direction="outgoing",
            settles=bid,
            paid_at=d.isoformat(),
            method="bank transfer",
            status="completed",
            vendor=vid,
            title=f"Payment {pid}: {bid}",
            actor="LW-E02",
        )
        self.upd(
            bill,
            ts + timedelta(minutes=30),
            "finance",
            "bill_paid",
            status="paid",
            paid_at=d.isoformat(),
            related=[pay.id],
        )
        self._bank(ts, -amount, vid, pay.id, bid, (("process", "PROC-VENDOR-BILLS"),), "outgoing")

    def _bank(
        self, ts: datetime, amount: float, party: str | None, records: str, ref: str, causes: tuple, kind: str
    ) -> None:
        self.cash = round(self.cash + amount, 2)
        bid = self.ids.next("BNK", 5)
        self.new(
            "BNK",
            "bank_entry",
            "finance",
            ts + timedelta(minutes=3),
            "finance",
            "bank_entry_booked",
            causes,
            rid=bid,
            amount=abs(round(amount, 2)),
            currency="USD",
            direction="credit" if amount >= 0 else "debit",
            booked_at=ts.date().isoformat(),
            account="Operating account",
            counterparty=party,
            records=records,
            reference=ref,
            balance_after=self.cash,
        )
        self.month_stats[ts.strftime("%Y-%m")]["in" if amount >= 0 else "out"] += abs(amount)

    def _finance_day(self, d: date) -> None:
        prev = self.days[self.i - 1] if self.i else self.start - timedelta(days=1)
        # every calendar day since the previous working day: billing starts, payroll, bills
        x = prev + timedelta(days=1)
        while x <= d:
            if x.day == 1:
                self._bill_clients(x, d)
                self._vendor_bills(x, d)
            x += timedelta(days=1)
        pay_day = self.roll_back(d.replace(day=25))
        if d == pay_day:
            self._payroll(d)
        if (
            d.month in (1, 4, 7, 10)
            and d == self.roll(d.replace(day=14))
            and self.P.get("corp_tax_instalment")
        ):
            self._corp_tax(d)

    def _bill_clients(self, period: date, d: date) -> None:
        for cid in sorted(self.clients):
            c = self.clients[cid]
            if c.billing == "quarterly" and period.month not in (1, 4, 7, 10):
                continue
            if not c.active_on(period) and not (c.start == period and c.status in ("signed", "active")):
                continue
            months = 3 if c.billing == "quarterly" else 1
            acv = c.acv_on(period) * self.fork.price_factor(period)
            amount = round(acv * months / 12, 2)
            if amount <= 0:
                continue
            num = self.ids.next("INV", 5)
            ts = self.at(d, "07:30", 45, ("finance", cid))
            due = d + timedelta(days=30)
            inv = self.new(
                "INV",
                "invoice",
                "finance",
                ts,
                "finance",
                "invoice_issued",
                (("process", c.contract.id), ("process", "PROC-BILLING"), ("rule", "RULE-NET30")),
                rid=num,
                number=num,
                title=f"Invoice {num}: {c.name}, {month_label(period)}",
                client=cid,
                contract=c.contract.id,
                amount=amount,
                currency="USD",
                issued_at=d.isoformat(),
                due_date=due.isoformat(),
                period=f"{period:%Y-%m}" + (f"/{months}m" if months > 1 else ""),
                owner="LW-E02",
                direction="receivable",
                billing_frequency=c.billing,
                status="issued",
            )
            r = self.rng("finance", cid, f"pay-{period}")
            delay = max(5.0, r.normal(c.h.get("pay_delay_days", 62.0), c.h.get("pay_delay_sd_days", 7.0)))
            if c.late_until and d <= c.late_until:
                delay += c.late_extra
            self.later(self.roll(d + timedelta(days=int(delay))), "receive", (inv.id,))
            self.month_stats[d.strftime("%Y-%m")]["invoiced"] += amount

    def _vendor_bills(self, period_start: date, d: date) -> None:
        prev = add_months(period_start, -1)
        for vid, v in sorted(self.st.vendors.items()):
            if v.kind == "cloud":
                self._cloud_bill(prev, d)
                continue
            if v.monthly_fee <= 0:
                continue
            r = self.rng("finance", vid, f"bill-{prev:%Y-%m}")
            cv = 0.05 if v.kind in ("nearshore", "offshore") else 0.0
            amount = round(v.monthly_fee * (1 + cv * float(r.normal())), 2)
            causes: tuple = (("process", v.agreement), ("process", "PROC-VENDOR-BILLS"))
            fx_from = getattr(self, "fx_from", None)
            if fx_from is not None and v.kind == "offshore" and prev >= fx_from.replace(day=1):
                amount = round(amount * (1 + self.fx_size), 2)
                causes += (("life_event", self.fx_event),)
            num = self.ids.next("BILL", 5)
            ts = self.at(d, "09:00", 120, ("finance", vid))
            due = d + timedelta(days=v.terms_days)
            bill = self.new(
                "BILL",
                "invoice",
                "finance",
                ts,
                "finance",
                "bill_received",
                causes,
                rid=num,
                number=num,
                title=f"{v.name} bill for {month_label(prev)}",
                client="LW",
                vendor=vid,
                contract=v.agreement,
                amount=amount,
                currency="USD",
                issued_at=d.isoformat(),
                due_date=due.isoformat(),
                period=f"{prev:%Y-%m}",
                owner="LW-E02",
                direction="payable",
                cost_class=v.cost_class,
                status="received",
                vendor_invoice_number=f"{v.invoice_prefix}-{prev:%y%m}-{int(r.integers(100, 999))}",
            )
            self.later(self.roll(due), "pay_bill", (bill.id,))
            self.month_stats[d.strftime("%Y-%m")]["bills"] += amount

    def _cloud_bill(self, period: date, d: date) -> None:
        cloud = self.st.cloud
        if cloud is None:
            return
        P = self.P
        t_years = max((period - self.start).days, 0) / 365.0
        base = P["cloud_cost_per_year_now"] / 12 * (1 + P["cloud_cost_growth_per_year"]) ** t_years
        k_gpu = 0.8  # case k_infra_gpu
        hits = [
            h * self.fork.gpu_factor(t0)
            for t0, h, _ in self.gpu
            if t0.strftime("%Y-%m") == period.strftime("%Y-%m")
        ]
        amount = round(base * (1 + k_gpu * P["gpu_share"] * min(sum(hits), 1.0) * 0.25), 2)
        cid = f"CB-{period:%Y-%m}"
        if cid in self.book.records:
            return
        ts = self.at(d, "06:30", 30, ("finance", "cloud"))
        due = self.roll(d + timedelta(days=10))
        gpu = round(amount * P["gpu_share"], 2)
        rec = self.new(
            "CB",
            "cloud_bill",
            "finance",
            ts,
            "finance",
            "cloud_bill_received",
            (("process", "PROC-CLOUD"), ("process", cloud.agreement)),
            rid=cid,
            vendor=cloud.id,
            contract=cloud.agreement,
            period=f"{period:%Y-%m}",
            amount=amount,
            currency="USD",
            due_date=due.isoformat(),
            gpu_share=P["gpu_share"],
            status="issued",
            number=f"NC-{period:%y%m}-{4100 + period.month}",
            services={"gpu": gpu, "other": round(amount - gpu, 2)},
        )
        self.later(due, "pay_cloud", (rec.id,))
        reserve = self.fork.gpu_reserve_per_year(period)
        if reserve > 0:
            rid = f"CB2-{period:%Y-%m}"
            r2 = self.new(
                "CB",
                "cloud_bill",
                "finance",
                ts + timedelta(minutes=5),
                "finance",
                "cloud_bill_received",
                (("process", "PROC-CLOUD"), ("decision", self.decision_id))
                if self.decision_id
                else (("process", "PROC-CLOUD"),),
                rid=rid,
                vendor=cloud.id,
                period=f"{period:%Y-%m}",
                amount=round(reserve / 12, 2),
                currency="USD",
                due_date=due.isoformat(),
                status="issued",
                number=f"GPU-RES-{period:%y%m}",
                services={"gpu_reservation": round(reserve / 12, 2)},
            )
            self.later(due, "pay_cloud", (r2.id,))

    def _pay_cloud(self, d: date, payload: tuple) -> None:
        (cid,) = payload
        r = self.book.records[cid]
        ts = self.at(d, "07:40", 20, ("finance", "cloud"))
        self.upd(r, ts, "finance", "cloud_bill_paid", status="paid", paid_at=d.isoformat())
        self._bank(
            ts,
            -float(r.state["amount"]),
            r.state["vendor"],
            cid,
            r.state.get("number", cid),
            (("process", "PROC-CLOUD"),),
            "direct_debit",
        )

    def _payroll(self, d: date) -> None:
        P = self.P
        m0 = d.replace(day=1)
        m1 = add_months(m0, 1)
        lines, gross = [], 0.0
        for pid in sorted(self.people):
            p = self.people[pid]
            a, b = max(p.start, m0), min(p.last_day or m1, m1 - timedelta(days=1))
            if b < a:
                continue
            share = ((b - a).days + 1) / (m1 - m0).days
            g = round(p.salary / 12 * share, 2)
            gross += g
            lines.append({"employee": pid, "gross": g, "commission": 0})
        gross = round(gross, 2)
        tax = round(gross * P["payroll_tax_rate"], 2)
        ded = round(gross * P["employee_deduction_rate"], 2)
        net = round(gross - ded, 2)
        rid = f"PR-{m0:%Y-%m}"
        ts = self.at(d, "08:40", 20, ("finance", "payroll"))
        rec = self.new(
            "PR",
            "payroll",
            "people",
            ts,
            "finance",
            "payroll_paid",
            (("process", "PROC-PAYROLL"),),
            rid=rid,
            title=f"Payroll {month_label(m0)}",
            period=f"{m0:%Y-%m}",
            amount=gross,
            currency="USD",
            employer_tax=tax,
            deductions=ded,
            net_pay=net,
            headcount=len(lines),
            employee=[x["employee"] for x in lines],
            lines=lines,
            paid_at=d.isoformat(),
            status="paid",
            owner="LW-E02",
        )
        self._bank(ts, -net, None, rid, f"Salaries {month_label(m0)}", (("process", rid),), "payroll")
        self.event(
            d, "payment", f"Payroll {month_label(m0)}: ${net / 1000:,.0f}k net to {len(lines)} people", rid
        )
        self._paye(m0, round(tax + ded, 2), rec.id)

    def _corp_tax(self, d: date) -> None:
        q = (d.month - 1) // 3
        year = d.year if q > 0 else d.year - 1
        quarter = q if q > 0 else 4
        tid = f"TAX-CT-{year}-Q{quarter}"
        amount = float(self.P["corp_tax_instalment"])
        ts = self.at(d, "07:00", 30, ("finance", "ct"))
        self.new(
            "TAX",
            "tax_payment",
            "finance",
            ts,
            "finance",
            "tax_assessed",
            (("rule", "RULE-CORP-TAX"),),
            rid=tid,
            tax="Corporation tax",
            period=f"{year}-Q{quarter}",
            amount=amount,
            currency="USD",
            authority="TA-UK",
            due_date=d.isoformat(),
            status="scheduled",
            owner="LW-E02",
            title=f"Corporation tax instalment {year}-Q{quarter}",
        )
        self._pay_tax(d, (tid,))

    # ================================================================================== month end
    def _month_end(self, d: date) -> None:
        nxt = self.days[self.i + 1] if self.i + 1 < len(self.days) else None
        if nxt is not None and nxt.month == d.month:
            return
        if d.replace(day=1) < self.start:  # the month of T: most of it is history
            return
        key = d.strftime("%Y-%m")
        ms = self.month_stats[key]
        hist = self.month_history()
        prev = [hist[k] for k in sorted(hist) if k < key][-3:]
        base = float(np.mean(prev)) if prev else self.P["incidents_per_month"]
        last = prev[-1] if prev else base
        n = int(ms.get("incidents", 0))
        hist[key] = n
        month_inc = [r for r in self.incidents if r.first_ts.strftime("%Y-%m") == key]
        loads = [x for t, x in self.loads if t.strftime("%Y-%m") == key]
        load = float(np.mean(loads)) if loads else 0.0
        signal = None
        leavers = [
            r.id
            for r in self.book.of("life_event")
            if r.state.get("event_type") == "resignation"
            and (d - date.fromisoformat(r.state["effective_date"])).days <= 75
        ]
        load_sig = self._load_signal(d, key, load, leavers, month_inc)
        cash_sig = self._cash_signal(d, key)
        # against the month before and the mean of the three months before
        rose = n >= last + 3 and n >= 1.25 * base
        fell = n <= last - 3 and n <= 0.75 * base
        drivers: list[tuple[str, str]] = []  # what lowered the incident rate (a fall needs one)
        fk = self.fork
        if fk.lever("lever_debt") > 0 and self.decision_id and d >= fk.dates["debt_ready"]:
            drivers += [("decision", self.decision_id)] + [("life_event", m) for m in self.milestones]
        if fk.lever("lever_gpu") > 0 and self.decision_id and d >= fk.dates["gpu_ready"]:
            drivers += [("decision", self.decision_id)] + [("life_event", m) for m in self.milestones]
        if rose or (fell and drivers):
            causes: list[tuple[str, str]] = [("process", r.id) for r in month_inc]
            shocks = sorted({c[1] for r in month_inc for c in r.causes if c[0] == "life_event"})
            causes += [("life_event", g) for g in shocks]
            if rose:
                causes += [("life_event", x) for x in leavers]
                if load_sig:
                    causes.append(("life_event", load_sig))
            else:
                causes += list(dict.fromkeys(drivers))
            word = "rose" if rose else "fell"
            dep = int(ms.get("cause:deploy", 0))
            gp = int(ms.get("cause:cloud provider capacity", 0))
            out = int(ms.get("cause:outside", 0))
            before = month_label(d.replace(day=1) - timedelta(days=1))
            stmt = (
                f"{n} incidents in {month_label(d)} against {int(last)} in {before} and {base:.1f} a month "
                "in the three months before: "
                f"{dep} after deploys, {gp} during a GPU capacity shortage, {out} from outside causes. "
                f"Engineering load averaged {load:.2f}."
            )
            ts = self.at(d, "17:30")
            sig = self.new(
                "SIG",
                "life_event",
                "engineering",
                ts,
                "operations",
                "signal_observed",
                tuple(causes),
                title=f"Incidents {word} in {month_label(d)}",
                statement=stmt,
                effective_date=d.isoformat(),
                event_type="signal",
                polarity="negative" if rose else "positive",
                incidents=n,
                incidents_before=round(base, 2),
                incidents_month_before=int(last),
                load=round(load, 3),
            )
            signal = sig.id
            self.event(
                d,
                "shock" if rose else "milestone",
                f"Incidents {word} in {month_label(d)}: {n} against {int(last)} in {before}",
                sig.id,
            )
        # leadership review of the month, on the first working day of the next month
        review_day = nxt or self.bday(d, 1)
        if review_day < self.end:
            others = [x for x in (load_sig, cash_sig) if x]
            self.later(review_day, "review", (key, signal, n, base, load, others))

    def _load_signal(
        self, d: date, key: str, load: float, leavers: list[str], month_inc: list[Rec]
    ) -> str | None:
        """``Engineering load over capacity in <month>``: the month's mean load above the burnout line."""
        if load <= self.P["burnout_load"]:
            return None
        causes: list[tuple[str, str]] = [("life_event", x) for x in leavers]
        start = d.replace(day=1)
        onboarding = [
            c.contract.id
            for c in self.clients.values()
            if c.new and c.start and start - timedelta(days=45) <= c.start <= d and c.contract
        ]
        causes += [("process", x) for x in onboarding]
        causes += [("process", r.id) for r in month_inc]
        if self.fork.debt_share(d) > 0 and self.decision_id:
            causes.append(("decision", self.decision_id))
        causes.append(("rule", "RULE-BURNOUT"))
        stmt = (
            f"Engineering load averaged {load:.2f} in {month_label(d)} (1.00 = full; the burnout line is "
            f"{self.P['burnout_load']:.2f}): {len(leavers)} recent resignations, {len(onboarding)} client "
            f"onboardings, {len(month_inc)} incidents of unplanned work."
        )
        sig = self.new(
            "SIG", "life_event", "engineering", self.at(d, "17:20"), "operations", "signal_observed",
            tuple(causes), title=f"Engineering load over capacity in {month_label(d)}", statement=stmt,
            effective_date=d.isoformat(), event_type="signal", polarity="negative", load=round(load, 3),
        )  # fmt: skip
        self.event(d, "shock", f"Engineering load over capacity in {month_label(d)}: {load:.2f}", sig.id)
        self.last_load_signal = sig.id
        return sig.id

    def _cash_signal(self, d: date, key: str) -> str | None:
        """``Cash fell in <month>``: the month's net cash flow below -$150k, with the payments behind it."""
        days = [x for x in self.series if x["date"][:7] == key]
        if not days:
            return None
        first = days[0]["values"]["cash"]
        out = self.cash - first
        if out > -150_000:
            return None
        causes: list[tuple[str, str]] = []
        big = []
        for r in self.book.records.values():
            if r.otype not in ("tax_payment", "payroll", "invoice") or not r.versions:
                continue
            paid = r.state.get("paid_at") or ""
            if paid[:7] != key or r.state.get("status") != "paid":
                continue
            if r.otype == "invoice" and (
                r.state.get("direction") != "payable" or r.state.get("amount", 0) < 100_000
            ):
                continue
            big.append((float(r.state.get("amount", 0)), r.id))
        causes += [("process", rid) for _, rid in sorted(big, reverse=True)[:8]]
        ended = [c.contract.id for c in self.clients.values() if c.end and c.end.strftime("%Y-%m") == key]
        causes += [("process", x) for x in ended[:5]]
        late = [
            c.late_event for c in self.clients.values() if c.late_event and c.late_until and c.late_until >= d
        ]
        causes += [("life_event", x) for x in late[:5]]
        stmt = (
            f"Cash fell by ${-out / 1000:,.0f}k in {month_label(d)} to ${self.cash / 1e6:.2f}M: "
            f"{len(big)} large payments (taxes, payroll, vendor bills), {len(ended)} contracts ended, "
            f"{len(late)} clients paying late."
        )
        sig = self.new(
            "SIG", "life_event", "finance", self.at(d, "17:40"), "finance", "signal_observed", tuple(causes),
            title=f"Cash fell in {month_label(d)}", statement=stmt, effective_date=d.isoformat(),
            event_type="signal", polarity="negative", cash=round(self.cash, 2), change=round(out, 2),
        )  # fmt: skip
        self.event(d, "payment", f"Cash fell in {month_label(d)} by ${-out / 1000:,.0f}k", sig.id)
        return sig.id

    def month_history(self) -> dict[str, float]:
        if not hasattr(self, "_mh"):
            import csv as _csv

            rows = list(
                _csv.DictReader((self.st.history / "datasets" / "metrics_monthly.csv").open(encoding="utf-8"))
            )
            self._mh = {
                r["month"]: float(r.get("incidents") or 0)
                for r in rows
                if r.get("incidents") not in (None, "")
            }
            last = sorted(self._mh)[-1] if self._mh else None
            if last and last == self.start.strftime("%Y-%m"):
                self._mh.pop(last)  # the month of T is partial
        return self._mh

    def _review(self, d: date, payload: tuple) -> None:
        key, signal, n, base, load, others = payload
        m = date.fromisoformat(key + "-01")
        ms = self.month_stats[key]
        causes: list[tuple[str, str]] = []
        if signal:
            causes.append(("life_event", signal))
        causes += [("life_event", x) for x in others]
        notices = [
            r.id
            for r in self.book.of("life_event")
            if r.state.get("event_type") == "churn" and r.state["effective_date"][:7] == key
        ]
        causes += [("life_event", x) for x in notices]
        if self.decision_id:
            causes.append(("decision", self.decision_id))
        ceo = "LW-E01"
        ts = self.at(d, "10:00", 60, ("review", key))
        arr = sum(c.acv_on(d) for c in self.clients.values() if c.active_on(d))
        stmt = (
            f"Leadership review of {month_label(m)}: cash ${self.cash / 1e6:.2f}M, ARR ${arr / 1e6:.2f}M, "
            f"{n} incidents, {int(ms.get('notices', 0))} client notices, engineering load {load:.2f}."
        )
        inc = [r.id for r in self.incidents if r.first_ts.strftime("%Y-%m") == key]
        if inc:
            stmt += " Incidents of the month: " + ", ".join(inc) + "."
        rev = self.new(
            "DEC",
            "decision",
            "company",
            ts,
            "decision",
            "review_held",
            tuple(causes),
            title=f"Leadership review: {month_label(m)}",
            statement=stmt,
            status="accepted",
            owner=ceo,
            actor=ceo,
            decided_at=ts.isoformat(),
        )
        self.event(d, "decision", f"Leadership review of {month_label(m)}", rev.id, ceo)
        self._note(
            "leadership_review",
            rev,
            d,
            {
                "month": key,
                "n": n,
                "base": base,
                "load": load,
                "arr": arr,
                "cash": self.cash,
                "notices": notices,
                "signal": signal,
            },
        )

    # ================================================================================== metrics
    def _metrics(self, d: date) -> None:
        arr = sum(c.acv_on(d) * self.fork.price_factor(d) for c in self.clients.values() if c.active_on(d))
        head = sum(1 for p in self.people.values() if p.employed(d))
        dt = self.downtime.get(d, {})
        avail = float(np.mean([1 - min(dt.get(s, 0.0), 1440) / 1440 for s in SERVICES]))
        hit = sum(
            h * self.fork.gpu_factor(d) * math.exp(-(d - t0).days / self.P["gpu_tau_days"])
            for t0, h, _ in self.gpu
            if t0 <= d
        )
        lat = {
            s: self.P["latency_now"][s]
            * self.fork.latency_factor(d)
            * (1 + 0.6 * hit * GPU_DEPENDENTS.get(s, 0.0))
            for s in SERVICES
        }
        noise = float(np.exp(self.rng("monitoring", "p95", d.isoformat()).normal(0, 0.03)))
        p95 = float(np.mean(list(lat.values()))) * noise
        for s in SERVICES:  # the rows of monitoring_daily (datasets of the metric catalogue)
            err = self.st.param("company", f"error_rate_{s}") or {"point": 0.0}
            self.monitoring.append({
                "date": d.isoformat(), "service": s, "p95_ms": round(lat[s] * noise, 1),
                "availability": round(1 - min(dt.get(s, 0.0), 1440) / 1440, 6),
                "downtime_minutes": int(dt.get(s, 0.0)), "error_rate": float(err["point"]),
            })  # fmt: skip
        inc = sum(1 for r in self.incidents if r.first_ts.date() == d)
        self.series.append(
            {
                "date": d.isoformat(),
                "values": {
                    "cash": round(self.cash, 2),
                    "load": round(self.today_load, 3),
                    "incidents": inc,
                    "arr": round(arr, 2),
                    "headcount": head,
                    "availability": round(avail, 5),
                    "p95_ms": round(p95, 1),
                },
            }
        )

    # ================================================================================== notes for writers
    def _note(self, kind: str, rec: Rec, d: date, ctx: dict[str, Any]) -> None:
        aid = f"{kind}:{rec.id}"
        rec.text_slots["description"] = aid
        self.notable.append({"id": aid, "type": kind, "record": rec.id, "date": d.isoformat(), "ctx": ctx})

    # ================================================================================== summary
    def summary(self) -> dict[str, Any]:
        first, last = self.series[0]["values"], self.series[-1]["values"]
        n_clients0 = sum(1 for c in self.st.clients.values() if c.status == "active")
        notices = sum(1 for r in self.book.of("life_event") if r.state.get("event_type") == "churn")
        years = (self.end - self.start).days / 365.0
        avail = float(np.mean([x["values"]["availability"] for x in self.series]))
        months = sorted(self.month_stats)
        resp = [
            (self.month_stats[k]["nps_n"], self.month_stats[k]["nps_pro"], self.month_stats[k]["nps_det"])
            for k in months
        ]
        last_nps = None
        for n, pro, det in resp:
            if n:
                last_nps = 100.0 * (pro - det) / n
        burn = (first["cash"] - last["cash"]) / max(len(months) - 1, 1)
        return {
            "cash_start": first["cash"],
            "cash_end": last["cash"],
            "arr_end": last["arr"],
            "headcount_end": last["headcount"],
            "incidents": len(self.incidents),
            "sev1": sum(1 for r in self.incidents if r.state["severity"] == "SEV1"),
            "client_notices": notices,
            "churn_annual": round(notices / max(n_clients0, 1) / years, 4),
            "availability": round(avail, 5),
            "nps_last": round(last_nps, 1) if last_nps is not None else None,
            "load_mean": round(float(np.mean([x for _, x in self.loads])), 3),
            "runway_months": 60.0 if burn <= 0 else round(min(last["cash"] / burn, 60.0), 1),
            "records": len(self.book.records),
        }


def _k_amount(text: str) -> float | None:
    """'+$49k' or '$1.2M' in a path text -> dollars."""
    import re

    m = re.search(r"\$([\d.,]+)\s*([kKmM])", text)
    if not m:
        return None
    x = float(m.group(1).replace(",", ""))
    return x * (1e6 if m.group(2).lower() == "m" else 1e3)


def _size(text: str) -> float:
    import re

    m = re.search(r"size\s*(-?[\d.]+)", text)
    return float(m.group(1)) if m else 0.0


def _mu_for_nps(level: float, sd: float) -> float:
    """The latent mean whose rounded scores (0-10) give the NPS ``level`` (bisection)."""
    from math import erf, sqrt

    def nps(mu: float) -> float:
        def cdf(x: float) -> float:
            return 0.5 * (1 + erf((x - mu) / (sd * sqrt(2))))

        return 100.0 * ((1 - cdf(8.5)) - cdf(6.5))

    lo, hi = 0.0, 12.0
    level = max(-99.0, min(99.0, level))
    for _ in range(40):
        mid = (lo + hi) / 2
        if nps(mid) < level:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def run_forward(
    st: StateAtT, params: Params, fork: Fork, scenario: int, seed: int, months: int = 6, **kw: Any
) -> ForwardRun:
    return ForwardRun(st, params, fork, scenario, seed, months, **kw).run()


__all__ = ["ForwardRun", "run_forward", "month_label"]
