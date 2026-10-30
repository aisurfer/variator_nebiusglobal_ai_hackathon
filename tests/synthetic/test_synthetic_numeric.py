import subprocess
import sys
from collections import defaultdict

import numpy as np
import pytest

from variator.domain import DistributionKind
from variator.domain.examples import warehouse_decision
from variator.synthetic import DemoCase, generate_demo_case, list_demo_cases, numeric
from variator.synthetic.company import CATEGORIES, REGIONS, month_index, month_label, simulate_company

CASES = ["warehouse", "price_increase", "new_market"]


@pytest.fixture(scope="module")
def cases() -> dict[str, DemoCase]:
    return {name: generate_demo_case(name) for name in CASES}


def _by_month(case: DemoCase, column: str, **filters: str) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in case.table("monthly_orders").rows:
        if all(row[key] == value for key, value in filters.items()):
            totals[str(row["month"])] += float(row[column])
    return dict(totals)


def _bounds(name: str) -> tuple[float, float]:
    variable = next(v for v in warehouse_decision().variables if v.name == name)
    d = variable.distribution
    if d.kind == DistributionKind.NORMAL:
        return d.mean - 2 * d.std, d.mean + 2 * d.std
    if d.kind == DistributionKind.FIXED:
        return d.value * 0.95, d.value * 1.05
    return d.low, d.high


def test_list_demo_cases():
    assert list_demo_cases() == CASES


def test_unknown_case_is_rejected():
    with pytest.raises(ValueError, match="unknown demo case"):
        generate_demo_case("moon_base")


@pytest.mark.parametrize("name", CASES)
def test_same_seed_gives_identical_case(name):
    first = generate_demo_case(name, seed=7).model_dump()
    numeric._history.cache_clear()  # force a fresh simulation
    second = generate_demo_case(name, seed=7).model_dump()
    assert first == second


def test_simulation_is_deterministic_and_seed_sensitive():
    a, b, c = simulate_company(3), simulate_company(3), simulate_company(4)
    assert np.array_equal(a.orders, b.orders)
    assert np.array_equal(a.marketing_spend, b.marketing_spend)
    assert not np.array_equal(a.orders, c.orders)


def test_cases_describe_the_same_company(cases):
    tables = [cases[name].table("monthly_orders").rows for name in CASES]
    assert tables[0] == tables[1] == tables[2]
    assert len({case.company for case in cases.values()}) == 1


def test_period_and_grain(cases):
    case = cases["warehouse"]
    assert (case.period_start, case.period_end) == ("2023-11", "2026-10")
    assert month_label(0) == "2023-11" and month_index("2026-10") == 35
    rows = case.table("monthly_orders").rows
    assert len(rows) == 36 * len(REGIONS) * len(CATEGORIES)
    assert len({(r["month"], r["region"], r["category"]) for r in rows}) == len(rows)
    assert [r["month"] for r in case.table("monthly_pnl").rows] == [month_label(t) for t in range(36)]


def test_holiday_and_garden_seasonality(cases):
    case = cases["warehouse"]
    for year in (2024, 2025):
        for category in ("home", "kitchen"):
            orders = _by_month(case, "orders", category=category)
            holiday = orders[f"{year}-11"] + orders[f"{year}-12"]
            winter = orders[f"{year + 1}-01"] + orders[f"{year + 1}-02"]
            assert holiday > 1.2 * winter
        garden = _by_month(case, "orders", category="garden")
        spring = sum(garden[f"{year + 1}-{m:02d}"] for m in (4, 5, 6))
        autumn = sum(garden[f"{year}-{m:02d}"] for m in (10, 11, 12))
        assert spring > 1.5 * autumn


def test_demand_grows_and_marketing_tracks_orders(cases):
    pnl = cases["warehouse"].table("monthly_pnl")
    orders = np.array(pnl.column("orders"), dtype=float)
    marketing = np.array(pnl.column("marketing_spend"), dtype=float)
    assert orders[24:].sum() > 1.05 * orders[12:24].sum()
    assert np.corrcoef(orders, marketing)[0, 1] > 0.6


def test_accounting_identities(cases):
    case = cases["warehouse"]
    for row in case.table("monthly_orders").rows:
        assert row["revenue"] == pytest.approx(row["orders"] * row["avg_order_value"], abs=0.02)
        assert row["gross_margin"] == pytest.approx(row["revenue"] - row["cogs"], abs=0.02)
    revenue = _by_month(case, "revenue")
    for row in case.table("monthly_pnl").rows:
        assert row["revenue"] == pytest.approx(revenue[row["month"]], abs=0.5)
        assert row["margin_per_order"] == pytest.approx(row["gross_margin"] / row["orders"], abs=0.01)


def test_customer_flows(cases):
    rows = cases["warehouse"].table("monthly_pnl").rows
    for prev, row in zip(rows, rows[1:], strict=False):
        expected = prev["active_customers"] - row["churned_customers"] + row["new_customers"]
        assert row["active_customers"] == expected
    for row in rows:
        assert 0.02 < row["churn_rate"] < 0.06
        assert row["new_customers"] > 0


