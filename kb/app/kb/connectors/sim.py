"""Simulation adapter (provider `sim`): the organisation simulator's event envelope -> one business record per line.

A line of a `sim` source carries the simulator's event envelope as its payload (VARIATOR `Event.to_timeline_line`):
  {run, option, scenario, ts, process, kind, entity_id, fields{}, causes[{type, id}], seq}
object_type is the business object type below, external_id equals the payload's entity_id. One business object is
one entity across its versions: every event of it (issued, sent, paid) is a new version. So `fields` holds the full
state of the object at `ts` (not only what changed), and `causes` every cause that still holds: like a Jira link that
disappears from the issue, a cause missing from a later event closes its causal link.

Entity id: sim.<entity_id> (sim.INV-2026-0031). Entity ids are unique across the object types of the source.

  object_type      kind             object_type      kind             cause catalog (targets of causes that are
  contract         Contract         requisition      Requisition      not business records)
  invoice          Invoice          payroll          Payroll            hypothesis   Hypothesis
  payment          Payment          incident         Incident           life_event   LifeEvent
  bank_entry       BankEntry        deploy           Deploy             habit        Habit
  deal             Deal             support_ticket   SupportTicket      rule         Policy
  employee_event   EmployeeEvent    cloud_bill       CloudBill          process      Process
  decision         Decision         tax_payment      TaxPayment
  nps_response     NpsResponse      (a client contact's answer to an NPS survey: client, contact, score 0-10, survey)
An unknown object type, a malformed envelope or an unknown cause type raises: the journal quarantines the delivery.

Declared links (no text extraction: every relation is a field of the envelope):
  causes[{type, id}]          -> causal link (effect = this record): decision implements, hypothesis based_on,
                                 life_event caused_by, habit motivated_by, rule governed_by, process caused_by
  supersedes (a decision)     -> supersedes (causal)
  client / vendor / counterparty / authority (organisation id of people.yaml)  -> counterparty (qualifier role)
  owner / assignee / author / reporter / employee (person or contact id of people.yaml) -> predicate of that name
  contact (a contact id of people.yaml: the client contact who wrote the record, e.g. the NPS respondent) -> author
  actor (person id)           -> the author of this version
  contract -> under_contract;  settles -> settles;  records -> records;  deal, incident, related -> relates_to
  status -> status;  project -> in_project;  component -> component   (literal statements)
A reference to another simulated object is its entity_id and resolves to sim.<id>, whatever its object type; an id
that is already a knowledge-base entity id (jira.ENG-1, gitlab.<project>.mr-1, mail.<id>, confluence.page-<id>,
person.<id>, org.<id>, sim.<id>) is used as it is. `<field>_name` (client_name, owner_name, ...) labels a reference.
"""
from __future__ import annotations

import re
from typing import Any

from kb.connectors._util import Links, aliases, clean_ws, evidence, iso, items, parse_dt, record_acl, string, truncate
from kb.connectors.base import AdapterContext, SourceConfig, register
from kb.connectors.directory import organization_entity_id, person_entity_id
from kb.contracts import CanonicalRecord, Ref, SourceEnvelope
from kb.lang import kind_label

KIND_BY_TYPE: dict[str, str] = {
    "contract": "Contract", "invoice": "Invoice", "payment": "Payment", "bank_entry": "BankEntry", "deal": "Deal",
    "employee_event": "EmployeeEvent", "requisition": "Requisition", "payroll": "Payroll", "incident": "Incident",
    "deploy": "Deploy", "support_ticket": "SupportTicket", "cloud_bill": "CloudBill", "tax_payment": "TaxPayment",
    "decision": "Decision", "nps_response": "NpsResponse",
    # the cause catalog
    "hypothesis": "Hypothesis", "life_event": "LifeEvent", "habit": "Habit", "rule": "Policy", "process": "Process",
}
OBJECT_TYPES = tuple(KIND_BY_TYPE)

