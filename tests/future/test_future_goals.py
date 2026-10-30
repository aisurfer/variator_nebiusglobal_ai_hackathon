"""Goals, milestones and checks of the actor run (QA M1/M2): config ids, monotone P, calendar values."""

from __future__ import annotations

import copy
from datetime import date

import pytest

from variator.future import delivery, export
from variator.future.forks import case_config, company_forks, compile_knobs

N = 150


def _doc(company, cfg):
    res = company.run(compile_knobs(company_forks(cfg)), N, 5)
    cube = export.build_cube(res, "t", company.world.anchor, 5)
    return export.month_json(
        res, case_id="company_best_year", cfg=cfg, params=company.params, decision=export.decide(cube, cfg),
        provenance={"seed": 5}, anchor=company.world.anchor, labels={},
    )  # fmt: skip


def _monotone(xs, up=True):
    return all((b >= a - 1e-12) if up else (b <= a + 1e-12) for a, b in zip(xs, xs[1:], strict=False))


@pytest.fixture(scope="module")
def doc_b(company):
    return _doc(company, company.cfg)


def test_case_b_goal_ids_and_thresholds_come_from_the_config(company, doc_b):
    items = {i["name"]: i for i in company.cfg["timeline"]["items"] if i["kind"] == "goal"}
    var = export.config_values(company.cfg)
    for opt in doc_b["options"].values():
        goals = {g["id"]: g for g in opt["goals"]}
        assert set(goals) == set(items)
        assert (
            goals["cash_goal"]["threshold"] == var["g_cash"]
            and goals["churn_goal"]["threshold"] == var["g_churn"]
        )
        assert goals["churn_goal"]["pMet"] == goals["churn_goal"]["pByMonth"][11]
        assert _monotone(goals["churn_goal"]["pByMonth"], up=False)  # churn to date only grows


def test_goals_follow_an_edit_of_the_goal_variable(company, doc_b):
    cfg = copy.deepcopy(company.cfg)
    for v in cfg["variables"]:
        if v["name"] == "g_cash":
            v["dist"]["value"] = 2_600_000.0
    edited = _doc(company, cfg)
    a = next(g for g in doc_b["options"]["status_quo"]["goals"] if g["id"] == "cash_goal")
    b = next(g for g in edited["options"]["status_quo"]["goals"] if g["id"] == "cash_goal")
    assert b["threshold"] == 2_600_000.0 and b["pMet"] < a["pMet"]


def test_case_b_milestones_belong_to_their_forks_and_are_monotone(company, doc_b):
    owners = {i["name"]: i["options"] for i in company.cfg["timeline"]["items"] if i["kind"] == "milestone"}
    seen = set()
    for f, opt in doc_b["options"].items():
        for m in opt["milestones"]:
            assert f in owners[m["id"]] and _monotone(m["pByMonth"])
            seen.add(m["id"])
    assert seen == set(owners)


def test_case_b_checks_match_the_calendar_and_the_rule_ids(company, doc_b):
    rules = {r["id"] for r in company.cfg["rules"]}
    for opt in doc_b["options"].values():
        cal = {r["id"]: r for r in opt["riskCalendar"]}
        ids = {c["id"] for c in opt["checks"]}
        assert {"sla_breach", "churn_high", "over_budget", "runway_low", "burnout"} <= ids
        for c in opt["checks"]:
            assert c["pEver"] == cal[c["id"]]["pEver"] and c["pByMonth"] == cal[c["id"]]["pByMonth"]
            assert _monotone(c["pByMonth"])
            if c["rule"]:
                assert c["rule"] in rules


def test_case_a_goals_milestones_checks(company):
    cfg = case_config(delivery.CASE)
    params = delivery.delivery_params(company.habits, cfg)
    res = delivery.simulate(params, 200, 3, as_of=date(2026, 10, 27), crew_=delivery.crew(company.habits))
    doc = delivery.month_doc(res, params, cfg, anchor=date(2026, 10, 26), seed=3, habits=company.habits)
    rules = {r["id"] for r in cfg["rules"]}
    for f, opt in doc["options"].items():
        goals = {g["id"]: g for g in opt["goals"]}
        assert {"money_goal", "beta", "load_test_env", "go_live"} <= set(goals)
        for g in ("beta", "load_test_env", "go_live"):
            assert goals[g]["cumulative"] and _monotone(goals[g]["pByMonth"])
        ms = {m["id"] for m in opt["milestones"]}
        assert ms == {"beta", "load_test_env", "go_live"}
        checks = {c["id"]: c for c in opt["checks"]}
        assert {"burn_over_budget", "late_go_live", "high_rework"} <= set(checks) and set(checks) - {
            "go_live_late"
        } <= rules | {"burn_over_budget"}
        assert ("change_requests" in checks) == (f == "offshore_fixed")
        live = {r["id"]: r for r in opt["riskCalendar"]}["go_live_by"]
        assert goals["go_live"]["pByMonth"] == live["pByMonth"]


def test_churn_breakdown_parts_add_up(company, doc_b):
    known = [i for i in range(company.world.clients.n) if company.world.clients.end_month[i] > 0]
    for opt in doc_b["options"].values():
        b = opt["churnBreakdown"]
        ids = {c["id"] for c in b["alreadyNoticed"]["clients"]}
        assert ids == {company.world.clients.ids[i] for i in known}
        assert b["alreadyNoticed"]["arrShare"] > 0
        assert b["newNotices"]["p10"] <= b["newNotices"]["p50"] <= b["newNotices"]["p90"]
        assert (
            b["terminations"]["p50"] >= b["alreadyNoticed"]["arrShare"]
        )  # the noticed contracts end in year 1
        assert b["total"]["p50"] == pytest.approx(
            b["alreadyNoticed"]["arrShare"] + b["criterion"]["p50"], abs=0.02
        )
        churn = next(m for m in doc_b["metrics"] if m["id"] == "churn")
        assert churn
