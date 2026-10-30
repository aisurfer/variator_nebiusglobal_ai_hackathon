"""Email adapter (Graph-like message JSON) and the directory's organizations/contacts: pure normalisation, no DB."""
from __future__ import annotations

import copy
from datetime import datetime

from kb.connectors.base import AdapterContext, ContainerConfig, SourceConfig, TenantSources, get_adapter
from kb.connectors.directory import organization_entity_id, short_org_name
from kb.connectors.email import DOC_KINDS
from kb.contracts import SourceEnvelope
from tests.unit.connectors.conftest import ext

MAIL, DIRECTORY = "mail-company", "directory-company"
PARTICIPANTS = ["user:o.kim", "user:a.sokolov"]


def _sources() -> TenantSources:
    mail = SourceConfig(MAIL, "email", "Mail", None, {}, {"id": MAIL, "provider": "email"})
    people = ContainerConfig("people", "People", ("grp:all-staff",))
    directory = SourceConfig(DIRECTORY, "directory", "Directory", None, {"people": people},
                             {"id": DIRECTORY, "provider": "directory"})
    return TenantSources("company", "Company", "Asia/Novosibirsk", {MAIL: mail, DIRECTORY: directory})


def _ctx(source_id: str) -> AdapterContext:
    ts = _sources()
    return AdapterContext("company", ts.sources[source_id], ts)


def _addr(name: str, address: str) -> dict:
    return {"emailAddress": {"name": name, "address": address}}


MESSAGE = {
    "id": "msg-000123",
    "internetMessageId": "<20260616.093100.000123@company.example>",
    "conversationId": "thr-000045",
    "subject": "Re: Contract with TransLogistics",
    "from": _addr("Olga Kim", "O.Kim@company.example"),
    "toRecipients": [_addr("Igor Ivanov", "i.ivanov@translog.example")],
    "ccRecipients": [_addr("Andrew Sokolov", "a.sokolov@company.example")],
    "sentDateTime": "2026-06-16T09:31:00+07:00",
    "receivedDateTime": "2026-06-16T09:31:05+07:00",
    "inReplyTo": "<20260615.170200.000098@translog.example>",
    "references": ["<20260615.170200.000098@translog.example>"],
    "importance": "normal",
    "body": {"contentType": "text", "content": "Hello Igor,\nplease find the contract for signature attached."},
    "hasAttachments": True,
    "attachments": [
        {"id": "att-1", "name": "Contract_14-2026_TransLogistics.docx",
         "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "size": 48211,
         "extractedText": "SERVICES AGREEMENT No. 14/2026\nThe price of the services is USD 240,000.",
         "documentMeta": {"docType": "contract", "title": "Services agreement No. 14/2026", "number": "14/2026",
                          "date": "2026-06-14", "counterparty": "org-translog", "amount": 240000.0,
                          "currency": "USD"}},
        {"id": "att-2", "name": "Invoice_77.pdf", "contentType": "application/pdf", "size": 1200,
         "extractedText": "Invoice No. 77", "documentMeta": {"docType": "invoice", "number": "77"}},
        {"id": "img-1", "name": "logo.png", "contentType": "image/png", "size": 300, "isInline": True},
    ],
}


def _env(payload: dict | None, *, acl: list[str] | None = PARTICIPANTS, op: str = "upsert") -> SourceEnvelope:
    mid = (payload or MESSAGE)["internetMessageId"]
    return SourceEnvelope(tenant_id="company", source_id=MAIL, delivery_id=f"t:{mid}", operation=op,
                          object_type="message", external_id=mid, container=None, payload=payload, acl=acl,
                          source_modified_at=datetime.fromisoformat("2026-06-16T09:31:05+07:00"))


def _normalize(payload: dict | None, **kw):
    return get_adapter("email").normalize(_env(payload, **kw), _ctx(MAIL))


def _stmts(rec, predicate: str) -> list:
    return [s.object.external_key() if s.object else s.literal for s in rec.statements if s.predicate == predicate]


