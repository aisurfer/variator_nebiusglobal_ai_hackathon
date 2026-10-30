"""Risk-constrained portfolio optimization over Monte Carlo scenarios (NVIDIA cuOpt, HiGHS fallback)."""

from variator.optimization.api import ScenarioRun, SolverChoice, optimize_decision, optimize_portfolio
from variator.optimization.cuopt_server import CuoptServerClient, CuoptServerConfig, CuoptServerError
from variator.optimization.formulation import MAX_CVAR_SCENARIOS
from variator.optimization.metrics import cvar_of_loss

__all__ = [
    "MAX_CVAR_SCENARIOS",
    "CuoptServerClient",
    "CuoptServerConfig",
    "CuoptServerError",
    "ScenarioRun",
    "SolverChoice",
    "cvar_of_loss",
    "optimize_decision",
    "optimize_portfolio",
]
