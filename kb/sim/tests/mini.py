"""A finished 5-day mini world for the densification, hygiene and audit tests.

Plancy built this world with the LLM engine in step mode and a fake LLM (tests/test_step.py there); this copy has no
engine, so the same storyline is written directly through the World API: an incoming e-mail with an agreement, an
internal e-mail, a ticket and a comment, a meeting page with a decision, a merge request.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from kbsim.models import Attachment, Company, DocSpec
from kbsim.world import World

COMPANY = """
company: {tenant_id: mini, name: "Mini Studio Ltd", short_name: Mini, city: Springfield, timezone: Asia/Bangkok,
          description: A small studio, start_date: 2026-06-15, days: 5, culture: calm}
people:
  - {id: ceo, employee_id: M-01, name: Andrew Sokolov, gender: m, role: Director, department: Management,
     email: a.sokolov@mini.dev, principal: user:a.sokolov, groups: [grp:all-staff, grp:management], personality: sharp,
     writing_style: brief, responsibilities: [everything], goals: [growth], routine: [check the revenue],
     accounts: {mail-mini: a.sokolov@mini.dev}, activity: 0.0}
  - {id: accountant, employee_id: M-02, name: Olga Kim, gender: f, role: Chief Accountant, department: Finance,
     email: o.kim@mini.dev, principal: user:o.kim, groups: [grp:all-staff, grp:finance], personality: strict,
     writing_style: formal, responsibilities: [accounting], goals: [order], routine: [payment reconciliation],
     accounts: {mail-mini: o.kim@mini.dev}, activity: 0.0}
  - {id: dev_back, employee_id: M-03, name: Nikita Morozov, gender: m, role: Backend, department: Engineering,
     email: n.morozov@mini.dev, principal: user:n.morozov, groups: [grp:all-staff, grp:dev], personality: calm,
     writing_style: technical, responsibilities: [API], goals: [quality], routine: [code review],
     accounts: {mail-mini: n.morozov@mini.dev}, activity: 0.0}
organizations:
  - {id: paypro, name: "PayPro Ltd", kind: vendor, domain: paypro.example, description: A payment provider}
contacts:
  - {id: pp_lena, name: Helen Belova, gender: f, org: paypro, role: Manager, email: h.belova@paypro.example,
     writing_style: polite}
products: []
projects: []
tools:
  jira: {SAAS: {key: SAAS, name: Product, acl: [grp:all-staff]}}
  confluence: {MGMT: {key: MGMT, name: Management, acl: [grp:management]}}
  gitlab: {mini/api: {key: mini/api, name: API, acl: [grp:dev]}}
  mail_domain: mini.dev
"""

SCENARIO = """
arcs: [{id: PAY, title: Provider, summary: choice of a payment provider, actors: [ceo, accountant]}]
beats:
  - {id: PAY-01, arc: PAY, day: 1, tick: am, actor: pp_lena, channel: incoming_email, audience: [ceo],
     creates: "email:pp-contract", intent: Send the agreement, facts: [{key: fee, any: ["2.4%"]}],
     attachments: [{handle: "doc:pp-contract", doc_type: contract, title: "Agreement No. 7/2026", number: "7/2026",
                    counterparty: paypro, amount: 120000, key_terms: ["fee 2.4%"], filename: "agreement.docx"}]}
  - {id: PAY-02, arc: PAY, day: 1, tick: pm, actor: ceo, channel: email, audience: [accountant], refers_to: [PAY-01],
     intent: Forward a thought about the agreement, facts: [{key: fee, any: ["2.4%"]}], reference_style: implicit}
  - {id: PAY-03, arc: PAY, day: 2, tick: am, actor: ceo, channel: jira_create, audience: [dev_back], container: SAAS,
     creates: "ticket:pay-int", intent: Create the integration ticket, facts: [{key: name, any: ["PayPro"]}]}
  - {id: PAY-04, arc: PAY, day: 2, tick: pm, actor: accountant, channel: jira_comment, target: "ticket:pay-int",
     intent: Object to the fee, facts: [{key: reserve, any: ["reserve"]}]}
  - {id: PAY-05, arc: PAY, day: 3, tick: am, actor: ceo, channel: meeting, audience: [accountant, dev_back],
     container: MGMT, creates: "page:pay-meeting", intent: Decide, decision: D-PAY, decision_state: reversed,
     facts: [{key: other, any: ["KassaPlus"]}], refers_to: [PAY-04], reference_style: structural}
  - {id: PAY-06, arc: PAY, day: 3, tick: pm, actor: dev_back, channel: mr, container: mini/api, target: "ticket:pay-int",
     creates: "mr:pay", intent: Open the MR, facts: []}
