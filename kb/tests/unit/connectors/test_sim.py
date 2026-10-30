"""Simulation adapter (provider `sim`) and the English delivery fixture: every line through its registered adapter,
kinds, one entity per business object across its versions, structural statements and declared causes, failures."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

import pytest

from kb.connectors.base import REGISTRY, get_adapter
from kb.connectors.sim import CAUSE_PREDICATES, KIND_BY_TYPE, OBJECT_TYPES, UnknownObjectType, sim_entity_id
from kb.contracts import CanonicalRecord, Ref
from tests.unit.connectors import conftest as vf
from tests.unit.connectors.sample_en import DIRECTORY, SIM, envelope, lines, normalize, records, sim_records, sources

KINDS = {  # the kind mapping of the simulator's object types
    "contract": "Contract", "invoice": "Invoice", "payment": "Payment", "bank_entry": "BankEntry", "deal": "Deal",
    "employee_event": "EmployeeEvent", "requisition": "Requisition", "payroll": "Payroll", "incident": "Incident",
    "deploy": "Deploy", "support_ticket": "SupportTicket", "cloud_bill": "CloudBill", "tax_payment": "TaxPayment",
    "decision": "Decision", "nps_response": "NpsResponse",
    "hypothesis": "Hypothesis", "life_event": "LifeEvent", "habit": "Habit", "rule": "Policy", "process": "Process",
}
BUSINESS = list(KINDS)[:15]


def _key(ref: Ref | None) -> str | None:
    return None if ref is None else ref.external_key() or f"entity:{ref.entity_id}"


def _stmts(rec: CanonicalRecord, predicate: str) -> list:
    return [_key(s.object) if s.object else s.literal for s in rec.statements if s.predicate == predicate]


def _causes(rec: CanonicalRecord) -> set[tuple[str, str]]:
    return {(c.predicate, c.cause.entity_id or "") for c in rec.causes}


def _head(entity_id: str) -> CanonicalRecord:
    return [r for _, r in sim_records() if r.entity_id == entity_id][-1]


def _sim_env(object_type: str, payload: dict | None, *, external_id: str = "X-1", op: str = "upsert"):
    ln = {"source": SIM, "object_type": object_type, "external_id": external_id, "container": "finance", "op": op,
          "at": "2026-10-20T10:00:00+02:00", "payload": payload}
    return envelope(0, ln)


def _payload(entity_id: str = "X-1", fields: dict | None = None, causes: list | None = None) -> dict:
    return {"run": "r", "option": "base", "scenario": "actual", "ts": "2026-10-20T10:00:00+02:00", "process": "billing",
            "kind": "invoice_issued", "entity_id": entity_id, "fields": fields or {}, "causes": causes or [], "seq": 1}


# ------------------------------------------------------------------------------------------------ the whole fixture
def test_every_fixture_line_goes_through_its_registered_adapter():
    providers = Counter()
    for ln, recs in records():
        src = sources().sources[ln["source"]]
        assert REGISTRY[src.provider] is get_adapter(src.provider)
        assert recs, ln
        assert recs[0].key.external_key() == f"{ln['source']}/{ln['object_type']}/{ln['external_id']}"
        providers[src.provider] += 1
    assert set(providers) == {"jira", "gitlab", "email", "sim"}
    assert providers["sim"] == sum(1 for ln in lines() if ln["source"] == SIM) >= 19


def test_the_fixture_covers_every_object_type_and_cause_type():
    sim = [ln for ln in lines() if ln["source"] == SIM]
    assert {ln["object_type"] for ln in sim} == set(OBJECT_TYPES) == set(KINDS)
    assert {c["type"] for ln in sim for c in ln["payload"]["causes"]} == set(CAUSE_PREDICATES)
    assert all(ln["external_id"] == ln["payload"]["entity_id"] for ln in sim)


def test_kinds_and_entity_ids():
    assert KIND_BY_TYPE == KINDS
    for ln, rec in sim_records():
        assert rec.kind == KINDS[ln["object_type"]]
        assert rec.entity_id == f"sim.{ln['external_id']}"
        assert rec.title and rec.body and rec.valid_from == datetime.fromisoformat(ln["payload"]["ts"])
        assert rec.fields["event"] == ln["payload"]["kind"]
        assert rec.fields["simulation"] == {"run": "sample", "option": "base", "scenario": "actual",
                                            "seq": ln["payload"]["seq"]}
        assert rec.acl == list(sources().sources[SIM].containers[ln["container"]].acl)
        assert rec.url is None and not rec.deleted


def test_versions_of_an_invoice_keep_one_entity():
    versions = [(ln, r) for ln, r in sim_records() if ln["external_id"] == "INV-2026-0031"]
    assert [r.fields["status"] for _, r in versions] == ["issued", "sent", "paid"]
    assert {r.entity_id for _, r in versions} == {"sim.INV-2026-0031"}
    assert {_key(r.key) for _, r in versions} == {"sim/invoice/INV-2026-0031"}
    assert [ln["version"] for ln, _ in versions] == [1, 2, 3]
    assert versions[0][1].valid_from < versions[1][1].valid_from < versions[2][1].valid_from
    assert [_stmts(r, "status") for _, r in versions] == [["issued"], ["sent"], ["paid"]]
    # the causes are the same on every version: the links stay bound to the first version that declared them
    assert len({frozenset(_causes(r)) for _, r in versions}) == 1


def test_invoice_record_statements_and_body():
    rec = _head("sim.INV-2026-0031")
    assert rec.title == "Invoice INV-2026-0031 — Northwind Foods"
    assert _stmts(rec, "counterparty") == [f"{DIRECTORY}/organization/org-northwind"]
    assert [s.qualifiers for s in rec.statements if s.predicate == "counterparty"] == [{"role": "client"}]
    assert _stmts(rec, "under_contract") == ["entity:sim.CTR-2026-014"]
    assert _stmts(rec, "owner") == ["entity:person.wp-e02"]
    assert _key(rec.actor) == "entity:person.wp-e02"
    assert _causes(rec) == {("caused_by", "sim.PROC-BILLING"), ("governed_by", "sim.POL-NET30")}
    assert {a.alias for a in rec.aliases} >= {"INV-2026-0031", rec.title}
    assert rec.fields["amount"] == 300.0 and rec.fields["currency"] == "EUR" and rec.fields["contract"] == "CTR-2026-014"
    assert rec.fields["causes"] == ["process:PROC-BILLING", "rule:POL-NET30"]
    for text in ("# Invoice INV-2026-0031", "status: paid", "300.00 EUR", "Northwind Foods (org-northwind)",
                 "## Causes", "- governed by: rule POL-NET30"):
        assert text in rec.body


def test_structural_statements_of_the_other_records():
    pay, bank = _head("sim.PAY-0042"), _head("sim.BNK-0107")
    assert _stmts(pay, "settles") == ["entity:sim.INV-2026-0031"]
    assert _stmts(bank, "records") == ["entity:sim.PAY-0042"]
    assert _stmts(bank, "counterparty") == [f"{DIRECTORY}/organization/org-northwind"]
    assert [s.qualifiers for s in bank.statements if s.predicate == "counterparty"] == [{}]
    assert _stmts(_head("sim.EE-0007"), "employee") == ["entity:person.wp-e05"]
    assert _head("sim.EE-0007").title == "Resignation: Tom Novak"
    ticket = _head("sim.SUP-0012")
    assert _stmts(ticket, "reporter") == ["entity:person.ct-nw_ross"]  # an external contact is a person too
    assert _stmts(ticket, "assignee") == ["entity:person.wp-e03"]
    assert _stmts(ticket, "relates_to") == ["entity:sim.INC-0005"]
    assert _stmts(ticket, "under_contract") == ["entity:sim.CTR-2026-014"]
    deploy = _head("sim.DEP-0019")
    assert deploy.title == "Deploy billing-job v1.4.2"
    assert _stmts(deploy, "relates_to") == ["entity:gitlab.wp3sample-api.mr-1"]  # a KB entity id is kept
    assert _stmts(deploy, "component") == ["billing-job"] and _stmts(deploy, "in_project") == ["api"]
    assert _stmts(deploy, "author") == ["entity:person.wp-e04"]
    assert _stmts(_head("sim.TAX-2026-Q3-VAT"), "counterparty") == [f"{DIRECTORY}/organization/org-taxoffice"]
    assert _stmts(_head("sim.CB-2026-10"), "counterparty") == [f"{DIRECTORY}/organization/org-skycloud"]
    assert _stmts(_head("sim.DEAL-001"), "under_contract") == ["entity:sim.CTR-2026-014"]
    assert _stmts(_head("sim.CTR-2026-014"), "relates_to") == ["entity:sim.DEAL-001"]


def test_nps_response_record():
    rec = _head("sim.NPS-0001")
    assert rec.kind == "NpsResponse"
    assert rec.title == "NPS response NPS-0001 (2026-Q4): 5/10 from Daniel Ross — Northwind Foods"
    assert _stmts(rec, "author") == ["entity:person.ct-nw_ross"]  # the contact who answered
    assert _stmts(rec, "counterparty") == [f"{DIRECTORY}/organization/org-northwind"]
    assert _causes(rec) == {("caused_by", "sim.INC-0005")}  # the incident the client saw
    assert rec.fields["score"] == 5 and rec.fields["survey"] == "2026-Q4"
    assert rec.fields["comment_slot"] == "sim:NPS-0001:nps_comment"  # kept for a writer, not in the body
    assert "comment_slot" not in rec.body.lower().replace(" ", "_") and "- Score: 5" in rec.body
    (written,) = normalize(_sim_env("nps_response", _payload("NPS-9", {
        "client": "org-northwind", "contact": "CT-nw_ross", "score": 9, "survey": "2026-Q4",
        "comment": "Fast fixes and clear updates."})))
    assert "## Comment\n\nFast fixes and clear updates." in written.body
    assert written.title == "NPS response NPS-9 (2026-Q4): 9/10 from CT-nw_ross — org-northwind"


def test_declared_causes_become_causal_specs():
    by_type: dict[str, set[tuple[str, frozenset | None]]] = defaultdict(set)
    for ln, rec in sim_records():
        specs = {c.cause.entity_id: c for c in rec.causes}
        for c in ln["payload"]["causes"]:
            spec = specs[sim_entity_id(c["id"])]
            assert spec.evidence and spec.evidence.locator["field"] == "causes"
            by_type[c["type"]].add((spec.predicate, frozenset(spec.sensitivity) if spec.sensitivity else None))
    assert by_type == {
        "decision": {("implements", None)}, "hypothesis": {("based_on", frozenset({"statement"}))},
        "life_event": {("caused_by", None)}, "habit": {("motivated_by", frozenset({"statement"}))},
        "rule": {("governed_by", None)}, "process": {("caused_by", None)},
    }
    assert _causes(_head("sim.REQ-0003")) == {("caused_by", "sim.EE-0007")}  # a life event that is a business record
    assert _causes(_head("sim.DEC-002")) == {("supersedes", "sim.DEC-001"), ("based_on", "sim.usage-export-demand")}
    assert _causes(_head("sim.DEP-0019")) == {("implements", "sim.fix-billing-retries")}
    assert _causes(_head("sim.SUP-0012")) == {("motivated_by", "sim.HAB-NW-ESCALATE")}
    assert _causes(_head("sim.TAX-2026-Q3-VAT")) == {("governed_by", "sim.POL-VAT-Q"), ("caused_by", "sim.vat-change")}


def test_every_reference_of_a_sim_record_resolves_inside_the_fixture():
    entities = {r.entity_id for _, recs in records() for r in recs}
    entities |= {f"person.{p.lower()}" for p in ("WP-E01", "WP-E02", "WP-E03", "WP-E04", "WP-E05", "WP-E06",
                                                  "CT-nw_ross")}
    orgs = {"org-northwind", "org-skycloud", "org-taxoffice"}
    for _, rec in sim_records():
        for c in rec.causes:
            assert c.cause.entity_id in entities, (rec.entity_id, c.predicate, c.cause)
        for s in rec.statements:
            if s.object is None:
                continue
            if s.object.entity_id:
                assert s.object.entity_id in entities, (rec.entity_id, s.predicate, s.object)
            else:
                assert s.object.source_id == DIRECTORY and s.object.external_id in orgs


def test_the_cause_catalog():
    assert _head("sim.POL-NET30").kind == "Policy" and _head("sim.POL-NET30").fields["rules"].startswith("Invoices")
    assert "## Rules" in _head("sim.POL-NET30").body
    assert _head("sim.usage-export-demand").kind == "Hypothesis"
    assert _head("sim.vat-change").kind == "LifeEvent"
    assert _head("sim.HAB-NW-ESCALATE").kind == "Habit"
    assert _head("sim.PROC-BILLING").kind == "Process"
    dec = _head("sim.DEC-001")
    assert dec.kind == "Decision" and dec.fields["statement"].startswith("Offer the usage export")
    assert dec.fields["status"] == "superseded"


# ------------------------------------------------------------------------------------------------ edge cases
def test_unknown_object_type_fails_clearly():
    with pytest.raises(UnknownObjectType, match=r"sim: unknown object type 'timesheet' \(known: contract, invoice"):
        normalize(_sim_env("timesheet", _payload()))
    with pytest.raises(UnknownObjectType):  # a delete of an unknown type too: no silent tombstone
        normalize(_sim_env("timesheet", None, op="delete"))
    assert issubclass(UnknownObjectType, ValueError)  # an adapter error: the ingest pipeline quarantines the delivery


@pytest.mark.parametrize("payload,match", [
    ({**_payload(), "fields": "amount=1"}, "not an event envelope"),
    ({**_payload(), "causes": {"type": "rule"}}, "not an event envelope"),
    (_payload(causes=[{"type": "whim", "id": "x"}]), "cause 0"),
    (_payload(causes=[{"type": "rule"}]), "cause 0"),
    (_payload(causes=["rule:x"]), "cause 0"),
])
def test_malformed_envelopes_fail_clearly(payload, match):
    with pytest.raises(ValueError, match=match):
        normalize(_sim_env("invoice", payload))


def test_delete_and_minimal_payload():
    (gone,) = normalize(_sim_env("invoice", None, external_id="INV-9", op="delete"))
    assert gone.deleted and gone.entity_id == "sim.INV-9" and gone.kind == "Invoice"
    (rec,) = normalize(_sim_env("invoice", {"entity_id": "INV-9", "ts": "2026-10-20T10:00:00+02:00"}, external_id="INV-9"))
    assert rec.entity_id == "sim.INV-9" and rec.title == "Invoice INV-9" and rec.causes == [] and rec.statements == []
    assert rec.fields["causes"] == [] and rec.actor is None


def test_ids_become_safe_entity_ids_and_kb_ids_are_kept():
    assert sim_entity_id("INV 2026/31") == "sim.INV-2026-31"
    assert sim_entity_id("jira.ENG-1") == "jira.ENG-1" and sim_entity_id("person.wp-e01") == "person.wp-e01"
    (rec,) = normalize(_sim_env("incident", _payload("INC 1", {"title": "Outage", "related": ["jira.ENG-2", "INC 1"]},
                                                     causes=[{"type": "process", "id": "INC 1"},
                                                             {"type": "decision", "id": "jira.ENG-1"},
                                                             {"type": "decision", "id": "jira.ENG-1"}])))
    assert rec.entity_id == "sim.INC-1"
    assert _causes(rec) == {("implements", "jira.ENG-1")}  # a cause to itself and duplicates are dropped
    assert _stmts(rec, "relates_to") == ["entity:jira.ENG-2"]


def test_sample_tenant_has_a_sim_source():
    src = vf.sources().sources["sim-vf"]
    assert src.provider == "sim" and src.container_acl("finance") == ["grp:exec"]
    (rec,) = vf.normalize(vf.make_env("sim-vf", "payment", "PAY-1", _payload("PAY-1", {
        "client": "org-translog", "client_name": "TransLogistics", "amount": 10, "currency": "USD",
        "direction": "incoming", "actor": "VF-E01"}), container="finance"))
    assert rec.kind == "Payment" and rec.title == "Payment PAY-1, 10.00 USD — TransLogistics"
    assert _stmts(rec, "counterparty") == ["directory-vf/organization/org-translog"]
    assert _key(rec.actor) == "entity:person.vf-e01" and rec.acl == ["grp:exec"]
