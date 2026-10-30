"""Language of the knowledge base: English only.

Plancy's knowledge base processed Russian and English; this copy for the VARIATOR MVP keeps English only. Answers,
explanations, labels and every text rule are English. A tenant may still declare `tenant.language: en` in sources.yaml.
"""
from __future__ import annotations

import re
from typing import Literal

Lang = Literal["en"]
LANGS: tuple[str, ...] = ("en",)
DEFAULT: Lang = "en"


def humanize(identifier: str) -> str:
    """«CommercialProposal» -> «commercial proposal», «informed_by» -> «informed by» (English fallback labels)."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", identifier).replace("_", " ")
    return s.lower() if not s.isupper() else s


# English labels of the seed ontology (the kind and predicate rows of config/ontology.yaml carry the same labels)
KIND_EN = {
    "Person": "person", "Account": "account", "Group": "group", "Project": "project", "Space": "space",
    "Repository": "repository", "Meeting": "meeting", "Decision": "decision", "ActionItem": "action item",
    "Epic": "epic", "Story": "story", "Task": "task", "Bug": "bug", "Issue": "issue (GitLab)",
    "Experiment": "experiment", "Spec": "requirements (PRD)", "Report": "report", "Document": "document",
    "MergeRequest": "merge request", "Commit": "commit", "Release": "release", "Policy": "policy",
    "ADR": "architecture decision", "KBArticle": "knowledge base article", "Email": "email",
    "Organization": "organization", "Contract": "contract", "Amendment": "amendment", "Invoice": "invoice",
    "Act": "acceptance act", "CommercialProposal": "commercial proposal", "Claim": "claim",
    "Letter": "official letter", "Specification": "specification", "PriceList": "price list", "Order": "order",
    "NDA": "NDA", "BusinessDocument": "business document",
    "Payment": "payment", "BankEntry": "bank entry", "Deal": "deal", "EmployeeEvent": "employee event",
    "Requisition": "job requisition", "Payroll": "payroll", "Incident": "incident", "Deploy": "deployment",
    "SupportTicket": "support ticket", "CloudBill": "cloud bill", "TaxPayment": "tax payment",
    "NpsResponse": "NPS response",
    "Hypothesis": "hypothesis", "LifeEvent": "life event", "Habit": "habit", "Process": "process",
}
PREDICATE_EN = {  # predicate -> (label, inverse label)
    "decided_in": ("decided at", "decision made at"), "motivated_by": ("motivated by", "motivates"),
    "informed_by": ("informed by", "informs"), "implements": ("implements", "implemented by"),
    "part_of": ("part of", "includes work"), "closes": ("closes", "closed by"), "includes": ("includes", "shipped in"),
    "based_on": ("based on", "basis for"), "governed_by": ("governed by", "governs"),
    "blocked_by": ("blocked by", "blocks"), "depends_on": ("depends on", "needed by"), "caused_by": ("caused by", "caused"),
    "supersedes": ("supersedes", "superseded by"), "refers_to": ("refers to", "referred to by"),
    "replies_to": ("replies to", "replied in"), "in_project": ("in project", "contains"),
    "in_space": ("in space", "contains"), "in_repository": ("in repository", "contains"), "author": ("author", "authored"),
    "assignee": ("assignee", "assigned"), "reporter": ("reporter", "reported"), "owner": ("owner", "owns"),
    "participant": ("participant", "took part"), "mentions": ("mentions", "mentioned in"),
    "relates_to": ("relates to", "relates to"), "status": ("status", "status"), "labels": ("labels", "labels"),
    "addressed_to": ("addressed to", "received"), "cc_to": ("cc", "received a copy"), "in_thread": ("in thread", "contains"),
    "attached_to": ("attached to", "has attachment"), "counterparty": ("counterparty", "counterparty of"),
    "works_at": ("works at", "employs"),
    "under_contract": ("under contract", "covers"), "settles": ("settles", "settled by"),
    "records": ("records", "recorded in"), "employee": ("employee", "employee record"),
    "component": ("component", "component"),
}


def kind_label(kind_id: str | None, label: str | None = None) -> str:
    """The English label of a kind (the ontology row's label for a kind outside the seed ontology)."""
    return KIND_EN.get(kind_id or "") or label or humanize(kind_id or "object")


def predicate_label(predicate_id: str, label: str | None = None, inverse: bool = False) -> str:
    """The English label of a predicate (or of its inverse)."""
    pair = PREDICATE_EN.get(predicate_id)
    if pair:
        return pair[1] if inverse else pair[0]
    return label or humanize(predicate_id)
