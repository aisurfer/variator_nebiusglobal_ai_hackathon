"""Loader: the generated history as typed tables, cut at the as-of date T.

``load_history(root, as_of=None)`` reads ``<root>/datasets`` (CSV and JSON Lines) and
``<root>/delivery`` (directory, sources, optional timeline). Every row has a knowledge
time (``ts``); rows at or after T (midnight of T in the company time zone) are dropped.
Snapshot columns of the flat tables (``status_at_t``, ``acv_at_t``, ``last_day``,
``salary_at_t``, ``done_at``, ``assignee``) are never read: the state at T comes from the
versioned ``sim_*`` records and the Jira transitions.

``truth.json`` is never read here (it is for the recovery check only).
"""

from __future__ import annotations

import csv
import gzip
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import yaml

from variator.orgsim.calendar import BusinessCalendar
from variator.orgsim.company.ukcal import company_calendar

ENGINEER_ROLES = ("backend", "frontend", "devops", "qa")
SPRINT_DAYS = 10
_FIRST_JIRA = re.compile(r'"event_id":\s*"[^"]*jira-company:issue:[^"]*:1"')


# ---------------------------------------------------------------------------------------------- parsing
def parse_ts(x: Any) -> datetime | None:
    """An aware datetime from an ISO string (``Z`` or an offset); None for empty values."""
    if x is None or x == "":
        return None
    if isinstance(x, datetime):
        return x
    s = str(x).replace("Z", "+00:00")
    t = datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo("UTC"))
    return t


def parse_date(x: Any) -> date | None:
    if x is None or x == "":
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def _bool(x: str) -> bool:
    return str(x).strip().lower() in ("true", "1", "yes")


def _float(x: str) -> float | None:
    return None if x is None or x == "" else float(x)


def _int(x: str) -> int | None:
    return None if x is None or x == "" else int(float(x))


CSV_TYPES: dict[str, dict[str, Any]] = {
    "issues": {
        "points": _int,
        "created": parse_ts,
        "sprints": _int,
        "blocked_times": _int,
        "unplanned": _bool,
        "comments": _int,
    },
    "transitions": {"ts": parse_ts},
    "merge_requests": {"iid": _int, "created": parse_ts, "merged_at": parse_ts, "notes": _int},
    "emails": {"sent": parse_ts, "thread": _int},
    "bank": {"ts": parse_ts, "date": parse_date, "amount": _float, "balance_after": _float},
    "clients": {"key": _bool, "new": _bool},
    "people": {"start": parse_date},
    "incident_impact": {"date": parse_date, "downtime_minutes": _float},
    "sprint_load": {"sprint": _int, "load": _float},
    "arr_daily": {"date": parse_date, "arr": _float, "active_clients": _int},
    "cloud_cost_daily": {"date": parse_date},
    "metrics_weekly": {"week_start": parse_date, "published": parse_ts},
    "payroll_lines": {"gross": _float, "commission": _float, "employer_tax": _float},
}
# Columns that hold the state at the end of the history, not at T: never read.
SNAPSHOT_COLUMNS: dict[str, tuple[str, ...]] = {
    "issues": ("status", "done_at", "assignee", "sprints", "blocked_times", "comments"),
    "merge_requests": ("notes",),
    "clients": ("start", "end", "status_at_t", "acv_at_t"),
    "people": ("last_day", "salary_at_t"),
}


@dataclass
class Table:
    """Rows of one dataset with typed values; ``col`` gives one column as a numpy array."""

    name: str
    rows: list[dict[str, Any]]

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def col(self, name: str, dtype: Any = float) -> np.ndarray:
        return np.array([r.get(name) for r in self.rows], dtype=dtype)

    def where(self, **eq: Any) -> list[dict[str, Any]]:
        return [r for r in self.rows if all(r.get(k) == v for k, v in eq.items())]