def test_warehouse_case_matches_domain_example(cases):
    case = cases["warehouse"]
    facts = case.key_facts
    decision = warehouse_decision()
    assert case.question == decision.question
    assert case.options == [option.name for option in decision.options]

    for fact, variable in [
        ("base_orders", "base_orders"),
        ("demand_growth_yoy", "demand_growth"),
        ("margin_per_order", "margin_per_order"),
        ("own_cost_per_order", "own_cost_per_order"),
    ]:
        low, high = _bounds(variable)
        assert low <= facts[fact] <= high, fact
    assert facts["margin_per_order"] == pytest.approx(9.0, abs=0.6)
    assert facts["own_cost_per_order"] == pytest.approx(3.0, abs=0.3)

    low, high = _bounds("tpl_cost_per_order")
    fees = case.table("tpl_quotes").column("fee_per_order")
    assert all(low <= fee <= high for fee in fees)
    assert facts["tpl_fee_median"] == pytest.approx(3.9, abs=0.25)

    sites = {row["option"]: row for row in case.table("site_options").rows}
    for option, variable in [("second_warehouse", "capex"), ("expand_current", "expansion_capex")]:
        d = next(v for v in decision.variables if v.name == variable).distribution
        row = sites[option]
        assert (row["capex_low"], row["capex_likely"], row["capex_high"]) == (d.low, d.mode, d.high)
    expand = sites["expand_current"]
    assert (expand["added_capacity_low"], expand["added_capacity_high"]) == _bounds("capacity_orders")


def test_warehouse_is_approaching_capacity(cases):
    rows = cases["warehouse"].table("warehouse_capacity").rows
    utilization = np.array([row["utilization"] for row in rows])
    overflow = np.array([row["overflow_orders_3pl"] for row in rows])
    assert 0.85 <= utilization[24:].mean() <= 0.99
    assert utilization[24:].mean() > utilization[:12].mean()
    assert utilization.max() >= 0.99
    assert overflow[24:].sum() > 0
    for row in rows:
        assert row["orders_received"] == row["orders_shipped_inhouse"] + row["overflow_orders_3pl"]


@pytest.mark.parametrize("seed", [42, 0, 1, 2, 3])
def test_implied_elasticity_is_negative(seed):
    case = generate_demo_case("price_increase", seed=seed)
    changes = case.table("price_changes").rows
    assert len(changes) >= 5
    for row in changes:
        assert row["implied_elasticity"] < 0, row
        assert row["actual_orders_3m_after"] < row["baseline_orders_3m_after"]
    assert -2.0 < case.key_facts["implied_elasticity_weighted"] < -0.5
    assert case.key_facts["proposed_price_increase"] == 0.08
    assert case.key_facts["churn_rate_after_broad_change"] > case.key_facts["churn_rate_before_broad_change"]


def test_price_index_steps_match_price_changes(cases):
    case = cases["price_increase"]
    index = {(r["month"], r["category"]): r["our_price_index"] for r in case.table("competitor_prices").rows}
    for change in case.table("price_changes").rows:
        month = str(change["effective_month"])
        previous = month_label(month_index(month) - 1)
        categories = CATEGORIES if change["category"] == "all" else (change["category"],)
        for category in categories:
            ratio = index[(month, category)] / index[(previous, category)]
            assert ratio == pytest.approx(1 + change["price_change_pct"], abs=1e-3)


def test_new_market_case(cases):
    case = cases["new_market"]
    facts = case.key_facts
    rampups = case.table("region_rampups").rows
    for name in {row["comparable"] for row in rampups}:
        orders = [row["orders"] for row in rampups if row["comparable"] == name]
        assert len(orders) == 24
        assert np.mean(orders[18:]) > 3 * np.mean(orders[:3])
    regions = {row["region"] for row in case.table("region_economics").rows}
    assert regions == set(REGIONS) and "central" not in regions
    smallest_region_month = min(row["orders_last_12m"] for row in case.table("region_economics").rows) / 12
    assert 1_000 < facts["steady_state_orders_per_month_likely"] < smallest_region_month
    assert facts["months_to_half_steady_state_min"] >= 3


def test_descriptions_quote_the_data(cases):
    warehouse = cases["warehouse"]
    assert f"{int(warehouse.key_facts['base_orders']):,}" in warehouse.description
    assert "fictional" in warehouse.company
    for case in cases.values():
        assert case.question.endswith("?")
        assert len(case.options) >= 2


def test_numeric_generator_needs_neither_pandas_nor_data_designer():
    code = (
        "import sys; from variator.synthetic import generate_demo_case; generate_demo_case('warehouse'); "
        "bad = [m for m in ('pandas', 'data_designer') if m in sys.modules]; "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
