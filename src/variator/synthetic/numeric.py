"""Ready-made demo cases cut from one simulated company history.

Public API: ``list_demo_cases``, ``generate_demo_case``, ``write_demo_case``
(re-exported from ``storage``). Everything here is deterministic for a given seed
and needs only NumPy; no LLM and no pandas.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

import numpy as np

from variator.synthetic.company import (
    CAPACITY_AFTER,
    CATEGORIES,
    CATEGORY_PROFILES,
    COMPANY_NAME,
    PRICE_CHANGES,
    REGION_PROFILES,
    REGIONS,
    CompanyHistory,
    affected_categories,
    derived_rng,
    month_index,
    simulate_company,
)
from variator.synthetic.models import CellValue, DemoCase, DemoTable
from variator.synthetic.storage import load_demo_case, write_demo_case

__all__ = [
    "generate_all_demo_cases",
    "generate_demo_case",
    "list_demo_cases",
    "load_demo_case",
    "write_all_demo_cases",
    "write_demo_case",
]

LAST12 = slice(24, 36)
PREV12 = slice(12, 24)
WINDOW = 3  # months used to measure price-change responses

# Capex and capacity quotes; kept equal to the ranges in
# variator.domain.examples.warehouse_decision (tests enforce this).
SITE_OPTIONS: tuple[dict[str, CellValue], ...] = (
    {
        "option": "second_warehouse",
        "description": "Lease and fit out a second DC near the south region",
        "capex_low": 1_800_000,
        "capex_likely": 2_200_000,
        "capex_high": 3_200_000,
        "added_capacity_low": 300_000,
        "added_capacity_high": 360_000,
        "lead_time_months": 12,
        "notes": "Shorter last mile to the south cuts in-house cost per order by ~8%",
    },
    {
        "option": "expand_current",
        "description": "Add a mezzanine and a second shift at the current DC",
        "capex_low": 700_000,
        "capex_likely": 900_000,
        "capex_high": 1_400_000,
        "added_capacity_low": 90_000,
        "added_capacity_high": 130_000,
        "lead_time_months": 6,
        "notes": "Growth above the added capacity is lost or sent to spot 3PL",
    },
    {
        "option": "outsource_3pl",
        "description": "Route all growth above today's volume to a contract 3PL",
        "capex_low": 30_000,
        "capex_likely": 45_000,
        "capex_high": 80_000,
        "added_capacity_low": 250_000,
        "added_capacity_high": 500_000,
        "lead_time_months": 3,
        "notes": "Capex is WMS integration only; fees per order in tpl_quotes",
    },
)

# Fictional 3PL vendors: (name, coverage, anchor fee per order).
TPL_VENDORS: tuple[tuple[str, str, float], ...] = (
    ("Quillfield Logistics", "national", 3.55),
    ("Tarnwick Fulfilment", "south, east", 3.85),
    ("Orrin Bay 3PL", "national", 3.95),
    ("Velmont Commerce Services", "south", 4.35),
)

# Comparable launches for the new-market case: (name, launch month, households k,
# steady-state orders per 1k households per month, logistic slope, months to half).
RAMPUP_COMPARABLES: tuple[tuple[str, str, float, float, float, float], ...] = (
    ("west_2021", "2021-03", REGION_PROFILES["west"].households_k, 11.8, 0.40, 7.0),
    ("south_2022", "2022-05", REGION_PROFILES["south"].households_k, 10.6, 0.36, 8.0),
    ("peer_benchmark", "2020-09", 430.0, 11.2, 0.32, 9.0),
)

CANDIDATE_REGION = "central"
CANDIDATE_HOUSEHOLDS_K = 350.0


# --- formatting helpers -----------------------------------------------------------


def _int(value: float | np.integer) -> int:
    return int(round(float(value)))


def _money(value: float | np.floating) -> float:
    return round(float(value), 2)


def _ratio(value: float | np.floating) -> float:
    return round(float(value), 4)


def _fmt_int(value: float) -> str:
    return f"{_int(value):,}"


def _fmt_pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


def _fmt_usd(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if abs(value) >= 10_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:,.2f}"


# --- shared tables ----------------------------------------------------------------


@lru_cache(maxsize=8)
def _history(seed: int) -> CompanyHistory:
    return simulate_company(seed)


def _monthly_orders_table(h: CompanyHistory) -> DemoTable:
    revenue, cogs = h.revenue, h.cogs
    rows: list[dict[str, CellValue]] = []
    for t, month in enumerate(h.months):
        for r, region in enumerate(REGIONS):
            for c, category in enumerate(CATEGORIES):
                rows.append(
                    {
                        "month": month,
                        "region": region,
                        "category": category,
                        "orders": _int(h.orders[t, r, c]),
                        "list_price_index": _ratio(h.price_index[t, c]),
                        "avg_order_value": _money(h.avg_price[t, r, c]),
                        "revenue": _money(revenue[t, r, c]),
                        "cogs": _money(cogs[t, r, c]),
                        "gross_margin": _money(revenue[t, r, c] - cogs[t, r, c]),
                    }
                )
    return DemoTable(
        name="monthly_orders",
        description=(
            "Monthly orders, prices, revenue and cost of goods by region and product category. "
            "gross_margin = revenue - cogs, i.e. contribution before fulfilment and marketing."
        ),
        columns=list(rows[0]),
        units={
            "month": "YYYY-MM",
            "orders": "orders/month",
            "list_price_index": "index, 2023-11 = 1.0",
            "avg_order_value": "USD/order",
            "revenue": "USD",
            "cogs": "USD",
            "gross_margin": "USD",
        },
        rows=rows,
    )


def _monthly_pnl_table(h: CompanyHistory) -> DemoTable:
    orders = h.total_orders
    revenue = h.revenue.sum(axis=(1, 2))
    cogs = h.cogs.sum(axis=(1, 2))
    fulfilment = h.fulfilment_cost
    rows: list[dict[str, CellValue]] = []
    for t, month in enumerate(h.months):
        margin = revenue[t] - cogs[t]
        rows.append(
            {
                "month": month,
                "orders": _int(orders[t]),
                "revenue": _money(revenue[t]),
                "cogs": _money(cogs[t]),
                "gross_margin": _money(margin),
                "margin_per_order": _money(margin / orders[t]),
                "fulfilment_cost": _money(fulfilment[t]),
                "inhouse_cost_per_order": _money(h.inhouse_cost_per_order[t]),
                "marketing_spend": _money(h.marketing_spend[t]),
                "active_customers": _int(h.active_customers[t]),
                "new_customers": _int(h.new_customers[t]),
                "churned_customers": _int(h.churned_customers[t]),
                "churn_rate": _ratio(h.churn_rate[t]),
                "warehouse_utilization": _ratio(h.utilization[t]),
            }
        )
    return DemoTable(
        name="monthly_pnl",
        description=(
            "Company-level monthly P&L and operating metrics. margin_per_order is the contribution margin "
            "before fulfilment; fulfilment_cost includes spot-3PL overflow; churn_rate is the monthly share "
            "of active customers lost."
        ),
        columns=list(rows[0]),
        units={
            "month": "YYYY-MM",
            "orders": "orders/month",
            "revenue": "USD",
            "cogs": "USD",
            "gross_margin": "USD",
            "margin_per_order": "USD/order",
            "fulfilment_cost": "USD",
            "inhouse_cost_per_order": "USD/order",
            "marketing_spend": "USD",
            "active_customers": "customers",
            "new_customers": "customers/month",
            "churned_customers": "customers/month",
            "churn_rate": "share per month",
            "warehouse_utilization": "share of monthly capacity",
        },
        rows=rows,
    )


def _company_metrics(h: CompanyHistory) -> dict[str, float]:
    orders = h.total_orders
    revenue = h.revenue.sum(axis=(1, 2))
    cogs = h.cogs.sum(axis=(1, 2))
    inhouse = h.inhouse_orders
    last_orders = float(orders[LAST12].sum())
    return {
        "orders_last_12m": last_orders,
        "orders_yoy_growth": _ratio(last_orders / orders[PREV12].sum() - 1),
        "revenue_last_12m": _money(revenue[LAST12].sum()),
        "aov_last_12m": _money(revenue[LAST12].sum() / last_orders),
        "margin_per_order_last_12m": _money((revenue[LAST12].sum() - cogs[LAST12].sum()) / last_orders),
        "inhouse_cost_per_order_last_12m": _money(
            (h.inhouse_cost_per_order[LAST12] * inhouse[LAST12]).sum() / inhouse[LAST12].sum()
        ),
        "churn_rate_last_12m": _ratio(h.churn_rate[LAST12].mean()),
        "active_customers": float(h.active_customers[-1]),
    }


def _base_case_fields(h: CompanyHistory) -> dict[str, object]:
    return {
        "company": COMPANY_NAME,
        "seed": h.seed,
        "period_start": h.months[0],
        "period_end": h.months[-1],
    }


# --- (a) warehouse ----------------------------------------------------------------


def _warehouse_capacity_table(h: CompanyHistory) -> DemoTable:
    rows: list[dict[str, CellValue]] = []
    for t, month in enumerate(h.months):
        rows.append(
            {
                "month": month,
                "capacity_orders": _int(h.capacity[t]),
                "orders_received": _int(h.total_orders[t]),
                "orders_shipped_inhouse": _int(h.inhouse_orders[t]),
                "overflow_orders_3pl": _int(h.overflow_orders[t]),
                "utilization": _ratio(h.utilization[t]),
                "inhouse_cost_per_order": _money(h.inhouse_cost_per_order[t]),
                "overflow_fee_per_order": _money(h.spot_fee_per_order[t]),
                "overtime_hours": _int(h.overtime_hours[t]),
                "on_time_ship_rate": _ratio(h.on_time_ship_rate[t]),
            }
        )
    return DemoTable(
        name="warehouse_capacity",
        description=(
            "Current distribution centre: monthly capacity, volume, overflow sent to a spot 3PL, cost per "
            "order and service level. Capacity rose in 2024-07 after a racking upgrade."
        ),
        columns=list(rows[0]),
        units={
            "month": "YYYY-MM",
            "capacity_orders": "orders/month",
            "orders_received": "orders/month",
            "orders_shipped_inhouse": "orders/month",
            "overflow_orders_3pl": "orders/month",
            "utilization": "share of capacity",
            "inhouse_cost_per_order": "USD/order",
            "overflow_fee_per_order": "USD/order (spot rate)",
            "overtime_hours": "hours/month",
            "on_time_ship_rate": "share of orders",
        },
        rows=rows,
    )


def _tpl_quotes_table(seed: int) -> DemoTable:
    rng = derived_rng(seed, "tpl_quotes")
    rows: list[dict[str, CellValue]] = []
    for index, (vendor, coverage, anchor) in enumerate(TPL_VENDORS):
        rows.append(
            {
                "vendor": vendor,
                "quote_date": f"2026-{8 + index % 3:02d}-{int(rng.integers(3, 27)):02d}",
                "region_coverage": coverage,
                "fee_per_order": _money(np.clip(anchor + rng.normal(0, 0.05), 3.45, 4.55)),
                "setup_fee": _int(round(rng.uniform(15_000, 60_000), -3)),
                "minimum_monthly_orders": _int(round(rng.uniform(3_000, 10_000), -2)),
                "contract_months": int(rng.choice([12, 24, 36])),
                "annual_escalator_pct": _ratio(rng.uniform(0.02, 0.05)),
                "peak_surcharge_per_order": _money(rng.uniform(0.15, 0.45)),
                "on_time_sla_pct": _ratio(rng.uniform(0.975, 0.995)),
            }
        )
    return DemoTable(
        name="tpl_quotes",
        description=(
            "Quotes from contract third-party logistics (3PL) providers for taking over growth volume. "
            "fee_per_order covers pick, pack and handover to carrier; vendor names are fictional."
        ),
        columns=list(rows[0]),
        units={
            "quote_date": "YYYY-MM-DD",
            "fee_per_order": "USD/order",
            "setup_fee": "USD one-off",
            "minimum_monthly_orders": "orders/month",
            "contract_months": "months",
            "annual_escalator_pct": "share per year",
            "peak_surcharge_per_order": "USD/order in Nov-Dec",
            "on_time_sla_pct": "share of orders",
        },
        rows=rows,
    )


def _site_options_table() -> DemoTable:
    rows = [dict(option) for option in SITE_OPTIONS]
    return DemoTable(
        name="site_options",
        description="Capex and capacity quotes for the three ways to handle growth.",
        columns=list(rows[0]),
        units={
            "capex_low": "USD",
            "capex_likely": "USD",
            "capex_high": "USD",
            "added_capacity_low": "orders/year",
            "added_capacity_high": "orders/year",
            "lead_time_months": "months",
        },
        rows=rows,
    )


def _warehouse_case(h: CompanyHistory) -> DemoCase:
    from variator.domain.examples import warehouse_decision

    metrics = _company_metrics(h)
    quotes = _tpl_quotes_table(h.seed)
    fees = sorted(float(fee) for fee in quotes.column("fee_per_order"))
    util = h.utilization
    peak_t = int(np.argmax(h.overflow_orders[LAST12])) + LAST12.start
    full_months = [h.months[t] for t in range(LAST12.start, LAST12.stop) if util[t] >= 0.995]
    south = REGIONS.index("south")
    south_orders = h.orders[:, south].sum(axis=1)
    south_share = float(south_orders[LAST12].sum() / h.total_orders[LAST12].sum())
    south_growth = float(south_orders[LAST12].sum() / south_orders[PREV12].sum() - 1)
    capacity_year = CAPACITY_AFTER * 12
    expand = SITE_OPTIONS[1]
    key_facts = {
        "base_orders": metrics["orders_last_12m"],
        "demand_growth_yoy": metrics["orders_yoy_growth"],
        "margin_per_order": metrics["margin_per_order_last_12m"],
        "own_cost_per_order": metrics["inhouse_cost_per_order_last_12m"],
        "capacity_orders_per_year": float(capacity_year),
        "avg_utilization_last_12m": _ratio(util[LAST12].mean()),
        "peak_utilization": _ratio(util.max()),
        "overflow_orders_last_12m": float(h.overflow_orders[LAST12].sum()),
        "overflow_fee_per_order": _money(h.spot_fee_per_order[LAST12].mean()),
        "tpl_fee_min": fees[0],
        "tpl_fee_median": _money(np.median(fees)),
        "tpl_fee_max": fees[-1],
        "south_share_last_12m": _ratio(south_share),
        "south_orders_yoy_growth": _ratio(south_growth),
        "second_warehouse_capex_likely": float(SITE_OPTIONS[0]["capex_likely"]),
        "expansion_capex_likely": float(expand["capex_likely"]),
        "expansion_capacity_low": float(expand["added_capacity_low"]),
        "expansion_capacity_high": float(expand["added_capacity_high"]),
    }
    next_year = key_facts["base_orders"] * (1 + key_facts["demand_growth_yoy"])
    description = (
        f"{COMPANY_NAME} ships {_fmt_int(key_facts['base_orders'])} orders a year "
        f"(+{_fmt_pct(key_facts['demand_growth_yoy'])} year on year) from one distribution centre rated for "
        f"{_fmt_int(capacity_year)} orders a year. Over the last 12 months the site ran at "
        f"{_fmt_pct(key_facts['avg_utilization_last_12m'], 0)} average utilization and was full in "
        f"{len(full_months)} months ({', '.join(full_months) or 'none'}); "
        f"{_fmt_int(key_facts['overflow_orders_last_12m'])} orders overflowed to a spot 3PL at about "
        f"{_fmt_usd(key_facts['overflow_fee_per_order'])} per order, peaking in {h.months[peak_t]}. "
        f"Contribution margin before fulfilment is {_fmt_usd(key_facts['margin_per_order'])} per order and "
        f"in-house fulfilment costs {_fmt_usd(key_facts['own_cost_per_order'])}. The south region is "
        f"{_fmt_pct(south_share, 0)} of volume and growing {_fmt_pct(south_growth, 0)} a year. "
        f"At the current trend next year needs about {_fmt_int(next_year)} orders of capacity. "
        f"Contract 3PL quotes range from {_fmt_usd(fees[0])} to {_fmt_usd(fees[-1])} per order; a second "
        f"warehouse is quoted at about {_fmt_usd(key_facts['second_warehouse_capex_likely'])} and expanding "
        f"the current site at {_fmt_usd(key_facts['expansion_capex_likely'])} for "
        f"{_fmt_int(key_facts['expansion_capacity_low'])}-{_fmt_int(key_facts['expansion_capacity_high'])} "
        "extra orders a year."
    )
    return DemoCase(
        name="warehouse",
        title="Warehouse at capacity: build, expand or outsource?",
        description=description,
        question=warehouse_decision().question,
        options=[str(option["option"]) for option in SITE_OPTIONS],
        key_facts=key_facts,
        tables=[
            _monthly_orders_table(h),
            _monthly_pnl_table(h),
            _warehouse_capacity_table(h),
            quotes,
            _site_options_table(),
        ],
        **_base_case_fields(h),
    )


# --- (b) price increase -----------------------------------------------------------


def _price_changes_table(h: CompanyHistory) -> DemoTable:
    rng = derived_rng(h.seed, "price_changes")
    elasticity = np.array([CATEGORY_PROFILES[c].elasticity for c in CATEGORIES])
    rows: list[dict[str, CellValue]] = []
    for change in PRICE_CHANGES:
        t0 = month_index(change.month)
        cats = affected_categories(change)
        after = slice(t0, t0 + WINDOW)
        before = slice(t0 - WINDOW, t0)
        actual = float(h.orders[after][:, :, cats].sum())
        # Baseline: what the demand model expects without this change, given the market-wide
        # demand index observed afterwards (so the market shock cancels out).
        without_change = h.expected_orders[after][:, :, cats] / (1 + change.pct) ** elasticity[cats]
        market = np.exp(h.common_shock[after])[:, None, None]
        baseline = float((without_change * market).sum()) * (1 + rng.normal(0, 0.01))
        volume_change = actual / baseline - 1
        rows.append(
            {
                "change_id": change.change_id,
                "effective_month": change.month,
                "category": change.category,
                "price_change_pct": _ratio(change.pct),
                "orders_3m_before": _int(h.orders[before][:, :, cats].sum()),
                "baseline_orders_3m_after": _int(baseline),
                "actual_orders_3m_after": _int(actual),
                "volume_change_vs_baseline_pct": _ratio(volume_change),
                "implied_elasticity": round(float(np.log(actual / baseline) / np.log1p(change.pct)), 3),
                "churn_rate_before": _ratio(h.churn_rate[before].mean()),
                "churn_rate_after": _ratio(h.churn_rate[after].mean()),
                "competitor_response": change.competitor_response,
            }
        )
    return DemoTable(
        name="price_changes",
        description=(
            "Past list-price changes and the observed volume response over the following 3 months. "
            "baseline_orders_3m_after is the demand-model forecast without the change, re-estimated with the "
            "market-wide demand index; implied_elasticity = ln(actual / baseline) / ln(1 + price change)."
        ),
        columns=list(rows[0]),
        units={
            "effective_month": "YYYY-MM",
            "category": "category or 'all'",
            "price_change_pct": "share",
            "orders_3m_before": "orders (3 months, affected categories)",
            "baseline_orders_3m_after": "orders (3 months)",
            "actual_orders_3m_after": "orders (3 months)",
            "volume_change_vs_baseline_pct": "share",
            "implied_elasticity": "%volume / %price",
            "churn_rate_before": "share per month",
            "churn_rate_after": "share per month",
        },
        rows=rows,
    )


def _competitor_prices_table(h: CompanyHistory) -> DemoTable:
    rng = derived_rng(h.seed, "competitor_prices")
    years = np.arange(h.n_months) / 12.0
    rows: list[dict[str, CellValue]] = []
    drift = {"home": 0.045, "garden": 0.04, "kitchen": 0.05, "pet": 0.055}
    competitor = {
        category: (1 + drift[category]) ** years * np.exp(np.cumsum(rng.normal(0, 0.004, h.n_months)))
        for category in CATEGORIES
    }
    for t, month in enumerate(h.months):
        for c, category in enumerate(CATEGORIES):
            ours = float(h.price_index[t, c])
            theirs = float(competitor[category][t])
            rows.append(
                {
                    "month": month,
                    "category": category,
                    "our_price_index": _ratio(ours),
                    "competitor_price_index": _ratio(theirs),
                    "price_gap_pct": _ratio(ours / theirs - 1),
                }
            )
    return DemoTable(
        name="competitor_prices",
        description=(
            "Our list-price index vs. a basket of three online competitors (both 2023-11 = 1.0, "
            "same product basket). Negative price_gap_pct means we are cheaper."
        ),
        columns=list(rows[0]),
        units={
            "month": "YYYY-MM",
            "our_price_index": "index, 2023-11 = 1.0",
            "competitor_price_index": "index, 2023-11 = 1.0",
            "price_gap_pct": "share",
        },
        rows=rows,
    )


def _price_increase_case(h: CompanyHistory) -> DemoCase:
    metrics = _company_metrics(h)
    changes = _price_changes_table(h)
    competitors = _competitor_prices_table(h)
    elasticities = [float(e) for e in changes.column("implied_elasticity")]
    weights = [float(p) for p in changes.column("price_change_pct")]
    weighted = float(np.average(elasticities, weights=weights))
    latest = [row for row in competitors.rows if row["month"] == h.months[-1]]
    gap = float(np.mean([float(row["price_gap_pct"]) for row in latest]))
    proposed = 0.08
    broad = next(row for row in changes.rows if row["category"] == "all")
    key_facts = {
        "orders_last_12m": metrics["orders_last_12m"],
        "revenue_last_12m": metrics["revenue_last_12m"],
        "aov_last_12m": metrics["aov_last_12m"],
        "margin_per_order_last_12m": metrics["margin_per_order_last_12m"],
        "proposed_price_increase": proposed,
        "implied_elasticity_weighted": round(weighted, 3),
        "implied_elasticity_min": min(elasticities),
        "implied_elasticity_max": max(elasticities),
        "churn_rate_last_12m": metrics["churn_rate_last_12m"],
        "churn_rate_before_broad_change": float(broad["churn_rate_before"]),
        "churn_rate_after_broad_change": float(broad["churn_rate_after"]),
        "price_gap_vs_competitors": _ratio(gap),
        "cost_inflation_yoy": 0.045,
    }
    naive_volume = (1 + proposed) ** weighted - 1
    description = (
        f"{COMPANY_NAME} sold {_fmt_int(metrics['orders_last_12m'])} orders in the last 12 months at an "
        f"average order value of {_fmt_usd(metrics['aov_last_12m'])} and a contribution margin of "
        f"{_fmt_usd(metrics['margin_per_order_last_12m'])} per order before fulfilment. Supplier and "
        f"inbound freight costs rise about 4.5% a year, and our list prices are "
        f"{_fmt_pct(abs(gap))} {'below' if gap < 0 else 'above'} the competitor basket. "
        f"Management proposes raising all list prices by {_fmt_pct(proposed, 0)} from January 2027. "
        f"Six past price changes (+4% to +6%) show implied volume elasticities between "
        f"{min(elasticities):.2f} and {max(elasticities):.2f} (price-weighted mean {weighted:.2f}). "
        f"After the last across-the-board change ({broad['effective_month']}, "
        f"+{_fmt_pct(float(broad['price_change_pct']), 0)}) monthly churn went from "
        f"{_fmt_pct(float(broad['churn_rate_before']), 2)} to "
        f"{_fmt_pct(float(broad['churn_rate_after']), 2)}. At the mean elasticity, +8% would cut order "
        f"volume by about {_fmt_pct(abs(naive_volume))}, but the estimate is uncertain and competitors "
        "may or may not follow."
    )
    return DemoCase(
        name="price_increase",
        title="Raise prices 8% across the board?",
        description=description,
        question="Should we raise list prices by 8% across all categories from January 2027?",
        options=["raise_8pct_all", "raise_selectively_pet_garden", "hold_prices"],
        key_facts=key_facts,
        tables=[_monthly_orders_table(h), _monthly_pnl_table(h), changes, competitors],
        **_base_case_fields(h),
    )


# --- (c) new market ---------------------------------------------------------------


def _region_economics_table(h: CompanyHistory) -> DemoTable:
    revenue, cogs = h.revenue, h.cogs
    rows: list[dict[str, CellValue]] = []
    for r, region in enumerate(REGIONS):
        profile = REGION_PROFILES[region]
        orders = float(h.orders[LAST12, r].sum())
        rev = float(revenue[LAST12, r].sum())
        margin = rev - float(cogs[LAST12, r].sum())
        rows.append(
            {
                "region": region,
                "orders_last_12m": _int(orders),
                "revenue_last_12m": _money(rev),
                "gross_margin_last_12m": _money(margin),
                "margin_per_order": _money(margin / orders),
                "orders_yoy_growth": _ratio(orders / h.orders[PREV12, r].sum() - 1),
                "avg_delivery_days": profile.delivery_days,
                "households_k": profile.households_k,
                "orders_per_1k_households_month": round(orders / 12 / profile.households_k, 2),
            }
        )
    return DemoTable(
        name="region_economics",
        description="Last-12-month economics of the four regions we serve today.",
        columns=list(rows[0]),
        units={
            "orders_last_12m": "orders",
            "revenue_last_12m": "USD",
            "gross_margin_last_12m": "USD",
            "margin_per_order": "USD/order",
            "orders_yoy_growth": "share",
            "avg_delivery_days": "days",
            "households_k": "thousand households",
            "orders_per_1k_households_month": "orders per 1k households per month",
        },
        rows=rows,
    )


def _blended_season() -> np.ndarray:
    season = np.array([np.array(CATEGORY_PROFILES[c].seasonality) for c in CATEGORIES])
    season = season / season.mean(axis=1, keepdims=True)
    shares = np.array([CATEGORY_PROFILES[c].share for c in CATEGORIES])
    return shares @ season / shares.sum()


def _rampups_table(seed: int) -> DemoTable:
    rng = derived_rng(seed, "rampups")
    season = _blended_season()
    rows: list[dict[str, CellValue]] = []
    for name, launch, households_k, per_1k, slope, half in RAMPUP_COMPARABLES:
        steady = households_k * per_1k * np.exp(rng.normal(0, 0.03))
        slope_j = slope * np.exp(rng.normal(0, 0.05))
        half_j = half + rng.normal(0, 0.4)
        year, month = (int(part) for part in launch.split("-"))
        previous_active = 0.0
        for m in range(1, 25):
            calendar = (month - 1 + m - 1) % 12
            level = steady / (1 + np.exp(-slope_j * (m - half_j)))
            orders = level * season[calendar] * np.exp(rng.normal(0, 0.04))
            launch_push = 70_000 * np.exp(-(m - 1) / 5)
            marketing = (launch_push + 1.5 * orders) * np.exp(rng.normal(0, 0.05))
            active = level / 0.5
            new = max(active - previous_active, 0.0) + 0.04 * previous_active
            previous_active = active
            rows.append(
                {
                    "comparable": name,
                    "launch_month": launch,
                    "months_since_launch": m,
                    "households_k": households_k,
                    "orders": _int(orders),
                    "marketing_spend": _money(marketing),
                    "new_customers": _int(new),
                }
            )
    return DemoTable(
        name="region_rampups",
        description=(
            "Monthly ramp-up after past region launches: our own west (2021) and south (2022) launches from "
            "the archive, plus an anonymized peer benchmark. Use orders per 1k households to compare markets."
        ),
        columns=list(rows[0]),
        units={
            "launch_month": "YYYY-MM",
            "months_since_launch": "months",
            "households_k": "thousand households",
            "orders": "orders/month",
            "marketing_spend": "USD/month",
            "new_customers": "customers/month",
        },
        rows=rows,
    )


def _rampup_stats(table: DemoTable) -> tuple[list[float], list[int]]:
    """Steady-state orders per 1k households (months 19-24) and months to 50% of it, per comparable."""
    per_1k: list[float] = []
    months_to_half: list[int] = []
    for name, *_ in RAMPUP_COMPARABLES:
        rows = [row for row in table.rows if row["comparable"] == name]
        orders = np.array([float(row["orders"]) for row in rows])
        steady = orders[18:24].mean()
        per_1k.append(float(steady / float(rows[0]["households_k"])))
        months_to_half.append(int(np.argmax(orders >= 0.5 * steady)) + 1)
    return per_1k, months_to_half


def _candidate_market_table(seed: int) -> DemoTable:
    rng = derived_rng(seed, "candidate_market")
    facts: list[tuple[str, float, str, str]] = [
        (
            "addressable_households_k",
            CANDIDATE_HOUSEHOLDS_K,
            "thousand households",
            "regional statistics office",
        ),
        ("online_penetration", _ratio(rng.uniform(0.56, 0.64)), "share of households", "market survey 2026"),
        ("competitors_with_local_dc", 2.0, "count", "web research"),
        ("est_delivery_days", round(float(rng.uniform(4.1, 4.7)), 1), "days", "carrier quotes"),
        ("extra_shipping_cost_per_order", _money(rng.uniform(0.75, 1.0)), "USD/order", "carrier quotes"),
        (
            "launch_fixed_cost",
            float(round(rng.uniform(100_000, 140_000), -3)),
            "USD",
            "legal, carrier onboarding",
        ),
        ("launch_marketing_budget_6m", 360_000.0, "USD", "marketing plan draft"),
        ("expected_cac", _money(rng.uniform(30, 40)), "USD per new customer", "south launch actuals"),
    ]
    rows: list[dict[str, CellValue]] = [
        {"metric": metric, "value": float(value), "unit": unit, "source": source}
        for metric, value, unit, source in facts
    ]
    return DemoTable(
        name="candidate_market",
        description=f"What we know about the candidate {CANDIDATE_REGION} region (not served today).",
        columns=["metric", "value", "unit", "source"],
        rows=rows,
    )


def _new_market_case(h: CompanyHistory) -> DemoCase:
    metrics = _company_metrics(h)
    rampups = _rampups_table(h.seed)
    candidate = _candidate_market_table(h.seed)
    facts = {str(row["metric"]): float(row["value"]) for row in candidate.rows}
    per_1k, to_half = _rampup_stats(rampups)
    steady_likely = float(np.mean(per_1k)) * CANDIDATE_HOUSEHOLDS_K
    key_facts = {
        "orders_last_12m": metrics["orders_last_12m"],
        "margin_per_order_last_12m": metrics["margin_per_order_last_12m"],
        "inhouse_cost_per_order_last_12m": metrics["inhouse_cost_per_order_last_12m"],
        "candidate_households_k": CANDIDATE_HOUSEHOLDS_K,
        "comparable_orders_per_1k_households_min": round(min(per_1k), 2),
        "comparable_orders_per_1k_households_max": round(max(per_1k), 2),
        "steady_state_orders_per_month_likely": float(round(steady_likely)),
        "months_to_half_steady_state_min": float(min(to_half)),
        "months_to_half_steady_state_max": float(max(to_half)),
        "launch_fixed_cost": facts["launch_fixed_cost"],
        "launch_marketing_budget_6m": facts["launch_marketing_budget_6m"],
        "extra_shipping_cost_per_order": facts["extra_shipping_cost_per_order"],
        "expected_cac": facts["expected_cac"],
    }
    net_margin = (
        metrics["margin_per_order_last_12m"]
        - metrics["inhouse_cost_per_order_last_12m"]
        - facts["extra_shipping_cost_per_order"]
    )
    description = (
        f"{COMPANY_NAME} serves four regions ({_fmt_int(metrics['orders_last_12m'])} orders in the last "
        f"12 months) and is considering entering the {CANDIDATE_REGION} region in 2027: "
        f"{_fmt_int(CANDIDATE_HOUSEHOLDS_K * 1000)} addressable households, 2 competitors with a local DC, "
        f"and deliveries would take about {facts['est_delivery_days']:.1f} days from our current warehouse. "
        f"Past launches (west 2021, south 2022, a peer benchmark) settled at {min(per_1k):.1f}-"
        f"{max(per_1k):.1f} orders per 1k households a month and took {min(to_half)}-{max(to_half)} months "
        f"to reach half of that level, which suggests about {_fmt_int(steady_likely)} orders a month at "
        f"steady state. After in-house fulfilment and "
        f"{_fmt_usd(facts['extra_shipping_cost_per_order'])} extra shipping, each order would contribute "
        f"about {_fmt_usd(net_margin)}. Launch needs "
        f"{_fmt_usd(facts['launch_fixed_cost'])} fixed cost plus a "
        f"{_fmt_usd(facts['launch_marketing_budget_6m'])} six-month marketing push."
    )
    return DemoCase(
        name="new_market",
        title=f"Enter the {CANDIDATE_REGION} region?",
        description=description,
        question=f"Should we enter the {CANDIDATE_REGION} region in 2027, and with what launch budget?",
        options=["full_launch", "pilot_two_cities", "do_not_enter"],
        key_facts=key_facts,
        tables=[
            _monthly_orders_table(h),
            _monthly_pnl_table(h),
            _region_economics_table(h),
            rampups,
            candidate,
        ],
        **_base_case_fields(h),
    )


# --- public API -------------------------------------------------------------------

_CASE_BUILDERS: dict[str, Callable[[CompanyHistory], DemoCase]] = {
    "warehouse": _warehouse_case,
    "price_increase": _price_increase_case,
    "new_market": _new_market_case,
}


def list_demo_cases() -> list[str]:
    """Names accepted by ``generate_demo_case``."""
    return list(_CASE_BUILDERS)


def generate_demo_case(name: str, *, seed: int = 42) -> DemoCase:
    """Build one demo case; the same name and seed always give identical tables."""
    try:
        builder = _CASE_BUILDERS[name]
    except KeyError:
        raise ValueError(f"unknown demo case {name!r}; choose from {list_demo_cases()}") from None
    return builder(_history(seed))


def generate_all_demo_cases(*, seed: int = 42) -> list[DemoCase]:
    """All demo cases for one seed (they share the same company history)."""
    return [generate_demo_case(name, seed=seed) for name in list_demo_cases()]


def write_all_demo_cases(root: str | Path, *, seed: int = 42) -> list[Path]:
    """Write every case to ``root/<case name>/``; returns all written paths."""
    written: list[Path] = []
    for case in generate_all_demo_cases(seed=seed):
        written.extend(write_demo_case(case, Path(root) / case.name))
    return written
