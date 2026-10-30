"""workspace/config/company.facts.json (the facts of the intake chat) matches the history: staff at T.

Every owner and person of the case configs must be on staff at T (a person who left before T owns nothing).
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/mvp"))
from make_company_facts import ACTORS, ARR_AT_T, BACKFILLS, LEFT, OUT, SCHEMA, build_facts  # noqa: E402

from variator.orgsim.company.spec import default_spec  # noqa: E402

CASES = ("company_best_year", "delivery_best_way")


def _facts() -> dict:
    return json.loads(OUT.read_text(encoding="utf-8"))


def test_facts_file_is_fresh():
    fresh = json.loads(json.dumps(build_facts()))
    assert _facts() == fresh, "run: .venv/bin/python scripts/mvp/make_company_facts.py"


def test_facts_match_the_spec():
    spec, facts = default_spec(), _facts()
    assert facts["schema"] == SCHEMA and facts["company"] == spec.company.name
    people = {p["id"]: p for p in facts["people"]}
    assert len(people) == 20  # 20 at the start, 3 left, 3 backfills
    assert not set(LEFT) & set(people)
    departments = {d["id"]: d for d in facts["departments"]}
    assert set(departments) == {d.id for d in spec.departments}
    for p in spec.people:
        if p.id in LEFT:
            continue
        f = people[p.id]
        assert (f["name"], f["title"]) == (p.name, p.title)
        assert departments[f["department"]]["name"].lower() == p.department.lower()
    assert all(f["manager"] is None or f["manager"] in people for f in people.values()), "managers on staff"
    assert all(d["head"] in people for d in departments.values()), "department heads on staff"
    ceo = people[facts["ceo"]]
    assert ceo["manager"] is None and ceo["title"] == "Chief Executive Officer"
    assert [(c["id"], c["name"], c["acv"], c["renewalInMonths"]) for c in facts["keyClients"]] == [
        (c.id, c.name, c.acv, c.renewal_in_months) for c in spec.key_clients
    ]
    assert facts["otherClients"] == spec.cohort.count
    assert [(v["id"], v["name"], v["kind"]) for v in facts["vendors"]] == [
        (v.id, v.name, v.kind) for v in spec.vendors
    ]


@pytest.mark.skipif(not ACTORS.exists(), reason=f"needs the roster at T of the actor engine: {ACTORS}")
def test_staff_equals_the_roster_at_t():
    actors = json.loads(ACTORS.read_text(encoding="utf-8"))["actors"]
    people = [a for a in actors if a["kind"] == "person"]
    on_staff = {a["id"]: a for a in people if a["status_at_t"] != "left"}
    assert {p["id"] for p in _facts()["people"]} == set(on_staff)
    assert {a["id"]: a["last_day"] for a in people if a["status_at_t"] == "left"} == LEFT
    for pid, name, title, seat, _manager, start in BACKFILLS:
        a = on_staff[pid]
        assert (a["name"], a["title"], a["seat"], a["start"]) == (name, title, seat, start)
    arr = {a["id"]: a["arr_at_t"] for a in actors if a["kind"] == "client"}
    assert {c["id"]: c["arrAtT"] for c in _facts()["keyClients"]} == {k: arr[k] for k in ARR_AT_T}


def test_meridian_event_takes_its_real_share_of_arr():
    """Case B: the placed event names Meridian Freight Group: its default size is its ARR at T over ARR."""
    from variator_it_services.company import company_model

    config = json.loads((ROOT / "workspace/config/cases/company_best_year.json").read_text(encoding="utf-8"))
    variables = {v["name"]: v["dist"]["value"] for v in config["variables"] if "value" in v["dist"]}
    meridian = next(c for c in _facts()["keyClients"] if c["id"] == "CL-001")
    share = meridian["arrAtT"] / variables["arr0"]
    assert variables["ev_account_size"] == pytest.approx(share) and 0.08 < share < 0.09
    assert f"about {share:.0%} of ARR" in config["items"]["account_event"]["description"]
    assert company_model().variable("ev_account_size").distribution.value == 0.1  # the template keeps 10%


@pytest.mark.parametrize("case", CASES)
def test_case_owners_and_people_are_on_staff_at_t(case):
    staff = {p["id"]: p for p in _facts()["people"]}
    config = json.loads((ROOT / f"workspace/config/cases/{case}.json").read_text(encoding="utf-8"))
    ids = [p["id"] for p in config["people"]]
    assert "LW-E01" in ids, f"{case}: the CEO is one of the people"
    for p in config["people"]:
        assert p["id"] in staff, f"{case}: {p['id']} {p['name']} is not on staff at T"
        assert (staff[p["id"]]["name"], staff[p["id"]]["title"]) == (p["name"], p["title"])
    refs = [config["owner"]]
    for group in ("items", "shocks"):
        for meta in config[group].values():
            refs += [meta.get("owner"), meta.get("person")]
    missing = sorted({r for r in refs if r and r not in staff})
    assert not missing, f"{case}: owners or people not on staff at T: {missing}"
    assert set(r for r in refs if r) <= set(ids), f"{case}: every owner and person is in the people list"
