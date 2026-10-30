"""The live month API: POST /api/variator/cases/{case_id}/month."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from variator.future import export, live
from variator.future.forks import company_forks, compile_knobs
from variator.web.app import create_app
from variator.web.settings import WebSettings

URL = "/api/variator/cases/company_best_year/month"
N = 120


@pytest.fixture(scope="module")
def client(company, tmp_path_factory):
    return TestClient(
        create_app(WebSettings(artifacts_dir=tmp_path_factory.mktemp("artifacts"), repo_root=None))
    )


def test_default_state_equals_the_engine(client, company):
    r = client.post(URL, json={"n": N, "seed": 7})
    assert r.status_code == 200
    doc = r.json()
    assert doc["source"] == "actors" and doc["provenance"]["live"] is True and doc["n"] == N
    res = company.run(compile_knobs(company_forks(company.cfg)), N, 7)
    dec = export.decide(export.build_cube(res, "t", company.world.anchor, 7), company.cfg)
    got = [(o["option"], o["pBest"]) for o in doc["ranking"]["options"]]
    assert got == [(o["option"], o["pBest"]) for o in dec["options"]]


def test_same_state_same_answer_and_cache(client):
    body = {"n": N, "seed": 3, "activePresets": ["gpu_crunch"], "prefs": {"weights": {"cash": 2.0}}}
    a = client.post(URL, json=body).json()
    b = client.post(URL, json=body).json()
    assert b["provenance"]["cached"] is True
    assert a["ranking"] == b["ranking"] and a["options"] == b["options"]


def test_edits_move_the_result(client):
    base = client.post(URL, json={"n": N, "seed": 5}).json()
    fewer = client.post(URL, json={"n": N, "seed": 5, "edits": {"v|hire_count|value": 2}}).json()
    cash = lambda d: d["options"]["hire_5"]["fans"]["cash"][-1]["p50"]  # noqa: E731
    assert cash(fewer) > cash(base)
    kp = client.post(URL, json={"n": N, "seed": 5, "timeline": {"events": ["kp_event"]}}).json()
    cap = lambda d: d["options"]["status_quo"]["fans"]["capacity"][8]["p50"]  # noqa: E731
    assert cap(kp) < cap(base)
    ignored = client.post(URL, json={"n": N, "seed": 5, "edits": {"v|opex|mode": 9e6}}).json()
    assert ignored["provenance"]["ignoredEdits"] == ["opex"]


def test_forks_on_and_off(client):
    doc = client.post(URL, json={"n": N, "forks": ["status_quo", "hire_5"]}).json()
    assert set(doc["options"]) == {"status_quo", "hire_5"}


def test_the_baseline_fork_stays_on(client):
    """The ranking anchors on the baseline fork: a state without it gets it back (as on the page)."""
    doc = client.post(URL, json={"n": N, "forks": ["pay_tech_debt", "raise_prices_8"]}).json()
    assert set(doc["options"]) == {"status_quo", "pay_tech_debt", "raise_prices_8"}
    assert live.normalize("company_best_year", {"forks": ["hire_5"]}, live.case_config("company_best_year"))[
        "forks"
    ] == ["status_quo", "hire_5"]


@pytest.mark.parametrize(
    "body",
    [
        {"n": "many"},
        {"n": 10},
        {"activePresets": ["no_such_preset"]},
        {"edits": {"v|no_such_variable|value": 1}},
        {"edits": {"v|hire_count|value": "five"}},
        {"forks": ["no_such_fork"]},
        {"prefs": {"riskAversion": 2}},
    ],
)
def test_bad_state_is_400(client, body):
    r = client.post(URL, json=body)
    assert r.status_code == 400 and "message" in r.json()["error"]


def test_unknown_case_is_404(client):
    assert client.post("/api/variator/cases/nope/month", json={}).status_code == 404


def test_n_is_capped():
    st = live.normalize("company_best_year", {"n": 5000}, live.case_config("company_best_year"))
    assert st["n"] == live.MAX_N and st["nCapped"]


def test_case_a_runs(client):
    r = client.post(
        "/api/variator/cases/delivery_best_way/month", json={"n": N, "activePresets": ["tight_budget"]}
    )
    assert r.status_code == 200
    doc = r.json()
    assert set(doc["options"]) == {"in_house", "nearshore_tm", "offshore_fixed"}
    rows = {r["id"]: r for r in doc["options"]["in_house"]["riskCalendar"]}
    assert rows["go_live_by"]["kind"] == "progress" and rows["go_live_by"]["rowType"] == "progress"
    assert all(r["rowType"] == "risk" for k, r in rows.items() if k != "go_live_by")
    assert "other_work_slows" in doc["costLines"]["in_house"]


def test_get_describes_the_route(client):
    r = client.get(URL)
    assert r.status_code == 200
    d = r.json()
    assert d["case"] == "company_best_year" and d["method"] == "POST" and d["limits"]["maxN"] == live.MAX_N
    assert client.get("/api/variator/cases/nope/month").status_code == 404


def test_pairwise_is_exact_and_respects_ties(client):
    doc = client.post(URL, json={"n": N, "seed": 9}).json()
    rk = doc["ranking"]
    lead = rk["options"][0]["option"]
    pw = rk["pairwise"]
    rivals = {o["option"] for o in rk["options"][1:]}
    assert {p["b"] for p in pw} == rivals and all(p["a"] == lead for p in pw)
    crits = {c for c in rk["weights"]} | {"total"}
    for b in rivals:
        rows = {p["crit"]: p for p in pw if p["b"] == b}
        assert set(rows) == crits
        for p in rows.values():
            assert 0 <= p["bWins"] + p["aWins"] <= 1 + 1e-9
        scores = {o["option"]: o["score"] for o in rk["options"]}
        assert rows["total"]["scoreDiff"] == pytest.approx(scores[b] - scores[lead], abs=2e-4)
    # within the meaningful difference neither side wins: runway is capped at 60 months in most scenarios
    rw = [p for p in pw if p["crit"] == "runway"]
    assert all(p["bWins"] < 0.5 and p["aWins"] < 0.5 for p in rw)


def test_live_week_on_request(client):
    doc = client.post(
        URL, json={"n": N, "seed": 4, "include": ["week"], "activePresets": ["gpu_crunch"], "weekN": 100}
    ).json()
    best = [o["option"] for o in doc["ranking"]["options"][:2]]
    assert sorted(doc["eventTrees"]) == sorted(best)
    t = doc["eventTrees"][best[0]]
    assert t["version"] == 2 and t["n"] == 100 and t["weeks"] == 52 and t["nodes"][0]["split"] is None
    assert doc["timings"]["week_tree_s"] > 0
    chosen = client.post(
        URL,
        json={
            "n": N,
            "seed": 4,
            "include": ["week"],
            "weekForks": ["hire_5", "raise_prices_8"],
            "weekN": 100,
        },
    ).json()
    assert sorted(chosen["eventTrees"]) == ["hire_5", "raise_prices_8"]
    assert client.post(URL, json={"include": ["day"]}).status_code == 400
    assert client.post(URL, json={"include": ["week"], "weekForks": ["a", "b", "c", "d"]}).status_code == 400


def test_path_drill_down(client):
    body = {"n": N, "seed": 4, "weekN": 100, "fork": "status_quo", "path": "s7"}
    r = client.post("/api/variator/cases/company_best_year/week/path", json=body)
    assert r.status_code == 200
    p = r.json()["path"]
    assert p["id"] == "s7" and p["nodes"][0] == "n0" and len(p["weeks"]) == 53
    bad = client.post("/api/variator/cases/company_best_year/week/path", json={**body, "path": "s999"})
    assert bad.status_code == 400
    a = client.get("/api/variator/cases/delivery_best_way/week/path?fork=in_house&path=s3")
    assert a.status_code == 200 and a.json()["unit"] == "month" and len(a.json()["path"]["weeks"]) == 25


@pytest.mark.parametrize("url", [URL, "/api/variator/cases/delivery_best_way/month"])
def test_a_weight_only_change_is_reranked_and_equals_a_full_run(client, url):
    base = {"n": N, "seed": 21}
    first = client.post(url, json=base).json()
    assert first["provenance"]["reranked"] is False
    crit = next(iter(first["ranking"]["weights"]))
    body = {**base, "prefs": {"weights": {crit: 5.0}, "riskAversion": 0.6}}
    fast = client.post(url, json=body).json()
    assert fast["provenance"]["reranked"] is True and fast["timings"]["rerank_s"] < 2.0
    assert fast["ranking"]["weights"] != first["ranking"]["weights"]
    with live._lock:  # a cold run of the same state
        live._cache.clear()
        live._sims.clear()
    full = client.post(url, json=body).json()
    assert full["provenance"]["reranked"] is False
    assert fast["ranking"] == full["ranking"] and fast["options"] == full["options"]


def test_md_edits_rerank_and_simulation_edits_do_not(client):
    base = {"n": N, "seed": 22}
    client.post(URL, json=base)
    md = client.post(URL, json={**base, "edits": {"v|md_cash|value": 1e6}}).json()
    assert md["provenance"]["reranked"] is True and md["provenance"]["ignoredEdits"] == []
    sim = client.post(URL, json={**base, "edits": {"v|hire_count|value": 3}}).json()
    assert sim["provenance"]["reranked"] is False


def test_prewarm_makes_the_first_preset_click_a_rerank(client):
    with live._lock:
        live._cache.clear()
        live._sims.clear()
    took = live.prewarm(*__import__("variator.web.month", fromlist=["_paths"])._paths())
    assert set(took) == set(live.CASES)
    cash_first = client.post(URL, json={"prefs": {"weights": {"cash": 3.0}}}).json()
    assert cash_first["provenance"]["reranked"] is True
