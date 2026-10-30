"""Contracts of the company simulator (Docs/KB/03-SIMULATION.md).

Three layers, each a file under sim/tenants/<tenant>/:
  company.yaml   -> Company: personas (one LLM agent per employee), organisations, contacts, tools and ACLs.
  scenario.yaml  -> Scenario: storylines (arcs) made of beats - facts the generator must get into the data.
  cases.yaml     -> Cases: 100 evaluation questions written BEFORE generation (query-first, as in HERB),
                    each pointing at the beats whose artefacts are the ground truth.
A generator writes sim/out/<tenant>/world.jsonl: an append-only log of WorldEvent - the ground truth from which
the exporter renders provider payloads (Jira, Confluence, GitLab, mail) and resolves case answers. (XWiki pages may
still exist in a world log; this copy has no XWiki connector, so the exporter skips them.)
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PersonaId = str  # short handle used everywhere in scenario/cases: "ceo", "accountant", "dev_backend"
ContactId = str  # external person handle: "tl_ivanov"
BeatId = str  # "PAY-03"
Handle = str  # named artefact created by a beat and referenced later: "ticket:pay-integration"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- company.yaml
class Persona(_M):
    id: PersonaId
    employee_id: str  # "KD-E01"
    name: str  # "Andrew Sokolov"
    gender: Literal["m", "f"]  # pronouns in generated prose
    role: str  # "Chief Executive Officer"
    department: str
    email: str  # "a.sokolov@company.example"
    principal: str  # "user:a.sokolov"
    groups: list[str]  # ACL groups, always includes "grp:all-staff"
    manager: PersonaId | None = None
    personality: str  # 2-4 sentences: temperament, values, typical conflicts
    writing_style: str  # how they write (length, greetings, emoji, typos, formality)
    responsibilities: list[str]
    goals: list[str]  # what they push for during the 100 days
    relationships: dict[PersonaId, str] = Field(default_factory=dict)
    routine: list[str]  # recurring work a generator can suggest ("payment reconciliation on Fridays")
    accounts: dict[str, str]  # source_id -> account id (jira/confluence accountId, gitlab username, xwiki user, mail)
    work_hours: str = "09:30-18:30"
    activity: float = 0.6  # probability to act in a tick without directives or inbox


class Organization(_M):
    id: str
    name: str  # "TransLogistics Ltd"
    kind: Literal["client", "prospect", "vendor", "partner", "bank", "landlord", "state", "other"]
    domain: str  # mail domain
    inn: str | None = None
    description: str


class Contact(_M):
    id: ContactId
    name: str
    gender: Literal["m", "f"]
    org: str  # Organization.id
    role: str
    email: str
    writing_style: str
    confidential: bool = False  # not listed in the shared handbook (e.g. job candidates)


class Container(_M):
    key: str  # Jira project key / Confluence space key / GitLab project path / XWiki space
    name: str
    acl: list[str]  # groups or principals


class Tools(_M):
    """Source instances of the tenant; ids become source_ids in the knowledge backend."""

    jira: dict[str, Container]  # source "jira-<tenant>": projects
    confluence: dict[str, Container]  # spaces
    gitlab: dict[str, Container]  # project paths
    xwiki: dict[str, Container] = Field(default_factory=dict)  # spaces (not exported: no XWiki connector)
    mail_domain: str  # internal mail domain
    group_mailboxes: dict[str, list[str]] = Field(default_factory=dict)  # "info@company.example" -> [groups]


class Product(_M):
    id: str
    name: str
    description: str


class Project(_M):
    id: str
    name: str
    client_org: str | None = None
    description: str
    jira_project: str | None = None
    gitlab_project: str | None = None


class CompanyInfo(_M):
    tenant_id: str  # "company"
    name: str  # "Northwind Digital Ltd"
    short_name: str  # "Northwind"
    city: str
    timezone: str = "Europe/Moscow"
    description: str
    start_date: date  # day 1 of the simulation
    days: int = 100
    holidays: list[date] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    culture: str  # how people work: meetings, chats, conflicts, formality


class Company(_M):
    company: CompanyInfo
    people: list[Persona]
    organizations: list[Organization]
    contacts: list[Contact]
    products: list[Product]
    projects: list[Project]
    tools: Tools

    @model_validator(mode="after")
    def _refs(self) -> "Company":
        ids = [p.id for p in self.people]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate persona ids")
        orgs = {o.id for o in self.organizations}
        for c in self.contacts:
            if c.org not in orgs:
                raise ValueError(f"contact {c.id}: unknown org {c.org}")
        return self


# ---------------------------------------------------------------- scenario.yaml
Channel = Literal[
    "email",  # actor -> audience (internal or external recipients), optional attachments (docs)
    "incoming_email",  # external contact (actor = contact id) -> audience, optional attachments
    "jira_create",  # new ticket in `container` (project key); creates handle
    "jira_comment",  # comment on `target` ticket handle
    "jira_transition",  # status/assignee change of `target`, optional comment
    "meeting",  # actor organises; audience attends; produces Confluence meeting notes in `container`
    "page",  # Confluence page create (no target) or update (target = page handle)
    "wiki",  # XWiki page create/update in `container` (space)
    "mr",  # GitLab merge request create in `container` (repo) or review/merge of `target`
]


class Fact(_M):
    """A string that must appear in the artefact. `any` lists acceptable surface forms (number formats, cases)."""

    key: str
    any: list[str]


class DocSpec(_M):
    """A business document attached to an email (contract, invoice, act, proposal, claim, letter...)."""

    handle: Handle  # "doc:contract-translog"
    doc_type: Literal["contract", "amendment", "invoice", "act", "proposal", "claim", "letter", "specification",
                      "price_list", "report", "order", "nda", "other"]
    title: str  # "Service agreement No. 14/2026"
    number: str | None = None
    doc_date: date | None = None
    counterparty: str | None = None  # Organization.id
    amount: float | None = None
    currency: str | None = "USD"
    key_terms: list[str] = Field(default_factory=list)  # facts that must be in the document text
    filename: str  # "Agreement_14-2026_TransLogistics.docx"


class Beat(_M):
    id: BeatId
    arc: str
    day: int  # 1..company.days
    tick: Literal["am", "pm"] = "am"
    actor: str  # persona id, or contact id for incoming_email
    channel: Channel
    audience: list[str] = Field(default_factory=list)  # persona / contact ids (recipients, attendees, assignee)
    container: str | None = None  # project / space / repo key where it happens
    target: Handle | None = None  # existing artefact to comment/update/transition
    creates: Handle | None = None  # handle of the artefact this beat creates (ticket/page/mr/email thread)
    intent: str  # what the actor must do/say, written for the actor
    facts: list[Fact] = Field(default_factory=list)
    refers_to: list[BeatId] = Field(default_factory=list)  # earlier beats; their artefacts are shown to the actor
    reference_style: Literal["implicit", "explicit", "structural"] = "implicit"
    # implicit: talk like people do ("as Olga wrote on Thursday"), never keys/URLs; explicit: keys allowed;
    # structural: also set the tool's link field (Jira link, "Closes" in MR, page property)
    attachments: list[DocSpec] = Field(default_factory=list)
    status: dict[str, str] = Field(default_factory=dict)  # jira_transition: {"status": "...", "assignee": "..."}
    decision: str | None = None  # decision id this beat makes/changes ("D-PAY")
    decision_state: Literal["proposed", "decided", "reversed", "restored", "amended", "cancelled"] | None = None
    confidential: bool = False  # restricted container / private mail only
    notes: str | None = None  # for authors, never shown to agents


class Decision(_M):
    id: str
    title: str
    history: list[dict[str, str]]  # [{"beat": "PAY-03", "state": "decided", "summary": "..."}]


class Arc(_M):
    id: str
    title: str
    summary: str
    actors: list[str]


class Scenario(_M):
    arcs: list[Arc]
    beats: list[Beat]
    decisions: list[Decision] = Field(default_factory=list)

    @model_validator(mode="after")
    def _order(self) -> "Scenario":
        seen: dict[str, Beat] = {}
        handles: dict[str, Beat] = {}
        for b in sorted(self.beats, key=lambda x: (x.day, x.tick == "pm")):
            if b.id in seen:
                raise ValueError(f"duplicate beat {b.id}")
            for r in b.refers_to:
                if r not in seen:
                    raise ValueError(f"{b.id} refers to {r} which is not earlier")
            if b.target and b.target not in handles:
                raise ValueError(f"{b.id} targets {b.target} which no earlier beat creates")
            seen[b.id] = b
            if b.creates:
                handles[b.creates] = b
            for d in b.attachments:
                handles[d.handle] = b
        return self


# ---------------------------------------------------------------- cases.yaml
CaseCategory = Literal[
    "lookup",  # who/what/when single fact
    "why",  # causal chain back to the root decision/meeting/document
    "history",  # how something changed; decision reversed and restored
    "as_of",  # what was known/true at a past moment
    "implicit_ref",  # resolving "that agreement", "as we agreed on Thursday"
    "stale",  # what relies on a changed/cancelled decision or document
    "conflict",  # two sources disagree
    "acl",  # a principal must NOT see something
    "numeric",  # amounts, dates, percentages from business documents
    "negative",  # never happened / not discussed
    "multi_hop",  # several artefacts across tools must be joined
    "impact",  # what-if: what would be affected
    "people",  # who owns/participated/approved
]


class CaseExpect(_M):
    answer_facts: list[Fact] = Field(default_factory=list)  # a correct answer contains these
    evidence_beats: list[BeatId] = Field(default_factory=list)  # artefacts that must be retrieved/cited
    root_beats: list[BeatId] = Field(default_factory=list)  # why: the first causes
    forbidden_beats: list[BeatId] = Field(default_factory=list)  # acl: must not be visible/cited
    expect_not_found: bool = False  # negative: the system should say it found nothing


class Case(_M):
    id: str  # "C001"
    difficulty: Literal["trivial", "tricky"]
    category: CaseCategory
    question: str  # Russian, as a manager would ask
    principal: str = "user:admin"  # who asks
    known_at: datetime | None = None  # ask "as known at"
    split: Literal["dev", "holdout"] | None = None  # tune on dev only, report on holdout (03-SIMULATION §9)
    expect: CaseExpect
    rationale: str  # why it is tricky / what it tests


class Cases(_M):
    cases: list[Case]


# ---------------------------------------------------------------- world.jsonl (generator output)
ArtifactType = Literal["email", "ticket", "page", "wiki", "mr", "commit", "doc"]
EventKind = Literal[
    "email.sent",  # data: Email
    "ticket.created",  # data: Ticket (full state)
    "ticket.commented",  # data: {"comment": TicketComment}
    "ticket.updated",  # data: {"changes": {field: new_value}, "comment": TicketComment|None}
    "page.created",  # data: Page
    "page.updated",  # data: {"body": str, "title": str|None, "properties": dict|None, "decisions": [...]|None}
    "wiki.created",  # data: WikiPage
    "wiki.updated",  # data: {"body": str, "title": str|None, "objects": dict|None}
    "mr.created",  # data: MergeRequest
    "mr.commented",  # data: {"comment": MRComment}
    "mr.merged",  # data: {"merge_commit": str}
    "commit.pushed",  # data: Commit
]


class TicketComment(_M):
    id: str
    author: PersonaId
    body: str
    created: datetime


class Ticket(_M):
    key: str  # "SAAS-42"
    project: str
    issue_type: str  # Jira English names: Task/Bug/Story/Epic/Sub-task
    summary: str
    description: str
    status: str  # To Do / In Progress / Review / Done / Won't Do ...
    priority: str = "Medium"
    reporter: PersonaId
    assignee: PersonaId | None = None
    labels: list[str] = Field(default_factory=list)
    parent: str | None = None  # epic/parent key
    links: list[dict[str, str]] = Field(default_factory=list)  # [{"type": "blocks"|"relates"|"is caused by", "key": ...}]
    remote_links: list[dict[str, str]] = Field(default_factory=list)  # [{"relationship": ..., "target": handle|url, "title": ...}]
    comments: list[TicketComment] = Field(default_factory=list)
    created: datetime
    updated: datetime


class Attachment(_M):
    doc_id: str  # world doc id
    filename: str
    content_type: str
    text: str  # extracted full text of the document
    meta: DocSpec


class Email(_M):
    id: str  # "msg-000123"
    message_id: str  # "<20260616.0931.000123@company.example>"
    thread_id: str  # conversation id
    in_reply_to: str | None = None  # message_id of the parent
    sender: str  # persona id or contact id
    to: list[str]
    cc: list[str] = Field(default_factory=list)
    subject: str
    body: str
    sent: datetime
    attachments: list[Attachment] = Field(default_factory=list)
    mailbox_acl: list[str] = Field(default_factory=list)  # principals/groups that can read it


class Page(_M):
    id: str  # "page-0042" (exported as numeric Confluence id)
    space: str
    title: str
    kind: Literal["doc", "meeting_notes", "spec", "report", "postmortem", "plan", "retro", "other"] = "doc"
    body: str  # markdown
    author: PersonaId
    attendees: list[PersonaId] = Field(default_factory=list)
    held_at: datetime | None = None
    decisions: list[str] = Field(default_factory=list)
    action_items: list[dict[str, str]] = Field(default_factory=list)  # [{"assignee": persona, "text": ...}]
    properties: dict[str, str] = Field(default_factory=dict)  # page-properties table (label -> value)
    created: datetime
    updated: datetime
    version: int = 1


class WikiPage(_M):
    id: str  # "Regulations.ExpensePolicy"
    space: str
    title: str
    body: str  # markdown (exported as xwiki/2.1)
    author: PersonaId
    objects: dict[str, str] = Field(default_factory=dict)
    created: datetime
    updated: datetime
    version: int = 1


class MRComment(_M):
    id: str
    author: PersonaId
    body: str
    created: datetime


class MergeRequest(_M):
    id: str  # "company/menu-backend!17"
    repo: str
    iid: int
    title: str
    description: str
    author: PersonaId
    reviewers: list[PersonaId] = Field(default_factory=list)
    state: Literal["opened", "merged", "closed"] = "opened"
    source_branch: str
    closes: list[str] = Field(default_factory=list)  # ticket keys
    comments: list[MRComment] = Field(default_factory=list)
    created: datetime
    updated: datetime
    merged_at: datetime | None = None
    merge_commit: str | None = None


class Commit(_M):
    sha: str
    repo: str
    author: PersonaId
    message: str
    created: datetime


class WorldEvent(_M):
    seq: int
    ts: datetime  # simulated time (tenant timezone offset)
    day: int
    actor: str  # persona id, contact id or "system"
    kind: EventKind
    artifact_type: ArtifactType
    artifact_id: str  # ticket key / email id / page id / wiki id / mr id / commit sha
    data: dict[str, Any]
    beat_id: BeatId | None = None  # the scenario beat this event realises
    handle: Handle | None = None  # handle bound to the artefact by this event


# ---------------------------------------------------------------- agent output (one LLM call per agent per tick)
ActionType = Literal["email", "jira_create", "jira_comment", "jira_transition", "page", "wiki", "mr_create",
                     "mr_comment", "mr_merge", "note"]


class Action(BaseModel):
    """Flat action proposed by an employee writer; the generator validates fields per type and owns ids/time."""

    type: ActionType
    beat_id: str | None = None  # directive this action fulfils
    # email
    to: list[str] | None = None  # persona ids, contact ids or e-mail addresses
    cc: list[str] | None = None
    subject: str | None = None
    reply_to: str | None = None  # email id from the inbox
    attach: list[str] | None = None  # doc handles offered in the directive
    # jira
    project: str | None = None
    issue_type: str | None = None
    issue: str | None = None  # ticket key for comment/transition
    summary: str | None = None
    status: str | None = None
    assignee: str | None = None
    priority: str | None = None
    labels: list[str] | None = None
    link_issue: str | None = None  # structural link target key
    link_type: str | None = None
    # page / wiki
    space: str | None = None
    page: str | None = None  # existing page/wiki id to update
    title: str | None = None
    kind: str | None = None
    attendees: list[str] | None = None
    decisions: list[str] | None = None
    action_items: list[str] | None = None  # "persona_id: text"
    # mr
    repo: str | None = None
    mr: str | None = None  # existing MR id
    closes: list[str] | None = None
    # shared
    body: str | None = None  # email body, description, comment, page/wiki markdown, MR description/comment, note


class AgentTurn(BaseModel):
    actions: list[Action]
