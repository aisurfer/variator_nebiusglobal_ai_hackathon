"""Canonical example decision used by tests and demos across modules."""

from __future__ import annotations

from variator.domain.decision import (
    Correlation,
    DecisionModel,
    DecisionOption,
    Distribution,
    DistributionKind,
    MetricSpec,
    Risk,
    RiskEffect,
    Variable,
)

_YEARS = range(1, 6)


def _volume(year: int) -> str:
    return f"base_orders * (1 + demand_growth) ** {year}"


def _growth_sum() -> str:
    """Sum of yearly volume multipliers over the horizon (compounding growth)."""
    return " + ".join(f"(1 + demand_growth) ** {t}" for t in _YEARS)


def warehouse_decision() -> DecisionModel:
    """A mid-size distributor choosing how to handle demand growth from 2027.

    Metric: 5-year profit contribution in USD (simplified, undiscounted). Tuned so
    no option dominates: expansion has the best mean, the 3PL the safest tail and
    the second warehouse the highest upside (each is best in roughly a third of
    scenarios).
    """

    def pert(low: float, mode: float, high: float) -> Distribution:
        return Distribution(kind=DistributionKind.PERT, low=low, mode=mode, high=high)

    return DecisionModel(
        question="Open a second warehouse, expand the current one, or outsource to a 3PL?",
        metric=MetricSpec(name="5-year profit contribution", unit="USD"),
        variables=[
            Variable(
                name="demand_growth",
                description="Annual order volume growth",
                unit="share",
                distribution=pert(0.02, 0.12, 0.25),
                source="assumption based on 2023-2026 sales trend",
            ),
            Variable(
                name="base_orders",
                description="Orders per year today",
                unit="orders",
                distribution=Distribution.fixed(400_000),
                source="data: orders_2026.csv",
            ),
            Variable(
                name="margin_per_order",
                description="Contribution margin per order before fulfilment",
                unit="USD",
                distribution=Distribution(kind=DistributionKind.NORMAL, mean=9.0, std=1.2),
                source="data: pnl_2026.xlsx",
            ),
            Variable(
                name="own_cost_per_order",
                description="In-house fulfilment cost per order",
                unit="USD",
                distribution=pert(2.6, 3.0, 3.8),
            ),
            Variable(
                name="tpl_cost_per_order",
                description="3PL fulfilment fee per order",
                unit="USD",
                distribution=pert(3.4, 3.9, 4.6),
                source="web: 3PL price survey",
            ),
            Variable(
                name="capex",
                description="Upfront investment",
                unit="USD",
                distribution=pert(1_800_000, 2_200_000, 3_200_000),
            ),
            Variable(
                name="expansion_capex",
                description="Upfront investment to expand the current site",
                unit="USD",
                distribution=pert(700_000, 900_000, 1_400_000),
            ),
            Variable(
                name="capacity_orders",
                description="Extra yearly capacity from expansion",
                unit="orders",
                distribution=Distribution(kind=DistributionKind.UNIFORM, low=90_000, high=130_000),
            ),
        ],
        options=[
            DecisionOption(
                name="second_warehouse",
                description=(
                    "Open an automated second warehouse near the south region: "
                    "15% lower fulfilment cost on all orders, no capacity limit"
                ),
                formula=f"base_orders * ({_growth_sum()}) * "
                "(margin_per_order - own_cost_per_order * 0.85) - capex",
                cost=2_200_000,
            ),
            DecisionOption(
                name="expand_current",
                description=(
                    "Expand and automate the current site (7% lower in-house cost); "
                    "orders above the new capacity overflow to a 3PL"
                ),
                formula=" + ".join(
                    f"minimum({_volume(t)}, base_orders + capacity_orders)"
                    " * (margin_per_order - own_cost_per_order * 0.93)"
                    f" + maximum({_volume(t)} - base_orders - capacity_orders, 0)"
                    " * (margin_per_order - tpl_cost_per_order)"
                    for t in _YEARS
                )
                + " - expansion_capex",
                cost=900_000,
            ),
            DecisionOption(
                name="outsource_3pl",
                description="Keep the current site as is and send all growth to a 3PL",
                formula=(
                    f"{len(_YEARS)} * base_orders * (margin_per_order - own_cost_per_order)"
                    f" + base_orders * ({_growth_sum()} - {len(_YEARS)})"
                    " * (margin_per_order - tpl_cost_per_order)"
                ),
                cost=0,
            ),
        ],
        correlations=[
            Correlation(a="demand_growth", b="margin_per_order", rho=0.3),
            Correlation(a="own_cost_per_order", b="tpl_cost_per_order", rho=0.5),
        ],
        risks=[
            Risk(
                name="construction_delay",
                description="Permits or construction slip by two quarters",
                probability=0.25,
                variable="capex",
                effect=RiskEffect.MULTIPLY,
                magnitude=pert(0.05, 0.15, 0.40),
                options=["second_warehouse"],
            ),
            Risk(
                name="tpl_price_hike",
                description="3PL raises fees after the first year",
                probability=0.3,
                variable="tpl_cost_per_order",
                effect=RiskEffect.ADD,
                magnitude=pert(0.2, 0.4, 0.9),
                options=["outsource_3pl"],
            ),
        ],
        exclusive_groups=[["second_warehouse", "expand_current", "outsource_3pl"]],
        assumptions=["Prices flat in real terms", "No discounting over 5 years"],
    )