"""

TZ = ZoneInfo("Asia/Bangkok")
START = date(2026, 6, 15)


def at(day: int, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(START + timedelta(days=day - 1), time(h, m), tzinfo=TZ)


def build_mini(root: Path) -> World:
    """Write sim/tenants/mini/{company,scenario}.yaml and sim/out/mini/world.jsonl under root; return the world."""
    tdir = root / "tenants" / "mini"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "company.yaml").write_text(COMPANY, encoding="utf-8")
    (tdir / "scenario.yaml").write_text(SCENARIO, encoding="utf-8")
    company = Company.model_validate(yaml.safe_load(COMPANY))
    w = World(company, root / "out" / "mini")
    spec = DocSpec(handle="doc:pp-contract", doc_type="contract", title="Agreement No. 7/2026", number="7/2026",
                   counterparty="paypro", amount=120000, key_terms=["fee 2.4%"], filename="agreement.docx")
    contract = Attachment(doc_id="doc-0001", filename="agreement.docx",
                          content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                          text="Agreement No. 7/2026. The processing fee 2.4% of every payment. " * 4, meta=spec)
    first = w.send_email(ts=at(1, "09:10"), day=1, sender="pp_lena", to=["ceo"], cc=[], subject="Agreement 7/2026",
                         body="Hello Andrew,\nplease find our agreement attached: the fee is 2.4%.\nBest, Helen",
                         attachments=[contract], beat_id="PAY-01", handle="email:pp-contract")
    w.send_email(ts=at(1, "15:00"), day=1, sender="ceo", to=["accountant"], cc=[], subject="PayPro terms",
                 body="Olga, the vendor from this morning wants 2.4%. Please check.", beat_id="PAY-02")
    key = w.create_ticket(ts=at(2, "10:00"), day=2, actor="ceo", project="SAAS", issue_type="Task",
                          summary="PayPro payment integration", description="Integrate the PayPro payment API.",
                          assignee="dev_back", priority="High", labels=["payments"], beat_id="PAY-03",
                          handle="ticket:pay-int")
    w.comment_ticket(ts=at(2, "15:00"), day=2, actor="accountant", key=key,
                     body="The fee eats into our reserve, let us compare providers first.", beat_id="PAY-04")
    w.create_page(ts=at(3, "11:00"), day=3, actor="ceo", space="MGMT", title="Payment provider meeting",
                  body="## Discussion\nOlga compared PayPro and KassaPlus.", kind="meeting_notes",
                  attendees=["ceo", "accountant", "dev_back"],
                  decisions=["We choose KassaPlus instead of PayPro for online payment"],
                  action_items=[{"assignee": "dev_back", "text": "Switch the API integration"}], properties={},
                  held_at=at(3, "10:00"), beat_id="PAY-05", handle="page:pay-meeting")
    w.create_mr(ts=at(3, "15:00"), day=3, actor="dev_back", repo="mini/api", title=f"{key}: payment API",
                description=f"Adds the payment API.\n\nCloses {key}", reviewers=["ceo"], closes=[key],
                beat_id="PAY-06", handle="mr:pay")
    assert first == "msg-000001"
    return w
