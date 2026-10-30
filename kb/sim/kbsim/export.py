"""Exporter (Docs/KB/03-SIMULATION.md §8): simulated world -> provider payloads -> replayable timeline.

    cd kb && PYTHONPATH=sim:app python -m kbsim.export company

Reads   sim/tenants/<t>/{company,scenario,cases}.yaml and sim/out/<t>/world.jsonl (WorldEvent per line, seq order)
Writes  config/tenants/<t>/sources.yaml, config/tenants/<t>/people.yaml   tenant config (sources, ACLs, directory)
        fixtures/<t>/timeline.jsonl.gz                                   one delivery per line, ordered by `at`
        sim/out/<t>/cases.resolved.json                                   cases with beats -> entity ids/versions
        sim/out/<t>/export.report.json                                    counts, warnings, id maps

Every change of an artefact becomes a FULL provider payload version, in the formats the backend adapters parse
(kb/app/kb/connectors/*): Jira REST v2 issue (+changelog, remotelinks), Confluence v2 page with an ADF body
(page-properties macro, decisionList/taskList, mentions), GitLab v4 merge request / commit, Microsoft-Graph-like
message with extracted attachments. Wiki pages of the world (wiki.created / wiki.updated) are skipped and counted in
the report: this copy of the backend has no XWiki connector. Timeline line:

    {at, source, object_type, external_id, container, op, modified_at, acl, version?, note, payload}

`at` = observed time (event time + 1 min, strictly non-decreasing), `modified_at` = event time, both ISO with the
tenant offset; `acl` is set for mail only (participants / group mailboxes), null elsewhere (container ACL).
When the backend package is importable (PYTHONPATH=app) every payload is also normalized by the real adapter:
entity ids and version numbers in cases.resolved.json then come from the adapters themselves, and adapter errors are
reported (the delivery stays in the timeline).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel

from kbsim import resolve as R
from kbsim.models import (
    Attachment, Cases, Commit, Company, Email, MergeRequest, MRComment, Page, Persona, Scenario, Ticket, TicketComment,
    WorldEvent,
)

log = logging.getLogger("kbsim.export")

SIM_DIR = Path(__file__).resolve().parents[1]  # kb/sim
KB_DIR = SIM_DIR.parent  # kb
UTC = timezone.utc
OBSERVE_DELAY = timedelta(minutes=1)


# ================================================================================================ paths / loading
@dataclass
class Paths:
    tenant: str
    sim_dir: Path = SIM_DIR
    kb_dir: Path = KB_DIR
    world_path: Path | None = None

    @property
    def tenant_dir(self) -> Path:
        return self.sim_dir / "tenants" / self.tenant

    @property
    def out_dir(self) -> Path:
        return self.sim_dir / "out" / self.tenant

    @property
    def world(self) -> Path:
        return self.world_path or self.out_dir / "world.jsonl"

    @property
    def config_dir(self) -> Path:
        return self.kb_dir / "config" / "tenants" / self.tenant

    @property
    def fixtures_dir(self) -> Path:
        return self.kb_dir / "fixtures" / self.tenant

    @property
    def timeline(self) -> Path:
        return self.fixtures_dir / "timeline.jsonl.gz"

    @property
    def resolved(self) -> Path:
        return self.out_dir / "cases.resolved.json"

    @property
    def report(self) -> Path:
        return self.out_dir / "export.report.json"


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_company(path: Path) -> Company:
    return Company.model_validate(_yaml(path))


def load_scenario(path: Path) -> Scenario | None:
    return Scenario.model_validate(_yaml(path)) if path.exists() else None


def load_cases(path: Path) -> Cases | None:
    return Cases.model_validate(_yaml(path)) if path.exists() else None


def read_world(path: Path) -> list[WorldEvent]:
    events = []
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(WorldEvent.model_validate_json(line))
            except Exception as exc:  # a broken line must not hide the rest of the world
                raise ValueError(f"{path}:{n}: {exc}") from exc
    return sorted(events, key=lambda e: e.seq)


def tenant_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours={"Europe/Moscow": 3}.get(name, 0)))


# ================================================================================================ time formats
class Clock:
    def __init__(self, tz: tzinfo):
        self.tz = tz

    def aware(self, dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=self.tz)

    def local(self, dt: datetime) -> datetime:
        return self.aware(dt).astimezone(self.tz)  # type: ignore[union-attr]

    def jira(self, dt: datetime) -> str:  # 2026-03-03T10:15:00.000+0300
        return self.local(dt).strftime("%Y-%m-%dT%H:%M:%S.000%z")

    def utc(self, dt: datetime) -> str:  # Confluence / GitLab: 2026-03-03T07:15:00.000Z
        return self.aware(dt).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # type: ignore[union-attr]

    def graph(self, dt: datetime) -> str:  # Microsoft Graph: 2026-03-03T07:15:00Z
        return self.aware(dt).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # type: ignore[union-attr]

    def iso(self, dt: datetime) -> str:  # timeline: ISO with the tenant offset
        return self.local(dt).isoformat()

    def zone_label(self) -> str:
        """Zone of a meeting time in the page properties. "+0700", not "+07:00" or "UTC+7": the Confluence adapter's
        parse_when reads "07:00" as an end time and "UTC" as the zone itself."""
        name = getattr(self.tz, "key", None) or str(self.tz)
        if name == "Europe/Moscow":
            return "MSK"
        return datetime(2026, 6, 1, tzinfo=self.tz).strftime("%z")


# ================================================================================================ directory
GROUP_NAMES = {
    "grp:all-staff": "All staff", "grp:all": "All staff", "grp:staff": "All staff",
    "grp:exec": "Management", "grp:management": "Management", "grp:leadership": "Management",
    "grp:board": "Board of directors",
    "grp:finance": "Finance and accounting", "grp:accounting": "Accounting",
    "grp:hr": "People operations", "grp:people": "People operations",
    "grp:legal": "Legal",
    "grp:sales": "Sales", "grp:commercial": "Commercial", "grp:account": "Account management",
    "grp:marketing": "Marketing", "grp:smm": "Social media", "grp:design": "Design",
    "grp:dev": "Engineering", "grp:engineering": "Engineering", "grp:tech": "Technology",
    "grp:backend": "Backend engineering", "grp:frontend": "Frontend engineering", "grp:mobile": "Mobile engineering",
    "grp:qa": "Quality assurance", "grp:devops": "DevOps",
    "grp:product": "Product", "grp:projects": "Project office", "grp:pmo": "Project office",
    "grp:delivery": "Project office", "grp:pm": "Project management",
    "grp:support": "Customer support", "grp:office": "Office", "grp:admin": "Administration",
    "grp:secretary": "Secretariat", "grp:clients": "Client projects",
}


def _login(email: str) -> str:
    return email.split("@", 1)[0]


def default_accounts(p: Persona, ids: R.Ids) -> dict[str, str]:
    """Source accounts of an employee: company.yaml values win, the rest is derived from the e-mail login."""
    login = _login(p.email)
    acc = {ids.provider_source(k): str(v) for k, v in p.accounts.items() if v}
    out = {
        ids.jira: acc.get(ids.jira) or f"712020:{login}",
        ids.confluence: acc.get(ids.confluence) or acc.get(ids.jira) or f"712020:{login}",
        ids.gitlab: acc.get(ids.gitlab) or login,
        ids.mail: (acc.get(ids.mail) or p.email).strip().lower(),  # email accounts are lower-cased addresses
    }
    for k, v in acc.items():
        out.setdefault(k, v)
    return out


def org_id(raw: str) -> str:
    return raw if raw.startswith("org-") else f"org-{raw}"


def contact_id(raw: str) -> str:
    return raw if raw.startswith("CT-") else f"CT-{raw}"


@dataclass
class Who:
    id: str
    name: str
    email: str | None
    accounts: dict[str, str] = field(default_factory=dict)
    principal: str | None = None
    employee: bool = False
    contact: bool = False
    mailbox: bool = False
    index: int = 0


class Directory:
    def __init__(self, company: Company, ids: R.Ids):
        self.ids = ids
        self.people: dict[str, Who] = {}
        self.by_email: dict[str, Who] = {}
        for i, p in enumerate(company.people):
            w = Who(p.id, p.name, p.email, default_accounts(p, ids), p.principal, employee=True, index=i)
            self.people[p.id] = w
            self.by_email[p.email.lower()] = w
        self.orgs = {o.id: o for o in company.organizations}
        for i, c in enumerate(company.contacts):
            w = Who(c.id, c.name, c.email, {ids.mail: c.email.strip().lower()}, contact=True, index=100 + i)
            self.people[c.id] = w
            self.by_email.setdefault(c.email.lower(), w)
        self.mailboxes: dict[str, list[str]] = {k.lower(): list(v) for k, v in company.tools.group_mailboxes.items()}

    def get(self, ref: str | None) -> Who | None:
        if not ref:
            return None
        if ref in self.people:
            return self.people[ref]
        low = ref.strip().lower()
        if low in self.by_email:
            return self.by_email[low]
        if low in self.mailboxes:
            return Who(low, _login(low), low, mailbox=True)
        if "@" in low:
            return Who(low, _login(low), low)
        return None

    def account(self, ref: str | None, source_id: str) -> str | None:
        w = self.get(ref)
        return w.accounts.get(source_id) if w else None

    def org_ref(self, oid: str | None) -> str | None:
        """documentMeta.counterparty: the organization id as listed in people.yaml ("org-translog")."""
        if not oid:
            return None
        return org_id(str(oid).strip())


# ================================================================================================ tenant config
def people_at(company: Company, first_event: datetime | None) -> datetime:
    """When the roster was recorded: the day before the simulation starts, and before the first event."""
    tz = tenant_tz(company.company.timezone)
    d = company.company.start_date - timedelta(days=1)
    at = datetime(d.year, d.month, d.day, 9, 0, tzinfo=tz)
    if first_event is not None:
        fe = first_event if first_event.tzinfo else first_event.replace(tzinfo=tz)
        at = min(at, fe - timedelta(hours=1))
    return at


def build_sources(company: Company, ids: R.Ids, first_event: datetime | None = None) -> dict[str, Any]:
    t, tools = company.company, company.tools

    def containers(d: dict) -> dict:
        return {k: {"name": c.name, "acl": list(c.acl)} for k, c in d.items()}

    return {
        "tenant": {"id": t.tenant_id, "name": t.name, "timezone": t.timezone,
                   "people_at": people_at(company, first_event).isoformat()},
        "sources": [
            {"id": ids.jira, "provider": "jira", "name": f"Jira Cloud — {t.short_name}", "base_url": ids.jira_base,
             "sync": {"mode": "webhook+reconcile", "reconcile_cron": "0 3 * * *"}, "containers": containers(tools.jira)},
            {"id": ids.confluence, "provider": "confluence", "name": f"Confluence Cloud — {t.short_name}",
             "base_url": ids.confluence_base, "sync": {"mode": "webhook+reconcile", "reconcile_cron": "30 3 * * *"},
             "containers": containers(tools.confluence)},
            {"id": ids.gitlab, "provider": "gitlab", "name": f"GitLab — {t.short_name}", "base_url": ids.gitlab_base,
             "sync": {"mode": "webhook+reconcile", "reconcile_cron": "0 4 * * *"},
             "jira_source": ids.jira, "jira_keys": list(tools.jira), "containers": containers(tools.gitlab)},
            {"id": ids.mail, "provider": "email", "name": f"Corporate mail — {tools.mail_domain}",
             "sync": {"mode": "graph-delta", "interval_min": 5}, "mail_domain": tools.mail_domain,
             "group_mailboxes": {k: list(v) for k, v in tools.group_mailboxes.items()}},
            {"id": ids.directory, "provider": "directory", "name": "People directory (HR roster)",
             "containers": {"people": {"name": "People", "acl": ["grp:all-staff"]}}},
        ],
    }


def _group_name(gid: str, members: list[Persona]) -> str:
    if gid in GROUP_NAMES:
        return GROUP_NAMES[gid]
    depts = {p.department for p in members}
    if len(depts) == 1 and members:
        return next(iter(depts))
    return gid.removeprefix("grp:").replace("-", " ").replace("_", " ").capitalize()


def own_organization(company: Company) -> dict[str, Any] | None:
    """Our company as an organization (it is the counterparty of outgoing documents), unless already listed."""
    t = company.company
    oid = org_id(t.tenant_id)
    if any(org_id(o.id) == oid for o in company.organizations):
        return None
    return {"id": oid, "name": t.name, "kind": "other", "domain": company.tools.mail_domain, "inn": None}


def load_availability(path: Path, company: Company) -> list[dict[str, Any]]:
    """sim/tenants/<t>/availability.yaml (persona -> [[from, to]]) as the contract's people.yaml list (05 §4.1)."""
    if not path.is_file():
        return []
    data = _yaml(path) or {}
    emp = {p.id: p.employee_id for p in company.people}
    out = []
    for persona, spans in sorted((data.get("absences") or {}).items()):
        for span in spans or []:
            out.append({"person": emp.get(persona, persona), "from": str(span[0]), "to": str(span[-1]),
                        "kind": "absence"})
    return out


