from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from variator.domain import (
    DecisionModel,
    Fact,
    FactOrigin,
    HistogramBin,
    OptimizationResult,
    OptionStats,
    Sensitivity,
    SimulationResult,
)
from variator.domain.examples import warehouse_decision


@pytest.fixture
def facts() -> list[Fact]:
    return [
        Fact(statement="The company shipped 400,000 orders in 2026.", value=400_000, unit="orders",
             period="FY2026", origin=FactOrigin.DATA, source="orders_2026.csv", confidence=0.95),
        Fact(statement="Contribution margin before fulfilment averaged $9.0 per order.", value=9.0,
             unit="USD", period="FY2026", origin=FactOrigin.DATA, source="pnl_2026.xlsx"),
        Fact(statement="In-house fulfilment costs about $3.0 per order.", value=3.0, unit="USD",
             origin=FactOrigin.DOCUMENT, source="ops_review.pdf p.4"),
        Fact(statement="Regional 3PL providers quote $3.4-4.6 per order.", origin=FactOrigin.WEB,
             source="https://example.com/3pl-prices", confidence=0.6),
        Fact(statement="Order volume grew 8-15% per year in 2023-2026.", origin=FactOrigin.USER),
    ]


GROUNDED_SOURCES = {
    "demand_growth": "[F5]",
    "base_orders": "[F1]",
    "margin_per_order": "[F2]",
    "own_cost_per_order": "[F3]",
    "tpl_cost_per_order": "[F4]",
    "capex": "assumption: contractor estimate",
    "expansion_capex": "assumption: contractor estimate",
    "capacity_orders": "assumption: site plan",
}


@pytest.fixture
def grounded_decision() -> DecisionModel:
    """The warehouse example with every variable citing the ``facts`` fixture."""
    model = warehouse_decision()
    variables = [v.model_copy(update={"source": GROUNDED_SOURCES[v.name]}) for v in model.variables]
    return model.model_copy(update={"variables": variables})


@pytest.fixture
def model_payload(grounded_decision) -> dict[str, Any]:
    return grounded_decision.model_dump(mode="json")


def _hist() -> list[HistogramBin]:
    return [HistogramBin(left=i * 1e5, right=(i + 1) * 1e5, count=100 + i) for i in range(20)]


def _stats(option: str, mean: float, p5: float, prob_loss: float, prob_best: float) -> OptionStats:
    return OptionStats(
        option=option, mean=mean, std=1_234_567.891, p5=p5, p25=mean * 0.8, p50=mean * 0.99,
        p75=mean * 1.2, p95=mean * 1.6, prob_loss=prob_loss, var_95=p5, cvar_95=p5 * 0.55,
        prob_best=prob_best, histogram=_hist(),
    )


def _sens(option: str, variable: str, low: float, high: float) -> Sensitivity:
    return Sensitivity(option=option, variable=variable, metric_at_low=low, metric_at_high=high,
                       swing=abs(high - low))


@pytest.fixture
def simulation(decision) -> SimulationResult:
    return SimulationResult(
        metric=decision.metric,
        n_scenarios=10_000,
        seed=7,
        backend="numpy",
        elapsed_s=0.123456,
        options=[
            _stats("second_warehouse", 3_456_789.123, 1_100_000.5, 0.031234, 0.52),
            _stats("expand_current", 3_100_000.0, 1_300_000.0, 0.012, 0.31),
            _stats("outsource_3pl", 2_900_000.0, 900_000.0, 0.05, 0.17),
        ],
        sensitivities=[
            _sens("second_warehouse", "margin_per_order", 1_500_000, 5_400_000),
            _sens("second_warehouse", "demand_growth", 2_200_000, 4_600_000),
            _sens("second_warehouse", "own_cost_per_order", 3_900_000, 3_000_000),
            _sens("expand_current", "margin_per_order", 1_600_000, 4_500_000),
            _sens("expand_current", "demand_growth", 2_600_000, 3_500_000),
            _sens("outsource_3pl", "margin_per_order", 1_900_000, 3_900_000),
            _sens("outsource_3pl", "demand_growth", 2_000_000, 3_700_000),
        ],
        risk_hit_rates={"construction_delay": 0.24987, "tpl_price_hike": 0.3012},
    )


@pytest.fixture
def optimization() -> OptimizationResult:
    return OptimizationResult(
        selected=["second_warehouse"], expected_value=3_456_789.123, cvar=605_000.0,
        budget_used=2_200_000, solver="highs", status="Optimal", proven_optimal=True, elapsed_s=0.5,
    )


@pytest.fixture
def make_narrative() -> Callable[..., dict[str, Any]]:
    """Factory for a valid analyst LLM reply; override any field via kwargs."""

    def make(**overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "recommendation": "second_warehouse",
            "headline": "Open the second warehouse: highest expected profit with acceptable downside.",
            "rationale": [
                "Second warehouse has the highest expected contribution and is best in 52% of scenarios.",
                "Its downside tail is comparable to expanding the current site.",
            ],
            "key_drivers": [
                {"variable": "demand_growth", "explanation": "Growth fills the new capacity."},
                {"variable": "margin_per_order", "explanation": "Margin scales every extra order."},
            ],
            "what_must_be_true": ["Demand keeps growing at least at the historical pace."],
            "early_warning_signals": ["Quarterly order growth falls below trend for two quarters."],
            "risks_to_watch": ["Construction delays raise capex."],
            "limitations": ["Capex ranges are contractor estimates."],
            "confidence": "medium",
        }
        payload.update(overrides)
        return payload

    return make
