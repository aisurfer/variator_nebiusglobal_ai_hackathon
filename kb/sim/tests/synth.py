"""A tiny synthetic tenant for the exporter/delivery tests: company, scenario, cases and a hand-written world log.

One storyline (payment provider) touches every artefact type: a ticket created/commented/updated with a structural
remote link to a meeting decision, a meeting page whose decision is later reversed, an incoming e-mail with a
contract and its reply, a regulation created and updated as a wiki page (not exported: no XWiki connector), an MR
reviewed and merged (+ merge commit), a confidential e-mail, and a beat that the generator never realised.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from kbsim.export import Paths

TENANT = "simt"
TZ = "+07:00"


def T(s: str) -> str:
    """'2026-06-15 10:05' -> ISO with the tenant offset."""
    return datetime.fromisoformat(s.replace(" ", "T") + ":00" + TZ).isoformat()


def _persona(pid: str, emp: str, name: str, gender: str, role: str, dept: str, login: str, groups: list[str],
             accounts: dict[str, str] | None = None) -> dict[str, Any]:
    return {"id": pid, "employee_id": emp, "name": name, "gender": gender, "role": role, "department": dept,
            "email": f"{login}@test.digital", "principal": f"user:{login}", "groups": ["grp:all-staff", *groups],
            "personality": "Calm.", "writing_style": "Brief.", "responsibilities": ["work"], "goals": ["goal"],
            "routine": ["routine"], "accounts": accounts or {}}


COMPANY: dict[str, Any] = {
    "company": {"tenant_id": TENANT, "name": "Test Digital Ltd", "short_name": "Test", "city": "Springfield",
                "timezone": "Asia/Bangkok", "description": "A test company.", "start_date": "2026-06-15",
                "days": 10, "culture": "We work in Jira and e-mail."},
    "people": [
        _persona("ceo", "KD-E01", "Andrew Sokolov", "m", "Chief Executive Officer", "Management", "a.sokolov",
                 ["grp:management"], {"jira-simt": "712020:a.sokolov", "mail": "A.Sokolov@test.digital"}),
        _persona("accountant", "KD-E02", "Olga Morozova", "f", "Chief Accountant", "Accounting", "o.morozova",
                 ["grp:finance"]),
        _persona("dev", "KD-E03", "Igor Petrov", "m", "Backend Developer", "Engineering", "i.petrov",
                 ["grp:dev"], {"gitlab": "ipetrov"}),
        _persona("pm", "KD-E04", "Maria Lebedeva", "f", "Project Manager", "Projects", "m.lebedeva",
                 ["grp:projects"]),
    ],
    "organizations": [{"id": "translog", "name": "TransLogistics Ltd", "kind": "client", "domain": "translog.com",
                       "inn": "5401234567", "description": "A logistics client."}],
    "contacts": [{"id": "tl_ivanov", "name": "Peter Ivanov", "gender": "m", "org": "translog",
                  "role": "Head of Logistics", "email": "P.Ivanov@translog.com", "writing_style": "Formal."}],
    "products": [],
    "projects": [],
    "tools": {
        "jira": {"MENU": {"key": "MENU", "name": "Menu", "acl": ["grp:all-staff"]},
                 "HR": {"key": "HR", "name": "People", "acl": ["grp:hr", "grp:management"]}},
        "confluence": {"GEN": {"key": "GEN", "name": "Company", "acl": ["grp:all-staff"]},
                       "MGMT": {"key": "MGMT", "name": "Management", "acl": ["grp:management"]}},
        "gitlab": {"test/menu-backend": {"key": "test/menu-backend", "name": "Backend", "acl": ["grp:dev", "grp:management"]}},
        "xwiki": {"Regulations": {"key": "Regulations", "name": "Regulations", "acl": ["grp:all-staff"]}},
        "mail_domain": "test.digital",
        "group_mailboxes": {"info@test.digital": ["grp:sales"]},
    },
}


def _beat(bid: str, day: int, tick: str, actor: str, channel: str, **kw: Any) -> dict[str, Any]:
    return {"id": bid, "arc": "pay", "day": day, "tick": tick, "actor": actor, "channel": channel,
            "intent": kw.pop("intent", "get it done"), **kw}


CONTRACT = {"handle": "doc:contract-translog", "doc_type": "contract", "title": "Service agreement No. 14/2026",
            "number": "14/2026", "doc_date": "2026-06-16", "counterparty": "translog", "amount": 1500000.0,
            "currency": "USD", "key_terms": ["1,500,000 USD"], "filename": "Agreement_14-2026_TransLogistics.docx"}

SCENARIO: dict[str, Any] = {
    "arcs": [{"id": "pay", "title": "Payment provider", "summary": "Choosing and changing the provider.",
              "actors": ["ceo", "accountant", "dev"]}],
    "beats": [
        _beat("PAY-01", 1, "am", "dev", "jira_create", container="MENU", creates="ticket:pay-integration",
              facts=[{"key": "p", "any": ["online payment"]}]),
        _beat("PAY-02", 1, "pm", "ceo", "meeting", container="GEN", audience=["accountant", "dev"],
              creates="page:pay-meeting", decision="D-PAY", decision_state="decided",
              facts=[{"key": "provider", "any": ["PayNova", "Pay Nova"]}], refers_to=["PAY-01"]),
        _beat("TL-01", 2, "am", "tl_ivanov", "incoming_email", audience=["pm"], creates="email:tl-contract",
              attachments=[CONTRACT]),
        _beat("PAY-03", 2, "am", "accountant", "jira_comment", target="ticket:pay-integration",
              facts=[{"key": "fee", "any": ["3.5%"]}], refers_to=["PAY-02"]),
        _beat("PAY-04", 2, "pm", "dev", "jira_transition", target="ticket:pay-integration",
              status={"status": "In Progress"}, reference_style="structural", refers_to=["PAY-02"]),
        _beat("TL-02", 3, "am", "pm", "email", audience=["tl_ivanov"], target="email:tl-contract"),
        _beat("PAY-05", 3, "pm", "ceo", "page", target="page:pay-meeting", decision="D-PAY",
              decision_state="reversed", facts=[{"key": "provider2", "any": ["CloudPayments"]}]),
        _beat("REG-01", 3, "pm", "ceo", "wiki", container="Regulations", creates="wiki:payment-policy"),
        _beat("REG-02", 4, "am", "accountant", "wiki", target="wiki:payment-policy",
              facts=[{"key": "cap", "any": ["2.9%"]}]),
        _beat("MR-01", 4, "am", "dev", "mr", container="test/menu-backend", creates="mr:pay"),
        _beat("MR-02", 5, "am", "dev", "mr", target="mr:pay"),
        _beat("HR-01", 5, "pm", "ceo", "email", audience=["accountant"], confidential=True,
              facts=[{"key": "salary", "any": ["250,000"]}]),
        _beat("GHOST-01", 6, "am", "pm", "page", container="GEN", creates="page:ghost"),
    ],
    "decisions": [{"id": "D-PAY", "title": "Payment provider",
                   "history": [{"beat": "PAY-02", "state": "decided", "summary": "PayNova"},
                               {"beat": "PAY-05", "state": "reversed", "summary": "CloudPayments"}]}],
}


def _case(cid: str, category: str, question: str, difficulty: str = "trivial", **expect: Any) -> dict[str, Any]:
    principal = expect.pop("principal", "user:admin")
    known_at = expect.pop("known_at", None)
    c = {"id": cid, "difficulty": difficulty, "category": category, "question": question, "principal": principal,
         "expect": expect, "rationale": "check"}
    if known_at:
        c["known_at"] = known_at
    return c


CASES: dict[str, Any] = {"cases": [
    _case("C001", "numeric", "What is the amount of the agreement with TransLogistics?", evidence_beats=["TL-01"],
          answer_facts=[{"key": "amount", "any": ["1,500,000", "1.5 million"]}]),
    _case("C002", "why", "Why was the payment integration ticket taken into work?", "tricky",
          evidence_beats=["PAY-04"], root_beats=["PAY-02"]),
    _case("C003", "acl", "What salary did the CEO and the accountant discuss?", "tricky", principal="user:i.petrov",
          forbidden_beats=["HR-01"], expect_not_found=True),
    _case("C004", "negative", "Did we sign an agreement with Medline?", expect_not_found=True),
    _case("C005", "lookup", "What is in the ghost page?", evidence_beats=["GHOST-01"]),
    _case("C006", "history", "How did the decision on the payment provider change?", "tricky",
          evidence_beats=["PAY-02", "PAY-05"], answer_facts=[{"key": "a", "any": ["PayNova"]},
                                                              {"key": "b", "any": ["CloudPayments"]}]),
    _case("C007", "as_of", "Which provider was chosen as of June 16?", "tricky", known_at=T("2026-06-16 18:00"),
          evidence_beats=["PAY-02"], answer_facts=[{"key": "a", "any": ["PayNova"]}]),
]}

MSG1 = "<20260616.100000.000001@translog.com>"
MSG2 = "<20260617.100000.000002@test.digital>"
MERGE_SHA = "3f1c2b9a0d4e5f60718293a4b5c6d7e8f9012345"


def world_events() -> list[dict[str, Any]]:
    ev: list[dict[str, Any]] = []

    def add(ts: str, actor: str, kind: str, atype: str, aid: str, data: dict, beat: str | None = None,
            handle: str | None = None) -> None:
        ev.append({"seq": len(ev) + 1, "ts": T(ts), "day": int(ts[8:10]) - 14, "actor": actor, "kind": kind,
                   "artifact_type": atype, "artifact_id": aid, "data": data, "beat_id": beat, "handle": handle})

    def ticket(key: str, summary: str, ts: str, reporter: str, **kw: Any) -> dict:
        return {"key": key, "project": key.split("-")[0], "issue_type": kw.pop("issue_type", "Task"),
                "summary": summary, "description": kw.pop("description", ""), "status": "To Do", "priority": "Medium",
                "reporter": reporter, "assignee": kw.pop("assignee", None), "labels": kw.pop("labels", []),
                "parent": None, "links": [], "remote_links": [], "comments": [], "created": T(ts), "updated": T(ts),
                **kw}

    add("2026-06-15 10:05", "dev", "ticket.created", "ticket", "MENU-1",
        ticket("MENU-1", "Online payment integration", "2026-06-15 10:05", "dev",
               description="## Goal\nConnect **online payment** of orders.\n\n- step 1\n  - sub-step\n- step 2\n\n"
                           "Details: https://example.org/spec.", labels=["payments"]),
        "PAY-01", "ticket:pay-integration")
    add("2026-06-15 10:30", "pm", "ticket.created", "ticket", "MENU-2",
        ticket("MENU-2", "Receipt after payment", "2026-06-15 10:30", "pm", issue_type="task"))
    add("2026-06-15 15:10", "ceo", "page.created", "page", "page-0001", {
        "id": "page-0001", "space": "GEN", "title": "Payment meeting June 15", "kind": "meeting_notes",
        "body": "## Agenda\n- Choice of the provider\n\n## Discussion\nOlga objects to the **3.5%** fee.\n\n"
                "| Provider | Fee |\n|---|---|\n| PayNova | 3.5% |\n| CloudPayments | 2.9% |\n\n"
                "## Decisions\n- duplicates the structured list\n",
        "author": "ceo", "attendees": ["ceo", "accountant", "dev"], "held_at": T("2026-06-15 15:00"),
        "decisions": ["We connect PayNova for online payment", "We run the pilot with the client TransLogistics"],
        "action_items": [{"assignee": "dev", "text": "Build the integration"}, {"assignee": "", "text": "Notify the clients"}],
        "properties": {"Based on": "ticket:pay-integration", "Owner": "ceo"},
        "created": T("2026-06-15 15:10"), "updated": T("2026-06-15 15:10"), "version": 1},
        "PAY-02", "page:pay-meeting")
    add("2026-06-16 10:00", "tl_ivanov", "email.sent", "email", "msg-000001", {
        "id": "msg-000001", "message_id": MSG1, "thread_id": "thr-0001", "in_reply_to": None, "sender": "tl_ivanov",
        "to": ["pm"], "cc": ["info@test.digital"], "subject": "Agreement for the pilot", "body": "Good afternoon,\nplease find the agreement attached.",
        "sent": T("2026-06-16 10:00"),
        "attachments": [{"doc_id": "doc-0001", "filename": CONTRACT["filename"],
                         "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         "text": "AGREEMENT No. 14/2026\nThe service fee is 1,500,000 USD.", "meta": CONTRACT}],
        "mailbox_acl": ["grp:sales", "user:m.lebedeva"]}, "TL-01", "email:tl-contract")
    add("2026-06-16 11:00", "accountant", "ticket.commented", "ticket", "MENU-1", {"comment": {
        "id": "comment-0001", "author": "accountant", "body": "A 3.5% fee is expensive, as I wrote on Thursday.",
        "created": T("2026-06-16 11:00")}}, "PAY-03")
    add("2026-06-16 16:00", "dev", "ticket.updated", "ticket", "MENU-1", {
        "changes": {"status": "In Progress", "assignee": "dev",
                    "remote_links": [{"relationship": "implements", "target": "page:pay-meeting", "title": "Payment decision"},
                                     {"relationship": "mentioned in", "target": "email:tl-contract", "title": "E-mail"}],
                    "links": [{"type": "relates", "key": "MENU-2"}]},
        "comment": {"id": "comment-0002", "author": "dev", "body": "Taking it into work.", "created": T("2026-06-16 16:00")}},
        "PAY-04")
    add("2026-06-17 10:00", "pm", "email.sent", "email", "msg-000002", {
        "id": "msg-000002", "message_id": MSG2, "thread_id": "thr-0001", "in_reply_to": MSG1, "sender": "pm",
        "to": ["tl_ivanov"], "cc": [], "subject": "Re: Agreement for the pilot", "body": "Peter, received it, we will sign.",
        "sent": T("2026-06-17 10:00"), "attachments": [], "mailbox_acl": ["user:m.lebedeva"]}, "TL-02")
    add("2026-06-17 15:00", "ceo", "page.updated", "page", "page-0001", {
        "body": None, "title": None, "decisions": ["We drop PayNova and connect CloudPayments",
                                                   "We run the pilot with the client TransLogistics"],
        "action_items": None, "properties": None}, "PAY-05")
    add("2026-06-17 16:00", "ceo", "wiki.created", "wiki", "Regulations.PaymentPolicy", {
        "id": "Regulations.PaymentPolicy", "space": "Regulations", "title": "Payment acceptance policy",
        "body": "## Purpose\nHow we accept payments.\n\n## Rules\n- Fee at most **3%**\n- Refund ~5 days\n",
        "author": "ceo", "objects": {"owner": "ceo", "status": "active"}, "created": T("2026-06-17 16:00"),
        "updated": T("2026-06-17 16:00"), "version": 1}, "REG-01", "wiki:payment-policy")
    add("2026-06-18 10:00", "accountant", "wiki.updated", "wiki", "Regulations.PaymentPolicy", {
        "body": "## Purpose\nHow we accept payments.\n\n## Rules\n- Fee at most **2.9%**\n", "title": None,
        "objects": None}, "REG-02")
    add("2026-06-18 11:00", "dev", "mr.created", "mr", "test/menu-backend!1", {
        "id": "test/menu-backend!1", "repo": "test/menu-backend", "iid": 1, "title": "CloudPayments integration",
        "description": "Adds the payment gateway.", "author": "dev", "reviewers": ["ceo"], "state": "opened",
        "source_branch": "feature/cloudpayments", "closes": ["ticket:pay-integration"], "comments": [],
        "created": T("2026-06-18 11:00"), "updated": T("2026-06-18 11:00")}, "MR-01", "mr:pay")
    add("2026-06-18 12:00", "ceo", "mr.commented", "mr", "test/menu-backend!1", {"comment": {
        "id": "note-0001", "author": "ceo", "body": "OK, but no card logs.", "created": T("2026-06-18 12:00")}})
    add("2026-06-19 10:00", "ceo", "mr.merged", "mr", "test/menu-backend!1", {"merge_commit": MERGE_SHA}, "MR-02")
    add("2026-06-19 10:00", "ceo", "commit.pushed", "commit", MERGE_SHA, {
        "sha": MERGE_SHA, "repo": "test/menu-backend", "author": "dev",
        "message": "Merge branch 'feature/cloudpayments' into 'main'\n\nCloudPayments integration\n\n"
                   "See merge request test/menu-backend!1", "created": T("2026-06-19 10:00")})
    add("2026-06-19 15:00", "ceo", "email.sent", "email", "msg-000003", {
        "id": "msg-000003", "message_id": "<20260619.150000.000003@test.digital>", "thread_id": "thr-0002",
        "in_reply_to": None, "sender": "ceo", "to": ["accountant"], "cc": [], "subject": "Salary of the new developer",
        "body": "Olga, I approved a salary of 250,000 USD.", "sent": T("2026-06-19 15:00"), "attachments": [],
        "mailbox_acl": ["user:a.sokolov", "user:o.morozova"]}, "HR-01")
    return ev


def write_tenant(root: Path, *, world: list[dict[str, Any]] | None = None) -> Paths:
    """Lay out a tenant under root: root/sim/{tenants,out}/simt, root/kb/{config,fixtures} are written by export."""
    sim = root / "sim"
    tdir = sim / "tenants" / TENANT
    tdir.mkdir(parents=True, exist_ok=True)
    for name, data in (("company", COMPANY), ("scenario", SCENARIO), ("cases", CASES)):
        (tdir / f"{name}.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    out = sim / "out" / TENANT
    out.mkdir(parents=True, exist_ok=True)
    with (out / "world.jsonl").open("w", encoding="utf-8") as fh:
        for e in world if world is not None else world_events():
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return Paths(TENANT, sim, root / "kb")
