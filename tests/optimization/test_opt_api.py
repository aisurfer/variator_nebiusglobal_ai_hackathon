"""Solver selection (auto / cuopt / highs) and fallback to HiGHS."""

from dataclasses import replace

import numpy as np
import pytest

from variator.optimization import cuopt_local, cuopt_server, formulation, optimize_portfolio
from variator.optimization.highs import solve_highs
from variator.optimization.outcome import SolverOutcome


def _tail_case(n=100):
    a = np.full(n, 130.0)
    a[::20] = -470.0
    return np.column_stack([a, np.full(n, 60.0)])


def _as_cuopt(status="optimal"):
    """A stand-in cuOpt solver that answers with HiGHS' solution but cuOpt's label."""

    def solve(milp, *, time_limit_s):
        solve.calls.append((milp, time_limit_s))
        outcome = solve_highs(milp, time_limit_s=time_limit_s)
        proven = status == "optimal"
        return replace(outcome, solver="cuopt", status=status, proven_optimal=proven, mip_gap=0.01)

    solve.calls = []
    return solve


@pytest.fixture
def local_gpu(monkeypatch):
    solver = _as_cuopt()
    monkeypatch.setattr(cuopt_local, "is_available", lambda: True)
    monkeypatch.setattr(cuopt_local, "solve_cuopt_local", solver)
    return solver


def _fail(*_args, **_kwargs):
    raise AssertionError("this backend must not be used")


def test_auto_prefers_local_cuopt(local_gpu, monkeypatch):
    monkeypatch.setenv(cuopt_server.SERVER_URL_ENV, "https://unused.example")
    monkeypatch.setattr(cuopt_server, "solve_cuopt_server", _fail)
    result = optimize_portfolio(_tail_case(), ["a", "b"], max_cvar_loss=0.0, time_limit_s=9)
    assert result.solver == "cuopt" and result.selected == ["b"] and result.proven_optimal
    assert local_gpu.calls[0][1] == 9
    (note,) = result.notes
    assert "local GPU, MIP gap 1.00%" in note and "beta" in note and "optimal within" in note


def test_auto_uses_server_when_no_local_gpu(monkeypatch):
    server = _as_cuopt()
    monkeypatch.setenv(cuopt_server.SERVER_URL_ENV, "https://cuopt.example")
    monkeypatch.setattr(cuopt_server, "solve_cuopt_server", server)
    monkeypatch.setattr(cuopt_local, "solve_cuopt_local", _fail)
    result = optimize_portfolio(_tail_case(), ["a", "b"])
    assert result.solver == "cuopt" and len(server.calls) == 1
    assert "cuOpt server" in result.notes[0]


def test_auto_without_cuopt_uses_highs_silently(monkeypatch):
    monkeypatch.setattr(cuopt_local, "solve_cuopt_local", _fail)
    monkeypatch.setattr(cuopt_server, "solve_cuopt_server", _fail)
    result = optimize_portfolio(_tail_case(), ["a", "b"])
    assert result.solver == "highs" and result.notes == []


def test_explicit_highs_skips_cuopt(local_gpu):
    result = optimize_portfolio(_tail_case(), ["a", "b"], solver="highs")
    assert result.solver == "highs" and local_gpu.calls == []


def test_explicit_cuopt_without_any_backend_notes_the_fallback():
    result = optimize_portfolio(_tail_case(), ["a", "b"], solver="cuopt")
    assert result.solver == "highs"
    assert any("cuOpt was requested" in note for note in result.notes)


def test_cuopt_exception_falls_back(monkeypatch):
    def boom(milp, *, time_limit_s):
        raise RuntimeError("CUDA driver version is insufficient")

    monkeypatch.setattr(cuopt_local, "is_available", lambda: True)
    monkeypatch.setattr(cuopt_local, "solve_cuopt_local", boom)
    result = optimize_portfolio(_tail_case(), ["a", "b"], max_cvar_loss=0.0)
    assert result.solver == "highs" and result.selected == ["b"] and result.status == "optimal"
    (note,) = result.notes
    assert "failed: RuntimeError: CUDA driver" in note and "fell back to HiGHS" in note


