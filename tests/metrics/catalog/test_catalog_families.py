"""Hand-computed values of every metric family on the synthetic history of ``conftest.py``."""

from __future__ import annotations

from datetime import date

import pytest

from variator.metrics.catalog.datasets import load_history
from variator.metrics.catalog.engine import evaluate, run
from variator.metrics.catalog.params import merge_params
from variator.metrics.catalog.registry import BY_ID, default_params

AS_OF = date(2026, 4, 1)


def _value(path, metric_id, as_of=AS_OF, override=None):
    params = merge_params(default_params(), {"metrics": {metric_id: override}} if override else None)
    return run(BY_ID[metric_id], load_history(path), params, as_of)


# (metric, expected value as of 2026-04-01, parameter override); the arithmetic is in conftest.py
CASES = [
    ("fin.cash", 400.0, None),  # balance after BNK-00002
    ("fin.burn", 202.92, None),  # (900 - 300) x 30.4375 / 90
    ("fin.runway", 2.0, None),  # 400 / 202.9167 = 1.97
    ("fin.revenue", 500.0, None),  # INV-1 300 + INV-2 600 / 3 for 2026-02
    ("fin.costs", 185.0, None),  # payroll 100 + employer tax 15 + bill 50 + cloud 20
    ("fin.gross_margin", 72.2, None),  # (500 - 20 - 60 x 1.15 - subcontractor BILL-1 50) / 500
    ("fin.dso", 60.0, None),  # 600 / (900 / 90)
    ("fin.taxes", 30.0, None),  # PAYE paid on 2026-03-22
    ("fin.budget_variance", -7.5, {"options": {"annual_budget": 2400}}),  # (185 - 200) / 200
    ("rev.arr", 1800.0, None),  # CTR-1 1500 (upsell from March) + CTR-3 300
    ("rev.new_arr", 600.0, None),  # CTR-3 300 + upsell 300
    ("rev.churn", 33.33, {"options": {"annualise": False}}),  # 600 / 1800
    ("rev.churn", 133.7, None),  # x 365 / 91 days of history
    ("ppl.headcount", 2.0, None),  # E1, E2 (E3 left on 2026-02-28)
    ("ops.incidents", 2.0, None),  # INC-1, INC-2 in 30 days
    ("ops.mttr", 1.5, None),  # mean(1, 3, 0.5) h in 90 days
    ("ops.mttr", 1.0, {"options": {"stat": "median"}}),
    ("ops.deploys", 1.0, None),  # 4 deploys in 28 days
    ("ops.open_requisitions", 1.0, None),  # REQ-2 (REQ-1 filled)
    # IMPL-1 (no "Onboarding:" title: an upsell) open 62 business days > 28, + CTR-3 went live 2 days late
    ("ops.overdue_implementations", 2.0, None),
    ("ops.overdue_implementations", 1.0, {"options": {"upsell_promise_bdays": 70}}),
    ("ops.overdue_implementations", 1.0, {"window_days": 7}),  # the late go-live (03-20) is out of 7 days
    ("ops.throughput", 1.0, None),  # PLAT-1 done on 2026-03-30
    ("ops.team_load", 110.0, None),  # Sprint 41 (ended 2026-03-30); Sprint 42 still runs
]


@pytest.mark.parametrize(("metric_id", "expected", "override"), CASES)
def test_hand_computed_values(history, metric_id, expected, override):
    c = _value(history(), metric_id, override=override)
    assert c.value == pytest.approx(expected, abs=0.01), c.caveats


def test_values_at_earlier_dates(history):
    path = history()
    assert _value(path, "rev.arr", date(2026, 2, 20)).value == 1800.0  # 1200 + 600; CTR-3 not started
    assert _value(path, "fin.cash", date(2026, 2, 15)).value == 1300.0
    assert _value(path, "ppl.headcount", date(2026, 3, 1)).value == 1.0  # E3 left, E2 not started
    assert _value(path, "ops.overdue_implementations", date(2026, 1, 30)).value == 0.0  # 19 business days
    assert _value(path, "ops.team_load", date(2026, 3, 20)).value == 90.0  # Sprint 40
    assert _value(path, "ops.throughput", date(2026, 3, 12)).value == 1.0  # PLAT-2