ORG_KINDS = {"state": "government"}  # world kinds -> the contract's (05 §4.2)


def build_people(company: Company, ids: R.Ids, availability: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    tools = company.tools
    own = own_organization(company)
    groups: list[str] = []
    for p in company.people:
        groups += p.groups
    for d in (tools.jira, tools.confluence, tools.gitlab):
        for c in d.values():
            groups += [g for g in c.acl if g.startswith("grp:")]
    for gs in tools.group_mailboxes.values():
        groups += [g for g in gs if g.startswith("grp:")]
    groups = list(dict.fromkeys(["grp:all-staff", *groups]))
    return {
        "admins": ["user:admin"],
        "groups": {g: {"name": _group_name(g, [p for p in company.people if g in p.groups])} for g in groups},
        "people": [{"id": p.employee_id, "name": p.name, "role": p.role, "department": p.department, "email": p.email,
                    "principal": p.principal, "groups": list(p.groups), "accounts": default_accounts(p, ids)}
                   for p in company.people],
        "organizations": [{"id": org_id(o.id), "name": o.name, "kind": ORG_KINDS.get(o.kind, o.kind), "domain": o.domain,
                           "inn": o.inn}
                          for o in company.organizations] + ([own] if own else []),
        "contacts": [{"id": contact_id(c.id), "name": c.name, "role": c.role, "org": org_id(c.org), "email": c.email,
                      "accounts": {ids.mail: c.email.strip().lower()}} for c in company.contacts],
        **({"availability": availability} if availability else {}),
    }


def write_yaml(path: Path, data: Any, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
    path.write_text(header + text, encoding="utf-8")


# ================================================================================================ markup: markdown -> ADF
def _tx(text: str, marks: list[dict] | None = None) -> dict:
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def _link(href: str) -> dict:
    return {"type": "link", "attrs": {"href": href}}


_INLINE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|\[(?P<lt>[^\]\n]+)\]\((?P<lu>[^)\s]+)\)"
    r"|\*\*(?P<b>[^*\n]+?)\*\*"
    r"|__(?P<b2>[^_\n]+?)__"
    r"|~~(?P<s>[^~\n]+?)~~"
    r"|(?<![\w*])\*(?P<i>[^*\s](?:[^*\n]*?[^*\s])?)\*(?![\w*])"
    r"|(?P<url>https?://[^\s<>\[\]\"'«»]+)"
)
_TRAIL = ".,;:!?)"


def adf_inline(text: str) -> list[dict]:
    out: list[dict] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(_tx(text[pos:m.start()]))
        if m.group("code"):
            out.append(_tx(m.group("code")[1:-1], [{"type": "code"}]))
        elif m.group("lt"):
            out.append(_tx(m.group("lt"), [_link(m.group("lu"))]))
        elif m.group("b") or m.group("b2"):
            out.append(_tx(m.group("b") or m.group("b2"), [{"type": "strong"}]))
        elif m.group("s"):
            out.append(_tx(m.group("s"), [{"type": "strike"}]))
        elif m.group("i"):
            out.append(_tx(m.group("i"), [{"type": "em"}]))
        else:
            u = m.group("url")
            trail = ""
            while u and u[-1] in _TRAIL:
                trail, u = u[-1] + trail, u[:-1]
            out.append(_tx(u, [_link(u)]))
            if trail:
                out.append(_tx(trail))
        pos = m.end()
    if pos < len(text):
        out.append(_tx(text[pos:]))
    return [n for n in out if n.get("text")]


def _para(content: list[dict]) -> dict:
    return {"type": "paragraph", "content": content}


def _heading(text: str, level: int = 2) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": adf_inline(text) or [_tx(text)]}


