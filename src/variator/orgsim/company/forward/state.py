"""The state of the company at T, read from the history delivery and the learned actor artifacts.

Nothing here reads the hidden truth (``truth.json``, ``spec.Truth``, ``habits.py``). Sources:

- ``<history>/delivery``: the timeline (the last version of each sim record before T), ``people.yaml``,
  ``sources.yaml`` and ``dashboard.json``;
- ``<history>/datasets``: flat tables of the same records (salaries, bank, invoices, taxes);
- ``<actors>``: the WP10 artifacts (``actors.json`` = who is there at T, ``habits.json`` and
  ``cohorts.json`` = learned parameters with intervals and source tags).

``StateAtT.snapshot_lines`` are the history lines that a branch delivery carries as its state at T:
the cause catalog, the vendor agreements, the contracts of the clients at T, the open invoices and
bills, the open deals and the last payroll. Their causes and references are cut to this set.
"""

from __future__ import annotations

import copy
import csv
import gzip
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from variator.orgsim.company.ukcal import company_calendar
from variator.orgsim.events import content_sha256

ID_RE = re.compile(r"^([A-Z]+(?:-[A-Z]+)?)-(\d+)$")
REF_FIELDS = ("contract", "settles", "records", "deal", "incident", "related")


def _jsonl_gz(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@dataclass
class PersonAtT:
    id: str
    name: str
    title: str
    role: str
    department: str
    manager: str | None
    seat: str
    start: date
    salary: float
    executive: bool
    login: str
    habits: dict[str, Any]


@dataclass
class ClientAtT:
    id: str
    name: str
    industry: str
    key: bool
    billing: str  # monthly | quarterly
    status: str  # active | notice | signed
    acv: float
    start: date | None
    end: date | None
    account_manager: str
    contract: str  # CTR-0001
    contract_fields: dict[str, Any]
    habits: dict[str, Any]
    contacts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class VendorAtT:
    id: str
    name: str
    kind: str  # nearshore, offshore, cloud, landlord, saas, professional, marketing
    agreement: str  # VA-VN-01
    monthly_fee: float
    terms_days: int
    cost_class: str
    invoice_prefix: str
    fee_source: str


@dataclass
class OpenInvoice:
    id: str
    fields: dict[str, Any]
    causes: list[dict[str, str]]
    direction: str  # receivable | payable


class StateAtT:
    """Everything the forward run starts from (read-only after ``load``)."""

    def __init__(self, history: str | Path, actors: str | Path):
        self.history = Path(history)
        self.actors_dir = Path(actors)
        d = self.history / "delivery"
        self.manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        self.sources = yaml.safe_load((d / "config" / "sources.yaml").read_text(encoding="utf-8"))
        self.people_yaml = yaml.safe_load((d / "config" / "people.yaml").read_text(encoding="utf-8"))
        self.dashboard = json.loads((d / "dashboard.json").read_text(encoding="utf-8"))
        hist = self.manifest.get("history") or {}
        self.anchor = date.fromisoformat(hist["anchor"])  # T: the first day after the history
        self.fork_date = self.anchor - timedelta(days=1)  # the last day of the history
        tz = self.sources["tenant"]["timezone"]
        self.tz_name = tz
        self.cal = company_calendar(tz, self.anchor.year - 1, self.anchor.year + 3)
        self.tenant = self.sources["tenant"]["id"]
        self.company = self.sources["tenant"]["name"]
        self.domain = next(s["mail_domain"] for s in self.sources["sources"] if s["provider"] == "email")
        # learned artifacts
        a = self.actors_dir
        self.actors = json.loads((a / "actors.json").read_text(encoding="utf-8"))
        self.habits = json.loads((a / "habits.json").read_text(encoding="utf-8"))
        cohorts = json.loads((a / "cohorts.json").read_text(encoding="utf-8"))
        self.cohorts = {c["id"]: c for c in cohorts["cohorts"]}
        self.risks = json.loads((a / "risk_register.json").read_text(encoding="utf-8"))
        self._timeline()
        self._directory()
        self._people()
        self._clients()
        self._vendors()
        self._finance()
        self._counters()

    # ------------------------------------------------------------------------------ timeline
    def _timeline(self) -> None:
        """The last sim line of each record before T (the history is already cut at T)."""
        path = self.history / "delivery" / "fixtures" / "timeline.jsonl.gz"
        self.sim_last: dict[str, dict[str, Any]] = {}
        self.sim_types: dict[str, str] = {}
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for x in fh:
                if '"source":"sim"' not in x:
                    continue
                ln = json.loads(x)
                if ln["source"] != "sim":
                    continue
                self.sim_last[ln["external_id"]] = ln
                self.sim_types[ln["external_id"]] = ln["object_type"]

    def fields(self, rid: str) -> dict[str, Any]:
        return self.sim_last[rid]["payload"]["fields"]

    # ------------------------------------------------------------------------------ directory
    def _directory(self) -> None:
        py = self.people_yaml
        self.dir_people = {p["id"]: p for p in py["people"]}
        self.orgs = {o["id"]: o for o in py["organizations"]}
        self.contacts_by_org: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in py["contacts"]:
            self.contacts_by_org[c["org"]].append(c)
        self.names = {p["id"]: p["name"] for p in py["people"]}
        self.names.update({o["id"]: o["name"] for o in py["organizations"]})
        self.names.update({c["id"]: c["name"] for c in py["contacts"]})

    # ------------------------------------------------------------------------------ actors
    def _people(self) -> None:
        self.people: dict[str, PersonAtT] = {}
        self.left: list[str] = []
        execs = {"ceo", "finance", "delivery"}
        for x in self.actors["actors"]:
            if x["kind"] != "person":
                continue
            if x.get("status_at_t") != "active":
                self.left.append(x["id"])
                continue
            email = self.dir_people[x["id"]]["email"]
            self.people[x["id"]] = PersonAtT(
                id=x["id"],
                name=x["name"],
                title=x["title"],
                role=x["role"],
                department=x["department"],
                manager=x.get("manager"),
                seat=x.get("seat") or x["id"],
                start=date.fromisoformat(x["start"]),
                salary=float(x["salary_at_t"]),
                executive=bool(x.get("executive")) or x["role"] in execs,
                login=email.split("@")[0],
                habits=self.habits["people"].get(x["id"], {}),
            )

    def _clients(self) -> None:
        self.clients: dict[str, ClientAtT] = {}
        for x in self.actors["actors"]:
            if x["kind"] != "client" or x.get("status_at_t") not in ("active", "notice", "signed"):
                continue
            ctr = next((k[4:] for k in x.get("kb_ids", []) if k.startswith("sim.CTR-")), None)
            if ctr is None or ctr not in self.sim_last:
                continue
            f = self.fields(ctr)
            acv = float(x.get("arr_at_t") or 0.0) or float(f.get("amount") or 0.0)
            self.clients[x["id"]] = ClientAtT(
                id=x["id"],
                name=x["name"],
                industry=x.get("industry") or "",
                key=bool(x.get("key")),
                billing=x.get("billing") or f.get("billing_frequency") or "monthly",
                status=x["status_at_t"],
                acv=acv,
                start=date.fromisoformat(f["start_date"]) if f.get("start_date") else None,
                end=date.fromisoformat(f["end_date"]) if f.get("end_date") else None,
                account_manager=x.get("account_manager") or f.get("owner"),
                contract=ctr,
                contract_fields=f,
                habits=self.habits["clients"].get(x["id"], {}),
                contacts=list(self.contacts_by_org.get(x["id"], [])),
            )

    def _vendors(self) -> None:
        self.vendors: dict[str, VendorAtT] = {}
        bills = [ln for k, ln in self.sim_last.items() if k.startswith("BILL-")]
        last_bill: dict[str, dict[str, Any]] = {}
        for ln in sorted(bills, key=lambda ln: ln["payload"]["fields"]["issued_at"]):
            last_bill[ln["payload"]["fields"]["vendor"]] = ln["payload"]["fields"]
        for x in self.actors["actors"]:
            if x["kind"] != "vendor":
                continue
            vid = x["id"]
            org = self.orgs.get(vid, {})
            kind = (org.get("description") or "vendor").split(" ")[0]
            h = self.habits["vendors"].get(vid, {})
            fee = h.get("monthly_fee_now") or h.get("monthly_fee") or {}
            lb = last_bill.get(vid, {})
            prefix = (lb.get("vendor_invoice_number") or "VN-0000-0").rsplit("-", 2)[0]
            self.vendors[vid] = VendorAtT(
                id=vid,
                name=x["name"],
                kind=kind,
                agreement=f"VA-{vid}",
                monthly_fee=float(fee.get("point") or 0.0),
                terms_days=int(x.get("payment_terms_days") or 30),
                cost_class=lb.get("cost_class") or "opex",
                invoice_prefix=prefix,
                fee_source=fee.get("source") or "assumption:none",
            )
        self.cloud = next((v for v in self.vendors.values() if v.kind == "cloud"), None)
        self.bank = next(
            (o["id"] for o in self.people_yaml["organizations"] if "bank" in (o.get("description") or "")),
            None,
        )

    # ------------------------------------------------------------------------------ finance
    def _finance(self) -> None:
        ds = self.history / "datasets"
        self.open_invoices: list[OpenInvoice] = []
        for rid, ln in self.sim_last.items():
            if ln["object_type"] != "invoice":
                continue
            f = ln["payload"]["fields"]
            if f.get("status") == "paid":
                continue
            self.open_invoices.append(
                OpenInvoice(rid, f, ln["payload"]["causes"], f.get("direction", "receivable"))
            )
        self.open_invoices.sort(key=lambda x: (x.fields.get("issued_at", ""), x.id))
        bank = _csv(ds / "bank.csv")
        self.cash_at_t = float(bank[-1]["balance_after"]) if bank else 0.0
        payrolls = sorted(k for k in self.sim_last if self.sim_types[k] == "payroll")
        self.last_payroll = payrolls[-1] if payrolls else None
        taxes = [self.fields(k) | {"id": k} for k in self.sim_last if self.sim_types[k] == "tax_payment"]
        ct = sorted((t for t in taxes if t.get("tax") == "Corporation tax"), key=lambda t: t["due_date"])
        self.corp_tax = ct[-1] if ct else None
        self.open_taxes = [t for t in taxes if t.get("status") != "paid"]
        clouds = sorted(k for k in self.sim_last if self.sim_types[k] == "cloud_bill")
        self.last_cloud_bill = self.fields(clouds[-1]) | {"id": clouds[-1]} if clouds else None
        deals = self.cohorts.get("prospects", {}).get("pipeline_at_t", {}).get("ids", [])
        self.open_deals = [d[4:] for d in deals if d[4:] in self.sim_last]

    # ------------------------------------------------------------------------------ ids
    def _counters(self) -> None:
        """The highest number of each id prefix in the history (future ids continue after it)."""
        top: dict[str, tuple[int, int]] = {}
        for rid in self.sim_last:
            m = ID_RE.match(rid)
            if not m:
                continue
            prefix, num = m.group(1), m.group(2)
            n, w = top.get(prefix, (0, len(num)))
            top[prefix] = (max(n, int(num)), max(w, len(num)))
        self.id_top = top
        self.person_top = max(int(p[4:]) for p in self.dir_people if p.startswith("LW-E"))
        self.prospect_top = max((int(o[3:]) for o in self.orgs if re.fullmatch(r"PR-\d+", o)), default=0)

    # ------------------------------------------------------------------------------ snapshot
    def snapshot_ids(self) -> list[str]:
        """The history records a branch carries as its state at T."""
        out: list[str] = []
        out += sorted(k for k in self.sim_last if self.sim_types[k] in ("process", "rule"))
        out += sorted(v.agreement for v in self.vendors.values() if v.agreement in self.sim_last)
        out += sorted(c.contract for c in self.clients.values())
        out += [x.id for x in self.open_invoices]
        out += list(self.open_deals)
        if self.last_payroll:
            out.append(self.last_payroll)
        out += [t["id"] for t in self.open_taxes]
        seen: set[str] = set()
        return [x for x in out if not (x in seen or seen.add(x))]

    def snapshot_lines(self) -> list[dict[str, Any]]:
        """History lines of ``snapshot_ids``; causes and references outside the set are removed."""
        if not hasattr(self, "_snapshot"):
            self._snapshot = self._snapshot_lines()
        return copy.deepcopy(self._snapshot)

    def snapshot_line(self, rid: str) -> dict[str, Any]:
        """The snapshot line of one record (the run continues it from this state)."""
        if not hasattr(self, "_snap_by_id"):
            self._snap_by_id = {ln["external_id"]: ln for ln in self.snapshot_lines()}
        return self._snap_by_id[rid]

    def _snapshot_lines(self) -> list[dict[str, Any]]:
        keep = set(self.snapshot_ids())
        out = []
        for rid in self.snapshot_ids():
            ln = copy.deepcopy(self.sim_last[rid])
            p = ln["payload"]
            p["causes"] = [c for c in p["causes"] if c["id"] in keep]
            f = p["fields"]
            for k in REF_FIELDS:
                v = f.get(k)
                if isinstance(v, list):
                    vv = [x for x in v if isinstance(x, str) and x in keep]
                    if vv:
                        f[k] = vv
                    else:
                        f.pop(k, None)
                elif isinstance(v, str) and v not in keep:
                    f.pop(k, None)
            for k in ("event_id", "sequence", "version", "previous_event_id"):
                ln[k] = None
            ln.pop("content_sha256", None)
            ln["content_sha256"] = content_sha256(ln)
            out.append(ln)
        return out

    # ------------------------------------------------------------------------------ helpers
    def workdays(self, start: date, end: date) -> list[date]:
        return self.cal.workdays(start, end)

    def at(self, d: date, hhmm: str) -> datetime:
        return self.cal.at(d, hhmm)

    def param(self, group: str, name: str) -> dict[str, Any] | None:
        """A learned parameter ``{point, low, mode, high, n, source}`` of habits.json."""
        return (self.habits.get(group) or {}).get(name)

    def contact(self, cid: str, kind: int = 0) -> dict[str, Any] | None:
        cs = self.contacts_by_org.get(cid) or []
        return cs[kind] if len(cs) > kind else (cs[0] if cs else None)