def test_v2_metrics_from_synthetic_files(history):
    path = history(nps=True, monitoring=True)
    assert _value(path, "cust.nps").value == 0.0  # (2 promoters - 2 detractors) / 5
    assert _value(path, "cust.nps", override={"options": {"detractor_max": 5}}).value == 20.0  # (2 - 1) / 5
    # traffic weights api 0.30, web-app 0.22: (0.3 x 99.9 + 0.22 x 99.8) / 0.52;
    # p95: (0.3 x 200 + 0.22 x 300) / 0.52
    assert _value(path, "ops.sla").value == pytest.approx(99.858)
    assert _value(path, "ops.latency").value == pytest.approx(242.3)
    # latency penalty with an objective of 200 ms: 100 x 0.002 x (242.31 - 200) / 50 = 0.169 points
    assert _value(path, "ops.sla", override={"options": {"slo_ms": 200}}).value == pytest.approx(99.688)
    equal = {"options": {"service_weights": {}}}  # no weights: every service weighs the same
    assert _value(path, "ops.sla", override=equal).value == pytest.approx(99.85)
    assert _value(path, "ops.latency", override=equal).value == pytest.approx(250.0)
    assert _value(path, "cust.nps", override={"options": {"mode": "window"}}).value == 0.0


def test_breakdowns_and_items(history):
    data = load_history(history())
    params = default_params()
    churn = run(BY_ID["rev.churn"], data, params, AS_OF)
    assert [b.label for b in churn.breakdown] == ["budget cuts"]
    item = churn.items[0]
    assert item.kb_id == "sim.CTR-2" and item.related_id == "sim.LE-1" and item.facts["lost ARR"] == 600.0
    # the cases count notices given, the churn tile contracts that ended: its note gives both (QA m2)
    assert churn.display == {
        "note": "Counts contracts that ended in the last 12 months. Notices given in the same period:"
        " 133.7% of ARR (1 notice, $600); those contracts end later."
    }
    assert churn.display["note"] in churn.caveats
    costs = run(BY_ID["fin.costs"], data, params, AS_OF)
    assert {b.label: b.value for b in costs.breakdown} == {
        "Payroll (gross)": 100.0,
        "Vistula": 50.0,
        "Nimbus": 20.0,
        "Employer payroll tax": 15.0,
    }
    revenue = run(BY_ID["fin.revenue"], data, params, AS_OF)
    assert {b.key: b.value for b in revenue.breakdown} == {"CL-001": 300.0, "CL-002": 200.0}
    assert {i.kb_id for i in revenue.items} == {"sim.INV-1", "sim.INV-2"}
    load = run(BY_ID["ops.team_load"], data, params, AS_OF)
    assert [(i.kb_id, i.facts["points done"]) for i in load.items] == [("person.e1", 3)]
    taxes = run(BY_ID["fin.taxes"], data, params, AS_OF)
    assert any("35" in c for c in taxes.caveats)  # PAYE March is due in the next 30 days


def test_money_sums_are_decimal_safe(history):
    from decimal import Decimal

    from variator.metrics.catalog.core import D

    assert sum((D(0.1) for _ in range(10)), Decimal(0)) == Decimal("1.0")
    assert _value(history(), "fin.revenue").value == 500.0


def test_series_has_weekly_points_up_to_the_date(history):
    path = history()
    ev = evaluate(BY_ID["fin.cash"], load_history(path), default_params(), AS_OF)
    dates = [p.date for p in ev.kb.series]
    assert len(dates) == 52 and dates[-1] == "2026-04-01" and dates[-2] == "2026-03-25"
    assert ev.kb.previous == ev.kb.series[-2].value
    before = [p for p in ev.kb.series if p.date < "2026-01-01"]
    assert before and all(p.value is None for p in before)  # before the history: unknown, not zero
