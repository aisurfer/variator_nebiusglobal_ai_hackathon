"""End-to-end behaviour of optimize_portfolio / optimize_decision on the HiGHS path."""

from dataclasses import dataclass

import numpy as np
import pytest
from opt_testing import brute_force, random_instance, ru_cvar

from variator.domain import DecisionModel, MetricSpec
from variator.optimization import formulation, optimize_decision, optimize_portfolio
from variator.optimization.metrics import cvar_of_loss


def _solve(inst, **kwargs):
    names = inst["names"]
    return optimize_portfolio(
        inst["scenarios"],
        names,
        costs=list(inst["costs"]),
        budget=inst["budget"],
        exclusive_groups=[[names[i] for i in g] for g in inst["groups"]],
        solver="highs",
        **kwargs,
    )


def _limit_between(inst, alpha):
    """A CVaR limit that cuts off the unconstrained optimum but keeps something feasible."""
    free = brute_force(inst["scenarios"], costs=inst["costs"], budget=inst["budget"], groups=inst["groups"])
    return 0.5 * free.cvar if free.cvar > 0 else free.cvar - 1.0


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("cvar_mode", ["none", "binding"])
@pytest.mark.parametrize(("n_scenarios", "alpha"), [(120, 0.9), (130, 0.95)])  # integer and fractional tail
def test_milp_matches_brute_force(seed, cvar_mode, n_scenarios, alpha):
    inst = random_instance(seed, n_scenarios=n_scenarios)
    limit = None if cvar_mode == "none" else _limit_between(inst, alpha)
    expected = brute_force(
        inst["scenarios"],
        costs=inst["costs"],
        budget=inst["budget"],
        groups=inst["groups"],
        alpha=alpha,
        max_cvar_loss=limit,
    )
    result = _solve(inst, cvar_alpha=alpha, max_cvar_loss=limit)

    if expected is None:
        assert result.status == "infeasible" and result.selected == []
        return
    assert result.status == "optimal" and result.proven_optimal and result.solver == "highs"
    assert result.expected_value == pytest.approx(expected.expected_value, abs=1e-6)
    mask = np.array([name in result.selected for name in inst["names"]])
    values = inst["scenarios"] @ mask.astype(float)
    assert -result.cvar == pytest.approx(ru_cvar(-values, alpha), abs=1e-9)  # cvar is metric-space
    assert result.budget_used == pytest.approx(float(inst["costs"] @ mask))
    assert result.budget_used <= inst["budget"] + 1e-9
    assert all(mask[list(g)].sum() <= 1 for g in inst["groups"])
    if limit is not None:
        assert -result.cvar <= limit + 1e-6


def _tail_case():
    """A: mean 100 but loses 470 in 5% of scenarios. B: a sure 60."""
    a = np.full(100, 130.0)
    a[::20] = -470.0
    return np.column_stack([a, np.full(100, 60.0)])


def test_unconstrained_takes_every_positive_option():
    result = optimize_portfolio(_tail_case(), ["a", "b"], solver="highs")
    assert result.selected == ["a", "b"]
    assert result.expected_value == pytest.approx(160.0)
    assert result.cvar == pytest.approx(-410.0)  # mean of the worst 5% of outcomes: 60 - 470
    assert result.budget_used is None and result.notes == []


def test_cvar_constraint_binds():
    free = optimize_portfolio(_tail_case(), ["a", "b"], solver="highs")
    capped = optimize_portfolio(_tail_case(), ["a", "b"], max_cvar_loss=0.0, solver="highs")
    assert capped.selected == ["b"]
    assert capped.expected_value == pytest.approx(60.0)
    assert capped.expected_value < free.expected_value
    assert capped.cvar == pytest.approx(60.0)  # >= -max_cvar_loss
    loose = optimize_portfolio(_tail_case(), ["a", "b"], max_cvar_loss=450.0, solver="highs")
    assert loose.selected == ["a", "b"]


def test_budget_constraint():
    result = optimize_portfolio(_tail_case(), ["a", "b"], costs=[5, 3], budget=6, solver="highs")
    assert result.selected == ["a"] and result.budget_used == 5.0


def test_missing_cost_counts_as_zero():
    result = optimize_portfolio(_tail_case(), ["a", "b"], costs=[None, 3], budget=3, solver="highs")
    assert result.selected == ["a", "b"] and result.budget_used == 3.0


def test_exclusive_group():
    result = optimize_portfolio(_tail_case(), ["a", "b"], exclusive_groups=[["a", "b"]], solver="highs")
    assert result.selected == ["a"]


