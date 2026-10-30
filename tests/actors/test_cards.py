"""Actor cards: the checker, the writer loop (fake runner), the plan, the KB delivery and its intake."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from variator.actors.cards import fmt
from variator.actors.cards.changes import case_items, load_cases, missing_items
from variator.actors.cards.check import check_card, citable, fallback
from variator.actors.cards.writer import CardWriters, wkey
from variator.narrative.check import Names

T = date(2026, 10, 27)
ACTORS = Path("/tmp/variator-1000/mvp-artifacts/actors")
HISTORY = Path("/tmp/variator-1000/mvp-history-v2")
CASES = Path(__file__).resolve().parents[2] / "workspace" / "config" / "cases"
NAMES = Names(
    people=[("Natalie Brooks", "Natalie"), ("Grace Whitfield", "Grace")], orgs=["Acme Freight", "Other Corp"]
)
KNOWN = {k: {"title": k, "kind": ""} for k in ("sim.CTR-0900", "sim.INV-1", "sim.INV-2", "sim.SUP-1")}
CARD = {
    "id": "CL-900",
    "kind": "client",
    "name": "Acme Freight",
    "people": [{"id": "LW-E18", "name": "Natalie Brooks"}],
    "orgs": [{"id": "CL-900", "name": "Acme Freight"}],
    "facts": [
        {
            "key": "f1",
            "topic": "identity",
            "evidence": ["sim.CTR-0900"],
            "text": "Acme Freight (logistics) is a key client since 2025-04-01; "
            "account manager Natalie Brooks.",
        },
        {
            "key": "f2",
            "topic": "payments",
            "evidence": ["sim.INV-1", "sim.INV-2"],
            "text": "Pays an invoice 44-64 (median 53) days after issue on average.",
        },
        {
            "key": "f3",
            "topic": "support",
            "evidence": ["sim.SUP-1"],
            "text": "Opens 2.08-4.18 (median 3.04) support tickets per month.",
        },
        {
            "key": "f4",
            "topic": "risk",
            "evidence": ["sim.CTR-0900"],
            "text": "Probability to give notice within a year: 10-27% (median 17%).",
        },
        {
            "key": "f5",
            "topic": "other",
            "evidence": ["dataset:x.csv"],
            "text": "A dataset fact with the number 99.",
        },
    ],
}
GOOD = {
    "sentences": [
        {
            "text": "Acme Freight has been a key logistics client since 1 April 2025, "
            "with Natalie Brooks as its account manager.",
            "cites": ["f1"],
        },
        {"text": "It pays an invoice 44-64 (median 53) days after issue on average.", "cites": ["f2"]},
        {
            "text": "It opens 2.08-4.18 (median 3.04) support tickets per month, "
            "and its probability to give notice within a year is 10-27% (median 17%).",
            "cites": ["f3", "f4"],
        },
    ]
}


def check(answer):
    return check_card(CARD, answer, NAMES, t=T, known=KNOWN, company="Larchwood Systems")


# ---------------------------------------------------------------------------------------------- checker
def test_a_profile_that_says_only_its_cited_facts_is_accepted():
    assert check(GOOD) == []


def test_a_number_from_another_fact_or_invented_is_rejected():
    bad = json.loads(json.dumps(GOOD))
    bad["sentences"][1]["text"] = "It pays an invoice after 61 days and opens 2.08-4.18 tickets per month."
    probs = check(bad)
    assert any("number 61" in p for p in probs)
    assert any("number 2.08" in p for p in probs)  # in the card, but not in the fact this sentence cites


def test_names_dates_and_citations_are_checked():
    bad = json.loads(json.dumps(GOOD))
    bad["sentences"][0]["text"] = "Acme Freight works with Grace Whitfield of Other Corp since 3 March 2027."
    bad["sentences"][2]["cites"] = ["f5"]  # dataset evidence only: not a KB record, so not citable
    probs = " | ".join(check(bad))
    assert "person Grace Whitfield" in probs
    assert "organisation Other Corp" in probs
    assert "not before T" in probs
    assert "cites unknown facts ['f5']" in probs


def test_too_short_and_unnamed_profiles_are_rejected():
    short = {"sentences": GOOD["sentences"][1:]}
    probs = " | ".join(check(short))
    assert "2 sentences" in probs
    assert "does not name Acme Freight" in probs
    assert check(None) == ["no sentences returned"]


def test_citable_facts_need_a_kb_record_and_the_fallback_uses_them():
    assert [f["key"] for f in citable(CARD, KNOWN)] == ["f1", "f2", "f3", "f4"]
    fb = fallback(CARD, KNOWN)
    assert fb["sentences"][0]["cites"] == ["f1"]
    assert all(len(s["cites"]) == 1 for s in fb["sentences"])


# ---------------------------------------------------------------------------------------------- writer
def test_the_writer_retries_a_rejected_card_and_drops_after_the_limit(tmp_path):
    plan = {
        "t": T.isoformat(),
        "company": {"name": "Larchwood Systems"},
        "cards": [CARD, {**CARD, "id": "future:qa", "name": "Future QA engineer"}],
    }
    calls = []

    def runner(request):
        calls.append(request)
        prompt = request["prompt"]
        good = "REJECTED" in prompt  # the retry prompt: the first card gets it right now
        return {
            "ok": True,
            "cost_usd": 0.001,
            "seconds": 0.1,
            "structured": {
                "CL-900": GOOD if good else {"sentences": GOOD["sentences"][:1]},
                wkey("future:qa"): {"sentences": GOOD["sentences"][:1]},
            },
        }

    w = CardWriters(plan, tmp_path, NAMES, KNOWN, runner=runner, max_attempts=2, log=lambda s: None)
    s = w.run()
    assert s["accepted"] == 1 and s["retried"] == 2 and s["dropped"] == ["future:qa"]
    assert s["cost_usd"] == pytest.approx(0.002)
    assert calls[0]["model"] == "ultra" and calls[0]["reasoning"] is True
    assert list(w.accepted()) == ["CL-900"]
    # resumable: a second run makes no calls
    assert w.run()["requests"] == 2 and len(calls) == 2


# ---------------------------------------------------------------------------------------------- formats
def test_habit_rows_have_one_display_form():
    assert fmt.display({"point": 52.95, "low": 43.87, "high": 63.85, "unit": "days"}) == {
        "value": "53",
        "interval": "44 – 64",
        "suffix": "days",
    }
    assert (
        fmt.display({"point": 0.1744, "low": 0.1016, "high": 0.2681, "unit": "probability per year"})["value"]
        == "17%"
    )
    assert fmt.display({"point": 1022400.0, "low": 1022400.0, "high": 1022400.0, "unit": "USD per year"}) == {
        "value": "$1.02M",
        "interval": "",
        "suffix": "/ year",
    }
    assert (
        fmt.display({"point": 530.3, "low": 470.3, "high": 530.3, "unit": "minutes after midnight (local)"})[
            "value"
        ]
        == "08:50"
    )
    assert fmt.source_label("data:sim_invoice@2026-10-27") == "data · invoice"
    assert fmt.source_kind("llm:pending") == "llm"


def test_every_mapped_case_item_exists_in_the_case_configs():
    cases = load_cases(CASES)
    if not cases:
        pytest.skip("no case configs")
    assert missing_items(cases) == []
    assert case_items(cases["company_best_year"])["raise_prices_8"]["kind"] == "decision"


# ------------------------------------------------------------------------------------------- real artifacts
@pytest.fixture(scope="module")
def plan():
    if not (ACTORS / "actors.json").is_file() or not (HISTORY / "datasets").is_dir():
        pytest.skip("no WP10 artifact set or history")
    from variator.actors.cards.build import build_plan

    return build_plan(ACTORS, HISTORY, CASES)


def test_the_plan_has_every_card_and_respects_people_and_as_of(plan):
    groups = {}
    for c in plan["cards"]:
        groups[c["group"]] = groups.get(c["group"], 0) + 1
    assert groups == {"company": 1, "people": 20, "clients": 6, "vendors": 7, "cohorts": 12}
    assert len(plan["risks"]) == 19
    for c in plan["cards"]:
        for f in c["facts"]:
            for d in __import__("re").findall(r"\b\d{4}-\d{2}-\d{2}\b", f["text"]):
                assert date.fromisoformat(d) < T, (c["id"], f["text"])
        if c["kind"] == "person":
            assert not any(r["key"] == "resignation_hazard" for r in c["habits"])
            assert not any("salary" in f["text"].lower() for f in c["facts"])
            assert c["risks"] == []
        assert sum(r["primary"] for r in c["habits"]) <= 6
    meridian = next(c for c in plan["cards"] if c["id"] == "CL-001")
    assert meridian["dossier"] == "org.CL-001"
    assert any(r["key"] == "pay_delay_days" and r.get("series") for r in meridian["habits"])
    assert any(ch["id"] == "key_account_event" for ch in meridian["changes"])
    churn = next(r for r in plan["risks"] if r["code"] == "client_churn")
    assert {s["id"] for s in churn["shocks"]} >= {"key_account_event", "big_client_leaves"}


def test_the_kb_delivery_links_each_card_and_passes_the_intake(plan, tmp_path):
    from variator.actors.cards.export import KB_DIR, export_delivery, intake_validate

    if not (KB_DIR / "sim" / "kbsim" / "intake.py").is_file():
        pytest.skip("no kb/ copy")
    m = export_delivery(
        plan, {}, None, HISTORY / "delivery", tmp_path / "delivery", "2026-10-26T19:00:00+00:00"
    )
    assert m["counts"]["events"] == 46
    assert m["sample_of"] and m["increment_of"]
    import gzip

    lines = [json.loads(x) for x in gzip.open(tmp_path / "delivery" / "fixtures" / "timeline.jsonl.gz", "rt")]
    assert {ln["object_type"] for ln in lines} == {"habit"}
    by = {ln["payload"]["fields"]["card"]: ln for ln in lines}
    assert by["CL-001"]["payload"]["fields"]["client"] == "CL-001"
    assert by["LW-E06"]["payload"]["fields"]["employee"] == "LW-E06"
    assert by["LW-E06"]["container"] == "people"
    assert all(ln["payload"]["fields"]["labels"] == ["profile"] for ln in lines)
    assert max(ln["at"] for ln in lines) < "2026-10-27"
    rep = intake_validate(tmp_path / "delivery", tmp_path / "intake")
    assert rep["errors"] == 0, rep["failed_checks"]
