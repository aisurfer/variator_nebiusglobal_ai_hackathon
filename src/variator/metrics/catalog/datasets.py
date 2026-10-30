"""History datasets as typed tables with the as-of rule.

``load_history(dir)`` reads one history directory of ``variator.orgsim.company``:

- ``datasets/sim_<type>.jsonl.gz``: one row per version of a business record (the KB ``sim`` entities);
- ``datasets/*.csv``: flat tables (Jira issues, transitions, merge requests, people, clients, bank, sprints;
  v2: ``monitoring_daily.csv``, ``nps_responses.csv``);
- ``delivery/``: the corpus manifest (anchor T, start), ``dashboard.json``, ``config/people.yaml`` and
  ``config/sources.yaml``, and the timeline for Jira and GitLab titles and the sprint calendar.

Money is ``Decimal``. Nothing known at or after T is kept: such rows are dropped at load and counted in
``dropped_after_t``. A missing file leaves its table out (``has(name)`` is false): a metric on it is unknown.
Columns that hold the final state of the history (``status``, ``done_at`` of issues, ``status_at_t``,
``acv_at_t``, ``salary_at_t``) are not loaded: they would leak the state at T into earlier dates.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from variator.metrics.catalog.asof import Clock, parse_date, parse_ts

SIM_TYPES = (
    "bank_entry",
    "cloud_bill",
    "contract",
    "deal",
    "decision",
    "deploy",
    "employee_event",
    "incident",
    "invoice",
    "life_event",
    "payment",
    "payroll",
    "process",
    "requisition",
    "rule",
    "support_ticket",
    "tax_payment",
)
FLAT = (
    "issues",
    "transitions",
    "merge_requests",
    "people",
    "clients",
    "bank",
    "sprint_load",
    "monitoring_daily",
    "nps_responses",
)
DONE = frozenset({"Done", "Won't Do"})
_SPRINT_NO = re.compile(r"(\d+)\s*$")


@dataclass(frozen=True)
class Version:
    """One version of a business record: known at ``ts``; ``f`` holds the full state then."""

    ts: datetime
    kind: str
    f: dict[str, Any]
    causes: tuple[str, ...]


class SimTable:
    """The versions of one KB object type, by record id, in time order."""

    def __init__(self, object_type: str, versions: dict[str, list[Version]]):
        self.object_type = object_type
        self._v = {k: sorted(v, key=lambda x: x.ts) for k, v in versions.items()}
        self._ts = {k: [x.ts for x in v] for k, v in self._v.items()}
        self._memo: dict[datetime, list[tuple[str, Version]]] = {}

    def __len__(self) -> int:
        return len(self._v)

    def __contains__(self, rid: object) -> bool:
        return rid in self._v

    def ids(self) -> list[str]:
        return list(self._v)

    def at(self, rid: str, cut: datetime) -> Version | None:
        """The version of ``rid`` known at ``cut`` (None: not known yet)."""
        ts = self._ts.get(rid)
        if not ts:
            return None
        i = bisect_right(ts, cut)
        return self._v[rid][i - 1] if i else None

    def history(self, rid: str, cut: datetime | None = None) -> list[Version]:
        v = self._v.get(rid, [])
        if cut is None:
            return list(v)
        return v[: bisect_right(self._ts[rid], cut)]

    def known(self, cut: datetime) -> list[tuple[str, Version]]:
        """The latest known version of every record known at ``cut``."""
        hit = self._memo.get(cut)
        if hit is None:
            hit = []
            for rid, ts in self._ts.items():
                i = bisect_right(ts, cut)
                if i:
                    hit.append((rid, self._v[rid][i - 1]))
            self._memo[cut] = hit
        return hit

    def versions(self, cut: datetime) -> Iterator[tuple[str, Version]]:
        """Every version known at ``cut`` (all records)."""
        for rid, ts in self._ts.items():
            for v in self._v[rid][: bisect_right(ts, cut)]:
                yield rid, v


@dataclass(frozen=True)
class IssueRow:
    key: str
    project: str
    type: str
    component: str
    points: int
    priority: str
    created: datetime
    reporter: str
    parent: str
    client: str
    unplanned: bool


@dataclass(frozen=True)
class TransitionRow:
    key: str
    ts: datetime
    frm: str
    to: str
    actor: str


@dataclass(frozen=True)
class MergeRequestRow:
    iid: int
    key: str
    author: str
    reviewer: str
    created: datetime
    merged_at: datetime | None
    deploy: str


@dataclass(frozen=True)
class PersonRow:
    id: str
    name: str
    title: str
    role: str
    department: str
    start: date
    last_day: date | None
    hire_slot: str


@dataclass(frozen=True)
class ClientRow:
    id: str
    name: str
    industry: str
    key: bool
    billing: str


@dataclass(frozen=True)
class SprintRow:
    """A two-week sprint; ``load`` is its realised load (assigned / capacity), known at ``end``."""

    n: int
    name: str
    start: datetime
    end: datetime
    load: float | None


@dataclass(frozen=True)
class MonitoringRow:
    date: date
    service: str
    p95_ms: float | None
    availability: float | None
    downtime_minutes: float | None
    error_rate: float | None


@dataclass(frozen=True)
class NpsRow:
    date: date
    response_id: str
    client: str
    contact: str
    survey: str
    score: int


@dataclass
class HistoryData:
    root: Path
    anchor: date
    start: date
    clock: Clock
    tenant: str = "company"
    company: str = ""
    currency: str = "USD"
    gitlab_project: str = ""
    run_id: str | None = None
    organization: dict[str, Any] | None = None
    sim: dict[str, SimTable] = field(default_factory=dict)
    issues: dict[str, IssueRow] = field(default_factory=dict)
    transitions: dict[str, list[TransitionRow]] = field(default_factory=dict)
    merge_requests: list[MergeRequestRow] = field(default_factory=list)
    people: dict[str, PersonRow] = field(default_factory=dict)
    clients: dict[str, ClientRow] = field(default_factory=dict)
    bank_kind: dict[str, str] = field(default_factory=dict)
    sprints: list[SprintRow] = field(default_factory=list)
    monitoring: list[MonitoringRow] = field(default_factory=list)
    nps: list[NpsRow] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    present: set[str] = field(default_factory=set)
    dropped_after_t: dict[str, int] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return self.clock.tz

    @property
    def t_start(self) -> datetime:
        """The first instant of T: nothing known at or after it is part of the history."""
        return self.clock.start_of_day(self.anchor)

    def has(self, name: str) -> bool:
        """Whether a table was delivered: ``sim_<type>`` or a flat table name."""
        return name in self.present

    def table(self, object_type: str) -> SimTable:
        return self.sim.get(object_type) or SimTable(object_type, {})

    def name(self, ref: object) -> str:
        """Display name of a person, organisation or contact id (the id itself when unknown)."""
        s = str(ref or "")
        return self.names.get(s, s)

    # ------------------------------------------------------------------ Jira as of a cut
    def status_at(self, key: str, cut: datetime) -> str | None:
        """Status of an issue known at ``cut`` (None: the issue did not exist yet)."""
        row = self.issues.get(key)
        if row is None or row.created > cut:
            return None
        status = "To Do"
        for t in self.transitions.get(key, []):
            if t.ts > cut:
                break
            status = t.to
        return status

    def done_at(self, key: str, cut: datetime) -> datetime | None:
        """The first move to Done known at ``cut``."""
        for t in self.transitions.get(key, []):
            if t.ts > cut:
                return None
            if t.to == "Done":
                return t.ts
        return None

    def worker(self, key: str, cut: datetime) -> str:
        """Who did the work: the actor of the first move to In Progress known at ``cut`` ("" when none)."""
        for t in self.transitions.get(key, []):
            if t.ts > cut:
                break
            if t.to == "In Progress":
                return t.actor
        return ""


# ---------------------------------------------------------------------------------------------- reading
def _open_text(path: Path) -> Iterable[str]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            yield from fh
    else:
        with path.open(encoding="utf-8") as fh:
            yield from fh


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _f(value: str | None) -> float | None:
    s = (value or "").strip()
    return float(s) if s else None


def _bool(value: str | None) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _load_sim(path: Path, object_type: str, t_start: datetime, dropped: dict[str, int]) -> SimTable:
    out: dict[str, list[Version]] = {}
    for line in _open_text(path):
        if not line.strip():
            continue
        row = json.loads(line, parse_float=Decimal)
        ts = parse_ts(row["ts"])
        if ts >= t_start:
            dropped[f"sim_{object_type}"] = dropped.get(f"sim_{object_type}", 0) + 1
            continue
        rid = str(row["id"])
        fields = {k: v for k, v in row.items() if k not in ("id", "ts", "kind", "process", "causes")}
        fields["process"] = row.get("process")
        out.setdefault(rid, []).append(
            Version(ts=ts, kind=str(row.get("kind", "")), f=fields, causes=tuple(row.get("causes") or ()))
        )
    return SimTable(object_type, out)


def _meta(root: Path) -> dict[str, Any]:
    """Anchor, start, time zone, tenant, company, currency and GitLab project from the delivery."""
    meta: dict[str, Any] = {}
    delivery = root / "delivery"
    man = delivery / "manifest.json"
    if man.is_file():
        m = json.loads(man.read_text(encoding="utf-8"))
        h = m.get("history") or {}
        meta["anchor"] = h.get("anchor")
        meta["start"] = h.get("start")
        meta["tenant"] = m.get("tenant")
        meta["company"] = m.get("name")
        meta["run_id"] = m.get("run_id")
    src = delivery / "config" / "sources.yaml"
    if src.is_file():
        s = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
        tenant = s.get("tenant") or {}
        meta["timezone"] = tenant.get("timezone")
        meta.setdefault("company", tenant.get("name"))
        for source in s.get("sources") or []:
            if source.get("provider") == "gitlab" and source.get("containers"):
                meta["gitlab_project"] = next(iter(source["containers"]))
    dash = delivery / "dashboard.json"
    if dash.is_file():
        d = json.loads(dash.read_text(encoding="utf-8"))
        meta["organization"] = d
        meta["currency"] = d.get("currency")
        meta.setdefault("timezone", d.get("timezone"))
    return meta


def _names(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Display names of people, organisations and contacts, and the department of each person."""
    path = root / "delivery" / "config" / "people.yaml"
    names: dict[str, str] = {}
    depts: dict[str, str] = {}
    if not path.is_file():
        return names, depts
    y = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for p in y.get("people") or []:
        names[str(p["id"])] = str(p.get("name") or p["id"])
        depts[str(p["id"])] = str(p.get("department") or "")
    for o in y.get("organizations") or []:
        names[str(o["id"])] = str(o.get("name") or o["id"])
    for c in y.get("contacts") or []:
        names[str(c["id"])] = str(c.get("name") or c["id"])
    return names, depts


