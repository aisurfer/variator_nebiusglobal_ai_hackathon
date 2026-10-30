"""Write kb/tests/fixtures/sample-en: VARIATOR's orgsim sample plus `sim` lines of every structured object type
(including `nps_response`, a client contact's answer to an NPS survey).

Run with VARIATOR's venv (it imports `variator`, which kb/ must not):
    .venv/bin/python scripts/mvp/make_kb_sample_en.py kb/tests/fixtures/sample-en
"""
from __future__ import annotations

import gzip
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from variator.orgsim.calendar import BusinessCalendar, RelativeDates
from variator.orgsim.delivery import write_delivery
from variator.orgsim.events import CAUSE_PREDICATES, Cause, Event
from variator.orgsim.sample import LAG, TENANT, TIMEZONE, build_sample

ANCHOR = date(2026, 10, 26)
SEED = 7
SIM = "sim"
CONTAINERS = {
    "finance": {"name": "Finance", "acl": ["grp:management"]},
    "sales": {"name": "Sales", "acl": ["grp:management", "grp:sales"]},
    "people": {"name": "People", "acl": ["grp:management"]},
    "engineering": {"name": "Engineering", "acl": ["grp:dev", "grp:management"]},
    "support": {"name": "Support", "acl": ["grp:all-staff"]},
    "management": {"name": "Management", "acl": ["grp:management"]},
}
NW = {"client": "org-northwind", "client_name": "Northwind Foods"}