def test_primary_email_record():
    recs = _normalize(MESSAGE)
    email = recs[0]
    assert (email.entity_id, email.kind, email.title) == ("mail.msg-000123", "Email", "Re: Contract with TransLogistics")
    assert email.key.external_key() == ext(MAIL, "message", "<20260616.093100.000123@company.example>")
    f = email.fields
    assert f["subject"] == "Re: Contract with TransLogistics"
    assert f["from"] == "o.kim@company.example"  # addresses are lower-cased: they are the account ids
    assert f["to"] == ["i.ivanov@translog.example"] and f["cc"] == ["a.sokolov@company.example"]
    assert f["sent_at"] == "2026-06-16T09:31:00+07:00" and f["thread_id"] == "thr-000045"
    assert f["importance"] == "normal" and f["has_attachments"] is True
    assert f["attachments"] == ["Contract_14-2026_TransLogistics.docx", "Invoice_77.pdf"]
    assert email.valid_from == datetime.fromisoformat("2026-06-16T09:31:00+07:00")
    assert email.url is None
    body = email.body
    assert "From: Olga Kim <o.kim@company.example>" in body
    assert "To: Igor Ivanov <i.ivanov@translog.example>" in body
    assert "Cc: Andrew Sokolov <a.sokolov@company.example>" in body
    assert "Date: 2026-06-16 09:31 +07:00" in body and "Subject: Re: Contract with TransLogistics" in body
    assert "please find the contract for signature attached." in body
    assert "- Contract_14-2026_TransLogistics.docx (Services agreement No. 14/2026)" in body
    assert "logo.png" not in body  # inline images are not documents


def test_participants_are_account_refs_and_thread_literal():
    email = _normalize(MESSAGE)[0]
    assert _stmts(email, "author") == [ext(MAIL, "account", "o.kim@company.example")]
    assert _stmts(email, "addressed_to") == [ext(MAIL, "account", "i.ivanov@translog.example")]
    assert _stmts(email, "cc_to") == [ext(MAIL, "account", "a.sokolov@company.example")]
    assert _stmts(email, "in_thread") == ["thr-000045"]
    assert email.actor.external_key() == ext(MAIL, "account", "o.kim@company.example")


def test_reply_declares_replies_to_the_parent_message():
    email = _normalize(MESSAGE)[0]
    assert [(c.predicate, c.cause.external_key()) for c in email.causes] == [
        ("replies_to", ext(MAIL, "message", "<20260615.170200.000098@translog.example>"))]
    assert email.causes[0].sensitivity is None  # the ontology has no typical_sensitivity for replies_to
    # no inReplyTo: the last of references is the direct parent
    p = copy.deepcopy(MESSAGE)
    del p["inReplyTo"]
    p["references"] = ["<root@company.example>", "<parent@company.example>"]
    assert [c.cause.external_id for c in _normalize(p)[0].causes] == ["<parent@company.example>"]
    # a thread starter declares nothing
    p = copy.deepcopy(MESSAGE)
    p.pop("inReplyTo"), p.pop("references")
    assert _normalize(p)[0].causes == []


def test_attachments_become_business_documents():
    recs = _normalize(MESSAGE)
    assert [r.entity_id for r in recs] == ["mail.msg-000123", "mail.msg-000123.att-1", "mail.msg-000123.att-2"]
    contract, invoice = recs[1], recs[2]
    assert contract.key.external_key() == ext(MAIL, "attachment", "<20260616.093100.000123@company.example>#att-1")
    assert (contract.kind, contract.title) == ("Contract", "Services agreement No. 14/2026")
    assert contract.fields == {
        "doc_type": "contract", "number": "14/2026", "doc_date": "2026-06-14", "counterparty": "org-translog",
        "amount": 240000.0, "currency": "USD", "filename": "Contract_14-2026_TransLogistics.docx",
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "size": 48211,
    }
    assert contract.body == MESSAGE["attachments"][0]["extractedText"]
    assert contract.valid_from == datetime.fromisoformat("2026-06-16T09:31:00+07:00")
    assert _stmts(contract, "attached_to") == [ext(MAIL, "message", "<20260616.093100.000123@company.example>")]
    assert _stmts(contract, "counterparty") == [ext(DIRECTORY, "organization", "org-translog")]
    assert {(a.alias, a.kind) for a in contract.aliases} >= {("14/2026", "key"),
                                                             ("Services agreement No. 14/2026", "title")}
    # no title in the meta: the file name; no counterparty: no statement
    assert (invoice.kind, invoice.title, invoice.fields["counterparty"]) == ("Invoice", "Invoice_77.pdf", None)
    assert _stmts(invoice, "counterparty") == []