def _timeline(
    path: Path, t_start: datetime
) -> tuple[dict[str, str], dict[int, tuple[str, datetime, datetime]]]:
    """Titles of Jira issues and merge requests (first version) and the sprint calendar."""
    titles: dict[str, str] = {}
    sprints: dict[int, tuple[str, datetime, datetime]] = {}
    for line in _open_text(path):
        if not line.strip():
            continue
        ln = json.loads(line)
        at = parse_ts(ln["at"]) if ln.get("at") else None
        if at is not None and at >= t_start:
            continue
        p = ln.get("payload") or {}
        if ln.get("object_type") == "issue" and isinstance(p.get("fields"), dict):
            f = p["fields"]
            key = f"jira.{ln['external_id']}"
            if key not in titles and f.get("summary"):
                titles[key] = str(f["summary"])
            for s in f.get("customfield_10020") or []:
                m = _SPRINT_NO.search(str(s.get("name", "")))
                if m and s.get("startDate") and s.get("endDate"):
                    sprints.setdefault(
                        int(m.group(1)), (str(s["name"]), parse_ts(s["startDate"]), parse_ts(s["endDate"]))
                    )
        elif ln.get("object_type") == "merge_request":
            iid = p.get("iid")
            container = ln.get("container") or ""
            if iid is not None and p.get("title"):
                key = f"gitlab.{container.strip('/').replace('/', '-')}.mr-{iid}"
                titles.setdefault(key, str(p["title"]))
    return titles, sprints