def main(out: Path) -> None:
    s = build_sample(ANCHOR, seed=SEED)
    rel = RelativeDates(ANCHOR, BusinessCalendar(timezone=TIMEZONE))
    s["sources"]["sources"].append({"id": SIM, "provider": "sim", "name": "Organisation simulator",
                                    "containers": CONTAINERS})
    s["people"]["organizations"] += [
        {"id": "org-skycloud", "name": "SkyCloud Ltd", "kind": "vendor", "domain": "skycloud.example"},
        {"id": "org-taxoffice", "name": "Federal Tax Office", "kind": "government", "domain": "tax.example"},
    ]
    events: list[Event] = []
    lines: list[dict[str, Any]] = []

    def at(n: int, hhmm: str):
        return rel.at(n, hhmm)

    def day(n: int) -> str:
        return rel.bdays(n).isoformat()

    def emit(t, object_type: str, eid: str, kind: str, container: str, fields: dict, causes=(), process=None):
        ev = Event(run="sample", option="base", scenario="actual", ts=t, process=process or object_type, kind=kind,
                   entity_id=eid, fields=fields, causes=tuple(Cause(type=c, id=i) for c, i in causes),
                   seq=1000 + len(events))
        events.append(ev)
        lines.append(ev.to_timeline_line(tenant=TENANT, source=SIM, object_type=object_type, container=container,
                                         observe_lag=LAG))

    # ---- the cause catalog
    emit(at(-10, "09:10"), "process", "PROC-BILLING", "process_defined", "finance",
         {"title": "Monthly billing run", "statement": "Invoices go out on the first working day of the month.",
          "owner": "WP-E02", "owner_name": "Sam Rivera", "actor": "WP-E01"}, process="governance")
    emit(at(-10, "09:12"), "process", "PROC-PAYROLL", "process_defined", "finance",
         {"title": "Monthly payroll", "statement": "Salaries are approved on the 20th and paid by the 25th.",
          "owner": "WP-E01", "owner_name": "Olivia Grant", "actor": "WP-E01"}, process="governance")
    emit(at(-10, "09:14"), "rule", "POL-NET30", "rule_adopted", "finance",
         {"title": "Net 30 payment terms", "rules": "Invoices are due 30 days after issue. A reminder goes out "
          "7 days after the due date.", "status": "active", "version": 2, "owner": "WP-E01"}, process="governance")
    emit(at(-10, "09:16"), "rule", "POL-VAT-Q", "rule_adopted", "finance",
         {"title": "Quarterly VAT filing", "rules": "VAT is filed and paid quarterly, by the 10th day of the month "
          "after the quarter.", "status": "active", "owner": "WP-E01"}, process="governance")
    emit(at(-10, "09:18"), "habit", "HAB-NW-ESCALATE", "habit_observed", "support",
         {"title": "Northwind escalates by mail", "statement": "The CTO of Northwind Foods reports a problem by "
          "e-mail and opens a support ticket the same day.", "level": "same day", **NW}, process="clients")
    emit(at(-10, "09:20"), "hypothesis", "usage-export-demand", "hypothesis_stated", "management",
         {"title": "Clients will pay for a usage export", "statement": "Clients with more than 50 users will pay "
          "for a monthly usage export.", "status": "open", "owner": "WP-E02", "owner_name": "Sam Rivera"},
         process="strategy")
    emit(at(-10, "09:22"), "life_event", "vat-change", "life_event_happened", "finance",
         {"title": "VAT change for Austrian customers", "statement": "The VAT rate for Austrian customers changed "
          "on the first of the month.", "effective_date": day(-12)}, process="world")

    # ---- the billing incident, its fix and the client's ticket
    emit(at(-9, "08:40"), "incident", "INC-0005", "incident_opened", "engineering",
         {"title": "Card payment retries fail in the billing job", "severity": "sev2", "status": "open",
          "component": "billing-job", "project": "api", **NW, "started_at": at(-9, "08:15").isoformat(),
          "owner": "WP-E03", "owner_name": "Maya Chen", "actor": "WP-E03"},
         [("process", "PROC-BILLING")], process="operations")
    emit(at(-9, "09:10"), "support_ticket", "SUP-0012", "ticket_opened", "support",
         {"title": "Several card payments failed last week", **NW, "status": "open", "priority": "high",
          "reporter": "CT-nw_ross", "reporter_name": "Daniel Ross", "assignee": "WP-E03",
          "assignee_name": "Maya Chen", "opened_at": at(-9, "09:05").isoformat(), "incident": "INC-0005",
          "component": "billing-job", "actor": "WP-E02"},
         [("habit", "HAB-NW-ESCALATE")], process="support")
    emit(at(-9, "09:30"), "decision", "fix-billing-retries", "decision_made", "engineering",
         {"title": "Retry failed card payments", "statement": "Retry failed card payments in the billing job "
          "with exponential backoff.", "status": "accepted", "owner": "WP-E03", "owner_name": "Maya Chen",
          "decided_at": at(-9, "09:30").isoformat(), "actor": "WP-E03"},
         [("life_event", "INC-0005")], process="engineering")

    # ---- the usage export: a hypothesis, two decisions, a deal and a contract
    emit(at(-9, "11:00"), "decision", "DEC-001", "decision_made", "management",
         {"title": "Sell the usage export", "statement": "Offer the usage export as a paid add-on at EUR 400 "
          "per month.", "status": "accepted", "owner": "WP-E01", "owner_name": "Olivia Grant",
          "decided_at": at(-9, "11:00").isoformat(), "actor": "WP-E01"},
         [("hypothesis", "usage-export-demand")], process="strategy")
    emit(at(-8, "11:00"), "deal", "DEAL-001", "deal_opened", "sales",
         {"title": "Usage export for Northwind", **NW, "owner": "WP-E02", "owner_name": "Sam Rivera",
          "status": "proposal", "amount": 4800.0, "currency": "EUR", "expected_close": day(-5), "actor": "WP-E02"},
         [("decision", "DEC-001")], process="sales")
    emit(at(-7, "10:00"), "decision", "DEC-002", "decision_made", "management",
         {"title": "Lower the usage export price", "statement": "Price the usage export at EUR 300 per month "
          "to win Northwind.", "status": "accepted", "owner": "WP-E01", "owner_name": "Olivia Grant",
          "decided_at": at(-7, "10:00").isoformat(), "supersedes": "DEC-001", "actor": "WP-E01"},
         [("hypothesis", "usage-export-demand")], process="strategy")
    emit(at(-7, "10:01"), "decision", "DEC-001", "decision_superseded", "management",
         {"title": "Sell the usage export", "statement": "Offer the usage export as a paid add-on at EUR 400 "
          "per month.", "status": "superseded", "owner": "WP-E01", "owner_name": "Olivia Grant",
          "decided_at": at(-9, "11:00").isoformat(), "actor": "WP-E01"},
         [("hypothesis", "usage-export-demand")], process="strategy")
    emit(at(-6, "15:00"), "contract", "CTR-2026-014", "contract_signed", "sales",
         {"title": "Usage export add-on", "number": "CTR-2026-014", **NW, "status": "signed", "amount": 3600.0,
          "currency": "EUR", "signed_at": day(-6), "start_date": day(-4), "end_date": day(246),
          "owner": "WP-E02", "owner_name": "Sam Rivera", "deal": "DEAL-001", "actor": "WP-E02"},
         [("decision", "DEC-002"), ("rule", "POL-NET30")], process="sales")
    emit(at(-6, "15:30"), "deal", "DEAL-001", "deal_won", "sales",
         {"title": "Usage export for Northwind", **NW, "owner": "WP-E02", "owner_name": "Sam Rivera",
          "status": "won", "amount": 3600.0, "currency": "EUR", "expected_close": day(-5),
          "closed_at": day(-6), "contract": "CTR-2026-014", "actor": "WP-E02"},
         [("decision", "DEC-001"), ("decision", "DEC-002")], process="sales")

    # ---- people: a resignation and the requisition it causes
    emit(at(-6, "09:00"), "employee_event", "EE-0007", "resignation_received", "people",
         {"type": "resignation", "employee": "WP-E05", "employee_name": "Tom Novak", "effective_date": day(15),
          "status": "notice", "role": "Backend Engineer", "department": "Engineering", "actor": "WP-E03"},
         process="people")
    emit(at(-5, "14:00"), "requisition", "REQ-0003", "requisition_opened", "people",
         {"role": "Backend Engineer", "department": "Engineering", "status": "open", "owner": "WP-E03",
          "owner_name": "Maya Chen", "opened_at": day(-5), "actor": "WP-E03"},
         [("life_event", "EE-0007")], process="people")

    # ---- the fix ships, the incident and the ticket close
    emit(at(-5, "10:30"), "deploy", "DEP-0019", "deployed", "engineering",
         {"component": "billing-job", "version": "v1.4.2", "environment": "production", "status": "succeeded",
          "project": "api", "author": "WP-E04", "author_name": "Lena Park",
          "deployed_at": at(-5, "10:30").isoformat(), "related": ["gitlab.wp3sample-api.mr-1"], "actor": "WP-E04"},
         [("decision", "fix-billing-retries")], process="engineering")
    emit(at(-5, "11:30"), "incident", "INC-0005", "incident_resolved", "engineering",
         {"title": "Card payment retries fail in the billing job", "severity": "sev2", "status": "resolved",
          "component": "billing-job", "project": "api", **NW, "started_at": at(-9, "08:15").isoformat(),
          "resolved_at": at(-5, "11:30").isoformat(), "owner": "WP-E03", "owner_name": "Maya Chen",
          "related": ["DEP-0019"], "actor": "WP-E03"},
         [("process", "PROC-BILLING")], process="operations")
    emit(at(-5, "12:00"), "support_ticket", "SUP-0012", "ticket_resolved", "support",
         {"title": "Several card payments failed last week", **NW, "status": "resolved", "priority": "high",
          "reporter": "CT-nw_ross", "reporter_name": "Daniel Ross", "assignee": "WP-E03",
          "assignee_name": "Maya Chen", "opened_at": at(-9, "09:05").isoformat(),
          "resolved_at": at(-5, "12:00").isoformat(), "incident": "INC-0005", "contract": "CTR-2026-014",
          "component": "billing-job", "actor": "WP-E03"},
         [("habit", "HAB-NW-ESCALATE")], process="support")

    # ---- money: the invoice (three versions), its payment, payroll, VAT and the cloud bill
    inv = {"number": "INV-2026-0031", **NW, "contract": "CTR-2026-014", "amount": 300.0, "currency": "EUR",
           "issued_at": day(-4), "due_date": (rel.bdays(-4) + timedelta(days=30)).isoformat(), "period": "2026-10",
           "owner": "WP-E02", "owner_name": "Sam Rivera"}
    inv_causes = [("process", "PROC-BILLING"), ("rule", "POL-NET30")]
    emit(at(-4, "09:00"), "invoice", "INV-2026-0031", "invoice_issued", "finance",
         {**inv, "status": "issued", "actor": "WP-E02"}, inv_causes, process="billing")
    emit(at(-4, "09:20"), "invoice", "INV-2026-0031", "invoice_sent", "finance",
         {**inv, "status": "sent", "sent_at": at(-4, "09:20").isoformat(), "actor": "WP-E02"}, inv_causes,
         process="billing")
    emit(at(-3, "09:00"), "payroll", "PR-2026-10", "payroll_approved", "people",
         {"period": "2026-10", "amount": 41200.0, "currency": "EUR", "headcount": 6, "status": "approved",
          "owner": "WP-E01", "owner_name": "Olivia Grant", "actor": "WP-E01"},
         [("process", "PROC-PAYROLL")], process="payroll")
    emit(at(-3, "10:55"), "tax_payment", "TAX-2026-Q3-VAT", "tax_paid", "finance",
         {"tax": "VAT", "period": "2026-Q3", "amount": 5400.0, "currency": "EUR", "authority": "org-taxoffice",
          "authority_name": "Federal Tax Office", "due_date": day(-2), "paid_at": day(-3), "status": "paid",
          "actor": "WP-E01"},
         [("rule", "POL-VAT-Q"), ("life_event", "vat-change")], process="tax")
    emit(at(-3, "11:00"), "bank_entry", "BNK-0109", "bank_entry_booked", "finance",
         {"direction": "debit", "amount": 5400.0, "currency": "EUR", "booked_at": day(-3),
          "account": "Operating account", "counterparty": "org-taxoffice", "counterparty_name": "Federal Tax Office",
          "records": "TAX-2026-Q3-VAT", "reference": "VAT 2026-Q3"},
         [("rule", "POL-VAT-Q")], process="bank")
    emit(at(-2, "09:00"), "cloud_bill", "CB-2026-10", "cloud_bill_received", "finance",
         {"number": "SC-88412", "vendor": "org-skycloud", "vendor_name": "SkyCloud Ltd", "period": "2026-10",
          "amount": 2310.55, "currency": "EUR", "due_date": day(8), "status": "received",
          "services": {"compute": 1650.0, "storage": 410.55, "network": 250.0}}, process="vendors")
    emit(at(-2, "15:55"), "payroll", "PR-2026-10", "payroll_paid", "people",
         {"period": "2026-10", "amount": 41200.0, "currency": "EUR", "headcount": 6, "status": "paid",
          "paid_at": day(-2), "owner": "WP-E01", "owner_name": "Olivia Grant", "actor": "WP-E01"},
         [("process", "PROC-PAYROLL")], process="payroll")
    emit(at(-2, "16:00"), "bank_entry", "BNK-0108", "bank_entry_booked", "finance",
         {"direction": "debit", "amount": 41200.0, "currency": "EUR", "booked_at": day(-2),
          "account": "Operating account", "records": "PR-2026-10", "reference": "Payroll 2026-10"},
         [("process", "PROC-PAYROLL")], process="bank")
    emit(at(-2, "10:00"), "requisition", "REQ-0003", "requisition_interviewing", "people",
         {"role": "Backend Engineer", "department": "Engineering", "status": "interviewing", "owner": "WP-E03",
          "owner_name": "Maya Chen", "opened_at": day(-5), "actor": "WP-E03"},
         [("life_event", "EE-0007")], process="people")
    emit(at(-1, "10:00"), "payment", "PAY-0042", "payment_received", "finance",
         {"number": "PAY-0042", "direction": "incoming", **NW, "amount": 300.0, "currency": "EUR",
          "settles": "INV-2026-0031", "paid_at": day(-1), "method": "bank transfer", "status": "completed"},
         [("process", "PROC-BILLING")], process="billing")
    emit(at(-1, "10:02"), "bank_entry", "BNK-0107", "bank_entry_booked", "finance",
         {"direction": "credit", "amount": 300.0, "currency": "EUR", "booked_at": day(-1),
          "account": "Operating account", "counterparty": "org-northwind", "counterparty_name": "Northwind Foods",
          "records": "PAY-0042", "reference": "INV-2026-0031"},
         [("process", "PROC-BILLING")], process="bank")
    emit(at(-1, "10:05"), "invoice", "INV-2026-0031", "invoice_paid", "finance",
         {**inv, "status": "paid", "sent_at": at(-4, "09:20").isoformat(), "paid_at": day(-1), "actor": "WP-E02"},
         inv_causes, process="billing")

    # ---- client feedback: an NPS response after the billing incident (a contact of the client is its author)
    emit(at(-4, "15:00"), "nps_response", "NPS-0001", "nps_responded", "support",
         {**NW, "contact": "CT-nw_ross", "contact_name": "Daniel Ross", "survey": "2026-Q4", "score": 5,
          "category": "detractor", "sent_at": day(-6), "responded_at": at(-4, "15:00").isoformat(),
          "comment_slot": "sim:NPS-0001:nps_comment"},
         [("process", "INC-0005")], process="nps")

    # ---- gold: every declared cause, first expression
    gold: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for ev, ln in sorted(zip(events, lines), key=lambda x: x[1]["at"]):
        links = [(CAUSE_PREDICATES[c.type], c.id, "sim_cause") for c in ev.causes]
        if ev.fields.get("supersedes"):
            links.append(("supersedes", ev.fields["supersedes"], "sim_field"))
        for pred, cid, how in links:
            k = (f"sim.{ev.entity_id}", pred, f"sim.{cid}")
            if k not in seen:
                seen.add(k)
                gold.append({"effect": k[0], "cause": k[2], "predicate": pred, "expressed_as": how,
                             "since": ln["at"], "explicit": True})
    cases = [
        {"id": "S003", "category": "lookup", "difficulty": "trivial", "language": "en", "split": "dev",
         "question": "Which invoice did Northwind Foods pay for the usage export?", "principal": "user:o.grant",
         "known_at": None, "evidence": [{"entity_id": "sim.PAY-0042", "why": "the payment"},
                                        {"entity_id": "sim.INV-2026-0031", "why": "the invoice"}],
         "roots": [], "forbidden": [], "answer_facts": [{"key": "invoice", "any": ["INV-2026-0031"]}],
         "expect_not_found": False, "rationale": "The payment settles the invoice."},
        {"id": "S004", "category": "why", "difficulty": "easy", "language": "en", "split": "holdout",
         "question": "Why did we open a requisition for a backend engineer?", "principal": "user:m.chen",
         "known_at": None,
         "evidence": [{"entity_id": "sim.REQ-0003", "why": "the requisition", "also": ["sim.EE-0007"]}],
         "roots": [{"entity_id": "sim.EE-0007", "why": "the resignation"}], "forbidden": [],
         "answer_facts": [{"key": "cause", "any": ["resignation"]}], "expect_not_found": False,
         "rationale": "The requisition is caused by Tom Novak's resignation (a life event cause)."},
    ]
    if out.exists():
        shutil.rmtree(out)
    write_delivery(out, tenant=TENANT, sources=s["sources"], people=s["people"], lines=s["lines"] + lines,
                   seed=SEED, language="en", dashboard=s["dashboard"], cases=s["cases"] + cases,
                   gold_links=s["gold"] + gold, sample_of=TENANT,
                   extra_manifest={"fixture": {"of": "variator.orgsim.sample.build_sample plus sim lines",
                                               "anchor": ANCHOR.isoformat()}})
    gz = out / "fixtures" / "timeline.jsonl.gz"  # kept unpacked in kb/: gzip bytes trip make check-english
    (out / "fixtures" / "timeline.jsonl").write_bytes(gzip.decompress(gz.read_bytes()))
    gz.unlink()
    print(f"{len(s['lines'])} provider lines + {len(lines)} sim lines, {len(gold)} sim gold links -> {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
