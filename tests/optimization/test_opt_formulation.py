import numpy as np
import pytest
from opt_testing import ru_cvar

from variator.optimization.formulation import (
    PortfolioProblem,
    build_formulation,
    constraint_violations,
    sample_indices,
)
from variator.optimization.metrics import cvar_of_loss, evaluate_portfolio


@pytest.mark.parametrize(("n", "alpha"), [(100, 0.95), (130, 0.95), (37, 0.9), (10, 0.95), (7, 0.5)])
def test_cvar_matches_rockafellar_uryasev_minimum(n, alpha):
    losses = np.random.default_rng(n).normal(0, 10, n)
    assert cvar_of_loss(losses, alpha) == pytest.approx(ru_cvar(losses, alpha), rel=1e-12, abs=1e-12)


def test_cvar_fractional_tail_by_hand():
    # tail = 0.25 * 6 = 1.5 scenarios: the worst (10) fully, half of the second worst (4)
    losses = np.array([10.0, 4.0, 1.0, 0.0, -1.0, -2.0])
    assert cvar_of_loss(losses, 0.75) == pytest.approx((10 + 0.5 * 4) / 1.5)


def test_cvar_rejects_bad_inputs():
    with pytest.raises(ValueError):
        cvar_of_loss(np.array([]), 0.9)
    with pytest.raises(ValueError):
        cvar_of_loss(np.array([1.0]), 1.0)


def test_evaluate_portfolio_sums_selected_options():
    scenarios = np.array([[1.0, 10.0, 100.0], [3.0, -30.0, 300.0]])
    metrics = evaluate_portfolio(
        scenarios, np.array([True, True, False]), alpha=0.5, costs=np.array([1.0, 2.0, 4.0])
    )
    assert metrics.expected_value == pytest.approx(-8.0)  # mean of [11, -27]
    assert metrics.cvar_loss == pytest.approx(27.0)  # worst half of losses [-11, 27]
    assert metrics.budget_used == 3.0


def _problem(**kwargs):
    defaults = {"scenarios": np.arange(12.0).reshape(4, 3) - 5, "option_names": ["a", "b", "c"]}
    return PortfolioProblem.from_inputs(**{**defaults, **kwargs})


def test_problem_validation():
    with pytest.raises(ValueError, match="columns"):
        _problem(option_names=["a", "b"])
    with pytest.raises(ValueError, match="unique"):
        _problem(option_names=["a", "a", "b"])
    with pytest.raises(ValueError, match="NaN"):
        _problem(scenarios=np.array([[np.nan, 1.0, 2.0]]))
    with pytest.raises(ValueError, match="unknown"):
        _problem(exclusive_groups=[["a", "zzz"]])
    with pytest.raises(ValueError, match="costs"):
        _problem(costs=[1.0])
    with pytest.raises(ValueError, match="cvar_alpha"):
        _problem(cvar_alpha=1.0)
    with pytest.raises(ValueError, match="matrix"):
        _problem(scenarios=np.ones(3))


def test_problem_normalizes_costs_and_groups():
    problem = _problem(costs=[None, 2, 3.5], exclusive_groups=[["c", "a", "a"], ["b"]])
    np.testing.assert_array_equal(problem.costs, [0.0, 2.0, 3.5])
    assert problem.groups == ((0, 2),)  # deduplicated, sorted, singleton group dropped


def test_formulation_without_cvar_has_only_option_variables():
    f = build_formulation(_problem(costs=[1, 2, 3], budget=4, exclusive_groups=[["a", "b"]]))
    assert f.n_vars == 3 and f.eta_index is None and f.sample_indices is None
    assert f.n_rows == 3  # cardinality, budget, one group
    np.testing.assert_array_equal(f.integrality, [1, 1, 1])
    np.testing.assert_allclose(f.c, -_problem().scenarios.mean(axis=0) / 6.0)  # value_scale = max |v| = 6
    dense = f.a.toarray()
    np.testing.assert_allclose(dense[1], [1 / 4, 2 / 4, 3 / 4])  # costs / max(|costs|, |budget|)
    assert f.row_upper[1] == pytest.approx(1.0)
    np.testing.assert_array_equal(dense[2], [1, 1, 0])


def test_formulation_with_cvar_follows_rockafellar_uryasev():
    problem = _problem(max_cvar_loss=3.0, cvar_alpha=0.75)
    f = build_formulation(problem)
    s, k = problem.scenarios.shape
    assert f.n_vars == k + 1 + s and f.eta_index == k and f.u_slice == slice(k + 1, k + 1 + s)
    assert f.var_lower[f.eta_index] == -np.inf and np.all(f.var_lower[f.u_slice] == 0)
    np.testing.assert_array_equal(f.integrality, [1, 1, 1] + [0] * (1 + s))
    dense = f.a.toarray()
    tail_rows = dense[1 : 1 + s]
    np.testing.assert_allclose(tail_rows[:, :k], problem.scenarios / f.value_scale)
    np.testing.assert_array_equal(tail_rows[:, k], 1.0)
    np.testing.assert_array_equal(tail_rows[:, k + 1 :], np.eye(s))
    assert np.all(f.row_lower[1 : 1 + s] == 0) and np.all(f.row_upper[1 : 1 + s] == np.inf)
    limit = dense[-1]
    assert limit[k] == 1.0
    np.testing.assert_allclose(limit[k + 1 :], 1 / ((1 - 0.75) * s))
    assert f.row_upper[-1] == pytest.approx(3.0 / f.value_scale)


def test_empty_portfolio_feasibility_flag():
    assert build_formulation(_problem(max_cvar_loss=0.0, costs=[1, 1, 1], budget=0)).empty_portfolio_feasible
    assert not build_formulation(_problem(max_cvar_loss=-1.0)).empty_portfolio_feasible
    assert not build_formulation(_problem(costs=[1, 1, 1], budget=-1)).empty_portfolio_feasible


def test_sample_indices_are_deterministic_and_even():
    assert sample_indices(100, 100) is None
    idx = sample_indices(10_000, 2_000)
    assert idx is not None and idx.size == 2_000 and idx[0] == 0 and idx[-1] == 9_999
    np.testing.assert_array_equal(idx, sample_indices(10_000, 2_000))
    assert set(np.diff(idx)) <= {4, 5, 6}


def test_subsampled_formulation_has_note_and_fewer_rows():
    scenarios = np.random.default_rng(0).normal(size=(1_000, 2))
    problem = PortfolioProblem.from_inputs(scenarios, ["a", "b"], max_cvar_loss=1.0)
    f = build_formulation(problem, max_scenarios=100)
    assert f.sample_indices is not None and f.sample_indices.size == 100
    assert f.n_vars == 2 + 1 + 100
    assert any("100 of 1,000 scenarios" in note for note in f.notes)


def test_budget_without_costs_is_noted():
    f = build_formulation(_problem(budget=10))
    assert any("without option costs" in note for note in f.notes)


def test_constraint_violations_reports_each_kind():
    problem = _problem(costs=[5, 5, 5], budget=6, exclusive_groups=[["a", "b"]], max_cvar_loss=-100.0)
    f = build_formulation(problem)
    issues = constraint_violations(problem, f, np.array([True, True, False]))
    assert [issue.split(" ")[0] for issue in issues] == ["budget", "more", "CVaR"]
    # Choosing nothing respects budget and groups, but its CVaR of 0 is above the -100 limit.
    (only,) = constraint_violations(problem, f, np.zeros(3, dtype=bool))
    assert only.startswith("CVaR limit exceeded")