def load_history(root: str | Path, *, timeline: bool = True, as_of_anchor: date | None = None) -> HistoryData:
    """Load a history directory. ``as_of_anchor`` replaces T (default: the anchor of the corpus manifest)."""
    root = Path(root)
    ds = root / "datasets"
    if not ds.is_dir():
        raise FileNotFoundError(f"no datasets directory: {ds}")
    meta = _meta(root)
    tz = ZoneInfo(meta.get("timezone") or "UTC")
    clock = Clock(tz)
    anchor = as_of_anchor or parse_date(meta.get("anchor"))
    start = parse_date(meta.get("start"))
    if anchor is None or start is None:
        # no corpus manifest: T is the day after the last record, the start is the first record
        stamps = [
            json.loads(line)["ts"][:10]
            for p in sorted(ds.glob("sim_*.jsonl*"))
            for line in _open_text(p)
            if line.strip()
        ]
        if not stamps:
            raise ValueError(f"{root}: no corpus manifest and no sim records to find T")
        anchor = anchor or date.fromisoformat(max(stamps)) + timedelta(days=1)
        start = start or date.fromisoformat(min(stamps))
    data = HistoryData(
        root=root,
        anchor=anchor,
        start=start,
        clock=clock,
        tenant=meta.get("tenant") or "company",
        company=meta.get("company") or "",
        currency=meta.get("currency") or "USD",
        gitlab_project=meta.get("gitlab_project") or "",
        run_id=meta.get("run_id"),
        organization=meta.get("organization"),
    )
    t_start = data.t_start
    dropped = data.dropped_after_t
    for ot in SIM_TYPES:
        path = ds / f"sim_{ot}.jsonl.gz"
        if not path.is_file():
            path = ds / f"sim_{ot}.jsonl"
        if path.is_file():
            data.sim[ot] = _load_sim(path, ot, t_start, dropped)
            data.present.add(f"sim_{ot}")
    names, depts = _names(root)
    data.names = names

    def drop(table: str) -> None:
        dropped[table] = dropped.get(table, 0) + 1

    path = ds / "issues.csv"
    if path.is_file():
        data.present.add("issues")
        for r in _csv(path):
            created = parse_ts(r["created"])
            if created >= t_start:
                drop("issues")
                continue
            data.issues[r["key"]] = IssueRow(
                key=r["key"],
                project=r.get("project", ""),
                type=r.get("type", ""),
                component=r.get("component", ""),
                points=int(float(r.get("points") or 0)),
                priority=r.get("priority", ""),
                created=created,
                reporter=r.get("reporter", ""),
                parent=r.get("parent", ""),
                client=r.get("client", ""),
                unplanned=_bool(r.get("unplanned")),
            )
    path = ds / "transitions.csv"
    if path.is_file():
        data.present.add("transitions")
        for r in _csv(path):
            ts = parse_ts(r["ts"])
            if ts >= t_start:
                drop("transitions")
                continue
            data.transitions.setdefault(r["key"], []).append(
                TransitionRow(
                    key=r["key"], ts=ts, frm=r.get("from", ""), to=r.get("to", ""), actor=r.get("actor", "")
                )
            )
        for rows in data.transitions.values():
            rows.sort(key=lambda t: t.ts)
    path = ds / "merge_requests.csv"
    if path.is_file():
        data.present.add("merge_requests")
        for r in _csv(path):
            created = parse_ts(r["created"])
            if created >= t_start:
                drop("merge_requests")
                continue
            merged = parse_ts(r["merged_at"]) if r.get("merged_at") else None
            data.merge_requests.append(
                MergeRequestRow(
                    iid=int(r["iid"]),
                    key=r.get("key", ""),
                    author=r.get("author", ""),
                    reviewer=r.get("reviewer", ""),
                    created=created,
                    merged_at=merged if merged is None or merged < t_start else None,
                    deploy=r.get("deploy", ""),
                )
            )
    path = ds / "people.csv"
    if path.is_file():
        data.present.add("people")
        for r in _csv(path):
            pid = r["id"]
            data.people[pid] = PersonRow(
                id=pid,
                name=r.get("name") or names.get(pid, pid),
                title=r.get("title", ""),
                role=r.get("role", ""),
                department=r.get("department") or depts.get(pid, ""),
                start=parse_date(r.get("start")) or data.start,
                last_day=parse_date(r.get("last_day")),
                hire_slot=r.get("hire_slot", ""),
            )
            names.setdefault(pid, data.people[pid].name)
    path = ds / "clients.csv"
    if path.is_file():
        data.present.add("clients")
        for r in _csv(path):
            data.clients[r["id"]] = ClientRow(
                id=r["id"],
                name=r.get("name", r["id"]),
                industry=r.get("industry", ""),
                key=_bool(r.get("key")),
                billing=r.get("billing", ""),
            )
            names.setdefault(r["id"], r.get("name", r["id"]))
    path = ds / "bank.csv"
    if path.is_file():
        data.present.add("bank")
        for r in _csv(path):
            if parse_ts(r["ts"]) >= t_start:
                drop("bank")
                continue
            data.bank_kind[r["id"]] = r.get("kind", "")
    loads: dict[int, float] = {}
    path = ds / "sprint_load.csv"
    if path.is_file():
        for r in _csv(path):
            loads[int(r["sprint"])] = float(r["load"])
    calendar: dict[int, tuple[str, datetime, datetime]] = {}
    tl = root / "delivery" / "fixtures" / "timeline.jsonl.gz"
    if timeline and tl.is_file():
        data.titles, calendar = _timeline(tl, t_start)
    if loads and calendar:
        base = min(calendar)
        for no, (name, s, e) in sorted(calendar.items()):
            n = no - base
            if s >= t_start:
                drop("sprint_load")
                continue
            data.sprints.append(SprintRow(n=n, name=name, start=s, end=e, load=loads.get(n)))
        data.present.add("sprint_load")
    path = ds / "monitoring_daily.csv"
    if path.is_file():
        data.present.add("monitoring_daily")
        for r in _csv(path):
            d = parse_date(r["date"])
            if d is None:
                continue
            if d >= data.anchor:
                drop("monitoring_daily")
                continue
            data.monitoring.append(
                MonitoringRow(
                    date=d,
                    service=r.get("service", ""),
                    p95_ms=_f(r.get("p95_ms")),
                    availability=_f(r.get("availability")),
                    downtime_minutes=_f(r.get("downtime_minutes")),
                    error_rate=_f(r.get("error_rate")),
                )
            )
    path = ds / "nps_responses.csv"
    if path.is_file():
        data.present.add("nps_responses")
        for r in _csv(path):
            d = parse_date(r["date"])
            if d is None or not (r.get("score") or "").strip():
                continue
            if d >= data.anchor:
                drop("nps_responses")
                continue
            data.nps.append(
                NpsRow(
                    date=d,
                    response_id=r["response_id"],
                    client=r.get("client", ""),
                    contact=r.get("contact", ""),
                    survey=r.get("survey", ""),
                    score=int(float(r["score"])),
                )
            )
    return data
