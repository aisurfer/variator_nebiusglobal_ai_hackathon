"""Case B release fixes (QA WP25): payroll tax (M9), inputs calibrated to T (m7), one churn limit (m8)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/mvp"))
from case_calibration import CALIBRATION, check  # noqa: E402
from make_company_facts import ACTORS, CATALOGUE, METRICS_AT_T, PAYROLL_AT_T  # noqa: E402

CONFIG = json.loads((ROOT / "workspace/config/cases/company_best_year.json").read_text(encoding="utf-8"))
FACTS = json.loads((ROOT / "workspace/config/company.facts.json").read_text(encoding="utf-8"))


def test_payroll_tax_hypothesis_matches_the_actor_engine():
    h = next(x for x in CONFIG["hypotheses"] if x["id"] == "payroll_tax_july")  # the actor engine's fork id
    assert h["label"] == "Payroll tax +5 pp from July" and "July" in h["description"]
    assert f"hypothesis.{h['id']}" in CONFIG["help"]
    values = {v["name"]: v["dist"].get("value") for v in CONFIG["variables"]}
    assert values["payroll"] == FACTS["payrollAtT"] and values["payroll_tax_add"] == 0
    assert 8 < values["payroll_tax_month"] < 8.5  # 1 July 2027, in months after the fork date


def test_inputs_are_calibrated_to_the_state_at_t():
    from case_company import CALIBRATED
    from variator_it_services.company import company_model

    plan = check(company_model(), CALIBRATED)
    for _name, _kind, metric, output in CALIBRATION:
        if output:
            assert plan[metric] == pytest.approx(FACTS["metricsAtT"][metric], rel=1e-9), metric
    variables = {v["name"]: v for v in CONFIG["variables"]}
    assert variables["arr0"]["dist"]["value"] == FACTS["metricsAtT"]["rev.arr"]
    assert variables["cash0"]["dist"]["value"] == FACTS["metricsAtT"]["fin.cash"]
    assert variables["base_churn"]["source"].startswith("history at T: rev.churn")
    assert abs(plan["ops.sla"] - FACTS["metricsAtT"]["ops.sla"]) < 0.0005  # an output: close, not exact


@pytest.mark.skipif(not CATALOGUE.exists(), reason=f"needs the metric catalogue at T: {CATALOGUE}")
def test_metrics_at_t_equal_the_catalogue():
    for metric, value in METRICS_AT_T.items():
        r = json.loads((CATALOGUE / f"{metric}.json").read_text(encoding="utf-8"))["result"]
        expected = r["value"] / 100 if r.get("unit") == "percent" else r["value"]
        assert value == pytest.approx(expected, rel=1e-9), metric


@pytest.mark.skipif(not ACTORS.exists(), reason=f"needs the roster at T: {ACTORS}")
def test_payroll_at_t_equals_the_roster():
    actors = json.loads(ACTORS.read_text(encoding="utf-8"))["actors"]
    staff = [a for a in actors if a["kind"] == "person" and a["status_at_t"] != "left"]
    assert PAYROLL_AT_T == sum(a["salary_at_t"] for a in staff)


def test_one_churn_limit_for_the_goal_the_flag_and_the_calendar():
    g = next(v["dist"]["value"] for v in CONFIG["variables"] if v["name"] == "g_churn")
    assert g == 0.2 and CONFIG["goals"]["churn"]["label"] == "Goal 20%"
    rule = next(r for r in CONFIG["rules"] if r["id"] == "churn_high")
    assert rule["branches"][0]["if"] == "churn > g_churn"
    assert "Churn above 20%" in json.dumps(CONFIG["help"])
