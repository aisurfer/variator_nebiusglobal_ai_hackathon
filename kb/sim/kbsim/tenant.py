"""Read-only view of a simulated tenant: its files, its world log and its working calendar.

Plancy's kbsim generated a world with an engine, a director and LLM agents (kbsim.engine / director / agents / step).
This copy keeps only the parts that work on a finished world - export, delivery, audit and densification - and they
read the tenant through `TenantState` instead of the engine:

  sim/tenants/<t>/company.yaml        the company (people, organisations, contacts, tools)
  sim/tenants/<t>/scenario.yaml       optional: storylines (beats, facts, decisions); empty when absent
  sim/tenants/<t>/availability.yaml   optional: absences of people {absences: {persona_id: [[from, to], ...]}}
  sim/tenants/<t>/banned_terms.yaml   optional: regexes of topics that must not appear {banned: [...]}
  sim/out/<t>/world.jsonl             the event log (kbsim.world)
  sim/out/<t>/state.json              optional: texts of business documents {director: {docs: {handle: text}}}
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from kbsim.models import Company, Scenario
from kbsim.world import World

SIM_ROOT = Path(__file__).resolve().parents[1]  # kb/sim


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


def load_tenant(tenant: str, root: Path | None = None) -> tuple[Company, Scenario]:
    base = (root or SIM_ROOT) / "tenants" / tenant
    company = Company.model_validate(_yaml(base / "company.yaml"))
    raw = _yaml(base / "scenario.yaml")
    scenario = Scenario.model_validate(raw) if raw else Scenario(arcs=[], beats=[])
    return company, scenario


class Days:
    """The working calendar of a tenant: day numbers (day 1 = company.start_date), workdays, absences."""

    def __init__(self, company: Company, tenant_dir: Path, out_dir: Path | None = None):
        self.company = company
        self.tz = ZoneInfo(company.company.timezone)
        self.holidays = set(company.company.holidays)
        self.absences: dict[str, list[tuple[date, date]]] = {}
        avail = _yaml(tenant_dir / "availability.yaml") or {}
        for pid, spans in (avail.get("absences") or {}).items():
            self.absences[pid] = [(date.fromisoformat(str(a)), date.fromisoformat(str(b))) for a, b in spans]
        self.docs: dict[str, str] = {}  # document handle -> full text (written when the world was generated)
        state = out_dir / "state.json" if out_dir else None
        if state and state.is_file():
            st = json.loads(state.read_text(encoding="utf-8"))
            self.docs.update(((st.get("director") or {}).get("docs")) or {})

    def date_of(self, day: int) -> date:
        return self.company.company.start_date + timedelta(days=day - 1)

    def is_workday(self, day: int) -> bool:
        d = self.date_of(day)
        return d.weekday() < 5 and d not in self.holidays

    def absent(self, persona_id: str, day: int) -> bool:
        d = self.date_of(day)
        return any(a <= d <= b for a, b in self.absences.get(persona_id, []))


WRITER_RULES = """You write texts for a simulation of the working life of a company. The texts become the corpus of a
company knowledge base, so they must look like real working artefacts: e-mails, Jira tickets and comments, Confluence
pages and meeting notes, GitLab merge requests.

Rules:
1. Write in English, in the voice of the person named in the request (role, manner, length of sentences).
2. Refer to the past the way people do ("as Olga wrote on Thursday", "after yesterday's call"); no URLs and no
   internal ids of messages or pages. Ticket keys (SAAS-123) are natural in Jira, GitLab and commits.
3. Never contradict this handbook or the request: amounts, dates, decisions and numbers come from there. Do not invent
   new major decisions or figures; small working details (steps, questions, clarifications) are fine.
4. No notes about the simulation, roles or instructions inside the texts."""


def handbook(company: Company) -> str:
    """The company handbook every writer reads once: people, organisations, contacts and tools."""
    c, t = company.company, company.tools
    lines = [f"# Company handbook: {c.name} ({c.city})", c.description, "", f"Culture: {c.culture}", ""]
    if company.products:
        lines.append("## Products")
        lines += [f"- {p.name} (id {p.id}): {p.description}" for p in company.products]
    if company.projects:
        lines.append("## Projects")
        for p in company.projects:
            extra = ", ".join(x for x in [f"client {p.client_org}" if p.client_org else "",
                                          f"Jira {p.jira_project}" if p.jira_project else "",
                                          f"GitLab {p.gitlab_project}" if p.gitlab_project else ""] if x)
            lines.append(f"- {p.name} (id {p.id}; {extra}): {p.description}")
    lines.append("## Employees (id - name - role - e-mail - manager)")
    for p in company.people:
        lines.append(f"- {p.id} - {p.name} - {p.role}, {p.department} - {p.email} - {p.manager or '-'}")
    lines.append("## External organisations (id - name - kind)")
    lines += [f"- {o.id} - {o.name} - {o.kind}: {o.description}" for o in company.organizations]
    lines.append("## External contacts (id - name - organisation - role - e-mail)")
    lines += [f"- {x.id} - {x.name} - {x.org} - {x.role} - {x.email}" for x in company.contacts if not x.confidential]
    lines.append("## Tools")
    lines.append("Jira projects: " + "; ".join(f"{k} \"{v.name}\"" for k, v in t.jira.items()))
    lines.append("Jira statuses: To Do -> In Progress -> Review -> Ready for QA -> Done; also Won't Do, Reopened.")
    lines.append("Confluence spaces: " + "; ".join(f"{k} \"{v.name}\"" for k, v in t.confluence.items()))
    lines.append("GitLab repositories: " + "; ".join(f"{k} \"{v.name}\"" for k, v in t.gitlab.items()))
    if t.group_mailboxes:
        lines.append("Group mailboxes: " + "; ".join(t.group_mailboxes))
    if c.glossary:
        lines.append("## Glossary")
        lines += [f"- {k}: {v}" for k, v in c.glossary.items()]
    return "\n".join(lines)


class TenantState:
    """A finished simulated tenant, read-only: company, scenario, world, calendar, banned terms."""

    def __init__(self, tenant: str, root: Path | None = None):
        self.tenant = tenant
        self.root = root or SIM_ROOT
        self.tenant_dir = self.root / "tenants" / tenant
        self.company, self.scenario = load_tenant(tenant, self.root)
        self.out = self.root / "out" / tenant
        self.w = World(self.company, self.out)
        self.w.load()
        self.d = Days(self.company, self.tenant_dir, self.out)
        self.banned: list[str] = list((_yaml(self.tenant_dir / "banned_terms.yaml") or {}).get("banned") or [])

    @property
    def handbook_path(self) -> Path:
        """sim/out/<t>/handbook.md: the writer rules and the company handbook (rewritten when the company changes)."""
        path = self.out / "handbook.md"
        text = WRITER_RULES + "\n\n" + handbook(self.company)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return path
