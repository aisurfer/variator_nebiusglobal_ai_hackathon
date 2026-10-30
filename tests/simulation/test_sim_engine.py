import time

import numpy as np
import pytest
from scipy import stats

from variator.domain import (
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
from variator.simulation import (
    CorrelationRepairWarning,
    NonFiniteMetricError,
    SimulationRun,
    cupy_available,
    simulate,
)
from variator.simulation.engine import HISTOGRAM_BINS, SENSITIVITY_TOP_DRIVERS


def var(name: str, distribution: Distribution) -> Variable:
    return Variable(name=name, description=name, distribution=distribution)


def option(name: str, formula: str, **overrides: Distribution) -> DecisionOption:
    return DecisionOption(name=name, description=name, formula=formula, overrides=overrides)


def model(variables, options, *, higher_is_better=True, **extra) -> DecisionModel:
    return DecisionModel(
        question="test",
        metric=MetricSpec(name="value", higher_is_better=higher_is_better),
        variables=variables,
        options=options,
        **extra,
    )


NORMAL = Distribution(kind=DistributionKind.NORMAL, mean=10.0, std=2.0)
UNIFORM = Distribution(kind=DistributionKind.UNIFORM, low=0.0, high=10.0)


# --- warehouse example -------------------------------------------------------


@pytest.fixture(scope="module")
def warehouse_run() -> SimulationRun:
    from variator.domain.examples import warehouse_decision

    return simulate(warehouse_decision(), n_scenarios=20_000, seed=11, backend="numpy")


def test_warehouse_shape_and_metadata(warehouse_run, decision):
    run = warehouse_run
    assert run.option_names == [o.name for o in decision.options]
    assert run.scenarios.shape == (20_000, 3)
    assert isinstance(run.scenarios, np.ndarray)
    assert np.isfinite(run.scenarios).all()
    res = run.result
    assert res.backend == "numpy" and res.n_scenarios == 20_000 and res.seed == 11
    assert res.elapsed_s > 0
    assert [o.option for o in res.options] == run.option_names


def test_warehouse_stats_are_consistent(warehouse_run):
    res = warehouse_run.result
    assert sum(o.prob_best for o in res.options) == pytest.approx(1.0)
    for j, o in enumerate(res.options):
        col = warehouse_run.scenarios[:, j]
        assert o.p5 <= o.p25 <= o.p50 <= o.p75 <= o.p95
        assert o.var_95 == o.p5
        assert o.cvar_95 <= o.var_95
        assert o.mean == pytest.approx(col.mean())
        assert o.prob_loss == pytest.approx((col < 0).mean())
        assert len(o.histogram) == HISTOGRAM_BINS
        assert sum(b.count for b in o.histogram) == len(col)
        assert o.histogram[0].left == pytest.approx(col.min())
        assert o.histogram[-1].right == pytest.approx(col.max())


def test_warehouse_risks_and_sensitivities(warehouse_run, decision):
    res = warehouse_run.result
    assert res.risk_hit_rates["construction_delay"] == pytest.approx(0.25, abs=0.01)
    assert res.risk_hit_rates["tpl_price_hike"] == pytest.approx(0.30, abs=0.01)
    by_option: dict[str, list] = {}
    for s in res.sensitivities:
        by_option.setdefault(s.option, []).append(s)
        assert s.swing == pytest.approx(abs(s.metric_at_high - s.metric_at_low))
    assert set(by_option) == {o.name for o in decision.options}
    for entries in by_option.values():
        swings = [s.swing for s in entries]
        assert swings == sorted(swings, reverse=True)
        assert len(entries) <= SENSITIVITY_TOP_DRIVERS
    # Fixed base_orders has no swing; margin drives every option most.
    assert all(s.variable != "base_orders" for s in res.sensitivities)
    assert all(entries[0].variable == "margin_per_order" for entries in by_option.values())
    # capex only matters for the second warehouse, and higher capex hurts.
    capex = [s for s in res.sensitivities if s.variable == "capex"]
    assert [s.option for s in capex] == ["second_warehouse"]
    assert capex[0].metric_at_high < capex[0].metric_at_low


def test_sensitivity_can_be_disabled(decision):
    run = simulate(decision, n_scenarios=1_000, seed=1, sensitivity=False)
    assert run.result.sensitivities == []


def test_seed_reproducibility(decision):
    a = simulate(decision, n_scenarios=5_000, seed=123)
    b = simulate(decision, n_scenarios=5_000, seed=123)
    c = simulate(decision, n_scenarios=5_000, seed=124)
    np.testing.assert_array_equal(a.scenarios, b.scenarios)
    assert a.result.options == b.result.options
    assert a.result.sensitivities == b.result.sensitivities
    assert not np.array_equal(a.scenarios, c.scenarios)


def test_invalid_scenario_count():
    with pytest.raises(ValueError, match="n_scenarios"):
        simulate(model([var("x", NORMAL)], [option("a", "x")]), n_scenarios=0)


def test_explicit_cupy_backend_without_gpu_fails_clearly():
    if cupy_available():
        pytest.skip("a GPU is available here")
    from variator.simulation import BackendUnavailableError

    with pytest.raises(BackendUnavailableError):
        simulate(model([var("x", NORMAL)], [option("a", "x")]), n_scenarios=10, backend="cupy")


# --- common random numbers and risks ---------------------------------------------


def test_override_reuses_the_same_uniforms():
    shifted = Distribution(kind=DistributionKind.NORMAL, mean=12.0, std=2.0)
    wider = Distribution(kind=DistributionKind.UNIFORM, low=5.0, high=25.0)
    m = model(
        [var("price", NORMAL), var("volume", UNIFORM)],
        [
            option("base", "price * volume"),
            option("premium", "price * volume", price=shifted),
            option("bigger", "volume", volume=wider),
            option("plain_volume", "volume"),
        ],
    )
    run = simulate(m, n_scenarios=10_000, seed=5)
    base, premium, bigger, plain = run.scenarios.T
    # Same uniform per scenario: the override shifts price by exactly 2 in every scenario.
    np.testing.assert_allclose(premium - base, 2.0 * plain, rtol=1e-9)
    np.testing.assert_allclose(bigger, 5.0 + 2.0 * plain, rtol=1e-12)


def test_risk_fires_at_its_probability_and_only_for_exposed_options():
    m = model(
        [var("cost", Distribution.fixed(100.0))],
        [option("exposed", "cost"), option("also_exposed", "cost + 0"), option("safe", "cost")],
        risks=[
            Risk(
                name="spike",
                description="cost spike",
                probability=0.3,
                variable="cost",
                effect=RiskEffect.MULTIPLY,
                magnitude=Distribution.fixed(0.5),
                options=["exposed", "also_exposed"],
            )
        ],
    )
    run = simulate(m, n_scenarios=50_000, seed=9)
    exposed, also, safe = run.scenarios.T
    assert set(np.unique(exposed)) == {100.0, 150.0}
    assert (exposed == 150.0).mean() == pytest.approx(0.3, abs=0.01)
    assert run.result.risk_hit_rates["spike"] == pytest.approx((exposed == 150.0).mean())
    np.testing.assert_array_equal(exposed, also)  # same events hit every exposed option
    assert (safe == 100.0).all()


def test_additive_risks_hit_all_options_by_default_and_share_magnitudes():
    magnitude = Distribution(kind=DistributionKind.UNIFORM, low=1.0, high=3.0)
    m = model(
        [var("x", Distribution.fixed(0.0)), var("y", UNIFORM)],
        [option("a", "x"), option("b", "x * 2 + y * 0")],
        risks=[
            Risk(
                name="hit",
                description="",
                probability=0.5,
                variable="x",
                effect=RiskEffect.ADD,
                magnitude=magnitude,
            ),
        ],
    )
    run = simulate(m, n_scenarios=20_000, seed=2)
    a, b = run.scenarios.T
    fired = a != 0
    assert fired.mean() == pytest.approx(0.5, abs=0.015)
    assert a[fired].min() >= 1.0 and a[fired].max() <= 3.0
    assert a[fired].mean() == pytest.approx(2.0, abs=0.03)
    np.testing.assert_allclose(b, 2 * a)


def test_two_risks_on_one_variable_compound():
    m = model(
        [var("x", Distribution.fixed(100.0))],
        [option("a", "x")],
        risks=[
            Risk(name="r1", description="", probability=1.0, variable="x", magnitude=Distribution.fixed(0.1)),
            Risk(
                name="r2",
                description="",
                probability=1.0,
                variable="x",
                effect=RiskEffect.ADD,
                magnitude=Distribution.fixed(5.0),
            ),
        ],
    )
    run = simulate(m, n_scenarios=10, seed=0, sensitivity=False)
    np.testing.assert_allclose(run.scenarios[:, 0], 100 * 1.1 + 5)


def test_achieved_rank_correlation_with_non_normal_marginals():
    pert = Distribution(kind=DistributionKind.PERT, low=0.0, mode=1.0, high=4.0)
    lognormal = Distribution(kind=DistributionKind.LOGNORMAL, mean=5.0, std=3.0)
    m = model(
        [var("a", pert), var("b", lognormal)],
        [option("show_a", "a"), option("show_b", "b")],
        correlations=[Correlation(a="a", b="b", rho=-0.6)],
    )
    run = simulate(m, n_scenarios=50_000, seed=4, sensitivity=False)
    assert stats.spearmanr(run.scenarios).statistic == pytest.approx(-0.6, abs=0.01)


def test_inconsistent_correlations_are_repaired_and_noted():
    m = model(
        [var("a", NORMAL), var("b", NORMAL), var("c", NORMAL)],
        [option("total", "a + b + c")],
        correlations=[
            Correlation(a="a", b="b", rho=0.9),
            Correlation(a="b", b="c", rho=0.9),
            Correlation(a="a", b="c", rho=-0.9),
        ],
    )
    with pytest.warns(CorrelationRepairWarning):
        run = simulate(m, n_scenarios=2_000, seed=1)
    assert any("positive definite" in note for note in run.notes)


# --- metric direction, ties, tornado, invalid math ------------------------------


def test_lower_is_better_semantics():
    cost = Distribution(kind=DistributionKind.UNIFORM, low=-1.0, high=3.0)
    m = model(
        [var("cost", cost)],
        [option("cheap", "cost"), option("pricey", "cost + 1")],
        higher_is_better=False,
    )
    run = simulate(m, n_scenarios=40_000, seed=3)
    cheap, pricey = run.result.options
    assert cheap.prob_best == 1.0 and pricey.prob_best == 0.0
    # Bad tail is the upper tail: VaR is the 95th percentile, CVaR the mean of the top 5%.
    assert cheap.var_95 == cheap.p95
    assert cheap.var_95 == pytest.approx(2.8, abs=0.02)
    assert cheap.cvar_95 == pytest.approx(2.9, abs=0.02)
    assert cheap.cvar_95 >= cheap.var_95
    assert cheap.prob_loss == pytest.approx(0.75, abs=0.01)  # P(metric > 0)


def test_ties_split_prob_best():
    m = model([var("x", NORMAL)], [option("a", "x"), option("b", "x"), option("c", "x - 1")])
    a, b, c = simulate(m, n_scenarios=1_000, seed=0).result.options
    assert (a.prob_best, b.prob_best, c.prob_best) == (0.5, 0.5, 0.0)


def test_tornado_pins_variable_at_p10_and_p90():
    m = model([var("a", UNIFORM), var("b", NORMAL)], [option("o", "a * 2"), option("p", "a + b")])
    run = simulate(m, n_scenarios=5_000, seed=8)
    o_a = next(s for s in run.result.sensitivities if s.option == "o")
    assert (o_a.variable, o_a.metric_at_low, o_a.metric_at_high, o_a.swing) == ("a", 2.0, 18.0, 16.0)
    p_entries = {s.variable: s for s in run.result.sensitivities if s.option == "p"}
    assert p_entries["a"].swing == pytest.approx(8.0)  # other inputs identical for both pins
    assert p_entries["b"].swing == pytest.approx(2 * 1.2816 * 2.0, rel=1e-3)


def test_non_finite_metric_raises_with_counts_and_example():
    m = model(
        [var("x", Distribution(kind=DistributionKind.NORMAL, mean=0.0, std=1.0))],
        [option("ok", "x"), option("bad", "log(x)")],
    )
    with pytest.raises(NonFiniteMetricError, match="'bad'") as info:
        simulate(m, n_scenarios=4_000, seed=1)
    assert set(info.value.counts) == {"bad"}
    assert 1_800 < info.value.counts["bad"] < 2_200
    assert "inputs {'x'" in str(info.value)


def test_constant_formula_and_tiny_runs():
    m = model([var("x", NORMAL)], [option("flat", "42"), option("x", "x")])
    run = simulate(m, n_scenarios=1, seed=0)
    flat = run.result.options[0]
    assert flat.mean == flat.p5 == flat.cvar_95 == 42.0 and flat.std == 0.0
    assert sum(b.count for b in flat.histogram) == 1


# --- performance -----------------------------------------------------------------


def test_cpu_performance_100k_scenarios(decision):
    start = time.perf_counter()
    run = simulate(decision, n_scenarios=100_000, seed=0, backend="numpy")
    elapsed = time.perf_counter() - start
    print(f"\n100k scenarios x {len(decision.options)} options on CPU: {elapsed:.2f}s")
    assert run.scenarios.shape == (100_000, 3)
    assert elapsed < 10.0  # measured ~0.8s on a laptop CPU; generous for slow CI
