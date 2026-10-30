"""Read a generated history (``<dir>/delivery``, ``<dir>/datasets``) into indexes.

Read-only. Everything the planner needs: the timeline lines grouped by object, the
directory (people, contacts, organisations, availability), the company calendar and the
flat datasets. Times are aware datetimes; ``Base.local`` converts to the company zone.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from functools import cached_property
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from variator.orgsim.company.ukcal import company_calendar

DELIVERY = "delivery"
DATASETS = "datasets"


def parse_time(text: Any) -> datetime | None:
    """ISO 8601 with an offset, 'Z' or a Jira '+0000' offset."""
    if not text:
        return None
    s = str(text).replace("Z", "+00:00")
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?([+-]\d\d):?(\d\d)$", s)
    if m:
        s = f"{m.group(1)}{m.group(2)}:{m.group(3)}"
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


@dataclass
class Person:
    id: str
    name: str
    login: str
    role: str  # ceo, finance, delivery, pm, backend, ...
    title: str
    groups: list[str]
    principal: str
    start: date
    end: date | None
    email: str

    @property
    def first(self) -> str:
        return self.name.split()[0]

    @property
    def account(self) -> str:
        return f"712020:{self.login}"


@dataclass
class Contact:
    id: str
    name: str
    org: str
    role: str
    email: str

    @property
    def first(self) -> str:
        parts = self.name.replace("Dr. ", "").split()
        return parts[0]


@dataclass
class Issue:
    key: str
    lines: list[dict[str, Any]] = field(default_factory=list)
    ats: list[datetime] = field(default_factory=list)

    def at(self, t: datetime) -> dict[str, Any] | None:
        """The payload as of t (the last version observed at or before t)."""
        k = bisect_right(self.ats, t)
        return self.lines[k - 1]["payload"] if k else None

    @property
    def last(self) -> dict[str, Any]:
        return self.lines[-1]["payload"]

    @property
    def first_at(self) -> datetime:
        return self.ats[0]

    @property
    def last_at(self) -> datetime:
        return self.ats[-1]

    def status_at(self, t: datetime) -> str | None:
        p = self.at(t)
        return p["fields"]["status"]["name"] if p else None

    def done_at(self) -> datetime | None:
        """The time of the last transition to Done (None if not done at the end)."""
        f = self.last["fields"]
        if f["status"]["name"] not in ("Done", "Won't Do"):
            return None
        hist = [h for h in self.last["changelog"]["histories"] if h["items"][0]["field"] == "status"]
        return parse_time(hist[-1]["created"]) if hist else None


class Base:
    """A history directory (read-only)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        d = self.root / DELIVERY
        self.manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        self.sources = yaml.safe_load((d / "config" / "sources.yaml").read_text(encoding="utf-8"))
        self.people_yaml = yaml.safe_load((d / "config" / "people.yaml").read_text(encoding="utf-8"))
        self.dashboard = json.loads((d / "dashboard.json").read_text(encoding="utf-8"))
        self.cases = [
            json.loads(x) for x in (d / "cases.jsonl").read_text(encoding="utf-8").splitlines() if x
        ]
        self.gold = [
            json.loads(x) for x in (d / "gold_links.jsonl").read_text(encoding="utf-8").splitlines() if x
        ]
        with gzip.open(d / "fixtures" / "timeline.jsonl.gz", "rt", encoding="utf-8") as fh:
            self.lines: list[dict[str, Any]] = [json.loads(x) for x in fh if x.strip()]
        self.tenant = self.sources["tenant"]["id"]
        self.company = self.sources["tenant"]["name"]
        self.tz = ZoneInfo(self.sources["tenant"]["timezone"])
        hist = self.manifest.get("history") or {}
        self.anchor = date.fromisoformat(hist.get("anchor") or self.lines[-1]["at"][:10])
        self.start = date.fromisoformat(hist.get("start") or self.lines[0]["at"][:10])
        self.cal = company_calendar(
            self.sources["tenant"]["timezone"], self.start.year - 1, self.anchor.year + 1
        )
        self.end = datetime.combine(self.anchor, time(0, 0), self.tz)  # T: nothing at or after it
        self.src = {s["provider"]: s["id"] for s in self.sources["sources"]}
        self.base_urls = {s["provider"]: s.get("base_url") for s in self.sources["sources"]}
        self.domain = next(s["mail_domain"] for s in self.sources["sources"] if s["provider"] == "email")
        self._directory()
        self._index()

    # ------------------------------------------------------------------------------ directory
    def _directory(self) -> None:
        roles = {r["id"]: r["role"] for r in self.dataset("people")}
        self.people: dict[str, Person] = {}
        for p in self.people_yaml["people"]:
            self.people[p["id"]] = Person(
                id=p["id"],
                name=p["name"],
                login=p["email"].split("@")[0],
                role=roles.get(p["id"], ""),
                title=p["role"],
                groups=list(p.get("groups") or []),
                principal=p["principal"],
                start=date.fromisoformat(p["start_date"]),
                end=date.fromisoformat(p["end_date"]) if p.get("end_date") else None,
                email=p["email"],
            )
        self.contacts: dict[str, Contact] = {
            c["id"]: Contact(c["id"], c["name"], c["org"], c.get("role") or "", c["email"])
            for c in self.people_yaml["contacts"]
        }
        self.orgs: dict[str, dict[str, Any]] = {o["id"]: o for o in self.people_yaml["organizations"]}
        self.absences: dict[str, list[tuple[date, date]]] = {}
        for a in self.people_yaml.get("availability") or []:
            self.absences.setdefault(a["person"], []).append(
                (date.fromisoformat(str(a["from"])), date.fromisoformat(str(a["to"])))
            )

    def employed(self, pid: str, d: date) -> bool:
        p = self.people[pid]
        return p.start <= d and (p.end is None or d <= p.end)

    def absent(self, pid: str, d: date) -> bool:
        return any(a <= d <= b for a, b in self.absences.get(pid, []))

    def present(self, pid: str, d: date) -> bool:
        return self.cal.is_workday(d) and self.employed(pid, d) and not self.absent(pid, d)

    def by_role(self, role: str, d: date | None = None) -> list[str]:
        return [
            pid
            for pid, p in sorted(self.people.items())
            if p.role == role and (d is None or self.employed(pid, d))
        ]

    def head(self, role: str) -> str:
        return self.by_role(role)[0]

    def org_name(self, oid: str) -> str:
        return (self.orgs.get(oid) or {}).get("name", oid)

    def contacts_of(self, oid: str) -> list[Contact]:
        return [c for c in self.contacts.values() if c.org == oid]

    # ------------------------------------------------------------------------------ timeline
    def _index(self) -> None:
        self.issues: dict[str, Issue] = {}
        self.sim: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.mails: dict[str, dict[str, Any]] = {}
        self.mail_by_mid: dict[str, dict[str, Any]] = {}
        self.pages: list[dict[str, Any]] = []
        self.mrs: dict[int, list[dict[str, Any]]] = {}
        for ln in self.lines:
            src = ln["source"]
            if src == self.src["jira"]:
                it = self.issues.setdefault(ln["external_id"], Issue(ln["external_id"]))
                it.lines.append(ln)
                it.ats.append(parse_time(ln["at"]))
            elif src == self.src["sim"]:
                self.sim.setdefault((ln["object_type"], ln["external_id"]), []).append(ln)
            elif src == self.src["email"]:
                self.mails[ln["payload"]["id"]] = ln
                self.mail_by_mid[ln["payload"]["internetMessageId"]] = ln
            elif src == self.src["confluence"]:
                self.pages.append(ln)
            elif src == self.src["gitlab"]:
                self.mrs.setdefault(ln["payload"]["iid"], []).append(ln)

    def records(self, otype: str) -> dict[str, list[dict[str, Any]]]:
        """sim records of a type: id -> versions (payloads), in time order."""
        return {k[1]: [ln["payload"] for ln in v] for k, v in self.sim.items() if k[0] == otype}

    def record_at(self, otype: str, rid: str, t: datetime) -> dict[str, Any] | None:
        best = None
        for ln in self.sim.get((otype, rid), []):
            if parse_time(ln["at"]) <= t:
                best = ln["payload"]
        return best

    def sim_line_last(self, otype: str, rid: str) -> dict[str, Any]:
        return self.sim[(otype, rid)][-1]

    def entity_first_at(self) -> dict[str, datetime]:
        out: dict[str, datetime] = {}
        for ln in self.lines:
            out.setdefault(self.entity_of(ln), parse_time(ln["at"]))
        return out

    def entity_of(self, ln: dict[str, Any]) -> str:
        src = ln["source"]
        if src == self.src["jira"]:
            return f"jira.{ln['external_id']}"
        if src == self.src["confluence"]:
            return f"confluence.page-{ln['external_id']}"
        if src == self.src["email"]:
            return f"mail.{ln['payload']['id']}"
        if src == self.src["gitlab"]:
            return f"gitlab.{self.gitlab_slug}.mr-{ln['payload']['iid']}"
        return f"sim.{ln['external_id']}"

    @cached_property
    def gitlab_slug(self) -> str:
        proj = next(iter(next(s for s in self.sources["sources"] if s["provider"] == "gitlab")["containers"]))
        return proj.replace("/", "-")

    @cached_property
    def gitlab_project(self) -> str:
        return next(iter(next(s for s in self.sources["sources"] if s["provider"] == "gitlab")["containers"]))

    # ------------------------------------------------------------------------------ urls
    def issue_url(self, key: str) -> str:
        return f"{self.base_urls['jira']}/browse/{key}"

    def page_url(self, space: str, page_id: str, frag: str | None = None) -> str:
        return f"{self.base_urls['confluence']}/spaces/{space}/pages/{page_id}" + (f"#{frag}" if frag else "")

    def mr_url(self, iid: int) -> str:
        return f"{self.base_urls['gitlab']}/{self.gitlab_project}/-/merge_requests/{iid}"

    # ------------------------------------------------------------------------------ time
    def local(self, t: datetime) -> datetime:
        return t.astimezone(self.tz)

    def at(self, d: date, hh: int, mm: int = 0) -> datetime:
        hh, mm = hh + mm // 60, mm % 60
        return datetime.combine(d, time(hh, mm), self.tz)

    def next_workday(self, d: date, n: int = 1) -> date:
        return self.cal.add_business_days(d, n)

    def workday_on_or_after(self, d: date) -> date:
        return self.cal.roll_forward(d)

    def workday_on_or_before(self, d: date) -> date:
        return self.cal.roll_backward(d)

    # ------------------------------------------------------------------------------ datasets
    def dataset(self, name: str) -> list[dict[str, Any]]:
        cache = self.__dict__.setdefault("_datasets", {})
        if name in cache:
            return cache[name]
        ds = self.root / DATASETS
        if (ds / f"{name}.csv").is_file():
            with (ds / f"{name}.csv").open(encoding="utf-8") as fh:
                rows: list[dict[str, Any]] = list(csv.DictReader(fh))
        elif (ds / f"{name}.jsonl.gz").is_file():
            with gzip.open(ds / f"{name}.jsonl.gz", "rt", encoding="utf-8") as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        else:
            rows = []
        cache[name] = rows
        return rows

    @cached_property
    def sprints(self) -> list[dict[str, Any]]:
        """The sprints of the Jira board (from the Sprint field), in time order, with their load."""
        seen: dict[int, dict[str, Any]] = {}
        for it in self.issues.values():
            for sp in it.last["fields"].get("customfield_10020") or []:
                seen[sp["id"]] = sp
        out = sorted(seen.values(), key=lambda s: s["startDate"])
        loads = {int(r["sprint"]): float(r["load"]) for r in self.dataset("sprint_load")}
        for n, sp in enumerate(out):
            sp = dict(sp)
            sp["load"] = loads.get(n)
            out[n] = sp
        return out

    def sprint_at(self, t: datetime) -> dict[str, Any] | None:
        for sp in self.sprints:
            if parse_time(sp["startDate"]) <= t < parse_time(sp["endDate"]):
                return sp
        return None
