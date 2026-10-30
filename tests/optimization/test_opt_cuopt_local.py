"""cuOpt Python API adapter, exercised with a fake ``cuopt.linear_programming`` module."""

import subprocess

import numpy as np
import pytest
from opt_testing import random_instance

from variator.optimization import cuopt_local, optimize_portfolio
from variator.optimization.cuopt_local import CuoptUnavailableError, solve_cuopt_local
from variator.optimization.cuopt_local import gpu_available as real_gpu_available
from variator.optimization.formulation import PortfolioProblem, build_formulation
from variator.optimization.highs import solve_highs


def _formulation(seed=3, max_cvar_loss=20.0):
    inst = random_instance(seed, n_scenarios=60, n_options=5)
    names = inst["names"]
    problem = PortfolioProblem.from_inputs(
        inst["scenarios"],
        names,
        costs=list(inst["costs"]),
        budget=inst["budget"],
        exclusive_groups=[[names[i] for i in g] for g in inst["groups"]],
        max_cvar_loss=max_cvar_loss,
    )
    return problem, build_formulation(problem)


def test_data_model_receives_the_formulation(fake_cuopt):
    _, f = _formulation()
    outcome = solve_cuopt_local(f, time_limit_s=12.5)

    (dm,) = fake_cuopt.data_models
    values, indices, offsets = dm.calls["set_csr_constraint_matrix"]
    assert values.dtype == np.float64 and indices.dtype == np.int32 and offsets.dtype == np.int32
    np.testing.assert_array_equal(values, f.a.data)
    np.testing.assert_array_equal(indices, f.a.indices)
    np.testing.assert_array_equal(offsets, f.a.indptr)
    np.testing.assert_array_equal(dm.calls["set_constraint_lower_bounds"][0], f.row_lower)
    np.testing.assert_array_equal(dm.calls["set_constraint_upper_bounds"][0], f.row_upper)
    np.testing.assert_array_equal(dm.calls["set_objective_coefficients"][0], f.c)
    np.testing.assert_array_equal(dm.calls["set_variable_lower_bounds"][0], f.var_lower)
    np.testing.assert_array_equal(dm.calls["set_variable_upper_bounds"][0], f.var_upper)
    var_types = dm.calls["set_variable_types"][0]
    assert list(var_types[: f.n_options]) == ["I"] * f.n_options
    assert set(var_types[f.n_options :]) == {"C"}
    assert dm.calls["set_maximize"] == (False,)
    assert fake_cuopt.settings[0].params == {"time_limit": 12.5}

    # The fake solves what it received with HiGHS: same answer means a faithful encoding.
    reference = solve_highs(f, time_limit_s=10)
    assert outcome.solver == "cuopt" and outcome.status == "optimal" and outcome.proven_optimal
    np.testing.assert_array_equal(f.selection(outcome.x), f.selection(reference.x))
    assert outcome.mip_gap == 0.0


def test_feasible_found_is_not_proven_optimal(fake_cuopt):
    fake_cuopt.status = "FeasibleFound"
    outcome = solve_cuopt_local(_formulation()[1], time_limit_s=1)
    assert outcome.status == "feasible" and not outcome.proven_optimal and outcome.x is not None


@pytest.mark.parametrize(
    ("cuopt_name", "status"),
    [("TimeLimit", "time_limit"), ("Infeasible", "infeasible"), ("UnboundedOrInfeasible", "infeasible"),
     ("NoTermination", "error")],
)
def test_status_mapping_without_solution(fake_cuopt, cuopt_name, status):
    fake_cuopt.status = cuopt_name
    outcome = solve_cuopt_local(_formulation()[1], time_limit_s=1)
    assert outcome.status == status and outcome.x is None and not outcome.proven_optimal


def test_error_status_is_reported(fake_cuopt):
    fake_cuopt.error_status = 4  # ErrorStatus.ValidationError
    fake_cuopt.error_message = "bad bounds"
    outcome = solve_cuopt_local(_formulation()[1], time_limit_s=1)
    assert outcome.status == "error" and "bad bounds" in outcome.message


def test_wrong_solution_length_is_an_error(fake_cuopt):
    fake_cuopt.primal = [1.0, 0.0]
    outcome = solve_cuopt_local(_formulation()[1], time_limit_s=1)
    assert outcome.status == "error" and "2 values" in outcome.message


def test_missing_package_raises_unavailable(monkeypatch):
    def broken_import(name):
        raise ImportError("libcuopt.so: cannot open shared object file")

    monkeypatch.setattr(cuopt_local.importlib, "import_module", broken_import)
    with pytest.raises(CuoptUnavailableError, match="libcuopt"):
        solve_cuopt_local(_formulation()[1], time_limit_s=1)


def test_optimize_portfolio_through_fake_cuopt_matches_highs(fake_cuopt):
    problem, _ = _formulation()
    kwargs = {
        "costs": list(problem.costs),
        "budget": problem.budget,
        "exclusive_groups": [[problem.option_names[i] for i in g] for g in problem.groups],
        "max_cvar_loss": problem.max_cvar_loss,
    }
    gpu = optimize_portfolio(problem.scenarios, problem.option_names, solver="auto", **kwargs)
    cpu = optimize_portfolio(problem.scenarios, problem.option_names, solver="highs", **kwargs)
    assert gpu.solver == "cuopt" and cpu.solver == "highs"
    assert gpu.selected == cpu.selected
    assert gpu.expected_value == pytest.approx(cpu.expected_value)
    assert any("cuOpt (local GPU" in note and "beta" in note for note in gpu.notes)


def test_is_available_needs_package_and_gpu(fake_cuopt, monkeypatch):
    assert cuopt_local.is_available()
    monkeypatch.setattr(cuopt_local, "gpu_available", lambda: False)
    assert not cuopt_local.is_available()


def test_gpu_detection(monkeypatch):
    cuopt_local._nvidia_smi_lists_gpu.cache_clear()
    monkeypatch.setattr(cuopt_local.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        cuopt_local.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="GPU 0: NVIDIA L40S (UUID: GPU-1)\n"),
    )
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    try:
        assert real_gpu_available()
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
        assert not real_gpu_available()
        cuopt_local._nvidia_smi_lists_gpu.cache_clear()
        monkeypatch.delenv("CUDA_VISIBLE_DEVICES")
        monkeypatch.setattr(cuopt_local.shutil, "which", lambda _: None)
        assert not real_gpu_available()
    finally:
        cuopt_local._nvidia_smi_lists_gpu.cache_clear()
