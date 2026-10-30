"""Monte Carlo simulation of a ``DecisionModel`` on NumPy (CPU) or CuPy (GPU).

Main entry point: ``simulate(model, n_scenarios=..., seed=..., backend=...)``.
"""

from variator.expr import CompiledFormula, FormulaError, compile_formula
from variator.simulation.backend import (
    AUTO_GPU_MIN_SCENARIOS,
    Backend,
    BackendUnavailableError,
    cupy_available,
    get_backend,
    to_host,
)
from variator.simulation.copula import (
    CorrelationRepairWarning,
    GaussianCopula,
    build_copula,
    nearest_positive_definite,
    spearman_to_pearson,
)
from variator.simulation.distributions import (
    analytic_mean,
    lognormal_log_params,
    pert_shape,
    ppf,
    quantile,
)
from variator.simulation.engine import NonFiniteMetricError, SimulationRun, simulate
from variator.simulation.sampling import Sampling, draw_uniforms
from variator.simulation.tensor import TensorRun, simulate_tensor

__all__ = [
    "AUTO_GPU_MIN_SCENARIOS",
    "Backend",
    "BackendUnavailableError",
    "CompiledFormula",
    "CorrelationRepairWarning",
    "FormulaError",
    "GaussianCopula",
    "NonFiniteMetricError",
    "Sampling",
    "SimulationRun",
    "TensorRun",
    "analytic_mean",
    "build_copula",
    "compile_formula",
    "cupy_available",
    "draw_uniforms",
    "get_backend",
    "lognormal_log_params",
    "nearest_positive_definite",
    "pert_shape",
    "ppf",
    "quantile",
    "simulate",
    "simulate_tensor",
    "spearman_to_pearson",
    "to_host",
]