# The cause types of the simulator (VARIATOR orgsim.events.CAUSE_PREDICATES) and their ontology predicates.
CAUSE_PREDICATES: dict[str, str] = {
    "decision": "implements", "hypothesis": "based_on", "life_event": "caused_by", "habit": "motivated_by",
    "rule": "governed_by", "process": "caused_by",
}
# based_on and motivated_by have no typical sensitivity that fits a catalog record: what the effect builds on is the
# statement of the hypothesis or habit. The other predicates keep their ontology sensitivity (decision: statement,
# rule: rules; caused_by: any change).
CAUSE_SENSITIVITY: dict[str, list[str]] = {"hypothesis": ["statement"], "habit": ["statement"]}

ORG_FIELDS = ("client", "vendor", "counterparty", "authority")
PERSON_FIELDS = ("owner", "assignee", "author", "reporter", "employee")
CONTACT_FIELDS = {"contact": "author"}  # a client contact who wrote the record (the respondent of an NPS response)
REF_FIELDS = {"contract": "under_contract", "settles": "settles", "records": "records", "deal": "relates_to",
              "incident": "relates_to", "related": "relates_to"}
LITERAL_FIELDS = {"status": "status", "project": "in_project", "component": "component"}
META_FIELDS = ("event", "event_at", "process", "simulation", "causes")  # written by the adapter, after `fields`
_KB_ID = re.compile(r"^(?:sim|jira|confluence|gitlab|mail|person|org)\.\S+$")
_UNSAFE = re.compile(r"[^0-9A-Za-z_.@-]+")
_NOT_DETAILS = {"title", "description", "statement", "rules", "actor", "actor_name", "comment", "comment_slot"}


class UnknownObjectType(ValueError):
    """A sim line whose object_type the adapter does not know (the delivery is quarantined)."""


def slug(value: Any) -> str:
    """An entity-id segment: characters outside [0-9A-Za-z_.@-] become '-'."""
    return _UNSAFE.sub("-", str(value).strip()).strip("-") or "x"


def sim_entity_id(sim_id: Any) -> str:
    """The knowledge-base entity of a simulated object; an id that already is a KB entity id is kept."""
    s = str(sim_id).strip()
    return s if _KB_ID.match(s) else f"sim.{slug(s)}"


def sim_ref(sim_id: Any, label: Any = None) -> Ref | None:
    if not isinstance(sim_id, (str, int)) or isinstance(sim_id, bool) or str(sim_id).strip() == "":
        return None
    return Ref(entity_id=sim_entity_id(sim_id), label=truncate(str(label or sim_id), 200))


def person_ref(person_id: Any, label: Any = None) -> Ref | None:
    """An employee or an external contact of people.yaml: both are person.<id> (kb.connectors.directory)."""
    if not isinstance(person_id, str) or not person_id.strip():
        return None
    pid = person_id.strip()
    return Ref(entity_id=pid if pid.startswith("person.") else person_entity_id(pid), label=string(label) or pid)


def org_ref(ctx: AdapterContext, org_id: Any, label: Any = None) -> Ref | None:
    """An organisation of people.yaml, as the mail adapter references it (the directory source's organization)."""
    if not isinstance(org_id, str) or not org_id.strip():
        return None
    oid = org_id.strip()
    dirs = ctx.tenant_sources.by_provider("directory") if ctx.tenant_sources else []
    if dirs and not oid.startswith("org."):
        return Ref(source_id=dirs[0].id, object_type="organization", external_id=oid, label=string(label) or oid)
    return Ref(entity_id=oid if oid.startswith("org.") else organization_entity_id(oid), label=string(label) or oid)


def kind_of(object_type: str) -> str:
    try:
        return KIND_BY_TYPE[object_type]
    except KeyError:
        raise UnknownObjectType(
            f"sim: unknown object type {object_type!r} (known: {', '.join(OBJECT_TYPES)})") from None


def _ids(value: Any) -> list[Any]:
    return items(value) if isinstance(value, list) else ([value] if value not in (None, "") else [])


def _label(f: dict[str, Any], key: str) -> Any:
    return f.get(f"{key}_name") or f.get(key)