_LIST = re.compile(r"^(?P<ind>[ \t]*)(?P<mark>[-*+]|\d{1,3}[.)])[ \t]+(?P<text>.*)$")
_HEAD = re.compile(r"^(?P<h>#{1,6})[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_TSEP = re.compile(r"^\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*\|?$")


@dataclass
class _Item:
    level: int
    ordered: bool
    text: str


def _list_items(lines: list[str]) -> list[_Item]:
    raw: list[tuple[int, bool, str]] = []
    for ln in lines:
        m = _LIST.match(ln)
        if m:
            ind = len(m.group("ind").replace("\t", "    "))
            raw.append((ind, m.group("mark")[0].isdigit(), m.group("text").strip()))
        elif raw:  # continuation of the previous item
            i, o, t = raw[-1]
            raw[-1] = (i, o, (t + " " + ln.strip()).strip())
    levels = sorted({i for i, _, _ in raw})
    return [_Item(levels.index(i), o, t) for i, o, t in raw if t]


def _build_list(items: list[_Item], i: int, level: int) -> tuple[dict, int]:
    ordered = items[i].ordered
    node: dict[str, Any] = {"type": "orderedList" if ordered else "bulletList", "content": []}
    if ordered:
        node["attrs"] = {"order": 1}
    while i < len(items) and items[i].level >= level:
        it = items[i]
        if it.level > level:
            sub, i = _build_list(items, i, it.level)
            if node["content"]:
                node["content"][-1]["content"].append(sub)
            else:
                node["content"].append({"type": "listItem", "content": [sub]})
            continue
        if it.ordered != ordered and node["content"]:
            break
        node["content"].append({"type": "listItem", "content": [_para(adf_inline(it.text) or [_tx(it.text)])]})
        i += 1
    return node, i


def _table(lines: list[str]) -> dict:
    rows: list[tuple[list[str], bool]] = []
    for ln in lines:
        s = ln.strip()
        if _TSEP.match(s):
            if rows:
                rows[-1] = (rows[-1][0], True)
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        rows.append((cells, False))
    width = max((len(c) for c, _ in rows), default=1)
    content = []
    for cells, header in rows:
        cells = cells + [""] * (width - len(cells))
        kind = "tableHeader" if header else "tableCell"
        content.append({"type": "tableRow", "content": [
            {"type": kind, "attrs": {}, "content": [_para(adf_inline(c))]} for c in cells]})
    return {"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "default"}, "content": content}


def md_to_adf_blocks(md: str) -> list[dict]:
    """Markdown (what agents write) -> ADF blocks: headings, paragraphs, lists, tables, code, quotes, rules."""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: list[dict] = []
    para: list[str] = []

    def flush() -> None:
        if not para:
            return
        content: list[dict] = []
        for k, ln in enumerate(para):
            if k:
                content.append({"type": "hardBreak"})
            content += adf_inline(ln)
        if content:
            out.append(_para(content))
        para.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        s = line.strip()
        if not s:
            flush()
            i += 1
            continue
        if s.startswith("```"):
            flush()
            lang = s[3:].strip()
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            node: dict[str, Any] = {"type": "codeBlock", "attrs": {"language": lang} if lang else {}}
            if body:
                node["content"] = [_tx("\n".join(body))]
            out.append(node)
            continue
        m = _HEAD.match(s)
        if m and m.group("text"):
            flush()
            out.append(_heading(m.group("text"), len(m.group("h"))))
            i += 1
            continue
        if _RULE.match(s):
            flush()
            out.append({"type": "rule"})
            i += 1
            continue
        if s.startswith("|"):
            flush()
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(_table(block))
            continue
        if s.startswith(">"):
            flush()
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            inner = md_to_adf_blocks("\n".join(quote))
            if inner:
                out.append({"type": "blockquote", "content": inner})
            continue
        if _LIST.match(line):
            flush()
            block = []
            while i < n and (_LIST.match(lines[i]) or (block and lines[i].startswith((" ", "\t")) and lines[i].strip())):
                block.append(lines[i])
                i += 1
            items = _list_items(block)
            j = 0
            while j < len(items):
                node, j = _build_list(items, j, items[j].level)
                out.append(node)
            continue
        para.append(s)
        i += 1
    flush()
    return out


# ------------------------------------------------------------------------------------------------ markdown -> Jira wiki
def _inline_sub(text: str, *, code: str, link: str, bold: str, italic: str, strike: str, escape: Any = None) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        plain = text[pos:m.start()]
        out.append(escape(plain) if escape else plain)
        if m.group("code"):
            out.append(code.format(m.group("code")[1:-1]))
        elif m.group("lt"):
            out.append(link.format(t=m.group("lt"), u=m.group("lu")))
        elif m.group("b") or m.group("b2"):
            out.append(bold.format(m.group("b") or m.group("b2")))
        elif m.group("s"):
            out.append(strike.format(m.group("s")))
        elif m.group("i"):
            out.append(italic.format(m.group("i")))
        else:
            out.append(m.group("url"))
        pos = m.end()
    rest = text[pos:]
    out.append(escape(rest) if escape else rest)
    return "".join(out)


def md_to_jira(md: str) -> str:
    """Markdown -> Jira wiki markup (REST API v2 text fields)."""
    def inline(t: str) -> str:
        return _inline_sub(t, code="{{{{{}}}}}", link="[{t}|{u}]", bold="*{}*", italic="_{}_", strike="-{}-")

    out: list[str] = []
    lines = (md or "").replace("\r\n", "\n").split("\n")
    in_code = False
    for k, line in enumerate(lines):
        s = line.strip()
        if s.startswith("```"):
            out.append("{code}" if not in_code else "{code}")
            in_code = not in_code
            continue
        if in_code:
            out.append(line)
            continue
        m = _HEAD.match(s)
        if m and m.group("text"):
            out.append(f"h{len(m.group('h'))}. {inline(m.group('text'))}")
            continue
        m = _LIST.match(line)
        if m:
            depth = len(m.group("ind").replace("\t", "    ")) // 2 + 1
            mark = ("#" if m.group("mark")[0].isdigit() else "*") * depth
            out.append(f"{mark} {inline(m.group('text').strip())}")
            continue
        if s.startswith("|"):
            if _TSEP.match(s):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            header = k + 1 < len(lines) and bool(_TSEP.match(lines[k + 1].strip()))
            sep = "||" if header else "|"
            out.append(sep + sep.join(inline(c) for c in cells) + sep)
            continue
        if s.startswith(">"):
            out.append("bq. " + inline(s[1:].strip()))
            continue
        out.append(inline(line.rstrip()))
    return "\n".join(out).strip()


# ------------------------------------------------------------------------------------------------ markdown sections
_DECISION_HEADINGS = {"decisions", "decision", "decisions made", "outcomes and decisions"}
_TASK_HEADINGS = {"action items", "next steps", "tasks", "action items and due dates", "who does what"}


def _hnorm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`:#.]", "", text).casefold()).strip()


def split_md_sections(md: str, names: set[str]) -> tuple[str, list[str]]:
    """Remove markdown sections whose heading is in `names`; return (rest, list item texts of removed sections)."""
    keep: list[str] = []
    items: list[str] = []
    cut_level: int | None = None
    for line in (md or "").replace("\r\n", "\n").split("\n"):
        m = _HEAD.match(line.strip())
        if m:
            level = len(m.group("h"))
            if cut_level is not None and level <= cut_level:
                cut_level = None
            if cut_level is None and _hnorm(m.group("text")) in names:
                cut_level = level
                continue
        if cut_level is not None:
            lm = _LIST.match(line)
            if lm and lm.group("text").strip():
                items.append(lm.group("text").strip())
            continue
        keep.append(line)
    return "\n".join(keep).strip(), items


# ================================================================================================ artefact states
def _fields_of(cls: type[BaseModel], data: dict[str, Any], warn: Any = None) -> BaseModel:
    """Validate world data against a contract model, dropping unknown keys (reported) instead of failing."""
    known = set(cls.model_fields)
    extra = sorted(set(data) - known)
    if extra and warn:
        warn(f"{cls.__name__}: unknown keys ignored: {', '.join(extra)}")
    return cls.model_validate({k: v for k, v in data.items() if k in known})


@dataclass
class HandleTarget:
    handle: str
    type: str  # ticket | page | wiki | mr | commit | email | doc (wiki: not exported)
    artifact_id: str
    beat_id: str | None = None
    att_id: str | None = None
    label: str | None = None


ISSUE_TYPES = {"task": "Task", "bug": "Bug", "story": "Story", "epic": "Epic",
               "sub-task": "Sub-task", "subtask": "Sub-task", "improvement": "Improvement"}
TYPE_IDS = {"Epic": "10000", "Story": "10001", "Task": "10002", "Sub-task": "10003", "Bug": "10004",
            "Improvement": "10005"}
STATUS_CATEGORY = {
    "to do": "new", "todo": "new", "backlog": "new", "open": "new", "new": "new", "selected for development": "new",
    "in progress": "indeterminate", "review": "indeterminate", "in review": "indeterminate", "code review": "indeterminate",
    "testing": "indeterminate", "qa": "indeterminate", "blocked": "indeterminate", "on hold": "indeterminate",
    "done": "done", "closed": "done", "resolved": "done", "won't do": "done", "cancelled": "done", "canceled": "done",
}
RESOLUTIONS = {"done": "Done", "closed": "Done", "resolved": "Done",
               "won't do": "Won't Do", "cancelled": "Won't Do", "canceled": "Won't Do"}
PRIORITIES = {"highest": "Highest", "high": "High", "medium": "Medium", "low": "Low", "lowest": "Lowest",
              "blocker": "Highest", "critical": "Highest", "major": "High", "minor": "Low", "trivial": "Lowest"}
# world link type -> (Jira link type name, inward phrase, outward phrase, side of the OTHER issue)
LINK_TYPES = {
    "blocks": ("Blocks", "is blocked by", "blocks", "outward"),
    "is blocked by": ("Blocks", "is blocked by", "blocks", "inward"),
    "relates": ("Relates", "relates to", "relates to", "outward"),
    "relates to": ("Relates", "relates to", "relates to", "outward"),
    "is caused by": ("Cause", "is caused by", "causes", "inward"),
    "caused by": ("Cause", "is caused by", "causes", "inward"),
    "causes": ("Cause", "is caused by", "causes", "outward"),
    "duplicates": ("Duplicate", "is duplicated by", "duplicates", "outward"),
    "is duplicated by": ("Duplicate", "is duplicated by", "duplicates", "inward"),
    "depends on": ("Dependency", "is depended on by", "depends on", "outward"),
    "is depended on by": ("Dependency", "is depended on by", "depends on", "inward"),
    "implements": ("Implements", "is implemented by", "implements", "outward"),
    "is implemented by": ("Implements", "is implemented by", "implements", "inward"),
    "clones": ("Cloners", "is cloned by", "clones", "outward"),
    "is cloned by": ("Cloners", "is cloned by", "clones", "inward"),
}
PAGE_LABELS = {"meeting_notes": ["meeting-notes"], "retro": ["retro", "meeting-notes"], "report": ["report"],
               "spec": ["spec"], "postmortem": ["postmortem"], "plan": ["plan"]}
IMPLEMENTS_LABELS = {"decision", "implements"}
DOC_SIZES = {"docx": 14_000, "doc": 30_000, "pdf": 42_000, "xlsx": 9_000, "xls": 22_000, "pptx": 60_000}
_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d+$")


@dataclass
class IssueState:
    ticket: Ticket
    id: str
    histories: list[dict] = field(default_factory=list)
    comment_ids: dict[str, str] = field(default_factory=dict)
    remote_ids: dict[tuple, int] = field(default_factory=dict)
    link_ids: dict[tuple, str] = field(default_factory=dict)


@dataclass
class PageState:
    page: Page
    num: str
    editor: str
    version: int = 1


@dataclass
class MRState:
    mr: MergeRequest
    project_id: int
    head: str
    merged_by: str | None = None


# ================================================================================================ version tracker
class Versions:
    """Normalizes each delivery with the real adapter and replays the journal's versioning rule (a version is
    created only when kind/title/fields/body/relations changed; derived children missing from a revision are
    tombstoned), so cases.resolved.json carries the version ids the backend will assign."""

    def __init__(self, sources_yaml: Path | None, tenant: str, *, enabled: bool = True):
        self.tenant = tenant
        self.heads: dict[str, tuple[int, str | None, bool]] = {}
        self.children: dict[str, list[str]] = {}
        self.errors: list[dict[str, Any]] = []
        self.missing: Counter[str] = Counter()
        self.ok = False
        self.reason: str | None = None
        if not enabled or sources_yaml is None:
            self.reason = "disabled"
            return
        try:
            from kb.connectors.base import AdapterContext, get_adapter, load_sources
            from kb.contracts import SourceEnvelope
        except Exception as exc:  # the backend is not on PYTHONPATH: conventions only
            self.reason = f"backend not importable: {exc!r}"
            return
        self._ctx, self._get, self._env = AdapterContext, get_adapter, SourceEnvelope
        self.ts = load_sources(sources_yaml)
        self.ok = True

    def adapter(self, source_id: str) -> Any:
        if not self.ok:
            return None
        src = self.ts.sources.get(source_id)
        if src is None:
            return None
        try:
            return self._get(src.provider)
        except LookupError:
            return None

    def envelope(self, line: dict[str, Any], delivery_id: str) -> Any:
        at = datetime.fromisoformat(line["at"])
        return self._env(
            tenant_id=self.tenant, source_id=line["source"], delivery_id=delivery_id, operation=line.get("op", "upsert"),
            object_type=line["object_type"], external_id=str(line["external_id"]), container=line.get("container"),
            provider_version=str(line["version"]) if line.get("version") is not None else None,
            source_modified_at=datetime.fromisoformat(line["modified_at"]) if line.get("modified_at") else at,
            observed_at=at, payload=line.get("payload"), acl=line.get("acl"),
        )

    def deliver(self, line: dict[str, Any], seq: int) -> list[R.Touch] | None:
        """Touches of one delivery, or None when no adapter can normalize it (caller uses conventions)."""
        adapter = self.adapter(line["source"])
        if adapter is None:
            self.missing[line["source"]] += 1
            return None
        env = self.envelope(line, f"export:{seq}")
        src = self.ts.sources[line["source"]]
        try:
            records = adapter.normalize(env, self._ctx(self.tenant, src, self.ts))
        except Exception as exc:  # the journal quarantines such a delivery: it creates no version
            self.errors.append({"seq": seq, "source": line["source"], "external_id": line["external_id"],
                                "error": repr(exc)[:500]})
            return []
        if not records:
            return []
        prim = records[0]
        touches = [self._apply(prim, "primary")]
        kids = records[1:]
        for r in kids:
            touches.append(self._apply(r, self._role(r, src.provider)))
        live = {r.entity_id for r in kids} if not prim.deleted else set()
        for child in self.children.get(prim.entity_id, []):
            if child not in live:
                touches.append(self._tombstone(child))
        self.children[prim.entity_id] = [r.entity_id for r in kids] if not prim.deleted else []
        return touches

    @staticmethod
    def _role(rec: Any, provider: str) -> str:
        if rec.kind == "Decision":
            return "decision"
        if rec.kind == "ActionItem":
            return "task"
        if provider == "email":
            return "attachment"
        return "derived"

    @staticmethod
    def content_hash(rec: Any) -> str:
        rel = {"s": [s.model_dump(mode="json", exclude_none=True) for s in rec.statements],
               "c": [c.model_dump(mode="json", exclude_none=True) for c in rec.causes]}
        blob = json.dumps({"kind": rec.kind, "title": rec.title, "fields": rec.fields, "body": rec.body, "rel": rel,
                           "valid_from": rec.valid_from}, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _apply(self, rec: Any, role: str) -> R.Touch:
        text = str((rec.fields or {}).get("statement") or (rec.fields or {}).get("text") or rec.title or "")
        head = self.heads.get(rec.entity_id)
        if rec.deleted:
            if head is None or head[2]:
                return R.Touch(rec.entity_id, head[0] if head else None, False, role, text, rec.url, deleted=True)
            self.heads[rec.entity_id] = (head[0] + 1, None, True)
            return R.Touch(rec.entity_id, head[0] + 1, True, role, text, rec.url, deleted=True)
        h = self.content_hash(rec)
        if head is not None and not head[2] and head[1] == h:
            return R.Touch(rec.entity_id, head[0], False, role, text, rec.url)
        seq = head[0] + 1 if head else 1
        self.heads[rec.entity_id] = (seq, h, False)
        return R.Touch(rec.entity_id, seq, True, role, text, rec.url)

    def _tombstone(self, entity_id: str) -> R.Touch:
        head = self.heads.get(entity_id)
        if head is None or head[2]:
            return R.Touch(entity_id, head[0] if head else None, False, "derived", deleted=True)
        self.heads[entity_id] = (head[0] + 1, None, True)
        return R.Touch(entity_id, head[0] + 1, True, "derived", deleted=True)

    def naive(self, entity_id: str, payload_key: Any, role: str, text: str = "") -> R.Touch:
        """Versioning by payload hash, for deliveries no adapter normalized."""
        h = hashlib.sha256(json.dumps(payload_key, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        head = self.heads.get(entity_id)
        if head is not None and not head[2] and head[1] == h:
            return R.Touch(entity_id, head[0], False, role, text)
        seq = head[0] + 1 if head else 1
        self.heads[entity_id] = (seq, h, False)
        return R.Touch(entity_id, seq, True, role, text)


# ================================================================================================ exporter
WRITER = "Nemotron Lightning"  # who wrote the texts of the simulation (03-SIMULATION.md §7)


class Provenance:
    """Where a world event came from (A6): the scenario beat, an employee agent's request of a swarm round, the
    densification plan, or the engine itself. Read from sim/out/<t>/{rounds, world.before-densify.jsonl}."""

    def __init__(self, out_dir: Path | None):
        self.rounds: dict[int, list[tuple[str, Path]]] = {}
        self.original: set[tuple] | None = None
        self.densify_tickets: dict[str, str] = {}
        if out_dir is None:
            return
        for d in sorted((out_dir / "rounds").glob("[0-9][0-9][0-9]-d[0-9][0-9][0-9]")) if (out_dir / "rounds").is_dir() else []:
            self.rounds.setdefault(int(d.name.rsplit("-d", 1)[1]), []).append((d.name.split("-", 1)[0], d))
        before = out_dir / "world.before-densify.jsonl"
        if before.is_file():
            self.original = set()
            with before.open(encoding="utf-8") as fh:
                for raw in fh:
                    if raw.strip():
                        e = json.loads(raw)
                        self.original.add((e["kind"], e["artifact_id"], e["ts"], e["actor"]))
            for req in sorted((out_dir / "rounds" / "densify-1").glob("req-*.json")):
                r = json.loads(req.read_text(encoding="utf-8"))
                for k in (r.get("checks") or {}).get("keys") or []:
                    self.densify_tickets.setdefault(k, r["id"])

    def __call__(self, ev: WorldEvent, iso_ts: str) -> dict[str, Any]:
        p: dict[str, Any] = {"world_seq": ev.seq}
        if self.original is not None and (ev.kind, ev.artifact_id, iso_ts, ev.actor) not in self.original:
            p["origin"] = "densify"
            p["stub"] = ev.handle or f"{ev.artifact_type}:{ev.artifact_id}"
            p["writer"] = WRITER
            if ev.kind == "ticket.created" and ev.artifact_id in self.densify_tickets:
                p["request"] = f"densify-1/{self.densify_tickets[ev.artifact_id]}"
            return p
        merge = ev.kind in ("mr.merged",) or (ev.kind == "commit.pushed" and
                                              str((ev.data or {}).get("message") or "").startswith("Merge branch"))
        if merge or ev.actor == "system":
            p["origin"] = "engine"  # a template of the engine, no writer
            return p
        p["origin"] = "scenario" if ev.beat_id else "agent"
        if ev.beat_id:
            p["beat"] = ev.beat_id
        p["writer"] = WRITER
        for no, d in self.rounds.get(ev.day, []):
            if (d / f"req-r{no}-{ev.actor}.json").is_file():
                p["request"] = f"r{no}-{ev.actor}"
                break
        return p


class Exporter:
    def __init__(self, company: Company, scenario: Scenario | None, events: list[WorldEvent], *,
                 sources_yaml: Path | None = None, validate: bool = True, out_dir: Path | None = None):
        self.company = company
        self.provenance = Provenance(out_dir)
        self.scenario = scenario
        self.events = sorted(events, key=lambda e: e.seq)
        self.ids = R.Ids(company.company.tenant_id, company.tools.mail_domain)
        self.clock = Clock(tenant_tz(company.company.timezone))
        self.dir = Directory(company, self.ids)
        self.beats = {b.id: b for b in (scenario.beats if scenario else [])}
        self.pages = R.PageNumbers()
        self.versions = Versions(sources_yaml, self.ids.tenant, enabled=validate)
        self.warnings: list[str] = []
        self._warned: set[str] = set()
        self.stats: Counter[str] = Counter()
        self.handles: dict[str, HandleTarget] = {}
        self.issues: dict[str, IssueState] = {}
        self.page_states: dict[str, PageState] = {}
        self.mrs: dict[str, MRState] = {}
        self.emails: dict[str, Email] = {}
        self.commits: dict[str, tuple[str, str]] = {}  # world sha -> (repo, git sha)
        self.page_space: dict[str, str] = {}
        self.wiki_title: dict[str, str] = {}
        self.ticket_summary: dict[str, str] = {}
        self.mr_ident: dict[str, tuple[str, int, str]] = {}
        self.email_subject: dict[str, str] = {}
        self.lines: list[tuple[datetime, int, dict]] = []
        self.touches: dict[int, list[R.Touch]] = {}
        self.failed: set[int] = set()
        self._last_at: dict[str, datetime] = {}
        self._jira_ids = 10100
        self._comment_ids = 20000
        self._hist_ids = 50000
        self._remote_ids = 30000
        self._link_ids = 40000
        self._references: dict[str, list[str]] = {}  # email id -> References chain (oldest first)
        self.project_ids = {k: 10000 + 10 * (i + 1) for i, k in enumerate(company.tools.jira)}
        self.space_ids = {k: str(98305 + i) for i, k in enumerate(company.tools.confluence)}
        self.repo_ids = {k: 70 + i for i, k in enumerate(company.tools.gitlab)}
        self._handle_re: re.Pattern | None = None
        self._prop_re: re.Pattern | None = None
        self._by_message_id: dict[str, Email] = {}

    # ------------------------------------------------------------------ helpers
    def warn(self, msg: str) -> None:
        if msg not in self._warned:
            self._warned.add(msg)
            self.warnings.append(msg)
            log.warning(msg)

    def beat_of(self, ev: WorldEvent) -> Any:
        return self.beats.get(ev.beat_id) if ev.beat_id else None

    def git_sha(self, world_sha: str, repo: str) -> str:
        s = str(world_sha).strip()
        if re.fullmatch(r"[0-9a-f]{40}", s):
            return s
        return hashlib.sha1(f"{self.ids.tenant}:{repo}:{s}".encode()).hexdigest()

    # ------------------------------------------------------------------ pass 1: identities and handles
    def index(self) -> None:
        for ev in self.events:
            d = ev.data or {}
            if ev.kind == "page.created":
                self.page_space[ev.artifact_id] = str(d.get("space") or "")
                self.pages(ev.artifact_id)
            elif ev.kind == "wiki.created":
                self.wiki_title[self.wiki_ref(ev.artifact_id, d.get("space"))] = str(d.get("title") or "")
            elif ev.kind == "ticket.created":
                self.ticket_summary[ev.artifact_id] = str(d.get("summary") or "")
            elif ev.kind == "mr.created":
                repo = str(d.get("repo") or ev.artifact_id.split("!")[0])
                iid = int(d.get("iid") or re.sub(r"\D", "", ev.artifact_id.split("!")[-1]) or 0)
                self.mr_ident[ev.artifact_id] = (repo, iid, str(d.get("title") or ""))
            elif ev.kind == "commit.pushed":
                repo = str(d.get("repo") or "")
                self.commits[ev.artifact_id] = (repo, self.git_sha(str(d.get("sha") or ev.artifact_id), repo))
            elif ev.kind == "email.sent":
                self.email_subject[ev.artifact_id] = str(d.get("subject") or "")
                for k, att in enumerate(d.get("attachments") or [], 1):
                    meta = (att or {}).get("meta") or {}
                    h = meta.get("handle")
                    if h:
                        self.handles.setdefault(h, HandleTarget(h, "doc", ev.artifact_id, ev.beat_id, f"att-{k}",
                                                                meta.get("title") or att.get("filename")))
            if ev.handle and ev.handle not in self.handles:
                self.handles[ev.handle] = HandleTarget(ev.handle, ev.artifact_type, ev.artifact_id, ev.beat_id)
            # the engine binds "beat:<id>" to the first artefact of a beat that created no named handle
            if ev.beat_id and f"beat:{ev.beat_id}" not in self.handles:
                self.handles[f"beat:{ev.beat_id}"] = HandleTarget(f"beat:{ev.beat_id}", ev.artifact_type,
                                                                  ev.artifact_id, ev.beat_id)
        for h in self.handles.values():
            h.label = h.label or self.handle_label(h)
        known = sorted(self.handles, key=len, reverse=True)
        if known:
            self._handle_re = re.compile(r"(?<![\w:/])(" + "|".join(re.escape(h) for h in known) + r")(?![\w\-/])")

    @staticmethod
    def wiki_ref(wid: str, space: str | None) -> str:
        """Space.Page of a wiki page (wiki pages are not exported; the ref only labels references to them)."""
        ref = str(wid).replace("/", ".").strip(".")
        return f"{space}.{ref}" if "." not in ref and space else ref

    def handle_label(self, h: HandleTarget) -> str:
        if h.type == "ticket":
            return h.artifact_id
        if h.type == "page":
            return ""  # filled from page state (title) when needed
        if h.type == "wiki":
            return self.wiki_title.get(self.wiki_ref(h.artifact_id, None), h.artifact_id)
        if h.type == "mr":
            repo, iid, _ = self.mr_ident.get(h.artifact_id, (h.artifact_id, 0, ""))
            return f"{repo}!{iid}"
        if h.type == "commit":
            return self.commits.get(h.artifact_id, ("", h.artifact_id))[1][:8]
        if h.type == "email":
            return self.email_subject.get(h.artifact_id, h.artifact_id)
        return h.artifact_id

    def page_title(self, world_id: str) -> str:
        st = self.page_states.get(world_id)
        return st.page.title if st else world_id

    def handle_text(self, handle: str) -> str:
        """Natural reference for a handle that leaked into prose: key, "title", !iid."""
        h = self.handles[handle]
        if h.type == "ticket":
            return h.artifact_id
        if h.type == "page":
            return f'"{self.page_title(h.artifact_id)}"'
        if h.type in ("wiki", "email", "doc"):
            return f'"{h.label}"'
        return h.label or handle

    def humanize(self, text: str | None) -> str:
        if not text:
            return ""
        if self._handle_re is None:
            return str(text)
        out = self._handle_re.sub(lambda m: self.handle_text(m.group(1)), str(text))
        if out != text:
            self.stats["handles_in_text"] += 1
        return out

    def decision_n(self, h: HandleTarget) -> int | None:
        """Decision item of a meeting page that the beat creating the handle decided (for 'implements' links)."""
        beat = self.beats.get(h.beat_id or "")
        st = self.page_states.get(h.artifact_id)
        if beat is None or not beat.decision or st is None or not st.page.decisions:
            return None
        decs = st.page.decisions
        if len(decs) == 1:
            return 1
        scores = [sum(1 for f in beat.facts if R.fact_in(f.any, R.norm(d))) for d in decs]
        best = max(scores)
        if best > 0 and scores.count(best) == 1:
            return scores.index(best) + 1
        return None

    def handle_url(self, handle: str, *, implements: bool = False) -> str | None:
        h = self.handles.get(handle)
        if h is None:
            return None
        if h.type == "ticket":
            return f"{self.ids.jira_base}/browse/{h.artifact_id}"
        if h.type == "page":
            space = self.page_space.get(h.artifact_id)
            num = self.pages(h.artifact_id)
            url = f"{self.ids.confluence_base}/spaces/{space}/pages/{num}" if space else \
                f"{self.ids.confluence_base}/pages/viewpage.action?pageId={num}"
            n = self.decision_n(h) if implements else None
            return url + (f"#dec-{n}" if n else "")
        if h.type == "mr":
            repo, iid, _ = self.mr_ident.get(h.artifact_id, ("", 0, ""))
            return f"{self.ids.gitlab_base}/{repo}/-/merge_requests/{iid}" if repo else None
        if h.type == "commit":
            repo, sha = self.commits.get(h.artifact_id, ("", ""))
            return f"{self.ids.gitlab_base}/{repo}/-/commit/{sha}" if repo else None
        return None  # email / doc: mail has no URLs

    def ticket_key(self, ref: str | None) -> str | None:
        if not ref:
            return None
        r = str(ref).strip()
        if r in self.handles:
            h = self.handles[r]
            return h.artifact_id if h.type == "ticket" else None
        return r if _KEY.match(r) else None

    def target_url(self, target: str, *, implements: bool = False) -> tuple[str | None, str | None]:
        """(url, default title) of a remote-link / property target: handle, URL or Jira key."""
        t = str(target or "").strip()
        if t in self.handles:
            h = self.handles[t]
            url = self.handle_url(t, implements=implements)
            title = self.page_title(h.artifact_id) if h.type == "page" else (
                f"{h.artifact_id} {self.ticket_summary.get(h.artifact_id, '')}".strip() if h.type == "ticket" else h.label)
            if url is None:
                self.stats[f"dropped_{h.type}_link"] += 1
            return url, title
        if t.startswith(("http://", "https://")):
            return t, None
        if _KEY.match(t):
            return f"{self.ids.jira_base}/browse/{t}", t
        if t:
            self.warn(f"link target {t!r} is neither a known handle, a URL nor a Jira key: dropped")
        return None, None

    # ------------------------------------------------------------------ users
    def jira_user(self, ref: str | None) -> dict | None:
        w = self.dir.get(ref)
        if w is None:
            return None
        if w.employee:
            acc = w.accounts[self.ids.jira]
            return {"self": f"{self.ids.jira_base}/rest/api/2/user?accountId={acc}", "accountId": acc,
                    "displayName": w.name, "emailAddress": w.email, "active": True,
                    "timeZone": self.company.company.timezone, "accountType": "atlassian"}
        acc = f"qm:{w.id}"
        return {"self": f"{self.ids.jira_base}/rest/api/2/user?accountId={acc}", "accountId": acc,
                "displayName": w.name, "emailAddress": w.email, "active": True, "accountType": "customer"}

    def gl_user(self, ref: str | None) -> dict | None:
        w = self.dir.get(ref)
        if w is None or not w.employee:
            return None
        u = w.accounts[self.ids.gitlab]
        uid = 100 + w.index
        return {"id": uid, "username": u, "name": w.name, "state": "active",
                "avatar_url": f"{self.ids.gitlab_base}/uploads/-/system/user/avatar/{uid}/avatar.png",
                "web_url": f"{self.ids.gitlab_base}/{u}"}

    def mention(self, ref: str) -> dict:
        w = self.dir.get(ref)
        acc = w.accounts.get(self.ids.confluence) if w else None
        if not w or not acc:
            return _tx(w.name if w else str(ref))
        return {"type": "mention", "attrs": {"id": acc, "text": "@" + w.name, "accessLevel": ""}}

    # ------------------------------------------------------------------ timeline
    def emit(self, ev: WorldEvent, *, source: str, object_type: str, external_id: str, container: str | None,
             payload: dict, acl: list[str] | None = None, version: Any = None) -> dict:
        ts = self.clock.aware(ev.ts)
        key = f"{source}/{object_type}/{external_id}"
        at = ts + OBSERVE_DELAY  # type: ignore[operator]
        last = self._last_at.get(key)
        if last is not None and at <= last:
            at = last + timedelta(seconds=1)
        self._last_at[key] = at
        line: dict[str, Any] = {
            "at": self.clock.iso(at), "source": source, "object_type": object_type, "external_id": external_id,
            "container": container, "op": "upsert", "modified_at": self.clock.iso(ts), "acl": acl,
        }
        if version is not None:
            line["version"] = version
        line["note"] = f"seq {ev.seq} {ev.kind}" + (f" · beat {ev.beat_id}" if ev.beat_id else "")
        line["provenance"] = self.provenance(ev, ev.ts.isoformat())  # ignored at load (kb.ingest.replay)
        line["payload"] = payload
        self.lines.append((at, ev.seq, line))
        self.stats[f"{source}:{object_type}"] += 1
        return line

    def run(self) -> None:
        self.index()
        handlers = {
            "ticket.created": self.on_ticket, "ticket.commented": self.on_ticket, "ticket.updated": self.on_ticket,
            "page.created": self.on_page, "page.updated": self.on_page,
            "mr.created": self.on_mr, "mr.commented": self.on_mr, "mr.merged": self.on_mr,
            "commit.pushed": self.on_commit, "email.sent": self.on_email,
        }
        for ev in self.events:
            if ev.kind.startswith("wiki."):
                self.stats["skipped_wiki"] += 1
                self.warn("wiki pages are not exported: this copy of the backend has no XWiki connector")
                continue
            fn = handlers.get(ev.kind)
            if fn is None:
                self.warn(f"event kind {ev.kind} is not exported")
                continue
            try:
                line, fallback = fn(ev)
            except Exception as exc:  # one broken event must not stop the export
                self.failed.add(ev.seq)
                self.warn(f"seq {ev.seq} {ev.kind} {ev.artifact_id}: {type(exc).__name__}: {exc}")
                continue
            touches = self.versions.deliver(line, ev.seq)
            if touches is None:
                touches = fallback()
            self.touches[ev.seq] = touches

    def timeline(self) -> list[dict]:
        return [line for _, _, line in sorted(self.lines, key=lambda x: (x[0], x[1]))]

    # ------------------------------------------------------------------ Jira
    def on_ticket(self, ev: WorldEvent) -> tuple[dict, Any]:
        d = ev.data or {}
        if ev.kind == "ticket.created":
            t = _fields_of(Ticket, d, self.warn)
            assert isinstance(t, Ticket)
            if ev.artifact_id in self.issues:
                self.warn(f"ticket {ev.artifact_id} created twice: second creation replaces the state")
            self._jira_ids += 1
            st = IssueState(t, str(self._jira_ids))
            self.issues[t.key] = st
            self.ticket_summary[t.key] = t.summary
        else:
            st = self.issues.get(ev.artifact_id)
            if st is None:
                raise KeyError(f"ticket {ev.artifact_id} has no ticket.created event")
            t = st.ticket
            if ev.kind == "ticket.updated":
                self._ticket_changes(st, ev, d.get("changes") or {})
            if d.get("comment"):
                c = _fields_of(TicketComment, d["comment"], self.warn)
                if not any(x.id == c.id for x in t.comments):  # type: ignore[attr-defined]
                    t.comments.append(c)  # type: ignore[arg-type]
            t.updated = max(self.clock.aware(t.updated), self.clock.aware(ev.ts))  # type: ignore[type-var]
        payload = self.jira_payload(st)
        line = self.emit(ev, source=self.ids.jira, object_type="issue", external_id=t.key, container=t.project,
                         payload=payload)
        return line, lambda: [self.versions.naive(R.jira_entity(t.key), payload["fields"], "primary")]

    def _ticket_changes(self, st: IssueState, ev: WorldEvent, changes: dict[str, Any]) -> None:
        t = st.ticket
        items = []
        for k, v in changes.items():
            if k not in Ticket.model_fields or k in ("key", "comments", "created"):
                self.warn(f"ticket.updated: field {k!r} ignored")
                continue
            old = getattr(t, k)
            if k in ("links", "remote_links"):
                add = [v] if isinstance(v, dict) else list(v or [])
                new = list(old) + [x for x in add if x not in old]
                setattr(t, k, new)
                for x in add:
                    if x not in old:
                        if k == "links":
                            items.append({"field": "Link", "fieldtype": "jira", "fromString": None,
                                          "toString": f"This issue {x.get('type')} {self.ticket_key(x.get('key')) or x.get('key')}"})
                        else:
                            items.append({"field": "RemoteIssueLink", "fieldtype": "jira", "fromString": None,
                                          "toString": str(x.get("title") or x.get("relationship") or "")})
                continue
            if k == "labels" and isinstance(v, str):
                v = [v]
            setattr(t, k, v)
            shown = {"assignee": lambda x: (self.dir.get(x).name if self.dir.get(x) else x) if x else None,
                     "reporter": lambda x: (self.dir.get(x).name if self.dir.get(x) else x) if x else None,
                     "labels": lambda x: " ".join(x or [])}.get(k, lambda x: None if x is None else str(x))
            items.append({"field": k, "fieldtype": "jira", "fromString": shown(old), "toString": shown(v)})
        if items:
            self._hist_ids += 1
            st.histories.append({"id": str(self._hist_ids), "author": self.jira_user(ev.actor),
                                 "created": self.clock.jira(ev.ts), "items": items})

    def jira_payload(self, st: IssueState) -> dict:
        t = st.ticket
        base = self.ids.jira_base
        itype = ISSUE_TYPES.get(t.issue_type.strip().casefold(), t.issue_type)
        status_key = t.status.strip().casefold()
        res = RESOLUTIONS.get(status_key)
        project = self.company.tools.jira.get(t.project)
        comments = []
        for c in t.comments:
            if c.id not in st.comment_ids:
                self._comment_ids += 1
                st.comment_ids[c.id] = str(self._comment_ids)
            cid = st.comment_ids[c.id]
            u = self.jira_user(c.author)
            comments.append({"self": f"{base}/rest/api/2/issue/{st.id}/comment/{cid}", "id": cid, "author": u,
                             "updateAuthor": u, "body": md_to_jira(self.humanize(c.body)),
                             "created": self.clock.jira(c.created), "updated": self.clock.jira(c.created)})
        links = []
        for ln in t.links:
            other = self.ticket_key(ln.get("key"))
            if not other:
                self.warn(f"{t.key}: issue link to {ln.get('key')!r} dropped (not a ticket)")
                continue
            kind = str(ln.get("type") or "relates").strip().casefold()
            name, inward, outward, side = LINK_TYPES.get(kind, (kind.title(), kind, kind, "outward"))
            lk = (kind, other)
            if lk not in st.link_ids:
                self._link_ids += 1
                st.link_ids[lk] = str(self._link_ids)
            o = self.issues.get(other)
            ref: dict[str, Any] = {"key": other, "self": f"{base}/rest/api/2/issue/{o.id if o else other}"}
            if o:
                ref["id"] = o.id
                ref["fields"] = {"summary": o.ticket.summary, "status": {"name": o.ticket.status},
                                 "issuetype": {"name": ISSUE_TYPES.get(o.ticket.issue_type.casefold(), o.ticket.issue_type)}}
            links.append({"id": st.link_ids[lk], "type": {"name": name, "inward": inward, "outward": outward},
                          f"{side}Issue": ref})
        remotes = []
        for rl in t.remote_links:
            rel = str(rl.get("relationship") or "relates to")
            url, default_title = self.target_url(str(rl.get("target") or rl.get("url") or ""),
                                                 implements=_norm_label(rel) in ("implements", "implementation of"))
            if not url:
                continue
            title = str(rl.get("title") or default_title or url)
            rk = (rel, url)
            if rk not in st.remote_ids:
                self._remote_ids += 1
                st.remote_ids[rk] = self._remote_ids
            rid = st.remote_ids[rk]
            app = ({"type": "com.atlassian.confluence", "name": "Confluence"} if url.startswith(self.ids.confluence_base)
                   else {"type": "com.gitlab", "name": "GitLab"} if url.startswith(self.ids.gitlab_base)
                   else {"type": "com.atlassian.jira", "name": "Jira"} if url.startswith(base) else
                   {"type": "web", "name": "Web"})
            remotes.append({"id": rid, "self": f"{base}/rest/api/2/issue/{t.key}/remotelink/{rid}",
                            "globalId": f"{self.ids.tenant}-remote-{rid}", "application": app, "relationship": rel,
                            "object": {"url": url, "title": title}})
        fields: dict[str, Any] = {
            "summary": self.humanize(t.summary),
            "description": md_to_jira(self.humanize(t.description)),
            "issuetype": {"id": TYPE_IDS.get(itype, "10002"), "name": itype, "subtask": itype == "Sub-task"},
            "project": {"id": str(self.project_ids.get(t.project, 10000)), "key": t.project,
                        "name": project.name if project else t.project},
            "status": {"name": t.status, "statusCategory": {"key": STATUS_CATEGORY.get(status_key, "indeterminate")}},
            "resolution": {"name": res} if res else None,
            "priority": {"name": PRIORITIES.get(t.priority.strip().casefold(), t.priority)},
            "assignee": self.jira_user(t.assignee) if t.assignee else None,
            "reporter": self.jira_user(t.reporter),
            "creator": self.jira_user(t.reporter),
            "created": self.clock.jira(t.created),
            "updated": self.clock.jira(t.updated),
            "labels": list(t.labels),
            "issuelinks": links,
            "comment": {"comments": comments, "maxResults": len(comments), "total": len(comments), "startAt": 0},
        }
        parent = self.ticket_key(t.parent)
        if parent:
            p = self.issues.get(parent)
            fields["parent"] = {"id": p.id if p else None, "key": parent,
                                "fields": {"summary": p.ticket.summary if p else parent,
                                           "issuetype": {"name": ISSUE_TYPES.get(p.ticket.issue_type.casefold(), "Epic") if p else "Epic"}}}
        return {"expand": "names,changelog", "id": st.id, "self": f"{base}/rest/api/2/issue/{st.id}", "key": t.key,
                "fields": fields, "names": {},
                "changelog": {"startAt": 0, "maxResults": len(st.histories), "total": len(st.histories),
                              "histories": [dict(h) for h in st.histories]},
                "remotelinks": remotes}

    # ------------------------------------------------------------------ Confluence
    def on_page(self, ev: WorldEvent) -> tuple[dict, Any]:
        d = ev.data or {}
        if ev.kind == "page.created":
            p = _fields_of(Page, d, self.warn)
            assert isinstance(p, Page)
            st = PageState(p, self.pages(p.id), ev.actor if self.dir.get(ev.actor) else p.author)
            self.page_states[p.id] = st
            self.page_space[p.id] = p.space
        else:
            st = self.page_states.get(ev.artifact_id)
            if st is None:
                raise KeyError(f"page {ev.artifact_id} has no page.created event")
            p = st.page
            for k, v in d.items():
                if v is None:
                    continue
                if k == "properties" and isinstance(v, dict):
                    p.properties = {**p.properties, **{str(a): str(b) for a, b in v.items()}}
                elif k in ("body", "title", "decisions", "action_items", "attendees", "held_at", "kind"):
                    setattr(p, k, _fields_of(Page, {**p.model_dump(), k: v}).__dict__[k])  # validated assignment
                elif k not in ("version", "updated", "author", "id", "space", "created"):
                    self.warn(f"page.updated: key {k!r} ignored")
            st.version += 1
            p.version = st.version
            p.updated = ev.ts
            st.editor = ev.actor if self.dir.get(ev.actor) else st.editor
        payload = self.confluence_payload(st)
        line = self.emit(ev, source=self.ids.confluence, object_type="page", external_id=st.num, container=p.space,
                         payload=payload, version=st.version)

        def fallback() -> list[R.Touch]:
            out = [self.versions.naive(R.page_entity(st.num), payload["body"], "primary")]
            for n, text in enumerate(p.decisions, 1):
                out.append(self.versions.naive(R.decision_entity(st.num, n), text, "decision", text))
            for n, it in enumerate(p.action_items, 1):
                out.append(self.versions.naive(R.task_entity(st.num, n), it, "task", str(it.get("text", ""))))
            return out

        return line, fallback

    def _prop_inline(self, label: str, value: Any) -> list[dict]:
        v = str(value if value is not None else "").strip()
        parts = [x.strip() for x in re.split(r"[,;]", v) if x.strip()]
        if parts and all(x in self.dir.people and self.dir.people[x].employee for x in parts):
            out: list[dict] = []
            for k, x in enumerate(parts):
                if k:
                    out.append(_tx(", "))
                out.append(self.mention(x))
            return out
        implements = _norm_label(label) in IMPLEMENTS_LABELS
        if self._prop_re is None:
            parts = [r"https?://[^\s,;]+", r"\b[A-Z][A-Z0-9]{1,9}-\d+\b"]
            if self._handle_re is not None:
                parts.insert(0, self._handle_re.pattern)
            self._prop_re = re.compile("|".join(f"(?:{x})" for x in parts))
        out = []
        pos = 0
        for m in self._prop_re.finditer(v):
            tok = m.group(0)
            if m.start() > pos:
                out += adf_inline(v[pos:m.start()])
            pos = m.end()
            if tok in self.handles:
                url, title = self.target_url(tok, implements=implements)
                out.append({"type": "inlineCard", "attrs": {"url": url}} if url else _tx(f"«{title or tok}»"))
            elif tok.startswith("http"):
                out.append({"type": "inlineCard", "attrs": {"url": tok}})
            elif tok in self.issues or tok in self.ticket_summary:
                out.append({"type": "inlineCard", "attrs": {"url": f"{self.ids.jira_base}/browse/{tok}"}})
            else:
                out.append(_tx(tok))
        if pos < len(v):
            out += adf_inline(v[pos:])
        return out or [_tx("—")]

    def _properties_block(self, rows: list[tuple[str, list[dict]]]) -> dict:
        def cell(kind: str, content: list[dict]) -> dict:
            return {"type": kind, "attrs": {}, "content": [_para(content)]}

        table = {"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
                 "content": [{"type": "tableRow", "content": [cell("tableHeader", [_tx(label)]), cell("tableCell", value)]}
                             for label, value in rows]}
        return {"type": "bodiedExtension", "attrs": {"extensionType": "com.atlassian.confluence.macro.core",
                                                     "extensionKey": "details", "parameters": {"macroParams": {}}},
                "content": [table]}

    def page_adf(self, p: Page) -> dict:
        meeting = p.kind in ("meeting_notes", "retro") or bool(p.attendees) or p.held_at is not None
        rows: list[tuple[str, list[dict]]] = []
        given = {_norm_label(k) for k in p.properties}
        if meeting:
            held = self.clock.local(p.held_at or p.created)
            ms = int(datetime(held.year, held.month, held.day, tzinfo=UTC).timestamp() * 1000)
            if not given & {"date", "meeting date", "held at", "when"}:
                rows.append(("Date", [{"type": "date", "attrs": {"timestamp": str(ms)}},
                                      _tx(f" {held:%H:%M} ({self.clock.zone_label()})")]))
            if p.attendees and not given & {"attendees", "participants"}:
                att: list[dict] = []
                for k, a in enumerate(p.attendees):
                    if k:
                        att.append(_tx(", "))
                    att.append(self.mention(a))
                rows.append(("Attendees", att))
            if not given & {"note taker"}:
                rows.append(("Note taker", [self.mention(p.author)]))
        for label, value in p.properties.items():
            rows.append((str(label), self._prop_inline(str(label), value)))
        blocks: list[dict] = []
        if rows:
            blocks.append(self._properties_block(rows))
        body = self.humanize(p.body)
        decisions = [self.humanize(x) for x in p.decisions]
        tasks = list(p.action_items)
        body, body_decisions = split_md_sections(body, _DECISION_HEADINGS)
        if not decisions and body_decisions:
            decisions = body_decisions  # the writer put the decisions only into a markdown list
            self.stats["decisions_from_body"] += 1
        if tasks:
            body, _ = split_md_sections(body, _TASK_HEADINGS)
        blocks += md_to_adf_blocks(body)
        if decisions:
            blocks.append(_heading("Decisions"))
            blocks.append({"type": "decisionList", "attrs": {"localId": "decisions"}, "content": [
                {"type": "decisionItem", "attrs": {"localId": f"dec-{n}", "state": "DECIDED"},
                 "content": adf_inline(text) or [_tx(text)]}
                for n, text in enumerate(decisions, 1)]})
        if tasks:
            items = []
            for n, it in enumerate(tasks, 1):
                who = str(it.get("assignee") or "").strip()
                text = self.humanize(str(it.get("text") or it.get("task") or ""))
                content: list[dict] = []
                if who and self.dir.get(who):
                    content += [self.mention(who), _tx(" - ")]
                content += adf_inline(text) or [_tx(text or "-")]
                due = str(it.get("due") or "").strip()
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
                    ms = int(datetime.fromisoformat(due).replace(tzinfo=UTC).timestamp() * 1000)
                    content += [_tx(" due "), {"type": "date", "attrs": {"timestamp": str(ms)}}]
                state = "DONE" if str(it.get("state") or it.get("status") or "").casefold() in (
                    "done", "closed") else "TODO"
                items.append({"type": "taskItem", "attrs": {"localId": f"task-{n}", "state": state}, "content": content})
            blocks.append(_heading("Action items"))
            blocks.append({"type": "taskList", "attrs": {"localId": "tasks"}, "content": items})
        return {"type": "doc", "version": 1, "content": blocks}

    def confluence_payload(self, st: PageState) -> dict:
        p = st.page
        space = self.company.tools.confluence.get(p.space)
        sid = self.space_ids.get(p.space, "98000")
        author_acc = self.dir.account(p.author, self.ids.confluence)
        editor_acc = self.dir.account(st.editor, self.ids.confluence) or author_acc
        labels = PAGE_LABELS.get(p.kind, [])
        if p.kind not in PAGE_LABELS and (p.attendees or p.held_at):
            labels = ["meeting-notes"]
        doc = self.page_adf(p)
        title = self.humanize(p.title)
        return {
            "id": st.num, "status": "current", "title": title, "spaceId": sid,
            "space": {"id": sid, "key": p.space, "name": space.name if space else p.space},
            "parentId": f"{sid}0", "parentType": "page", "authorId": author_acc, "ownerId": author_acc,
            "createdAt": self.clock.utc(p.created),
            "version": {"number": st.version, "message": "", "minorEdit": False, "authorId": editor_acc,
                        "createdAt": self.clock.utc(p.updated)},
            "labels": {"results": [{"id": str(int(hashlib.sha1(x.encode()).hexdigest()[:6], 16)), "name": x,
                                    "prefix": "global"} for x in labels]},
            "body": {"atlas_doc_format": {"value": json.dumps(doc, ensure_ascii=False),
                                          "representation": "atlas_doc_format"}},
            "_links": {"webui": f"/spaces/{p.space}/pages/{st.num}/{title.replace(' ', '+')}",
                       "tinyui": f"/x/{st.num[-4:]}", "base": self.ids.confluence_base},
        }

    # ------------------------------------------------------------------ GitLab
    def on_mr(self, ev: WorldEvent) -> tuple[dict, Any]:
        d = ev.data or {}
        if ev.kind == "mr.created":
            m = _fields_of(MergeRequest, d, self.warn)
            assert isinstance(m, MergeRequest)
            pid = self.repo_ids.get(m.repo, 99)
            st = MRState(m, pid, hashlib.sha1(f"{self.ids.tenant}:{m.id}:head".encode()).hexdigest())
            self.mrs[ev.artifact_id] = st
            if m.repo not in self.company.tools.gitlab:
                self.warn(f"MR {m.id}: repo {m.repo} is not a GitLab container of the tenant")
        else:
            st = self.mrs.get(ev.artifact_id)
            if st is None:
                raise KeyError(f"MR {ev.artifact_id} has no mr.created event")
            m = st.mr
            if ev.kind == "mr.commented" and d.get("comment"):
                c = _fields_of(MRComment, d["comment"], self.warn)
                if not any(x.id == c.id for x in m.comments):  # type: ignore[attr-defined]
                    m.comments.append(c)  # type: ignore[arg-type]
            elif ev.kind == "mr.merged":
                m.state = "merged"
                m.merged_at = ev.ts
                m.merge_commit = str(d.get("merge_commit") or "") or None
                st.merged_by = ev.actor if self.dir.get(ev.actor) else (m.reviewers[0] if m.reviewers else m.author)
            m.updated = ev.ts
        payload = self.mr_payload(st)
        line = self.emit(ev, source=self.ids.gitlab, object_type="merge_request", external_id=f"{m.repo}!{m.iid}",
                         container=m.repo, payload=payload)
        return line, lambda: [self.versions.naive(R.mr_entity(m.repo, m.iid),
                                                  {k: payload[k] for k in ("title", "description", "state", "notes")},
                                                  "primary")]

    def mr_payload(self, st: MRState) -> dict:
        m = st.mr
        desc = self.humanize(m.description).strip()
        closes = [k for k in (self.ticket_key(x) for x in m.closes) if k]
        missing = [k for k in closes if not re.search(rf"\b(?:[Cc]los|[Ff]ix|[Rr]esolv|[Ii]mplement)\w*:?\s+[^\n]*\b{re.escape(k)}\b", desc)]
        if missing:
            desc = (desc + "\n\n" if desc else "") + "Closes " + ", ".join(missing)
        author = self.gl_user(m.author)
        merged_by = self.gl_user(st.merged_by) if m.state == "merged" else None
        merge_sha = self.git_sha(m.merge_commit, m.repo) if m.merge_commit else (
            hashlib.sha1(f"{self.ids.tenant}:{m.id}:merge".encode()).hexdigest() if m.state == "merged" else None)
        notes = []
        for c in m.comments:
            notes.append({"id": int(hashlib.sha1(f"{m.id}:{c.id}".encode()).hexdigest()[:7], 16), "type": None,
                          "body": self.humanize(c.body), "author": self.gl_user(c.author),
                          "created_at": self.clock.utc(c.created), "updated_at": self.clock.utc(c.created),
                          "system": False, "noteable_type": "MergeRequest", "noteable_iid": m.iid})
        return {
            "id": st.project_id * 10000 + m.iid, "iid": m.iid, "project_id": st.project_id,
            "title": self.humanize(m.title), "description": desc, "state": m.state,
            "created_at": self.clock.utc(m.created), "updated_at": self.clock.utc(m.updated),
            "merged_by": merged_by, "merge_user": merged_by,
            "merged_at": self.clock.utc(m.merged_at) if m.merged_at and m.state == "merged" else None,
            "closed_at": self.clock.utc(m.updated) if m.state == "closed" else None,
            "target_branch": "main", "source_branch": m.source_branch, "author": author,
            "assignees": [author] if author else [], "assignee": author,
            "reviewers": [u for u in (self.gl_user(r) for r in m.reviewers) if u],
            "labels": [], "draft": False, "work_in_progress": False, "merge_status": "can_be_merged",
            "detailed_merge_status": "mergeable" if m.state == "opened" else "not_open",
            "sha": st.head, "merge_commit_sha": merge_sha, "squash_commit_sha": None, "squash": False,
            "user_notes_count": len(notes),
            "references": {"short": f"!{m.iid}", "relative": f"!{m.iid}", "full": f"{m.repo}!{m.iid}"},
            "web_url": f"{self.ids.gitlab_base}/{m.repo}/-/merge_requests/{m.iid}",
            "notes": notes,
        }

    def on_commit(self, ev: WorldEvent) -> tuple[dict, Any]:
        c = _fields_of(Commit, ev.data or {}, self.warn)
        assert isinstance(c, Commit)
        sha = self.git_sha(c.sha, c.repo)
        self.commits[ev.artifact_id] = (c.repo, sha)
        # a merge commit is authored by whoever merges (GitLab), not by the author of the merge request
        merge = c.message.startswith("Merge branch") and self.dir.get(ev.actor) is not None
        w = self.dir.get(ev.actor if merge else c.author)
        msg = self.humanize(c.message).strip()
        payload = {
            "id": sha, "short_id": sha[:8], "created_at": self.clock.utc(c.created), "parent_ids": [],
            "title": msg.split("\n", 1)[0], "message": msg,
            "author_name": w.name if w else c.author, "author_email": w.email if w else None,
            "authored_date": self.clock.utc(c.created),
            "committer_name": w.name if w else c.author, "committer_email": w.email if w else None,
            "committed_date": self.clock.utc(c.created),
            "author_username": w.accounts.get(self.ids.gitlab) if w else None,
            "web_url": f"{self.ids.gitlab_base}/{c.repo}/-/commit/{sha}",
        }
        line = self.emit(ev, source=self.ids.gitlab, object_type="commit", external_id=f"{c.repo}@{sha}",
                         container=c.repo, payload=payload)
        return line, lambda: [self.versions.naive(R.commit_entity(c.repo, sha), payload["message"], "primary")]

    # ------------------------------------------------------------------ mail
    def address(self, ref: str) -> dict:
        w = self.dir.get(ref)
        if w is None:
            self.warn(f"mail participant {ref!r} is neither a persona, a contact nor an address")
            return {"emailAddress": {"name": str(ref), "address": str(ref)}}
        return {"emailAddress": {"name": w.name, "address": w.email}}

    def mail_acl(self, e: Email, ev: WorldEvent) -> list[str]:
        if e.mailbox_acl:
            return sorted(set(e.mailbox_acl))
        beat = self.beat_of(ev)
        acl: set[str] = set()
        for ref in [e.sender, *e.to, *e.cc]:
            w = self.dir.get(ref)
            if w is None:
                continue
            if w.employee and w.principal:
                acl.add(w.principal)
            elif w.mailbox and not (beat and beat.confidential):
                acl.update(self.dir.mailboxes.get(w.email or "", []))
        self.stats["mail_acl_computed"] += 1
        return sorted(acl)

    def attachment(self, k: int, a: Attachment) -> dict:
        meta = a.meta
        ext = a.filename.rsplit(".", 1)[-1].lower() if "." in a.filename else ""
        size = DOC_SIZES.get(ext, 2_000) + len(a.text.encode("utf-8"))
        return {
            "id": f"att-{k}", "name": a.filename, "contentType": a.content_type, "size": size,
            "extractedText": self.humanize(a.text),
            "documentMeta": {"docType": meta.doc_type, "title": meta.title, "number": meta.number,
                             "date": meta.doc_date.isoformat() if meta.doc_date else None,
                             "counterparty": self.dir.org_ref(meta.counterparty), "amount": meta.amount,
                             "currency": meta.currency if meta.amount is not None or meta.currency else None},
        }

    def on_email(self, ev: WorldEvent) -> tuple[dict, Any]:
        e = _fields_of(Email, ev.data or {}, self.warn)
        assert isinstance(e, Email)
        self.emails[e.id] = e
        self._by_message_id[e.message_id] = e
        refs: list[str] = []
        parent = self._by_message_id.get(e.in_reply_to) if e.in_reply_to else None
        if parent is not None:
            prefs = self._references.get(parent.id, [])
            refs = [*prefs, parent.message_id]
        elif e.in_reply_to:
            refs = [e.in_reply_to]
        self._references[e.id] = refs
        sent = self.clock.aware(e.sent)
        atts = [self.attachment(k, a) for k, a in enumerate(e.attachments, 1)]
        payload = {
            "id": e.id, "internetMessageId": e.message_id, "conversationId": e.thread_id,
            "subject": self.humanize(e.subject), "from": self.address(e.sender),
            "toRecipients": [self.address(x) for x in e.to], "ccRecipients": [self.address(x) for x in e.cc],
            "sentDateTime": self.clock.graph(sent), "receivedDateTime": self.clock.graph(sent + timedelta(seconds=40)),
            "inReplyTo": e.in_reply_to, "references": refs, "importance": "normal",
            "body": {"contentType": "text", "content": self.humanize(e.body)},
            "hasAttachments": bool(atts), "attachments": atts,
        }
        acl = self.mail_acl(e, ev)
        line = self.emit(ev, source=self.ids.mail, object_type="message", external_id=e.message_id, container=None,
                         payload=payload, acl=acl)

        def fallback() -> list[R.Touch]:
            out = [self.versions.naive(R.mail_entity(e.id), payload, "primary", e.subject)]
            for a in atts:
                out.append(self.versions.naive(R.attachment_entity(e.id, a["id"]), a, "attachment",
                                               str(a["documentMeta"].get("title") or a["name"])))
            return out

        return line, fallback

    # ------------------------------------------------------------------ report
    def report(self) -> dict[str, Any]:
        return {
            "tenant": self.ids.tenant,
            "events": len(self.events),
            "deliveries": len(self.lines),
            "by_source": dict(sorted(self.stats.items())),
            "failed_events": sorted(self.failed),
            "adapters": {"validated": self.versions.ok, "reason": self.versions.reason,
                         "no_adapter": dict(self.versions.missing), "errors": self.versions.errors[:200],
                         "error_count": len(self.versions.errors)},
            "warnings": self.warnings[:500],
            "page_ids": self.pages.items(),
            "handles": {h: {"type": t.type, "artifact_id": t.artifact_id, "att_id": t.att_id,
                            "url": self.handle_url(h)} for h, t in sorted(self.handles.items())},
        }


def _norm_label(text: Any) -> str:
    t = str(text or "").casefold()
    t = re.sub(r"\s+", " ", t)
    return t.strip(" \t\n:.;,*_-—–()[]")


# ================================================================================================ writers / main
def write_timeline(path: Path, lines: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        for line in lines:
            gz.write((json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            n += 1
    return n


def read_timeline(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def write_config(paths: Paths, company: Company, first_event: datetime | None) -> Path:
    ids = R.Ids(company.company.tenant_id, company.tools.mail_domain)
    header = f"# GENERATED by kbsim.export from sim/tenants/{paths.tenant}/company.yaml — do not edit by hand.\n"
    sources_path = paths.config_dir / "sources.yaml"
    write_yaml(sources_path, build_sources(company, ids, first_event), header)
    write_yaml(paths.config_dir / "people.yaml",
               build_people(company, ids, load_availability(paths.tenant_dir / "availability.yaml", company)), header)
    return sources_path


class ExportBlocked(SystemExit):
    """The audit of the world found more than the tenant allows (sim/tenants/<t>/delivery.yaml audit_thresholds)."""

    def __init__(self, over: dict[str, Any]):
        self.over = over
        super().__init__("export blocked by the audit thresholds: " + json.dumps(over, ensure_ascii=False))


def export_settings(tenant_dir: Path) -> dict[str, Any]:
    p = tenant_dir / "delivery.yaml"
    return (_yaml(p) or {}) if p.is_file() else {}


def audit_gate(tenant: str, sim_dir: Path) -> dict[str, Any] | None:
    """Run kbsim.audit when the tenant sets thresholds; the checks over their threshold, else None (A6)."""
    limits = export_settings(sim_dir / "tenants" / tenant).get("audit_thresholds") or {}
    if not limits:
        return None
    from kbsim.audit import Audit
    from kbsim.tenant import TenantState

    found = Audit(TenantState(tenant, sim_dir)).run()
    over = {k: {"found": len(found.get(k) or []), "allowed": int(v)} for k, v in limits.items()
            if len(found.get(k) or []) > int(v)}
    return over or None


def export(paths: Paths, *, validate: bool = True, config_only: bool = False) -> dict[str, Any]:
    company = load_company(paths.tenant_dir / "company.yaml")
    if company.company.tenant_id != paths.tenant:
        raise ValueError(f"company.yaml tenant_id {company.company.tenant_id!r} != {paths.tenant!r}")
    if config_only:
        sources_path = write_config(paths, company, None)
        return {"tenant": paths.tenant, "config": [str(sources_path), str(paths.config_dir / "people.yaml")]}
    scenario = load_scenario(paths.tenant_dir / "scenario.yaml")
    cases = load_cases(paths.tenant_dir / "cases.yaml")
    events = read_world(paths.world)
    sources_path = write_config(paths, company, events[0].ts if events else None)

    blocked = audit_gate(paths.tenant, paths.sim_dir)
    if blocked:
        raise ExportBlocked(blocked)
    ex = Exporter(company, scenario, events, sources_yaml=sources_path, validate=validate, out_dir=paths.out_dir)
    ex.run()
    lines = ex.timeline()
    n = write_timeline(paths.timeline, lines)

    resolved = None
    if cases is not None:
        failed = ex.failed | {e["seq"] for e in ex.versions.errors}
        resolved = R.resolve_cases(cases, scenario, ex.events, ex.touches, tenant=paths.tenant, tz=ex.clock.tz,
                                   failed=failed)
        resolved["generated_at"] = datetime.now(UTC).isoformat()
        resolved["timeline"] = str(paths.timeline)
        paths.resolved.parent.mkdir(parents=True, exist_ok=True)
        paths.resolved.write_text(json.dumps(resolved, ensure_ascii=False, indent=1), encoding="utf-8")
    report = ex.report() | {"timeline": str(paths.timeline), "lines": n,
                            "cases": (resolved or {}).get("stats")}
    from kbsim.delivery import dump_json, fixture_manifest

    man = fixture_manifest(paths.tenant, name=company.company.name, sim_dir=paths.sim_dir, sources=sources_path,
                           people=paths.config_dir / "people.yaml", timeline=paths.timeline, resolved=resolved,
                           report=report)
    (paths.fixtures_dir / "manifest.json").write_text(dump_json(man), encoding="utf-8")
    paths.report.parent.mkdir(parents=True, exist_ok=True)
    paths.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kbsim.export", description=__doc__.split("\n", 1)[0])
    ap.add_argument("tenant")
    ap.add_argument("--sim-dir", type=Path, default=SIM_DIR, help="kb/sim (tenants/, out/)")
    ap.add_argument("--kb-dir", type=Path, default=KB_DIR, help="kb (config/tenants/, fixtures/)")
    ap.add_argument("--world", type=Path, default=None, help="world.jsonl (default sim/out/<tenant>/world.jsonl)")
    ap.add_argument("--no-validate", action="store_true", help="do not normalize payloads with the backend adapters")
    ap.add_argument("--config-only", action="store_true",
                    help="only write config/tenants/<t>/{sources,people}.yaml from company.yaml (no world needed)")
    ap.add_argument("--delivery", type=Path, default=None, metavar="DIR",
                    help="write a delivery of the corpus contract into DIR instead (kbsim.delivery, 05 §1)")
    ap.add_argument("--no-sample", action="store_true", help="with --delivery: without sample/")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.delivery is not None:
        from kbsim.delivery import build

        rep = build(args.tenant, args.delivery, sim_dir=args.sim_dir, kb_dir=args.kb_dir, validate=not args.no_validate,
                    with_sample=not args.no_sample)
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0
    paths = Paths(args.tenant, args.sim_dir, args.kb_dir, args.world)
    if args.config_only:
        print(json.dumps(export(paths, config_only=True), ensure_ascii=False, indent=1))
        return 0
    rep = export(paths, validate=not args.no_validate)
    summary = {k: rep[k] for k in ("tenant", "events", "deliveries", "by_source", "failed_events", "cases")}
    summary["adapters"] = {k: rep["adapters"][k] for k in ("validated", "reason", "no_adapter", "error_count")}
    summary["warnings"] = len(rep["warnings"])
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 1 if rep["failed_events"] or rep["adapters"]["error_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