def _read_csv(path: Path, name: str) -> list[dict[str, Any]]:
    types = CSV_TYPES.get(name, {})
    drop = SNAPSHOT_COLUMNS.get(name, ())
    out = []
    with path.open(newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            row: dict[str, Any] = {}
            for k, v in raw.items():
                if k in drop:
                    continue
                conv = types.get(k)
                if conv is not None:
                    row[k] = conv(v)
                else:
                    row[k] = _maybe_number(v)
            out.append(row)
    return out


def _maybe_number(v: str) -> Any:
    if v is None or v == "":
        return None
    try:
        if any(c in v for c in ".eE"):
            return float(v)
        return int(v)
    except ValueError:
        return v


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:  # type: ignore[operator]
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------------------------- model
@dataclass
class PersonInfo:
    """Static attributes of a person (from the directory) and the state at T."""

    id: str
    name: str
    title: str
    role: str
    department: str
    manager: str | None
    seat: str
    start: date
    hire_slot: str | None
    last_day: date | None = None  # known at T (notice given or left)
    left: bool = False  # the last day is before T
    salary: float | None = None  # in force at T (payroll and salary events)
    executive: bool = False


@dataclass
class ClientInfo:
    """Static attributes of a client and the contract state at T."""

    id: str
    name: str
    industry: str
    key: bool
    billing: str
    new: bool
    account_manager: str
    contract: str | None = None
    start: date | None = None
    end: date | None = None  # first day without the service (known at T)
    status: str = "active"  # active | notice | churned | signed
    acv: float = 0.0  # in force at T
    notice_at: date | None = None
    notice_days: int | None = None
    term_months: int | None = None
    term_end: date | None = None


@dataclass
class History:
    root: Path
    as_of: date
    t_end: datetime  # T 00:00 in the company time zone (exclusive bound)
    start: date  # the first day of the history
    tz: ZoneInfo
    cal: BusinessCalendar
    company: dict[str, Any]
    gitlab_slug: str
    tables: dict[str, Table]
    records: dict[str, list[dict[str, Any]]]  # sim type -> versions (ts < T), in time order
    people: dict[str, PersonInfo]
    clients: dict[str, ClientInfo]
    orgs: dict[str, dict[str, Any]]
    directory: dict[str, dict[str, Any]]  # people.yaml entries by id
    issue_text: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> summary, labels
    sprints: list[tuple[int, date, date]] = field(default_factory=list)  # (n, start, end exclusive)
    dropped: dict[str, int] = field(default_factory=dict)  # rows cut by the as-of date
    optional: dict[str, str] = field(default_factory=dict)  # kind -> dataset name (NPS, monitoring)

    # ------------------------------------------------------------------ helpers
    def table(self, name: str) -> Table:
        return self.tables.get(name) or Table(name, [])

    def latest(self, otype: str) -> dict[str, dict[str, Any]]:
        """The last version before T of each record of a sim type."""
        out: dict[str, dict[str, Any]] = {}
        for r in self.records.get(otype, []):
            out[r["id"]] = r
        return out

    def versions(self, otype: str) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self.records.get(otype, []):
            out[r["id"]].append(r)
        return out

    def local(self, t: datetime) -> datetime:
        return t.astimezone(self.tz)

    def workdays(self, a: date, b: date) -> list[date]:
        """Working days in [a, b)."""
        return self.cal.workdays(a, b)

    def bdays(self, a: date, b: date) -> int:
        return self.cal.business_days_between(a, b)

    def years(self, a: date, b: date) -> float:
        """Exposure in years of 252 working days."""
        return max(self.bdays(a, b), 0) / 252.0

    def kb(self, kind: str, ident: str) -> str:
        """The KB entity id of a record: sim.<id>, jira.<KEY>, mail.<id>, gitlab.<slug>.mr-<iid>."""
        if kind == "jira":
            return f"jira.{ident}"
        if kind == "mail":
            return f"mail.{ident}"
        if kind == "mr":
            return f"gitlab.{self.gitlab_slug}.mr-{ident}"
        return f"sim.{ident}"

    @property
    def window_days(self) -> list[date]:
        return self.workdays(self.start, self.as_of)


# ---------------------------------------------------------------------------------------------- loading
def _default_as_of(records: dict[str, list[dict[str, Any]]], tz: ZoneInfo) -> date:
    last = None
    for rows in records.values():
        for r in rows:
            t = r["ts"]
            if last is None or t > last:
                last = t
    if last is None:
        raise ValueError("the history has no records")
    return last.astimezone(tz).date() + timedelta(days=1)


def _sprints(cal: BusinessCalendar, start: date, end: date) -> list[tuple[int, date, date]]:
    """Two-week sprints from the first Monday (the first partial sprint is sprint 0)."""
    days = cal.workdays(start, end + timedelta(days=40))
    first_monday = next((i for i, d in enumerate(days) if d.weekday() == 0), 0)
    starts = (
        [0] + list(range(first_monday, len(days), SPRINT_DAYS))
        if first_monday
        else list(range(0, len(days), SPRINT_DAYS))
    )
    starts = sorted(set(starts))
    out = []
    for n, i in enumerate(starts):
        j = starts[n + 1] if n + 1 < len(starts) else min(i + SPRINT_DAYS, len(days) - 1)
        if days[i] >= end:
            break
        out.append((n, days[i], days[j]))
    return out


def load_history(root: str | Path, as_of: date | str | None = None, *, timeline: bool = True) -> History:
    """Read a generated history directory and cut it at ``as_of`` (default: the day after the last row)."""
    root = Path(root)
    ds = root / "datasets"
    if not ds.is_dir():
        raise FileNotFoundError(f"no datasets directory in {root}")
    dl = root / "delivery"
    sources = {}
    if (dl / "config" / "sources.yaml").is_file():
        sources = yaml.safe_load((dl / "config" / "sources.yaml").read_text(encoding="utf-8")) or {}
    tenant = sources.get("tenant", {})
    tzname = tenant.get("timezone", "Europe/London")
    tz = ZoneInfo(tzname)
    gitlab_slug = "larchwood-platform"
    for s in sources.get("sources", []):
        if s.get("provider") == "gitlab":
            conts = list((s.get("containers") or {}).keys())
            if conts:
                gitlab_slug = conts[0].replace("/", "-")

    # sim records: one row per version
    records: dict[str, list[dict[str, Any]]] = {}
    for p in sorted(ds.glob("sim_*.jsonl*")):
        otype = p.name[len("sim_") :].split(".")[0]
        rows = _read_jsonl(p)
        for r in rows:
            r["ts"] = parse_ts(r["ts"])
        rows.sort(key=lambda r: r["ts"])
        records[otype] = rows

    if as_of is None:
        T = _default_as_of(records, tz)
    else:
        T = parse_date(as_of)  # type: ignore[assignment]
    t_end = datetime.combine(T, time(0, 0), tzinfo=tz)
    dropped: dict[str, int] = {}

    def cut(name: str, rows: list[dict[str, Any]], key) -> list[dict[str, Any]]:
        keep = [r for r in rows if key(r) is not None and key(r) < t_end]
        if len(keep) != len(rows):
            dropped[name] = len(rows) - len(keep)
        return keep

    for otype in list(records):
        records[otype] = cut(f"sim_{otype}", records[otype], lambda r: r["ts"])

    first = min((r["ts"] for rows in records.values() for r in rows), default=t_end)
    start = first.astimezone(tz).date()
    cal = company_calendar(tzname, start.year - 3, T.year + 2)

    # flat tables
    tables: dict[str, Table] = {}
    for p in sorted(ds.glob("*.csv")):
        name = p.stem
        tables[name] = Table(name, _read_csv(p, name))

    def day_key(col: str):
        return lambda r: datetime.combine(r[col], time(0, 0), tzinfo=tz) if r.get(col) else None

    deploy_ts = {r["id"]: r["ts"] for r in records.get("deploy", [])}
    incident_ts: dict[str, datetime] = {}
    for r in records.get("incident", []):
        incident_ts.setdefault(r["id"], r["ts"])
    rules = {
        "issues": lambda r: r["created"],
        "transitions": lambda r: r["ts"],
        "merge_requests": lambda r: r["created"],
        "emails": lambda r: r["sent"],
        "bank": lambda r: r["ts"],
        "incident_impact": lambda r: incident_ts.get(r["incident"]),
        "arr_daily": lambda r: day_key("date")(r) + timedelta(days=1),
        "cloud_cost_daily": lambda r: day_key("date")(r) + timedelta(days=1),
        "metrics_weekly": lambda r: r["published"],
    }
    for name, key in rules.items():
        if name in tables:
            tables[name] = Table(name, cut(name, tables[name].rows, key))
    if "merge_requests" in tables:
        for r in tables["merge_requests"].rows:
            if r.get("merged_at") and r["merged_at"] >= t_end:
                r["merged_at"] = None
            if r.get("deploy") and (deploy_ts.get(r["deploy"]) is None):
                r["deploy"] = None
    if "payroll_lines" in tables:
        keep = []
        for r in tables["payroll_lines"].rows:
            y, m = (int(x) for x in str(r["period"]).split("-"))
            if date(y, m, 25) < T:
                keep.append(r)
        tables["payroll_lines"] = Table("payroll_lines", keep)
    if "metrics_monthly" in tables:
        keep = []
        for r in tables["metrics_monthly"].rows:
            y, m = (int(x) for x in str(r["month"]).split("-"))
            nxt = date(y + (m == 12), m % 12 + 1, 1)
            if nxt <= T:
                keep.append(r)
        tables["metrics_monthly"] = Table("metrics_monthly", keep)
    sprints = _sprints(cal, start, T)
    if "sprint_load" in tables:
        ends = {n: e for n, _, e in sprints}
        keep = [r for r in tables["sprint_load"].rows if ends.get(r["sprint"]) and ends[r["sprint"]] <= T]
        tables["sprint_load"] = Table("sprint_load", keep)

    # optional v2 datasets: NPS and monitoring
    optional: dict[str, str] = {}
    for name in list(tables) + [f"sim_{k}" for k in records]:
        low = name.lower()
        if "nps" in low:
            optional.setdefault("nps", name)
        elif any(x in low for x in ("monitor", "latency", "availability", "uptime")):
            optional.setdefault("monitoring", name)
    for name in optional.values():
        if name in tables:
            rows = tables[name].rows
            tkey = _time_column(rows)
            if tkey:
                tables[name] = Table(name, cut(name, rows, _row_time(tkey, tz)))

    # directory
    directory: dict[str, dict[str, Any]] = {}
    orgs: dict[str, dict[str, Any]] = {}
    pfile = dl / "config" / "people.yaml"
    if pfile.is_file():
        pdata = yaml.safe_load(pfile.read_text(encoding="utf-8")) or {}
        directory = {p["id"]: p for p in pdata.get("people", [])}
        orgs = {o["id"]: o for o in pdata.get("organizations", [])}

    h = History(
        root=root,
        as_of=T,
        t_end=t_end,
        start=start,
        tz=tz,
        cal=cal,
        company={"name": tenant.get("name", ""), "timezone": tzname, "tenant": tenant.get("id", "")},
        gitlab_slug=gitlab_slug,
        tables=tables,
        records=records,
        people={},
        clients={},
        orgs=orgs,
        directory=directory,
        sprints=[s for s in sprints if s[1] < T],
        dropped=dropped,
        optional=optional,
    )
    h.people = _people(h)
    h.clients = _clients(h)
    if timeline:
        h.issue_text = _issue_text(dl / "fixtures" / "timeline.jsonl.gz", t_end)
    return h


def _time_column(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    for k in ("ts", "responded_at", "submitted_at", "published", "date", "day", "week_start"):
        if k in rows[0]:
            return k
    return None


def _row_time(col: str, tz: ZoneInfo):
    def key(r: dict[str, Any]) -> datetime | None:
        v = r.get(col)
        if v is None or v == "":
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, date):
            return datetime.combine(v, time(0, 0), tzinfo=tz) + timedelta(days=1)
        s = str(v)
        if len(s) <= 10:
            return datetime.combine(date.fromisoformat(s), time(0, 0), tzinfo=tz) + timedelta(days=1)
        return parse_ts(s)

    return key


def _people(h: History) -> dict[str, PersonInfo]:
    """People employed at some time before T, with the state at T from the employee events."""
    out: dict[str, PersonInfo] = {}
    joined: dict[str, date] = {}
    last_day: dict[str, date] = {}
    left: set[str] = set()
    salary: dict[str, float] = {}
    for e in h.records.get("employee_event", []):
        pid = e.get("employee")
        if not pid:
            continue
        if e["kind"] == "employee_joined":
            joined[pid] = parse_date(e["effective_date"])  # type: ignore[assignment]
            if e.get("salary"):
                salary[pid] = float(e["salary"])
        elif e["kind"] == "resignation_received" and e.get("end_date"):
            last_day[pid] = parse_date(e["end_date"])  # type: ignore[assignment]
        elif e["kind"] == "employee_left":
            left.add(pid)
            last_day[pid] = parse_date(e["effective_date"])  # type: ignore[assignment]
        elif e["kind"] == "salary_changed" and e.get("salary"):
            eff = parse_date(e["effective_date"])
            if eff and eff < h.as_of:
                salary[pid] = float(e["salary"])
    lines: dict[str, list[float]] = defaultdict(list)
    for r in sorted(h.table("payroll_lines"), key=lambda r: str(r["period"])):
        lines[r["employee"]].append(float(r["gross"]))
    # the median of the last three months: a joiner or a leaver month is pro rata
    payroll = {pid: float(np.median(v[-3:])) * 12 for pid, v in lines.items()}
    for r in h.table("people"):
        pid = r["id"]
        start = joined.get(pid) or r["start"]
        if r.get("hire_slot") and pid not in joined:
            continue  # hired after T
        if start is None or start >= h.as_of:
            continue
        d = h.directory.get(pid, {})
        title = r.get("title") or d.get("role", "")
        out[pid] = PersonInfo(
            id=pid,
            name=r["name"],
            title=title,
            role=r["role"],
            department=r["department"],
            manager=d.get("manager"),
            seat=r.get("seat") or pid,
            start=start,
            hire_slot=r.get("hire_slot") or None,
            last_day=last_day.get(pid),
            left=pid in left,
            salary=salary.get(pid) or payroll.get(pid),
            executive=str(title).startswith(("Chief", "Head of")),
        )
    return out


def _clients(h: History) -> dict[str, ClientInfo]:
    """Clients with a contract record before T and the contract state at T."""
    contracts = h.latest("contract")
    by_client: dict[str, dict[str, Any]] = {}
    for c in contracts.values():
        if c.get("contract_type") == "vendor":
            continue
        by_client[c["client"]] = c
    out: dict[str, ClientInfo] = {}
    for r in h.table("clients"):
        c = by_client.get(r["id"])
        if c is None:
            continue
        status = c.get("status", "active")
        end = parse_date(c.get("end_date"))
        if status == "terminated" or (end is not None and end <= h.as_of):
            status = "churned"
        acv = 0.0
        for item in c.get("acv_schedule") or []:
            if parse_date(item["from"]) <= h.as_of:  # type: ignore[operator]
                acv = float(item["acv"])
        if status == "churned":
            acv = 0.0
        out[r["id"]] = ClientInfo(
            id=r["id"],
            name=r["name"],
            industry=r["industry"],
            key=bool(r["key"]),
            billing=r["billing"],
            new=bool(r["new"]),
            account_manager=r["account_manager"],
            contract=c["id"],
            start=parse_date(c.get("start_date")),
            end=end,
            status=status,
            acv=acv,
            notice_at=parse_date(c.get("notice_at")),
            notice_days=c.get("notice_days"),
            term_months=c.get("term_months"),
            term_end=parse_date(c.get("term_end")),
        )
    return out


def _issue_text(path: Path, t_end: datetime) -> dict[str, dict[str, Any]]:
    """Summary and labels of each Jira issue at its first version before T (from the timeline)."""
    out: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return out
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if not _FIRST_JIRA.search(line[:400]):
                continue
            o = json.loads(line)
            if o.get("source") != "jira-company" or o.get("version") != 1:
                continue
            at = parse_ts(o.get("world_at") or o.get("at"))
            if at is None or at >= t_end:
                continue
            fields = (o.get("payload") or {}).get("fields") or {}
            out[o["external_id"]] = {
                "summary": fields.get("summary", ""),
                "labels": list(fields.get("labels") or []),
                "description": fields.get("description") or "",
            }
    return out