def _money(f: dict[str, Any]) -> str | None:
    amount = f.get("amount")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    return f"{amount:,.2f}" + (f" {f['currency']}" if string(f.get("currency")) else "")


def _title(object_type: str, f: dict[str, Any], sim_id: str) -> str:
    if string(f.get("title")):
        return " ".join(f["title"].split())
    num = string(f.get("number")) or sim_id
    party = next((str(_label(f, k)) for k in ORG_FIELDS if _label(f, k)), None)
    to = f" — {party}" if party else ""
    money = _money(f)
    if object_type in ("contract", "invoice"):
        return f"{object_type.capitalize()} {num}{to}"
    if object_type in ("payment", "bank_entry"):
        what = "Payment" if object_type == "payment" else "Bank entry"
        return f"{what} {num}" + (f", {money}" if money else "") + to
    if object_type == "deal":
        return f"Deal {num}{to}"
    if object_type == "employee_event":
        what = str(f.get("type") or "employee event").replace("_", " ").capitalize()
        return f"{what}: {_label(f, 'employee') or num}"
    if object_type == "requisition":
        return f"Requisition {num}: {f['role']}" if string(f.get("role")) else f"Requisition {num}"
    if object_type == "payroll":
        return f"Payroll {f.get('period') or num}"
    if object_type == "deploy":
        return " ".join(str(x) for x in ("Deploy", f.get("component") or num, f.get("version") or "") if x).strip()
    if object_type == "cloud_bill":
        return f"Cloud bill {f.get('period') or num}{to}"
    if object_type == "tax_payment":
        return f"{f.get('tax') or 'Tax'} payment {f.get('period') or num}"
    if object_type == "nps_response":
        survey = f" ({f['survey']})" if string(f.get("survey")) else ""
        score = f.get("score")
        score = f": {score}/10" if isinstance(score, (int, float)) and not isinstance(score, bool) else ""
        who = _label(f, "contact")
        return f"NPS response {num}{survey}{score}" + (f" from {who}" if who else "") + to
    text = string(f.get("statement")) or string(f.get("rules"))
    if text:
        return truncate(text, 160)
    return f"{kind_label(KIND_BY_TYPE[object_type]).capitalize()} {num}"


def _value(f: dict[str, Any], key: str) -> str:
    v = f[key]
    name = f.get(f"{key}_name")
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return "; ".join(f"{k}: {x}" for k, x in v.items())
    if isinstance(v, float):
        return f"{v:,.2f}"
    return f"{name} ({v})" if name and name != v else str(v)