def test_nothing_selected_when_every_option_loses():
    result = optimize_portfolio(-np.abs(_tail_case()), ["a", "b"], solver="highs")
    assert result.status == "optimal" and result.selected == []
    assert result.expected_value == 0.0 and result.cvar == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [{"max_cvar_loss": -1_000.0}, {"costs": [1, 1], "budget": -1.0}],
    ids=["cvar-stricter-than-status-quo", "negative-budget"],
)
def test_infeasible_returns_status_instead_of_raising(kwargs):
    result = optimize_portfolio(_tail_case(), ["a", "b"], solver="highs", **kwargs)
    assert result.status == "infeasible" and result.selected == []
    assert result.expected_value == 0.0 and result.cvar is None and not result.proven_optimal
    assert any("No portfolio satisfies" in note for note in result.notes)


def test_subsampling_note_and_full_matrix_recomputation(monkeypatch):
    monkeypatch.setitem(formulation.MAX_CVAR_SCENARIOS, "highs", 50)
    s = 400
    sample = formulation.sample_indices(s, 50)
    a = np.full(s, 20.0)
    a[np.setdiff1d(np.arange(s), sample)[::10]] = -60.0  # losses hide outside the sample
    scenarios = np.column_stack([a, np.random.default_rng(0).normal(5, 1, s)])

    result = optimize_portfolio(scenarios, ["a", "b"], max_cvar_loss=0.0, solver="highs")

    assert result.selected == ["a", "b"]  # the sample saw no losses for "a"
    full_values = scenarios.sum(axis=1)
    assert result.expected_value == pytest.approx(full_values.mean())
    assert -result.cvar == pytest.approx(cvar_of_loss(-full_values, 0.95))
    assert result.cvar < 0.0  # the worst tail loses money on the full matrix ...
    assert cvar_of_loss(-full_values[sample], 0.95) <= 0.0  # ... but not on the sample
    assert any("sample of 50 of 400 scenarios" in note for note in result.notes)
    assert any("above the limit" in note for note in result.notes)


def test_rejects_bad_solver_and_time_limit():
    with pytest.raises(ValueError, match="solver"):
        optimize_portfolio(_tail_case(), ["a", "b"], solver="gurobi")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="time_limit_s"):
        optimize_portfolio(_tail_case(), ["a", "b"], time_limit_s=0)


@dataclass
class FakeRun:
    scenarios: np.ndarray
    option_names: list[str]


def _warehouse_run(decision: DecisionModel) -> FakeRun:
    """Second warehouse: best mean, fat left tail. Expansion: solid. 3PL: safe but small."""
    rng = np.random.default_rng(7)
    s = 2_000
    second = rng.normal(3.0e6, 0.8e6, s)
    second[rng.random(s) < 0.15] -= 4.0e6
    expand = rng.normal(2.0e6, 0.5e6, s)
    outsource = rng.normal(1.2e6, 0.02e6, s)
    return FakeRun(np.column_stack([second, expand, outsource]), [o.name for o in decision.options])


def test_warehouse_decision_known_answers(decision):
    run = _warehouse_run(decision)
    free = optimize_decision(run, decision, solver="highs")
    assert free.selected == ["second_warehouse"]  # one pick: the three options are exclusive
    assert free.budget_used == 2_200_000

    on_budget = optimize_decision(run, decision, budget=1_000_000, solver="highs")
    assert on_budget.selected == ["expand_current"] and on_budget.budget_used == 900_000

    # Worst-5% average profit must stay above 800k: rules out the fat-tailed second warehouse.
    cautious = optimize_decision(run, decision, max_cvar_loss=-800_000, solver="highs")
    assert cautious.selected == ["expand_current"] and cautious.cvar >= 800_000

    # Above 1.1M only the 3PL qualifies (expansion's tail averages ~0.97M).
    safe = optimize_decision(run, decision, max_cvar_loss=-1_100_000, solver="highs")
    assert safe.selected == ["outsource_3pl"] and safe.cvar >= 1_100_000


def test_optimize_decision_rejects_unknown_options(decision):
    run = FakeRun(np.zeros((10, 1)), ["mystery_option"])
    with pytest.raises(ValueError, match="mystery_option"):
        optimize_decision(run, decision)


def test_optimize_decision_lower_is_better_metric(decision):
    model = decision.model_copy(update={"metric": MetricSpec(name="3-year cost", higher_is_better=False)})
    s = 100
    # second_warehouse saves money (negative cost) in every scenario; the others add cost.
    run = FakeRun(
        np.column_stack([np.full(s, -50.0), np.full(s, 30.0), np.full(s, 10.0)]),
        [o.name for o in model.options],
    )
    result = optimize_decision(run, model, solver="highs")
    assert result.selected == ["second_warehouse"]
    assert result.expected_value == pytest.approx(-50.0)  # metric units
    assert result.cvar == pytest.approx(-50.0)  # worst (highest) tail of the cost
    assert any("lower-is-better" in note for note in result.notes)
