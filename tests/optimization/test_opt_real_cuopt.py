"""Real cuOpt runs. Skipped unless a GPU with cuopt is present (``gpu``) or ``--live`` with a server.

On a GPU box: ``pip install --extra-index-url=https://pypi.nvidia.com cuopt-cu12`` (plus CuPy, which
the shared conftest uses to detect the GPU), then ``pytest tests/optimization -m gpu``.
For the server: set ``CUOPT_SERVER_URL`` (and ``CUOPT_SERVER_TOKEN``) and run with ``--live``.
"""

import os

import numpy as np
import pytest
from opt_testing import brute_force, random_instance

from variator.optimization import cuopt_local, optimize_portfolio
from variator.optimization.cuopt_server import solve_cuopt_server
from variator.optimization.formulation import PortfolioProblem, build_formulation


def _instance_kwargs(seed):
    inst = random_instance(seed, n_scenarios=300, n_options=6)
    names = inst["names"]
    constraints = {"costs": inst["costs"], "budget": inst["budget"], "groups": inst["groups"]}
    free = brute_force(inst["scenarios"], **constraints)
    limit = 0.5 * free.cvar if free.cvar > 0 else None
    kwargs = {
        "costs": list(inst["costs"]),
        "budget": inst["budget"],
        "exclusive_groups": [[names[i] for i in g] for g in inst["groups"]],
        "max_cvar_loss": limit,
    }
    expected = brute_force(inst["scenarios"], **constraints, max_cvar_loss=limit)
    return inst, kwargs, expected


@pytest.mark.gpu
@pytest.mark.parametrize("seed", range(3))
def test_local_cuopt_matches_brute_force(seed):
    pytest.importorskip("cuopt")
    assert cuopt_local.is_available()
    inst, kwargs, expected = _instance_kwargs(seed)
    result = optimize_portfolio(inst["scenarios"], inst["names"], solver="cuopt", time_limit_s=20, **kwargs)
    assert result.solver == "cuopt", result.notes  # no silent fallback
    assert result.status in ("optimal", "feasible")
    assert result.expected_value == pytest.approx(expected.expected_value, rel=1e-4)


@pytest.mark.gpu
def test_local_cuopt_at_scale():
    """The case the GPU is for: 10,000 scenario rows in the CVaR block."""
    pytest.importorskip("cuopt")
    rng = np.random.default_rng(0)
    k = 12
    scenarios = rng.normal(rng.uniform(0, 50, k), rng.uniform(10, 200, k), size=(10_000, k))
    result = optimize_portfolio(scenarios, [f"o{i}" for i in range(k)], max_cvar_loss=100.0, solver="cuopt")
    assert result.solver == "cuopt", result.notes
    assert result.cvar is not None and -result.cvar <= 100.0 + 1e-6


@pytest.mark.live
def test_cuopt_server_solves_a_small_milp():
    if not os.environ.get("CUOPT_SERVER_URL"):
        pytest.skip("CUOPT_SERVER_URL is not set")
    inst, kwargs, expected = _instance_kwargs(0)
    problem = PortfolioProblem.from_inputs(inst["scenarios"], inst["names"], **kwargs)
    f = build_formulation(problem)
    outcome = solve_cuopt_server(f, time_limit_s=10)
    assert outcome.status in ("optimal", "feasible"), outcome.message
    chosen = f.selection(outcome.x)
    assert float((inst["scenarios"] @ chosen).mean()) == pytest.approx(expected.expected_value, rel=1e-4)
