"""The card extractor (T19): Nemotron Ultra extracts each actor card from its facts and records.

Offline: a stub history and a fake runner, no model call.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from variator.actors.cards import cli as C
from variator.actors.cards.artifacts import profile
from variator.actors.cards.check import check_card
from variator.actors.cards.records import records_for
from variator.actors.cards.writer import EXAMPLE, CardWriters, wkey
from variator.narrative.check import Names

T = date(2026, 10, 27)
TZ = ZoneInfo("Europe/London")
NAMES = Names(people=[("Natalie Brooks", "Natalie"), ("Priya Shah", "Priya")], orgs=["Acme Freight"])


def at(day: str, hour: int = 10) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=TZ)


def history(records: dict[str, list[dict]]) -> SimpleNamespace:
    """What ``records_for`` reads of a ``History``: its sim records, time zone, T and KB ids."""
    return SimpleNamespace(
        records=records, tz=TZ, t_end=at("2026-10-27", 0), kb=lambda kind, ident: f"sim.{ident}"
    )


H = history(
    {
        "support_ticket": [
            {"id": "SUP-1", "ts": at("2026-09-01"), "kind": "ticket_opened", "client": "CL-900",
             "title": "[Acme Freight] Route export fails", "status": "open", "priority": "P2"},
            {"id": "SUP-1", "ts": at("2026-09-03"), "kind": "ticket_escalated", "client": "CL-900",
             "title": "[Acme Freight] Route export fails", "status": "escalated", "priority": "P2"},
            {"id": "SUP-2", "ts": at("2026-10-27", 9), "kind": "ticket_opened", "client": "CL-900",
             "title": "[Acme Freight] On the as-of day", "status": "open"},
            {"id": "SUP-3", "ts": at("2026-09-05"), "kind": "ticket_opened", "client": "CL-901",
             "title": "[Other Corp] Not ours", "status": "open"},
        ],
        "incident": [
            {"id": "INC-1", "ts": at("2026-08-10"), "kind": "incident_opened", "client": "CL-777",
             "affected_clients": ["CL-777", "CL-900"], "assignee": "LW-E18", "title": "api: errors",
             "severity": "SEV2"},
        ],
        "payment": [
            {"id": "PAY-1", "ts": at("2026-07-15"), "kind": "payment_received", "client": "CL-900",
             "title": "Payment PAY-1: INV-7", "status": "completed", "amount": 14200.0, "currency": "USD"},
        ],
        "employee_event": [
            {"id": "EE-1", "ts": at("2026-06-02"), "kind": "leave_booked", "employee": "LW-E18",
             "status": "approved", "type": "vacation"},
        ],
        "vendor_bill": [
            {"id": "BILL-1", "ts": at("2026-10-02"), "kind": "bill_approved", "vendor": "VN-01",
             "title": "Vistula bill for September 2026", "amount": 1000.5, "currency": "USD"},
        ],
    }
)
CLIENT = {
    "id": "CL-900",
    "kind": "client",
    "name": "Acme Freight",
    "link": {"field": "client", "id": "CL-900", "name": "Acme Freight"},
    "people": [{"id": "LW-E18", "name": "Natalie Brooks"}],
    "orgs": [{"id": "CL-900", "name": "Acme Freight"}],
    "facts": [
        {"key": "f1", "topic": "identity", "evidence": ["sim.CTR-0900"],
         "text": "Acme Freight (logistics) is a key client since 2025-04-01; "
         "account manager Natalie Brooks."},
        {"key": "f2", "topic": "payments", "evidence": ["sim.INV-7"],
         "text": "Pays an invoice 44-64 (median 53) days after issue on average."},
        {"key": "f3", "topic": "support", "evidence": ["sim.SUP-1"],
         "text": "Opens 2.08-4.18 (median 3.04) support tickets per month."},
    ],
}
PERSON = {"id": "LW-E18", "kind": "person", "name": "Natalie Brooks", "facts": [],
          "link": {"field": "employee", "id": "LW-E18", "name": "Natalie Brooks"}}
VENDOR = {"id": "VN-01", "kind": "vendor", "name": "Vistula", "facts": [],
          "link": {"field": "vendor", "id": "VN-01", "name": "Vistula"}}
COMPANY = {"id": "company", "kind": "company", "name": "Larchwood Systems", "facts": [],
           "link": {"field": "counterparty", "id": "LW", "name": "Larchwood Systems"}}
RECS = records_for(H, [CLIENT, PERSON, VENDOR, COMPANY])
GOOD = {
    "sentences": [
        {"text": "Acme Freight has been a key logistics client since 1 April 2025, with Natalie Brooks "
         "as its account manager.", "cites": ["f1"]},
        {"text": "It pays an invoice 44-64 (median 53) days after issue on average.", "cites": ["f2"]},
        {"text": "It opens 2.08-4.18 (median 3.04) support tickets per month and escalates a P2 bug "
         "that stays open.", "cites": ["f3", "sim.SUP-1"]},
    ],
    "traits": [
        {"text": "Escalates a blocking bug when it stays open.", "cites": ["sim.SUP-1"]},
        {"text": "Pays in full with a single transfer of 14200 USD.", "cites": ["sim.PAY-1"]},
    ],
}


def check(answer, records=None):
    recs = RECS["CL-900"] if records is None else records
    return check_card(CLIENT, answer, NAMES, t=T, company="Larchwood Systems", records=recs)


# ---------------------------------------------------------------------------------------------- records
def test_a_card_gets_its_actors_own_records_before_t_newest_first():
    ids = [(r["id"], r["kind"]) for r in RECS["CL-900"]]
    assert ids == [
        ("sim.SUP-1", "ticket escalated"),
        ("sim.SUP-1", "ticket opened"),
        ("sim.INC-1", "incident opened"),  # the client is among the affected clients
        ("sim.PAY-1", "payment received"),
    ]
    assert all(r["date"] < T.isoformat() for recs in RECS.values() for r in recs)  # SUP-2 is on T
    assert [r["id"] for r in RECS["LW-E18"]] == ["sim.INC-1", "sim.EE-1"]  # assignee, employee
    assert RECS["LW-E18"][1]["text"] == "leave booked; status approved; type vacation"
    assert RECS["VN-01"][0]["text"] == "Vistula bill for September 2026; amount 1000.5; currency USD"
    assert RECS["CL-900"][3]["text"].endswith("amount 14200; currency USD")
    assert RECS["company"] == []


def test_the_record_budget_and_the_kb_pages_limit_the_records():
    small = records_for(H, [CLIENT], budget=220)["CL-900"]  # each record costs its text and 40
    assert [r["kind"] for r in small] == ["ticket escalated", "ticket opened"]  # the newest fit
    known = {"sim.SUP-1": {}, "sim.PAY-1": {}}
    assert {r["id"] for r in records_for(H, [CLIENT], known)["CL-900"]} == {"sim.SUP-1", "sim.PAY-1"}


# ---------------------------------------------------------------------------------------------- checker
def test_a_card_that_cites_facts_for_numbers_and_records_for_behaviour_is_accepted():
    assert check(GOOD) == []


def test_the_worked_example_of_the_prompt_passes_the_checker():
    card = {
        "id": "CL-EX",
        "kind": "client",
        "name": "Harbour Optics",
        "people": [{"id": "P-1", "name": "Priya Shah"}],
        "orgs": [{"id": "CL-EX", "name": "Harbour Optics"}],
        "facts": [{**f, "evidence": ["sim.CTR-EX"]} for f in EXAMPLE["card"]["facts"]],
    }
    names = Names(people=[("Priya Shah", "Priya")], orgs=["Harbour Optics"])
    probs = check_card(card, EXAMPLE["answer"], names, t=T, records=EXAMPLE["card"]["records"])
    assert probs == []


def test_traits_and_record_citations_are_checked():
    bad = json.loads(json.dumps(GOOD))
    bad["traits"][0]["cites"] = []
    bad["traits"][1]["cites"] = ["sim.SUP-404"]
    probs = " | ".join(check(bad))
    assert "trait 1: cites no record" in probs
    assert "trait 2: cites records not in the request ['sim.SUP-404']" in probs

    changed = json.loads(json.dumps(GOOD))
    changed["traits"][1]["text"] = "Pays in full with a single transfer of 14500 USD."
    assert any("trait 2" in p and "14500" in p for p in check(changed))

    no_traits = {"sentences": GOOD["sentences"]}
    assert "no traits returned" in check(no_traits)
    facts_only = {"sentences": [*GOOD["sentences"][:2], {**GOOD["sentences"][2], "cites": ["f3", "f2"],
                                                         "text": "It opens 2.08-4.18 (median 3.04) support "
                                                         "tickets per month."}]}
    assert check(facts_only, records=[]) == []  # a card without records needs no traits
    assert "sentence 3: cites unknown facts ['sim.SUP-1']" in check(no_traits, records=[])


def test_a_record_on_or_after_t_cannot_be_cited():
    late = [*RECS["CL-900"], {"id": "sim.SUP-2", "date": "2026-10-27", "kind": "ticket opened",
                              "text": "[Acme Freight] On the as-of day; status open"}]
    bad = json.loads(json.dumps(GOOD))
    bad["traits"][0]["cites"] = ["sim.SUP-2"]
    assert any("cites the record sim.SUP-2, dated on or after T" in p for p in check(bad, records=late))


# ---------------------------------------------------------------------------------------------- extractor
def test_ultra_gets_the_records_in_the_request_and_the_card_is_accepted(tmp_path):
    other = {**CLIENT, "id": "CL-901", "name": "Other Corp", "link": {"field": "client", "id": "CL-901"}}
    plan = {"t": T.isoformat(), "company": {"name": "Larchwood Systems"}, "cards": [CLIENT, other]}
    recs = {"CL-900": RECS["CL-900"], "CL-901": []}
    calls = []

    def runner(request):
        calls.append(request)
        return {"ok": True, "cost_usd": 0.01, "seconds": 1.0, "model": "nvidia/Nemotron-3-Ultra-550b-a55b",
                "structured": {"CL-900": GOOD, "CL-901": {"sentences": []}}}

    w = CardWriters(plan, tmp_path, NAMES, None, runner=runner, max_attempts=1, log=lambda s: None,
                    records=recs)
    s = w.run()
    req = calls[0]
    assert req["model"] == "ultra" and req["reasoning"] is True
    props = req["schema"]["properties"]
    assert props["CL-900"]["required"] == ["sentences", "traits"]
    trait_ids = props["CL-900"]["properties"]["traits"]["items"]["properties"]["cites"]["items"]["enum"]
    assert trait_ids == ["sim.SUP-1", "sim.INC-1", "sim.PAY-1"]
    cites = props["CL-900"]["properties"]["sentences"]["items"]["properties"]["cites"]["items"]["enum"]
    assert cites == ["f1", "f2", "f3", "sim.SUP-1", "sim.INC-1", "sim.PAY-1"]
    assert "traits" not in props[wkey("CL-901")]["properties"]  # no records, no traits
    prompt = req["prompt"]
    assert "RECORDS" in prompt and "EXAMPLES" in prompt and "Harbour Optics" in prompt
    assert "[Acme Freight] Route export fails" in prompt and "On the as-of day" not in prompt
    assert s["accepted"] == 1 and s["records"] == 4
    assert s["model"] == "nvidia/Nemotron-3-Ultra-550b-a55b"  # the model the API answered with
    assert w.accepted()["CL-900"]["traits"] == GOOD["traits"]
    assert w.examples(w.load_state(), skip=[])[0]["answer"] == GOOD


def test_the_cli_keeps_the_records_it_sent(tmp_path):
    plan = {"t": T.isoformat(), "source": {"history": None}, "cards": [CLIENT]}
    assert C._records(tmp_path, plan, make=True) == {}  # no history: facts only
    path = tmp_path / "writer" / "records.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"CL-900": RECS["CL-900"]}), encoding="utf-8")
    assert C._records(tmp_path, plan, make=False)["CL-900"] == RECS["CL-900"]  # a later check reads the same


def test_the_card_page_shows_record_chips_and_the_traits():
    p = profile(CLIENT, GOOD, None)
    assert [c["id"] for c in p["sentences"][2]["cites"]] == ["sim.SUP-1"]  # f3's evidence, then the record
    assert [t["text"] for t in p["traits"]] == [t["text"] for t in GOOD["traits"]]
    assert [c["id"] for c in p["traits"][1]["cites"]] == ["sim.PAY-1"]
    assert "traits" not in profile(CLIENT, {"sentences": GOOD["sentences"]}, None)
