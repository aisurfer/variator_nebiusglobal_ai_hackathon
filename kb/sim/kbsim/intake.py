"""Intake of a corpus delivery: validation only (Docs/KB/05-CORPUS-CONTRACT.md §13).

    cd kb && PYTHONPATH=sim:app python -m kbsim.intake validate <dir> [--out <report dir>]

validate  the checks of part A of the checklist that can be made on the files (timeline, config, cases, gold
          links, dashboard, manifest); JSON and Markdown with the events and cases that fail. Errors break the
          contract; warnings are worth a look (a target of §6–7 missed, a late link).
(Plancy's convert-cases / load / report commands depend on the evaluation harness and the Plancy compose stack and
are not part of this copy; loading into the knowledge base is `python -m kb.cli seed`.)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from kbsim import contract as K
from kbsim.delivery import Readers, _payload_text, _ws
from kbsim.resolve import fact_in, norm

SIM_DIR = Path(__file__).resolve().parents[1]
KB_DIR = SIM_DIR.parent
RESERVED = {"kedr", "vectorforge"}
FILES = ("config/sources.yaml", "config/people.yaml", "fixtures/timeline.jsonl.gz", "cases.jsonl", "gold_links.jsonl",
         "dashboard.json", "manifest.json")
OBJECT_TYPES = {"jira": {"issue"}, "confluence": {"page"}, "email": {"message"},
                "gitlab": {"merge_request", "commit", "release", "issue"}, "directory": {"person"},
                "sim": set(K.SIM_OBJECT_TYPES)}
CONTAINERS = {"jira", "confluence", "gitlab"}
ACL_BY_CONTAINER_OR_LINE = {"sim"}  # a sim record is readable through its container's acl or its own acl
ORG_KINDS = {"client", "prospect", "vendor", "partner", "landlord", "government", "bank", "candidate", "other"}
ROLES = ("ceo", "sales", "engineering")
CATEGORY_MIN = {"lookup": 9, "why": 11, "history": 10, "as_of": 8, "conflict": 6, "stale": 6, "impact": 3,
                "multi_hop": 6, "acl": 8, "negative": 6, "implicit_ref": 10, "numeric": 10, "people": 7}
CASE_FIELDS = ("id", "category", "difficulty", "question", "principal", "evidence", "answer_facts", "expect_not_found")
# §7: per person per working day, ±50%
DENSITY = {"new tickets": 0.75, "status changes": 2.2, "ticket comments": 1.5, "meeting notes": 0.7, "e-mails": 1.3,
           "merge requests": 0.35}
TARGETS = {"tickets with a parent": 0.30, "tickets informed by a meeting": 0.05, "tickets implementing a decision": 0.05,
           "bugs caused by a merge request": 0.10, "tickets with a block": 0.03, "merge requests closing a ticket": 0.90}
MAX_ITEMS = 60
CALC = re.compile(r"^calc_")


# ================================================================================================ results
@dataclass
class Check:
    id: str
    title: str
    level: str  # error | warning
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    note: str | None = None
    measured: dict[str, Any] | None = None

    def add(self, **item: Any) -> None:
        self.total += 1
        if len(self.items) < MAX_ITEMS:
            self.items.append(item)

    @property
    def status(self) -> str:
        return "ok" if not self.total else ("fail" if self.level == "error" else "warn")


@dataclass
class Report:
    root: str
    tenant: str
    checks: list[Check] = field(default_factory=list)

    def check(self, cid: str, title: str, level: str = "error") -> Check:
        c = Check(cid, title, level)
        self.checks.append(c)
        return c

    @property
    def errors(self) -> int:
        return sum(c.total for c in self.checks if c.level == "error")

    @property
    def warnings(self) -> int:
        return sum(c.total for c in self.checks if c.level == "warning")

    def as_dict(self) -> dict[str, Any]:
        return {"delivery": self.root, "tenant": self.tenant, "errors": self.errors, "warnings": self.warnings,
                "failed_checks": [c.id for c in self.checks if c.status == "fail"],
                "warned_checks": [c.id for c in self.checks if c.status == "warn"],
                "checks": [{"id": c.id, "title": c.title, "level": c.level, "status": c.status, "count": c.total,
                            **({"measured": c.measured} if c.measured else {}), **({"note": c.note} if c.note else {}),
                            "items": c.items} for c in self.checks]}

    def markdown(self) -> str:
        L = [f"# Delivery check `{self.tenant}`", "",
             f"Directory: `{self.root}`. Errors: **{self.errors}**, warnings: **{self.warnings}**. "
             "An error breaks the contract (Docs/KB/05-CORPUS-CONTRACT.md §13, part A); a warning means a target was "
             "missed or a case is worth a look.", "",
             "| Check | Level | Result | Count |", "|---|---|---|---|"]
        mark = {"ok": "✓", "fail": "✗ error", "warn": "⚠ warning"}
        for c in self.checks:
            L.append(f"| `{c.id}` {c.title} | {c.level} | {mark[c.status]} | {c.total} |")
        for c in self.checks:
            if not c.total and not c.measured:
                continue
            L += ["", f"## `{c.id}` — {c.title}", ""]
            if c.note:
                L += [c.note, ""]
            if c.measured:
                L += ["Measured: " + ", ".join(f"{k} {v}" for k, v in c.measured.items()), ""]
            for it in c.items[:25]:
                L.append("- " + "; ".join(f"{k}: {v}" for k, v in it.items()))
            if c.total > 25:
                L.append(f"- … and {c.total - 25} more")
        return "\n".join(L) + "\n"


# ================================================================================================ loading
@dataclass
class Bundle:
    root: Path
    delivery: K.Delivery
    sources: dict[str, Any]
    people: dict[str, Any]
    cases: list[dict[str, Any]]
    gold: list[dict[str, Any]]
    dashboard: dict[str, Any] | None
    manifest: dict[str, Any] | None

    @classmethod
    def load(cls, root: Path) -> "Bundle":
        def jl(name: str) -> list[dict[str, Any]]:
            p = root / name
            return list(K.lines_of(p)) if p.is_file() else []

        def js(name: str) -> Any:
            p = root / name
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

        return cls(root, K.Delivery.load(root), yaml.safe_load((root / "config" / "sources.yaml").read_text("utf-8")),
                   yaml.safe_load((root / "config" / "people.yaml").read_text("utf-8")), jl("cases.jsonl"),
                   jl("gold_links.jsonl"), js("dashboard.json"), js("manifest.json"))


def _where(i: int, ln: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"line": i + 1, "at": ln.get("at"), "source": ln.get("source"), "object": ln.get("external_id"), **extra}


def _dt(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?([+-]\d\d)(\d\d)$", str(s))  # Jira 2026-06-22T…+0700
        return datetime.fromisoformat(f"{m.group(1)}{m.group(2)}:{m.group(3)}") if m else None


# ================================================================================================ directory helpers
class Directory:
    def __init__(self, people: dict[str, Any], sources: dict[str, Any]):
        self.people = people.get("people") or []
        self.contacts = people.get("contacts") or []
        self.orgs = people.get("organizations") or []
        self.by_account: dict[tuple[str, str], str] = {}
        for p in self.people + self.contacts:
            for src, acc in (p.get("accounts") or {}).items():
                self.by_account[(src, str(acc).strip().lower())] = p["id"]
        self.emails = {str(p.get("email") or "").strip().lower(): p for p in self.people + self.contacts if p.get("email")}
        self.employees = {p["id"]: p for p in self.people}
        self.mailboxes: dict[str, list[str]] = {}
        for s in sources.get("sources") or []:
            for box, groups in (s.get("group_mailboxes") or {}).items():
                self.mailboxes[str(box).strip().lower()] = list(groups or [])
        self.absences: dict[str, list[tuple[date, date]]] = defaultdict(list)
        for a in people.get("availability") or []:
            self.absences[a["person"]].append((date.fromisoformat(str(a["from"])), date.fromisoformat(str(a["to"]))))

    def person(self, source: str, account: Any) -> str | None:
        if account is None:
            return None
        return self.by_account.get((source, str(account).strip().lower()))

    def absent(self, person: str | None, day: date) -> bool:
        return any(a <= day <= b for a, b in self.absences.get(person or "", []))


def _addr(x: Any) -> str | None:
    a = ((x or {}).get("emailAddress") or {}).get("address")
    return str(a).strip().lower() if a else None


def accounts_of(provider: str, p: dict[str, Any]) -> list[tuple[str, Any]]:
    """(role, account) of every person a payload names (§4.2: each of them must be in people.yaml)."""
    out: list[tuple[str, Any]] = []
    if provider == "jira":
        f = p.get("fields") or {}
        for k in ("assignee", "reporter", "creator"):
            if f.get(k):
                out.append((k, f[k].get("accountId")))
        for c in (f.get("comment") or {}).get("comments") or []:
            out.append(("comment author", (c.get("author") or {}).get("accountId")))
        for h in (p.get("changelog") or {}).get("histories") or []:
            out.append(("changelog author", (h.get("author") or {}).get("accountId")))
    elif provider == "confluence":
        out.append(("author", p.get("authorId")))
        out.append(("version author", (p.get("version") or {}).get("authorId")))
        for n in K._walk(K.adf_of(p)):
            if n.get("type") == "mention":
                out.append(("mention", (n.get("attrs") or {}).get("id")))
    elif provider == "gitlab":
        for k in ("author",):
            if p.get(k):
                out.append((k, p[k].get("username")))
        for k in ("assignees", "reviewers"):
            for u in p.get(k) or []:
                out.append((k, u.get("username")))
        for n in p.get("notes") or []:
            out.append(("note author", (n.get("author") or {}).get("username")))
    return [(r, a) for r, a in out if a]


def actor_of(provider: str, ln: dict[str, Any], d: Directory) -> str | None:
    """The person whose action this line is (for availability): the newest history/comment/note, else the author."""
    p = ln.get("payload") or {}
    src = ln["source"]
    mod = _dt(ln.get("modified_at"))
    if provider == "jira":
        cands = [(_dt(h.get("created")), (h.get("author") or {}).get("accountId"))
                 for h in (p.get("changelog") or {}).get("histories") or []]
        cands += [(_dt(c.get("created")), (c.get("author") or {}).get("accountId"))
                  for c in ((p.get("fields") or {}).get("comment") or {}).get("comments") or []]
        cands = [(t, a) for t, a in cands if t and mod and abs((t - mod).total_seconds()) < 120]
        acc = cands[-1][1] if cands else ((p.get("fields") or {}).get("creator") or {}).get("accountId")
        return d.person(src, acc)
    if provider == "confluence":
        return d.person(src, (p.get("version") or {}).get("authorId") or p.get("authorId"))
    if provider == "email":
        a = _addr(p.get("from"))
        return d.person(src, a) or (d.emails.get(a or "") or {}).get("id")
    if provider == "sim":
        actor = (p.get("fields") or {}).get("actor") if isinstance(p.get("fields"), dict) else None
        return actor if isinstance(actor, str) and actor in d.employees else None
    if provider == "gitlab":
        notes = [n for n in p.get("notes") or [] if _dt(n.get("created_at")) and mod and
                 abs((_dt(n.get("created_at")) - mod).total_seconds()) < 120]
        if notes:
            return d.person(src, (notes[-1].get("author") or {}).get("username"))
        merged = _dt(p.get("merged_at"))
        if p.get("merged_by") and merged and mod and abs((merged - mod).total_seconds()) < 120:
            return d.person(src, (p.get("merged_by") or {}).get("username"))
        if ln.get("object_type") == "commit":
            return (d.emails.get(str(p.get("author_email") or "").lower()) or {}).get("id")
        return d.person(src, (p.get("author") or {}).get("username"))
    return None


# ================================================================================================ validate
def validate(root: Path, *, adapters: bool = True) -> Report:
    root = root.resolve()
    missing = [n for n in FILES if not (root / n).is_file()]
    rep = Report(str(root), "?")
    c = rep.check("files", "Every file of the delivery is present (§1)")
    for n in missing:
        c.add(file=n)
    if any(n in missing for n in ("config/sources.yaml", "config/people.yaml", "fixtures/timeline.jsonl.gz")):
        return rep
    b = Bundle.load(root)
    rep.tenant = str((b.sources.get("tenant") or {}).get("id") or "?")
    d = b.delivery
    dir_ = Directory(b.people, b.sources)
    readers = Readers(b.sources, b.people)
    is_sample = (b.manifest or {}).get("sample_of") is not None
    if not is_sample and not (root / "sample").is_dir():
        rep.check("sample", "There is a sample/ slice (§10)", "warning").add(msg="no sample/")

    _manifest_and_dashboard(rep, b, dir_)
    _format(rep, b, adapters=adapters)
    _history(rep, b, is_sample=is_sample)
    _rights(rep, b, dir_, readers)
    _directory(rep, b, dir_)
    _links(rep, b)
    _density(rep, b, dir_)
    _cases(rep, b, readers, is_sample=is_sample)
    _language(rep, b)
    _sim(rep, b)
    _exceptions(rep, b)
    return rep


def _exceptions(rep: Report, b: Bundle) -> None:
    """Items of case checks the delivery declares as known exceptions (manifest case_exceptions) become warnings."""
    declared = {(x.get("case"), x.get("check")): x.get("reason") for x in (b.manifest or {}).get("case_exceptions") or []}
    if not declared:
        return
    w = rep.check("declared_exceptions", "Exceptions to the §8.3 conditions declared in the manifest (with a reason)", "warning")
    for c in rep.checks:
        if c.level != "error":
            continue
        keep = [it for it in c.items if (it.get("case"), c.id) not in declared]
        moved = [it for it in c.items if (it.get("case"), c.id) in declared]
        for it in moved:
            w.add(check=c.id, case=it["case"], reason=" ".join(str(declared[(it["case"], c.id)]).split()))
        c.items = keep
        c.total -= len(moved)


def _manifest_and_dashboard(rep: Report, b: Bundle, d: Directory) -> None:
    m = b.manifest or {}
    c = rep.check("tenant_id", "The tenant id is new (not one of Plancy's tenants kedr, vectorforge)")
    if rep.tenant in RESERVED and not m.get("reference"):
        c.add(tenant=rep.tenant)
    elif rep.tenant in RESERVED:
        c.note = "Reference delivery (manifest.reference = true): a taken id is allowed."
    c = rep.check("manifest", "manifest.json: contract version, generator, seed, language, counters, licenses (§11)")
    for k in ("contract", "language", "generator", "seed", "counts", "checks", "commercial_use_allowed", "datasets",
              "legitimate_propagation"):
        if k not in m:
            c.add(field=k)
    if m and m.get("contract") != K.CONTRACT:
        c.add(field="contract", value=m.get("contract"), expected=K.CONTRACT)
    for name, sha in (m.get("files_sha256") or {}).items():
        p = b.root / name
        if p.is_file() and K and sha != _sha(p):
            c.add(file=name, msg="sha256 differs from the manifest")
    c = rep.check("dashboard", "dashboard.json: three roles, language, an engineering department of 4+ (§4.3–4.4)")
    dash = b.dashboard or {}
    roles = {r.get("id"): r for r in dash.get("roles") or []}
    for rid in ROLES:
        r = roles.get(rid)
        if not r:
            c.add(role=rid, msg="missing")
            continue
        pid = str(r.get("personId") or "").removeprefix("person.").upper()
        if pid not in {p["id"].upper() for p in d.people}:
            c.add(role=rid, personId=r.get("personId"), msg="not an employee of people.yaml")
    if dash and m.get("language") and dash.get("language") and dash["language"] != m["language"]:
        c.add(field="language", dashboard=dash["language"], manifest=m["language"])
    eng = dash.get("engineeringDepartment")
    if eng:
        n = sum(1 for p in d.people if p.get("department") == eng)
        if n < 4:
            c.add(field="engineeringDepartment", value=eng, people=n, msg="fewer than 4 people")
    elif dash:
        rep.check("dashboard_fields", "dashboard.json: engineeringDepartment, northStar (A2)", "warning").add(
            msg="engineeringDepartment not set")


def _sha(p: Path) -> str:
    import hashlib

    return hashlib.sha256(p.read_bytes()).hexdigest()


def _format(rep: Report, b: Bundle, *, adapters: bool) -> None:
    d = b.delivery
    fields = rep.check("line_fields", "Required fields of a timeline line (§2.1)")
    src = rep.check("source_declared", "The source and the object type are declared (§2.3)")
    order = rep.check("order", "The timeline is sorted by at; at ≥ modified_at (§2.1)")
    lag = rep.check("observe_lag", "at − modified_at is at most 5 minutes (§2.1)", "warning")
    ident = rep.check("identity", "external_id equals the id in the payload (§2.3)")
    prev = None
    for i, ln in enumerate(d.lines):
        for k in ("at", "source", "object_type", "external_id", "op", "modified_at"):
            if ln.get(k) in (None, ""):
                fields.add(**_where(i, ln, field=k))
        prov = d.sources.provider(ln.get("source", ""))
        if prov is None:
            src.add(**_where(i, ln, msg="source not in sources.yaml"))
            continue
        if ln.get("object_type") not in OBJECT_TYPES.get(prov, {ln.get("object_type")}):
            src.add(**_where(i, ln, msg=f"object_type {ln.get('object_type')!r} for {prov}"))
        if prov in CONTAINERS and not ln.get("container"):
            fields.add(**_where(i, ln, field="container"))
        if prov == "email" and not ln.get("acl"):
            fields.add(**_where(i, ln, field="acl", msg="mail needs its participants as acl"))
        if prov in ACL_BY_CONTAINER_OR_LINE and not ln.get("container") and ln.get("acl") is None:
            fields.add(**_where(i, ln, field="container", msg="a sim record needs a container or its own acl"))
        if ln.get("op") == "upsert" and not isinstance(ln.get("payload"), dict):
            fields.add(**_where(i, ln, field="payload"))
        if ln.get("op") == "delete" and ln.get("payload") is not None:
            fields.add(**_where(i, ln, field="payload", msg="delete must carry payload null"))
        at, mod = _dt(ln.get("at")), _dt(ln.get("modified_at"))
        if at and prev and at < prev:
            order.add(**_where(i, ln, msg=f"earlier than the previous line ({prev.isoformat()})"))
        if at and mod and at < mod:
            order.add(**_where(i, ln, msg=f"at < modified_at {ln.get('modified_at')}"))
        elif at and mod and at - mod > timedelta(minutes=5):
            lag.add(**_where(i, ln, msg=f"observed {at - mod} after the change"))
        prev = at or prev
        p = ln.get("payload") or {}
        if p:
            want = {"jira": p.get("key"), "confluence": p.get("id"), "email": p.get("internetMessageId"),
                    "sim": p.get("entity_id")}.get(prov)
            if want is not None and str(want) != str(ln.get("external_id")):
                ident.add(**_where(i, ln, payload_id=want))
    if adapters:
        _adapters(rep, b)


def _adapters(rep: Report, b: Bundle) -> None:
    c = rep.check("adapter", "Every payload is parsed by the adapter of its provider (§2.3)")
    try:
        from kb.connectors.base import AdapterContext, get_adapter, load_sources
        from kb.contracts import SourceEnvelope
    except Exception as exc:  # the backend is not importable (PYTHONPATH=app missing)
        c.level = "warning"
        c.add(msg=f"backend adapters not importable: {type(exc).__name__}")
        return
    ts = load_sources(b.root / "config" / "sources.yaml")
    for i, ln in enumerate(b.delivery.lines):
        s = ts.sources.get(ln.get("source"))
        if s is None:
            continue
        try:
            env = SourceEnvelope(tenant_id=ts.tenant_id, source_id=ln["source"], delivery_id=f"intake:{i}",
                                 operation=ln.get("op", "upsert"), object_type=ln["object_type"],
                                 external_id=str(ln["external_id"]), container=ln.get("container"),
                                 provider_version=str(ln["version"]) if ln.get("version") is not None else None,
                                 source_modified_at=_dt(ln.get("modified_at")) or _dt(ln["at"]),
                                 observed_at=_dt(ln["at"]), payload=ln.get("payload"), acl=ln.get("acl"))
            recs = get_adapter(s.provider).normalize(env, AdapterContext(ts.tenant_id, s, ts))
            if not recs:
                c.add(**_where(i, ln, msg="no records"))
        except Exception as exc:
            c.add(**_where(i, ln, msg=f"{type(exc).__name__}: {str(exc)[:160]}"))


def _history(rep: Report, b: Bundle, *, is_sample: bool = False) -> None:
    share_level = "warning" if is_sample else "error"  # shares of a whole company mean little on a slice
    d = b.delivery
    st = rep.check("status_changelog", "The ticket status = the last changelog transition (§2.2)")
    last: dict[str, dict[str, Any]] = {}
    passed_ip: dict[str, bool] = defaultdict(bool)
    reopened = 0
    for i, ln in enumerate(d.lines):
        prov = d.sources.provider(ln.get("source", ""))
        p = ln.get("payload") or {}
        if prov == "jira" and p:
            hist = [it for h in (p.get("changelog") or {}).get("histories") or [] for it in h.get("items") or []
                    if str(it.get("field")).lower() == "status"]
            status = ((p.get("fields") or {}).get("status") or {}).get("name")
            if hist and str(hist[-1].get("toString")) != str(status):
                st.add(**_where(i, ln, status=status, changelog=hist[-1].get("toString")))
            for it in hist:
                if norm(it.get("toString")) == "in progress":
                    passed_ip[ln["external_id"]] = True
                if norm(it.get("toString")) == "reopened":
                    reopened += 1
            last[ln["external_id"]] = p
        elif prov == "gitlab" and ln.get("object_type") == "merge_request" and p:
            last[f"mr:{ln['external_id']}"] = p
    tasks = {k: v for k, v in last.items() if not k.startswith("mr:")}
    done = [k for k, p in tasks.items() if ((p.get("fields") or {}).get("status") or {}).get("statusCategory", {}).get("key") == "done"
            and norm(((p.get("fields") or {}).get("resolution") or {}).get("name")) != "won't do"]
    share = sum(1 for k in done if passed_ip[k]) / len(done) if done else 1.0
    c = rep.check("in_progress_share", "≥ 80% of the closed tickets went through In Progress (§13)", share_level)
    c.measured = {"closed": len(done), "through In Progress": f"{share:.0%}", "reopened transitions": reopened,
                  "open at the end": len(tasks) - len(done)}
    if done and share < 0.8:
        c.add(share=f"{share:.0%}")
    w = rep.check("reopen_and_open", "There are Reopened transitions and open tickets (§2.2)", "warning")
    if tasks and not reopened:
        w.add(msg="no Reopened transitions")
    if tasks and len(tasks) == len(done):
        w.add(msg="every task is closed")
    mrs = [p for k, p in last.items() if k.startswith("mr:")]
    merged = [p for p in mrs if p.get("state") == "merged"]
    c = rep.check("mr_merged", "≥ 80% of the MRs are merged (§13)", share_level)
    c.measured = {"merge requests": len(mrs), "merged": f"{len(merged) / len(mrs):.0%}" if mrs else "—"}
    if mrs and len(merged) / len(mrs) < 0.8:
        c.add(share=f"{len(merged) / len(mrs):.0%}")
    c = rep.check("mr_merge_fields", "Merged MRs have merged_at and a merge commit (§13)")
    for p in merged:
        if not p.get("merged_at") or not (p.get("merge_commit_sha") or p.get("squash_commit_sha")):
            c.add(mr=p.get("iid"), msg="merged without merged_at or merge commit")


def _rights(rep: Report, b: Bundle, d: Directory, readers: Readers) -> None:
    dl = b.delivery
    cont = rep.check("containers", "Every container is declared in sources.yaml with an acl (§3)")
    declared = {(s["id"], k): (v or {}).get("acl") for s in b.sources.get("sources") or []
                for k, v in (s.get("containers") or {}).items()}
    for (sid, k), acl in declared.items():
        prov = dl.sources.provider(sid)
        if (prov in CONTAINERS or prov in ACL_BY_CONTAINER_OR_LINE) and not acl:
            cont.add(source=sid, container=k, msg="no acl")
    seen: set[tuple[str, str]] = set()
    for i, ln in enumerate(dl.lines):
        prov = dl.sources.provider(ln.get("source", ""))
        key = (ln.get("source"), ln.get("container"))
        sim_box = prov in ACL_BY_CONTAINER_OR_LINE and ln.get("container")
        if (prov in CONTAINERS or sim_box) and key not in declared and key not in seen:
            seen.add(key)
            cont.add(**_where(i, ln, container=ln.get("container"), msg="not declared"))
    mail = rep.check("mail_acl", "The acl of an e-mail = its employee participants (+ groups of group mailboxes) (§3)")
    principal = {p["id"]: p.get("principal") for p in d.people}
    for i, ln in enumerate(dl.lines):
        if dl.sources.provider(ln.get("source", "")) != "email" or not ln.get("payload"):
            continue
        p = ln["payload"]
        addrs = [a for a in [_addr(p.get("from"))] + [_addr(x) for x in (p.get("toRecipients") or []) +
                                                     (p.get("ccRecipients") or []) + (p.get("bccRecipients") or [])] if a]
        want: set[str] = set()
        for a in addrs:
            pid = d.person(ln["source"], a) or (d.emails.get(a) or {}).get("id")
            if pid in principal and principal[pid]:
                want.add(principal[pid])
            want.update(d.mailboxes.get(a, []))
        got = set(ln.get("acl") or [])
        if got != want:
            mail.add(**_where(i, ln, missing=sorted(want - got), extra=sorted(got - want)))
    closed = rep.check("closed_areas", "There are restricted areas (§3)", "warning")
    if declared and all("grp:all-staff" in (acl or []) for acl in declared.values()):
        closed.add(msg="every container is open to grp:all-staff")


def _directory(rep: Report, b: Bundle, d: Directory) -> None:
    dl = b.delivery
    acc = rep.check("accounts", "Every account of a payload is in people.yaml (§4.2)")
    seen: set[tuple[str, str]] = set()
    known = {"person": set(d.employees) | {str(c["id"]) for c in d.contacts},  # ids that sim payloads name
             "organization": {str(o["id"]) for o in d.orgs}}
    for i, ln in enumerate(dl.lines):
        prov = dl.sources.provider(ln.get("source", ""))
        p = ln.get("payload") or {}
        if not p:
            continue
        if prov == "email":
            for a in [_addr(p.get("from"))] + [_addr(x) for x in (p.get("toRecipients") or []) + (p.get("ccRecipients") or [])]:
                if a and (ln["source"], a) not in seen and not (d.person(ln["source"], a) or a in d.emails
                                                                 or a in d.mailboxes):
                    seen.add((ln["source"], a))
                    acc.add(**_where(i, ln, account=a, role="mail participant"))
            continue
        if prov == "sim":  # people and organisations by their people.yaml ids, not by accounts
            f = p.get("fields") if isinstance(p.get("fields"), dict) else {}
            for what, keys in (("person", K.SIM_PERSON_FIELDS), ("organization", K.SIM_ORG_FIELDS)):
                for k in keys:
                    for a in K.sim_ids(f.get(k)):
                        if a in known[what] or a.startswith(("person.", "org.")):
                            continue
                        if (ln["source"], a) not in seen:
                            seen.add((ln["source"], a))
                            acc.add(**_where(i, ln, account=a, role=f"{k} ({what})"))
            continue
        for role, a in accounts_of(prov or "", p):
            if (ln["source"], str(a)) not in seen and not d.person(ln["source"], a):
                seen.add((ln["source"], str(a)))
                acc.add(**_where(i, ln, account=a, role=role))
    org = rep.check("organizations", "Organisation kinds are from the list; domains match the contacts' mail (§4.2)")
    doms = {o["id"]: str(o.get("domain") or "").lower() for o in d.orgs}
    for o in d.orgs:
        if o.get("kind") not in ORG_KINDS:
            org.add(org=o["id"], kind=o.get("kind"))
    dom = rep.check("contact_domains", "The mail domain of a contact = the domain of its organisation", "warning")
    for ct in d.contacts:
        want = doms.get(ct.get("org"))
        got = str(ct.get("email") or "").lower().rpartition("@")[2]
        if want and got and got != want:
            dom.add(contact=ct["id"], email=ct.get("email"), org_domain=want)
    av = rep.check("availability", "Nobody acts on the days of their absence (§4.2)")
    for i, ln in enumerate(dl.lines):
        prov = dl.sources.provider(ln.get("source", ""))
        mod = _dt(ln.get("modified_at"))
        if not mod or not ln.get("payload"):
            continue
        who = actor_of(prov or "", ln, d)
        if who and d.absent(who, mod.date()):
            av.add(**_where(i, ln, person=who, day=mod.date().isoformat()))
    rp = rep.check("reply_order", "A reply comes after its parent, in the same thread (§2.3)")
    sent: dict[str, tuple[datetime | None, str | None]] = {}
    for i, ln in enumerate(dl.lines):
        if dl.sources.provider(ln.get("source", "")) != "email" or not ln.get("payload"):
            continue
        p = ln["payload"]
        t = _dt(p.get("sentDateTime"))
        sent[str(p.get("internetMessageId") or "").strip()] = (t, p.get("conversationId"))
        parent = p.get("inReplyTo")
        if parent and str(parent).strip() in sent:
            pt, pconv = sent[str(parent).strip()]
            if pt and t and t <= pt:
                rp.add(**_where(i, ln, msg=f"sent {t.isoformat()} not after the parent {pt.isoformat()}"))
            if pconv and p.get("conversationId") != pconv:
                rp.add(**_where(i, ln, msg="another conversationId than the parent"))


def _links(rep: Report, b: Bundle) -> None:
    dl = b.delivery
    first: dict[str, str] = {}
    for ln in dl.lines:
        eid = dl.entity_of(ln)
        if eid and ln.get("op") != "delete":
            first.setdefault(eid, ln["at"])
            for x in dl.derived(ln):
                first.setdefault(x, ln["at"])
    dang = rep.check("dangling", "Remote links, Closes, inReplyTo and page properties point at objects (§13)")
    late = rep.check("late_links", "A link to an object that appears later (late binding)", "warning")
    declared = dl.declared()
    for lk in declared:
        ln = dl.lines[lk.line]
        if lk.cause is None:
            dang.add(**_where(lk.line, ln, effect=lk.effect, predicate=lk.predicate, target=lk.target))
        elif first.get(lk.cause, "") > ln["at"]:
            late.add(**_where(lk.line, ln, effect=lk.effect, predicate=lk.predicate, cause=lk.cause,
                              cause_first=first.get(lk.cause)))
    # every remote link and page-property link into a source of the delivery, whatever its relationship
    known = {lk.target for lk in declared}
    for i, ln in enumerate(dl.lines):
        for u in dl.link_urls(ln):
            if u in known or not dl.inside(u):
                continue
            if not dl.known(dl.url_entity(u)):
                dang.add(**_where(i, ln, effect=dl.entity_of(ln), predicate="(link)", target=u))
        for k, target, eid in dl.sim_refs(ln):  # contract, settles, records, related…: other simulated objects
            if eid is None:
                dang.add(**_where(i, ln, effect=dl.entity_of(ln), predicate=f"({k})", target=target))
    by_key = {(lk.effect, lk.predicate, lk.cause): lk for lk in declared}
    gold = rep.check("gold_expressed", "Every gold link is expressed in its source the way expressed_as says (§6)")
    for n, g in enumerate(b.gold, 1):
        if g.get("expressed_as") not in K.EXPRESSED_AS:
            gold.add(row=n, msg=f"expressed_as {g.get('expressed_as')!r}")
            continue
        if g.get("explicit", True):
            lk = by_key.get((g["effect"], g["predicate"], g.get("cause")))
            if lk is None:
                gold.add(row=n, effect=g["effect"], predicate=g["predicate"], cause=g.get("cause"),
                         msg="not expressed in any payload")
            elif lk.expressed_as != g["expressed_as"]:
                gold.add(row=n, effect=g["effect"], msg=f"expressed as {lk.expressed_as}, not {g['expressed_as']}")
            continue
        if g["effect"] not in dl.entities or g.get("cause") not in dl.entities:
            gold.add(row=n, effect=g["effect"], cause=g.get("cause"), msg="unknown entity")
            continue
        if g.get("quote"):
            texts = [_payload_text(ln.get("payload")) for ln in dl.lines if dl.entity_of(ln) == g["effect"]]
            if not any(_ws(g["quote"]) in t for t in texts):
                gold.add(row=n, effect=g["effect"], quote=g["quote"], msg="the quote is not in the effect's text")
    noquote = sum(1 for g in b.gold if not g.get("explicit", True) and not g.get("quote"))
    if noquote:
        rep.check("gold_quotes", "Implicit links without a quote: the target cannot be checked against the text", "warning").add(count=noquote)
    _coverage(rep, b, declared)


def _coverage(rep: Report, b: Bundle, declared: list[K.Link]) -> None:
    """§6 targets of link coverage (warnings: a smaller or another kind of company may legitimately differ)."""
    dl = b.delivery
    issues: dict[str, dict[str, Any]] = {}
    mrs: set[str] = set()
    for ln in dl.lines:
        prov = dl.sources.provider(ln.get("source", ""))
        if prov == "jira" and ln.get("payload"):
            issues[f"jira.{ln['external_id']}"] = ln["payload"]
        if prov == "gitlab" and ln.get("object_type") == "merge_request":
            mrs.add(dl.entity_of(ln) or "")
    by_eff: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for lk in declared:
        by_eff[lk.effect].add((lk.predicate, (lk.cause or "")))
    tickets = [k for k, p in issues.items() if norm(((p.get("fields") or {}).get("issuetype") or {}).get("name")) != "epic"]
    bugs = [k for k in tickets if norm(((issues[k].get("fields") or {}).get("issuetype") or {}).get("name")) == "bug"]
    meetings = {lk.cause for lk in declared if lk.predicate == "decided_in" and lk.cause}

    def share(xs: list[str], pred: Callable[[str], bool]) -> float:
        return sum(1 for x in xs if pred(x)) / len(xs) if xs else 0.0

    got = {
        "tickets with a parent": share(tickets, lambda k: any(p == "part_of" for p, _ in by_eff[k])),
        "tickets informed by a meeting": share(tickets, lambda k: any(p == "informed_by" and c in meetings for p, c in by_eff[k])),
        "tickets implementing a decision": share(tickets, lambda k: any(p == "implements" for p, _ in by_eff[k])),
        "bugs caused by a merge request": share(bugs, lambda k: any(p == "caused_by" and ".mr-" in c for p, c in by_eff[k])),
        "tickets with a block": share(tickets, lambda k: any(p == "blocked_by" for p, _ in by_eff[k]) or
                                      any(p == "blocked_by" and c == k for e in by_eff for p, c in by_eff[e])),
        "merge requests closing a ticket": share(sorted(mrs), lambda k: any(p == "closes" for p, _ in by_eff[k])),
    }
    c = rep.check("coverage", "Link coverage is not below the targets (§6)", "warning")
    c.measured = {k: f"{v:.0%} (≥ {TARGETS[k]:.0%})" for k, v in got.items()}
    for k, v in got.items():
        if v < TARGETS[k]:
            c.add(target=k, measured=f"{v:.1%}", expected=f"≥ {TARGETS[k]:.0%}")
    implicit = sum(1 for g in b.gold if not g.get("explicit", True))
    if not implicit:
        rep.check("implicit_gold", "Implicit references are written into gold_links (explicit: false) (§6)").add(msg="none")


def _density(rep: Report, b: Bundle, d: Directory) -> None:
    dl = b.delivery
    if not dl.lines:
        return
    ats = [_dt(ln["at"]) for ln in dl.lines]
    days = {a.date() for a in ats if a and a.weekday() < 5}
    first, last = min(a for a in ats if a), max(a for a in ats if a)
    workdays = sum(1 for k in range((last.date() - first.date()).days + 1)
                   if (first.date() + timedelta(days=k)).weekday() < 5)
    people = len(d.people) or 1
    seen_tickets: set[str] = set()
    n: Counter = Counter()
    pages: set[str] = set()
    for ln in dl.lines:
        prov = dl.sources.provider(ln.get("source", ""))
        p = ln.get("payload") or {}
        if prov == "jira" and p:
            if ln["external_id"] not in seen_tickets:
                seen_tickets.add(ln["external_id"])
                n["new tickets"] += 1
        elif prov == "email":
            n["e-mails"] += 1
        elif prov == "gitlab" and ln.get("object_type") == "merge_request" and ln["external_id"] not in pages:
            pages.add(ln["external_id"])
            n["merge requests"] += 1
        elif prov == "confluence" and p and ln["external_id"] not in pages:
            labels = {str(x.get("name") or "").casefold() for x in ((p.get("labels") or {}).get("results") or [])}
            if labels & K.MEETING_LABELS:
                pages.add(ln["external_id"])
                n["meeting notes"] += 1
    last_issue: dict[str, dict[str, Any]] = {}
    for ln in dl.lines:
        if dl.sources.provider(ln.get("source", "")) == "jira" and ln.get("payload"):
            last_issue[ln["external_id"]] = ln["payload"]
    for p in last_issue.values():
        n["status changes"] += sum(1 for h in (p.get("changelog") or {}).get("histories") or [] for it in h.get("items") or []
                                   if str(it.get("field")).lower() == "status")
        n["ticket comments"] += len(((p.get("fields") or {}).get("comment") or {}).get("comments") or [])
    c = rep.check("density", "Density per person per working day within ±50% of the target (§7)", "warning")
    c.measured = {k: f"{n[k] / people / max(workdays, 1):.2f} ({v})" for k, v in DENSITY.items()}
    c.measured["people · workdays"] = f"{people} · {workdays} (active {len(days)})"
    for k, v in DENSITY.items():
        x = n[k] / people / max(workdays, 1)
        if not v * 0.5 <= x <= v * 1.5:
            c.add(object=k, per_person_day=round(x, 2), reference=v)
    ns = (b.dashboard or {}).get("northStar") or {}
    c = rep.check("north_star", "A weekly North Star report for every week of the period (§7)", "warning")
    if not ns:
        c.add(msg="dashboard.json has no northStar")
        return
    weeks: set[tuple[int, int]] = set()
    want_label = norm(ns.get("reportLabel") or "report")
    prop = K.norm_label(ns.get("property") or "")
    for ln in dl.lines:
        p = ln.get("payload") or {}
        if dl.sources.provider(ln.get("source", "")) != "confluence" or not p:
            continue
        labels = {norm(x.get("name")) for x in ((p.get("labels") or {}).get("results") or [])}
        if want_label in labels and any(K.norm_label(lbl) == prop for lbl, _ in K.page_properties(K.adf_of(p))):
            t = _dt(ln.get("modified_at"))
            if t:
                weeks.add(tuple(t.isocalendar())[:2])
    all_weeks = {tuple((first.date() + timedelta(days=k)).isocalendar())[:2] for k in range((last.date() - first.date()).days + 1)}
    c.measured = {"weeks with a report": len(weeks), "weeks in the period": len(all_weeks)}
    missing = sorted(all_weeks - weeks)
    if len(missing) > 1:  # the first or the last partial week may have none
        c.add(missing_weeks=", ".join(f"{y}-W{w:02d}" for y, w in missing[:20]))


def _cases(rep: Report, b: Bundle, readers: Readers, *, is_sample: bool) -> None:
    dl = b.delivery
    lines_of: dict[str, list[int]] = defaultdict(list)
    owner: dict[str, str] = {}
    for i, ln in enumerate(dl.lines):
        eid = dl.entity_of(ln)
        if eid:
            lines_of[eid].append(i)
            for x in dl.derived(ln):
                lines_of[x].append(i)
                owner[x] = eid
    text_cache: dict[int, str] = {}

    def text(i: int) -> str:
        if i not in text_cache:
            text_cache[i] = norm(_payload_text(dl.lines[i].get("payload")))
        return text_cache[i]

    def visible(principal: str, eid: str, known_at: str | None = None) -> bool:
        idx = [i for i in lines_of.get(eid, []) if not known_at or dl.lines[i]["at"] <= known_at]
        return any(readers(principal, dl.lines[i]) for i in idx)

    c = rep.check("case_fields", "Case fields (§8.1)")
    for n, cs in enumerate(b.cases, 1):
        for k in CASE_FIELDS:
            if k not in cs:
                c.add(case=cs.get("id", f"row {n}"), field=k)
    cat = rep.check("case_minimum", "At least the minimum per category (§8.2)", "warning" if is_sample else "error")
    got = Counter(cs.get("category") for cs in b.cases)
    scale = max(len(b.cases), 100) / 100
    for k, v in CATEGORY_MIN.items():
        if got[k] < int(v * scale) and not is_sample:
            cat.add(category=k, cases=got[k], minimum=int(v * scale))
    cat.measured = dict(sorted(got.items()))
    sp = rep.check("case_split", "The dev/holdout split is about 70/30 (§8.2)", "warning")
    splits = Counter(cs.get("split") for cs in b.cases)
    sp.measured = dict(splits)
    if b.cases and not is_sample and not 0.2 <= splits.get("holdout", 0) / len(b.cases) <= 0.4:
        sp.add(holdout=splits.get("holdout", 0), cases=len(b.cases))

    ent = rep.check("case_entities", "Every entity_id and version of a case exists in the timeline (§8.3)")
    vis = rep.check("case_visible", "The evidence is visible to the principal (except acl) (§8.3)")
    acl = rep.check("case_acl", "acl: the forbidden is invisible, at least one piece of evidence is visible (§8.3)")
    leak = rep.check("acl_facts", "Restricted facts do not occur in texts visible to the principal (except the manifest) (§3, §8.3)")
    facts = rep.check("case_facts", "Every answer fact is in the text of at least one piece of evidence (§8.3)")
    calc = rep.check("case_facts_computed", "Computed facts (calc_*): not in the text by construction", "warning")
    asof = rep.check("case_as_of", "as_of: the evidence was created before known_at (§8.3)")
    why = rep.check("case_why_path", "why: there is a path over gold_links from the evidence to the roots (§8.3)")
    neg = rep.check("case_negative", "negative: the decoys do not occur in the dataset (§8.3)")
    declared_prop = {(p.get("case"), p.get("fact")) for p in (b.manifest or {}).get("legitimate_propagation") or []}
    graph: dict[str, set[str]] = defaultdict(set)
    for g in b.gold:
        if g.get("cause"):
            graph[g["effect"]].add(g["cause"])
            graph[owner.get(g["effect"], g["effect"])].add(g["cause"])
    for cs in b.cases:
        cid, principal, known_at = cs.get("id"), cs.get("principal") or "user:admin", cs.get("known_at")
        parts = [("evidence", e) for e in cs.get("evidence") or []] + [("roots", e) for e in cs.get("roots") or []] + \
            [("forbidden", e) for e in cs.get("forbidden") or []]
        for part, e in parts:
            eid = e.get("entity_id")
            if eid not in dl.entities:
                ent.add(case=cid, part=part, entity_id=eid, msg="not in the timeline")
                continue
            v = e.get("version")
            if v is not None and int(v) > len(lines_of.get(eid, [])):
                ent.add(case=cid, part=part, entity_id=eid, version=v, msg=f"only {len(lines_of[eid])} deliveries")
        ev_ids = [e.get("entity_id") for e in cs.get("evidence") or [] if e.get("entity_id") in dl.entities]
        if cs.get("category") != "acl":
            for eid in ev_ids:
                if not visible(principal, eid):
                    vis.add(case=cid, principal=principal, entity_id=eid)
        else:
            for f in cs.get("forbidden") or []:
                if f.get("entity_id") in dl.entities and visible(principal, f["entity_id"]):
                    acl.add(case=cid, principal=principal, forbidden=f["entity_id"], msg="visible")
            if ev_ids and not any(visible(principal, e) for e in ev_ids):
                acl.add(case=cid, principal=principal, msg="no evidence is visible")
            for f in cs.get("forbidden") or []:
                banned = {f.get("entity_id"), *(f.get("also") or []), *(f.get("also_forbidden") or [])}
                for fact in f.get("facts") or []:
                    if (cid, fact.get("key")) in declared_prop:
                        continue
                    forms = [norm(x) for x in fact.get("any") or [] if len(norm(x)) >= 3]
                    for i, ln in enumerate(dl.lines):
                        if dl.entity_of(ln) in banned or not ln.get("payload") or not readers(principal, ln):
                            continue
                        hit = next((x for x in forms if x in text(i)), None)
                        if hit:
                            leak.add(**_where(i, ln, case=cid, fact=fact.get("key"), form=hit))
                            break
        ev_lines = sorted({i for e in cs.get("evidence") or [] for x in [e.get("entity_id"), *(e.get("also") or [])]
                           for i in lines_of.get(x, [])})
        for fact in cs.get("answer_facts") or []:
            if any(fact_in(fact.get("any") or [], text(i)) for i in ev_lines):
                continue
            (calc if CALC.match(str(fact.get("key"))) else facts).add(case=cid, fact=fact.get("key"),
                                                                      forms=" | ".join(fact.get("any") or []))
        if cs.get("category") == "as_of" and known_at:
            for eid in ev_ids:
                if not any(dl.lines[i]["at"] <= known_at for i in lines_of.get(eid, [])):
                    asof.add(case=cid, entity_id=eid, known_at=known_at, msg="created after known_at")
        if cs.get("category") == "why" and cs.get("roots"):
            roots = {r.get("entity_id") for r in cs["roots"]}
            starts = [owner.get(e, e) for e in ev_ids] + ev_ids
            if not _reaches(starts, roots, graph, owner):
                why.add(case=cid, evidence=", ".join(ev_ids), roots=", ".join(sorted(x for x in roots if x)))
        if cs.get("category") == "negative":
            m = re.search(r"keywords?:\s*(.+)$", cs.get("rationale") or "", re.I | re.S)
            for kw in [x.strip(" .«»\"'") for x in re.split(r"[,;]", m.group(1))] if m else []:
                if len(kw) >= 4:
                    k = norm(kw)
                    i = next((i for i in range(len(dl.lines)) if dl.lines[i].get("payload") and k in text(i)), None)
                    if i is not None:
                        neg.add(**_where(i, dl.lines[i], case=cid, keyword=kw))


def _reaches(starts: list[str], roots: set[str], graph: dict[str, set[str]], owner: dict[str, str], depth: int = 8) -> bool:
    frontier = set(starts)
    seen = set(frontier)
    for _ in range(depth):
        if frontier & roots or {owner.get(x, x) for x in frontier} & {owner.get(r, r) for r in roots}:
            return True
        nxt = set()
        for x in frontier:
            nxt |= graph.get(x, set()) | graph.get(owner.get(x, x), set())
        frontier = nxt - seen
        seen |= frontier
        if not frontier:
            return False
    return bool(frontier & roots)


_LAT = re.compile(r"[a-zA-Z]")


def _language(rep: Report, b: Bundle) -> None:
    """English only: a text where letters outside the Latin alphabet exceed 5% is in another language."""
    lang = (b.manifest or {}).get("language") or (b.dashboard or {}).get("language")
    c = rep.check("language", "Texts are in the tenant's language, English (§9)", "warning")
    if lang != "en":
        c.add(msg=f"language {lang!r} is not en")
        return
    other = 0
    total = 0
    sample: list[dict[str, Any]] = []
    for i, ln in enumerate(b.delivery.lines):
        if ln.get("object_type") == "commit" or not ln.get("payload"):
            continue  # commit messages are often English in any company
        t = _payload_text(ln["payload"])
        lat = len(_LAT.findall(t))
        foreign = sum(1 for ch in t if ch.isalpha() and not ch.isascii())
        if foreign + lat < 40:
            continue
        total += 1
        share = foreign / (foreign + lat)
        if share > 0.05:
            other += 1
            if len(sample) < 10:
                sample.append(_where(i, ln, non_latin=f"{share:.0%}"))
    c.measured = {"texts": total, "in another language": other}
    if total and other / total > 0.05:
        for s in sample:
            c.add(**s)


def _sim(rep: Report, b: Bundle) -> None:
    """§2.4: a sim line carries the simulator's event envelope with the required fields of its object type."""
    c = rep.check("sim_records", "Simulation records carry the event envelope and the fields of their type (§2.4)")
    for i, ln in enumerate(b.delivery.lines):
        if b.delivery.sources.provider(ln.get("source", "")) != "sim" or not isinstance(ln.get("payload"), dict):
            continue
        p = ln["payload"]
        f = p.get("fields")
        if not isinstance(f, dict) or not isinstance(p.get("causes", []), list) or not p.get("ts") or not p.get("kind"):
            c.add(**_where(i, ln, msg="not an event envelope {ts, kind, entity_id, fields{}, causes[]}"))
            continue
        bad = [x for x in p.get("causes") or [] if not isinstance(x, dict)
               or x.get("type") not in K.SIM_CAUSE_PREDICATES or not K.sim_ids(x.get("id"))]
        if bad:
            c.add(**_where(i, ln, msg=f"causes must be {{type: {'|'.join(K.SIM_CAUSE_PREDICATES)}, id}}: {bad[:3]}"))
        missing = [r for r in K.SIM_REQUIRED.get(ln.get("object_type"), ())
                   if all(f.get(k) in (None, "", [], {}) for k in r.split("|"))]
        if missing:
            c.add(**_where(i, ln, missing=", ".join(missing)))


# ================================================================================================ CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kbsim.intake", description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate")
    p.add_argument("dir", type=Path)
    p.add_argument("--out", type=Path, default=None, help="directory for validate.json and validate.md")
    p.add_argument("--no-adapters", action="store_true", help="skip parsing every payload with the backend adapters")
    args = ap.parse_args(argv)
    rep = validate(args.dir, adapters=not args.no_adapters)
    d = rep.as_dict()
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "validate.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        (args.out / "validate.md").write_text(rep.markdown(), encoding="utf-8")
    print(json.dumps({k: d[k] for k in ("delivery", "tenant", "errors", "warnings", "failed_checks", "warned_checks")},
                     ensure_ascii=False, indent=1))
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