class SimAdapter:
    provider = "sim"

    def normalize(self, env: SourceEnvelope, ctx: AdapterContext) -> list[CanonicalRecord]:
        kind = kind_of(env.object_type)
        key = Ref(source_id=env.source_id, object_type=env.object_type, external_id=str(env.external_id))
        if env.operation == "delete" or env.payload is None:
            return [CanonicalRecord(key=key, entity_id=sim_entity_id(env.external_id), kind=kind,
                                    title=str(env.external_id), deleted=True)]
        p = env.payload
        f = {} if p.get("fields") is None else p["fields"]
        causes = [] if p.get("causes") is None else p["causes"]
        if not isinstance(f, dict) or not isinstance(causes, list):
            raise ValueError(f"sim: {env.external_id}: the payload is not an event envelope (fields{{}}, causes[])")
        sim_id = str(p.get("entity_id") or env.external_id)
        entity_id = sim_entity_id(sim_id)
        title = _title(env.object_type, f, sim_id)
        at = parse_dt(p.get("ts")) or env.source_modified_at
        event = string(p.get("kind"))

        links = Links(Ref(entity_id=entity_id))
        for k, predicate in LITERAL_FIELDS.items():
            v = f.get(k)
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != "":
                links.statement(predicate, literal=v,
                                qualifiers={"source_id": env.source_id} if k == "project" else None)
        for k in ORG_FIELDS:
            links.statement("counterparty", org_ref(ctx, f.get(k), f.get(f"{k}_name")),
                            qualifiers=None if k == "counterparty" else {"role": k},
                            evidence=evidence(f"{k}: {f.get(k)}", field=k))
        for k in PERSON_FIELDS:
            for pid in _ids(f.get(k)):
                links.statement(k, person_ref(pid, f.get(f"{k}_name")), evidence=evidence(f"{k}: {pid}", field=k))
        for k, predicate in CONTACT_FIELDS.items():
            for pid in _ids(f.get(k)):
                links.statement(predicate, person_ref(pid, f.get(f"{k}_name")),
                                evidence=evidence(f"{k}: {pid}", field=k))
        for k, predicate in REF_FIELDS.items():
            for target in _ids(f.get(k)):
                links.statement(predicate, sim_ref(target),
                                qualifiers={"role": k} if k in ("deal", "incident") else None,
                                evidence=evidence(f"{k}: {target}", field=k))
        for target in _ids(f.get("supersedes")):
            links.cause("supersedes", sim_ref(target), evidence=evidence(f"supersedes: {target}", field="supersedes"))

        declared: list[str] = []
        cause_lines: list[str] = []
        for n, c in enumerate(causes):
            ctype, cid = (c.get("type"), c.get("id")) if isinstance(c, dict) else (None, None)
            if ctype not in CAUSE_PREDICATES or not isinstance(cid, (str, int)) or str(cid).strip() == "":
                raise ValueError(f"sim: {sim_id}: cause {n} {c!r} is not {{type: one of "
                                 f"{', '.join(CAUSE_PREDICATES)}, id}}")
            predicate = CAUSE_PREDICATES[ctype]
            declared.append(f"{ctype}:{cid}")
            cause_lines.append(f"- {predicate.replace('_', ' ')}: {ctype.replace('_', ' ')} {cid}")
            links.cause(predicate, sim_ref(cid, f"{ctype.replace('_', ' ')} {cid}"),
                        sensitivity=CAUSE_SENSITIVITY.get(ctype),
                        evidence=evidence(f"{ctype}: {cid}", field="causes", type=ctype, index=n))

        fields: dict[str, Any] = {k: v for k, v in f.items() if k not in META_FIELDS}
        fields.update({
            "event": event,
            "event_at": iso(at),
            "process": string(p.get("process")),
            "simulation": {k: p.get(k) for k in ("run", "option", "scenario", "seq")},
            "causes": declared,
        })

        status = f.get("status")
        head = " · ".join(x for x in [
            f"{kind_label(kind).capitalize()} {sim_id}",
            f"status: {status}" if status not in (None, "") else None, _money(f),
            f"{event} ({p.get('process')})" if event and p.get("process") else event,
            f"{at:%Y-%m-%d %H:%M}" if at else None] if x)
        parts = [f"# {title}", head]
        for k in ("statement", "rules"):
            if string(f.get(k)):
                parts.append(f"## {k.capitalize()}\n\n{clean_ws(f[k])}")
        details = [f"- {k.replace('_', ' ').capitalize()}: {_value(f, k)}" for k in f
                   if k not in _NOT_DETAILS and k not in META_FIELDS and not k.endswith("_name")
                   and f[k] not in (None, "", [], {})]
        if details:
            parts.append("## Details\n\n" + "\n".join(details))
        if string(f.get("description")):
            parts.append("## Description\n\n" + clean_ws(f["description"]))
        if string(f.get("comment")):
            parts.append("## Comment\n\n" + clean_ws(f["comment"]))
        if cause_lines:
            parts.append("## Causes\n\n" + "\n".join(cause_lines))

        return [CanonicalRecord(
            key=key,
            entity_id=entity_id,
            kind=kind,
            title=title,
            fields=fields,
            body="\n\n".join(parts),
            valid_from=at,
            actor=person_ref(f.get("actor"), f.get("actor_name")),
            aliases=aliases((sim_id, "key"), (string(f.get("number")), "key"), (title, "title")),
            statements=links.statements,
            causes=links.causes,
            acl=record_acl(env, ctx, env.container),
        )]

    def ref_from_url(self, url: str, source: SourceConfig) -> Ref | None:
        return None  # the simulator has no browsable source system


register(SimAdapter())