def _cuopt(status, x=None, message=""):
    return SolverOutcome("cuopt", status, x, status == "optimal", message)


def _returning(outcome_factory):
    def solve(milp, *, time_limit_s):
        return outcome_factory(milp)

    return solve


@pytest.mark.parametrize(
    ("factory", "reason"),
    [
        (lambda m: _cuopt("time_limit", message="TimeLimit"), "status 'time_limit'"),
        (lambda m: _cuopt("error", message="NoTermination"), "status 'error'"),
        (lambda m: _cuopt("infeasible"), "reported infeasible although"),
        (lambda m: _cuopt("optimal"), "no solution vector"),
        # picks both options: violates the exclusive group and the CVaR limit
        (lambda m: _cuopt("optimal", np.r_[1.0, 1.0, np.zeros(m.n_vars - 2)]), "violates"),
    ],
    ids=["time-limit", "error", "false-infeasible", "no-vector", "violation"],
)
def test_untrustworthy_cuopt_outcomes_fall_back(monkeypatch, factory, reason):
    monkeypatch.setattr(cuopt_local, "is_available", lambda: True)
    monkeypatch.setattr(cuopt_local, "solve_cuopt_local", _returning(factory))
    result = optimize_portfolio(_tail_case(), ["a", "b"], exclusive_groups=[["a", "b"]], max_cvar_loss=0.0)
    assert result.solver == "highs" and result.selected == ["b"]
    assert any(reason in note and "fell back to HiGHS" in note for note in result.notes)


def test_true_infeasibility_from_cuopt_is_trusted(monkeypatch):
    monkeypatch.setattr(cuopt_local, "is_available", lambda: True)
    monkeypatch.setattr(cuopt_local, "solve_cuopt_local", _returning(lambda m: _cuopt("infeasible")))
    result = optimize_portfolio(_tail_case(), ["a", "b"], max_cvar_loss=-1_000.0)
    assert result.solver == "cuopt" and result.status == "infeasible" and result.selected == []


def test_feasible_cuopt_solution_is_near_optimal(monkeypatch):
    monkeypatch.setattr(cuopt_local, "is_available", lambda: True)
    monkeypatch.setattr(cuopt_local, "solve_cuopt_local", _as_cuopt(status="feasible"))
    result = optimize_portfolio(_tail_case(), ["a", "b"], time_limit_s=2)
    assert result.solver == "cuopt" and result.status == "feasible" and not result.proven_optimal
    assert any("near-optimal" in note for note in result.notes)
    assert any("2s time limit" in note for note in result.notes)


def test_scenario_cap_depends_on_the_solver(local_gpu, monkeypatch):
    monkeypatch.setitem(formulation.MAX_CVAR_SCENARIOS, "cuopt", 300)
    monkeypatch.setitem(formulation.MAX_CVAR_SCENARIOS, "highs", 50)
    result = optimize_portfolio(_tail_case(400), ["a", "b"], max_cvar_loss=0.0)
    milp, _ = local_gpu.calls[0]
    assert milp.sample_indices.size == 300
    assert any("300 of 400 scenarios" in note for note in result.notes)


def test_fallback_rebuilds_with_the_highs_cap(monkeypatch):
    monkeypatch.setitem(formulation.MAX_CVAR_SCENARIOS, "cuopt", 300)
    monkeypatch.setitem(formulation.MAX_CVAR_SCENARIOS, "highs", 50)
    monkeypatch.setattr(cuopt_local, "is_available", lambda: True)
    monkeypatch.setattr(cuopt_local, "solve_cuopt_local", _returning(lambda m: _cuopt("error")))
    result = optimize_portfolio(_tail_case(400), ["a", "b"], max_cvar_loss=0.0)
    assert result.solver == "highs"
    assert any("50 of 400 scenarios" in note for note in result.notes)
    assert not any("300 of 400" in note for note in result.notes)