def test_doc_type_to_kind_mapping():
    assert DOC_KINDS["proposal"] == "CommercialProposal" and DOC_KINDS["price_list"] == "PriceList"
    assert DOC_KINDS["other"] == "BusinessDocument"
    for doc_type, kind in [("amendment", "Amendment"), ("act", "Act"), ("claim", "Claim"), ("letter", "Letter"),
                           ("specification", "Specification"), ("order", "Order"), ("nda", "NDA"),
                           ("report", "Report"), ("something-new", "BusinessDocument")]:
        p = copy.deepcopy(MESSAGE)
        p["attachments"] = [{"id": "a", "name": "x.pdf", "documentMeta": {"docType": doc_type}}]
        assert _normalize(p)[1].kind == kind


def test_acl_comes_from_the_envelope_only():
    for r in _normalize(MESSAGE):
        assert r.acl == PARTICIPANTS
    for r in _normalize(MESSAGE, acl=None):
        assert r.acl is None  # nothing invented: the journal default (no container ACL) applies


def test_delete_and_html_body():
    gone = _normalize(None, op="delete")
    assert len(gone) == 1 and gone[0].deleted and gone[0].kind == "Email"
    p = copy.deepcopy(MESSAGE)
    p["body"] = {"contentType": "html", "content": "<p>Good afternoon!</p><ul><li>item&nbsp;1</li></ul><br>Olga"}
    body = _normalize(p)[0].body
    assert "Good afternoon!" in body and "- item\xa01" in body and "<p>" not in body


def test_directory_organization_and_contact():
    adapter = get_adapter("directory")
    org = {"id": "org-translog", "name": "TransLogistics LLC", "kind": "client", "domain": "translog.example",
           "inn": "5406123456"}
    env = SourceEnvelope(tenant_id="company", source_id=DIRECTORY, delivery_id="b:org", object_type="organization",
                         external_id="org-translog", container="organizations", payload=org)
    [o] = adapter.normalize(env, _ctx(DIRECTORY))
    assert (o.entity_id, o.kind, o.title) == ("org.translog", "Organization", "TransLogistics LLC")
    assert o.fields == {"org_id": "org-translog", "kind": "client", "domain": "translog.example", "inn": "5406123456"}
    assert {(a.alias, a.kind) for a in o.aliases} >= {("TransLogistics LLC", "name"), ("translog.example", "key"),
                                                      ("TransLogistics", "label")}
    assert o.acl == ["grp:all-staff"]  # no "organizations" container: the people ACL
    assert organization_entity_id("acme") == "org.acme" and short_org_name("Acme") is None
    assert short_org_name("LLC Acme") == "Acme" and short_org_name("Acme, Inc.") == "Acme"

    contact = {"id": "CT-tl-ivanov", "name": "Igor Ivanov", "role": "Director", "org": "org-translog",
               "org_name": "TransLogistics LLC", "email": "i.ivanov@translog.example",
               "accounts": {MAIL: "i.ivanov@translog.example"}}
    env = SourceEnvelope(tenant_id="company", source_id=DIRECTORY, delivery_id="b:ct", object_type="contact",
                         external_id="CT-tl-ivanov", container="contacts", payload=contact)
    [c] = adapter.normalize(env, _ctx(DIRECTORY))
    assert (c.entity_id, c.kind, c.title) == ("person.ct-tl-ivanov", "Person", "Igor Ivanov")
    assert c.fields["external"] is True and c.fields["org"] == "org-translog" and c.fields["role"] == "Director"
    assert [(s.predicate, s.object.external_key()) for s in c.statements] == [
        ("works_at", ext(DIRECTORY, "organization", "org-translog"))]
    assert [a.kind for a in c.aliases if a.alias == "i.ivanov@translog.example"] == ["email"]  # account = e-mail: once
    assert "TransLogistics LLC" in c.body and "(external contact)" in c.body
