"""The real generated history: registry, habits, risks, facts, artifacts and the recovery check."""

from __future__ import annotations

import json

import pytest

from variator.actors.artifacts import write_set
from variator.actors.recovery import TARGET, load_truth, recovery
from variator.web.artifacts import ArtifactStore

PREFIXES = ("data:", "group:", "web:", "llm:", "assumption:")


def _params(doc):
    if isinstance(doc, dict):
        if {"point", "low", "mode", "high", "n", "source"} <= set(doc):
            yield doc
        else:
            for v in doc.values():
                yield from _params(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from _params(v)


@pytest.fixture(scope="module")
def rec(history, learned):
    truth = load_truth(history)
    if truth is None:
        pytest.skip("no truth.json")
    return recovery(learned, truth)


def test_registry_has_all_actor_kinds(learned):
    kinds = {a["kind"] for a in learned.actors}
    assert {"person", "client", "vendor", "regulator"} <= kinds
    people = [a for a in learned.actors if a["kind"] == "person"]
    assert all(
        a["role"] and a["department"] and a["status_at_t"] in ("active", "notice", "left") for a in people
    )
    clients = [a for a in learned.actors if a["kind"] == "client"]
    assert any(a["key"] for a in clients) and any(not a["key"] for a in clients)


def test_params_are_pert_ready_and_tagged(learned):
    docs = learned.documents()
    n = 0
    for name in ("habits.json", "cohorts.json", "risk_register.json"):
        for p in _params(docs[name]):
            assert p["low"] <= p["mode"] <= p["high"], (name, p)
            assert p["low"] <= p["point"] <= p["high"], (name, p)
            assert p["source"].startswith(PREFIXES), p["source"]
            n += 1
    assert n > 500


def test_cohorts_include_future_employees(learned):
    ids = {c["id"] for c in learned.cohorts}
    assert {"staff", "clients:key", "clients:standard", "prospects", "engineers"} <= ids
    future = [c for c in learned.cohorts if c["kind"] == "future_employee"]
    assert future
    assert any(c["llm_pending"] for c in future)  # roles without hires wait for the LLM step
    assert all("hiring_lead_months" in c["params"] for c in future)


def test_risk_register(learned):
    codes = {r["code"]: r for r in learned.risks}
    for code in ("incident_sev2", "client_churn", "late_payment_spell", "resignation", "key_person_absence"):
        assert code in codes, code
    for r in learned.risks:
        assert r["id"].startswith("R")
        assert r["evidence"] and all("." in e for e in r["evidence"])
        assert r["base_rate_per_month"]["high"] >= r["base_rate_per_month"]["low"]
    churn = codes["client_churn"]
    assert churn["hazard_realised"] and churn["hazard_base"]
    assert all(e.startswith(("sim.", "jira.", "mail.", "gitlab.", "person.")) for e in churn["evidence"])


def test_facts_have_evidence_and_habit_numbers(learned):
    f = learned.facts
    eng = next(a["id"] for a in learned.actors if a["kind"] == "person" and a["role"] == "backend")
    doc = f[eng]
    pace = [x for x in doc["facts"] if x.get("param") == "throughput_points_per_sprint"]
    assert pace and pace[0]["evidence"] and pace[0]["evidence"][0].startswith("jira.")
    p = learned.people.params[eng]["throughput_points_per_sprint"]
    assert f"{p.point:.1f}".rstrip("0").rstrip(".") in pace[0]["text"]
    key = next(a["id"] for a in learned.actors if a["kind"] == "client" and a["key"])
    assert any(x["topic"] == "payments" for x in f[key]["facts"])
    assert all(x["evidence"] or x["topic"] in ("rate",) for d in f.values() for x in d["facts"])


def test_recovery_meets_the_target(rec):
    s = rec["summary"]
    assert s["estimate_rows_enough_data"] >= 100
    assert s["coverage"] >= TARGET, rec["worst_misses"]
    for r in rec["rows"]:
        if not r["inside"]:
            assert r["explanation"]


def test_artifact_set_is_served(learned, rec, tmp_path):
    out = tmp_path / "actors"
    m = write_set(learned, out, rec)
    names = {f["path"] for f in m["files"]}
    assert {"actors.json", "habits.json", "cohorts.json", "risk_register.json", "recovery.json"} <= names
    assert any(n.startswith("facts/") for n in names)
    store = ArtifactStore(tmp_path)
    sets = store.list_sets()
    assert sets[0]["id"] == "actors" and "manifest" in sets[0]
    path = store.file(
        "actors", "facts/" + next(a["id"] for a in learned.actors if a["kind"] == "person") + ".json"
    )
    assert json.loads(path.read_text())["facts"]
