import numpy as np
import pytest

from variator.domain import Correlation, DistributionKind
from variator.domain.examples import warehouse_decision
from variator.quality import fit_distribution, score_series, select_trend, simplify, swing_shares


def test_score_rewards_size_and_penalizes_gaps():
    rng = np.random.default_rng(0)
    good = score_series(rng.normal(100, 5, 60))
    few = score_series([1.0, 2.0, 3.0])
    gaps = score_series([1.0, None, None, None, 2.0, 3.0, None, None, 4.0, 5.0])
    assumption = score_series(rng.normal(100, 5, 60), origin="assumption")
    assert good.q > 0.8 > few.q
    assert gaps.q < good.q and any("missing" in i for i in gaps.issues)
    assert assumption.q < good.q


def test_trend_selection_prefers_simple_models():
    rng = np.random.default_rng(1)
    flat = 100 + rng.normal(0, 1, 36)
    rising = 100 + 2.0 * np.arange(36) + rng.normal(0, 1, 36)
    seasonal = 100 + 10 * np.sin(np.arange(48) * 2 * np.pi / 12) + rng.normal(0, 1, 48)
    assert select_trend(flat).model == "constant"
    assert select_trend(rising).model == "linear"
    assert select_trend(seasonal).model == "seasonal"


def test_fit_distribution_uses_three_points_for_low_quality():
    assert fit_distribution([1, 2, 3, 10], q=0.3).kind == DistributionKind.PERT
    assert fit_distribution([5, 5], q=0.9).kind == DistributionKind.FIXED
    assert fit_distribution([1, 9], q=0.9).kind == DistributionKind.UNIFORM
    rng = np.random.default_rng(2)
    assert fit_distribution(rng.normal(50, 5, 200), q=0.9).kind == DistributionKind.NORMAL


def test_simplify_replaces_shapes_drops_correlations_and_freezes():
    model = warehouse_decision()
    assert any(c.a == "demand_growth" for c in model.correlations)
    quality = {v.name: 0.9 for v in model.variables} | {"margin_per_order": 0.2}
    result = simplify(model, quality, swing_shares={"capacity_orders": 0.001})
    margin = result.model.variable("margin_per_order").distribution
    assert margin.kind == DistributionKind.PERT
    assert all("margin_per_order" not in (c.a, c.b) for c in result.model.correlations)
    assert result.model.variable("capacity_orders").distribution.kind == DistributionKind.FIXED
    assert len(result.notes) == 3
    assert any(c.a == "own_cost_per_order" for c in result.model.correlations)
    unknown = simplify(model, {})  # no quality data: every input counts as an assumption
    assert unknown.model.correlations == []
    kept = simplify(model, quality, swing_shares={"capacity_orders": 0.001}, keep=["capacity_orders"])
    assert kept.model.variable("capacity_orders").distribution.kind != DistributionKind.FIXED


def test_swing_shares_cover_secondary_criteria():
    from variator_it_services import delivery_model

    shares = swing_shares(delivery_model(), n_scenarios=500)
    # mgmt_ratio drives only the resources axis; it must not look negligible
    assert shares["mgmt_ratio"] > 0.05
    assert shares["value_per_month"] == 0.0  # fixed
    assert all(0 <= v <= 1 for v in shares.values())


def test_correlation_type_still_valid():
    Correlation(a="x", b="y", rho=0.2)
    with pytest.raises(ValueError):
        Correlation(a="x", b="y", rho=1.5)
