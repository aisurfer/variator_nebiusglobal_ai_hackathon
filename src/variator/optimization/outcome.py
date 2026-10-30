"""What every solver adapter returns: a status plus the raw variable vector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

SolveStatus = Literal["optimal", "feasible", "infeasible", "unbounded", "time_limit", "error"]
SolverName = Literal["cuopt", "highs"]

# Statuses that come with a usable variable vector.
SOLUTION_STATUSES: frozenset[str] = frozenset({"optimal", "feasible"})

# cuOpt MILPTerminationStatus names (Python API enum names and HTTP "status" strings), see
# python/cuopt/cuopt/linear_programming/solver/solver_wrapper.pyx and
# cpp/src/mip_heuristics/solution/solution.cu at https://github.com/NVIDIA/cuopt/tree/v26.10.00
_CUOPT_STATUS: dict[str, SolveStatus] = {
    "Optimal": "optimal",
    "FeasibleFound": "feasible",
    "Infeasible": "infeasible",
    # The objective only involves bounded binaries, so "unbounded or infeasible" means infeasible.
    "UnboundedOrInfeasible": "infeasible",
    "Unbounded": "unbounded",
    # cuOpt reports TimeLimit only when no feasible solution was found (FeasibleFound otherwise).
    "TimeLimit": "time_limit",
    "WorkLimit": "time_limit",
}


def cuopt_status(name: str) -> SolveStatus:
    """Map a cuOpt MILP termination status name to our status vocabulary."""
    return _CUOPT_STATUS.get(name, "error")


@dataclass(frozen=True)
class SolverOutcome:
    solver: SolverName
    status: SolveStatus
    x: np.ndarray | None
    proven_optimal: bool
    message: str = ""
    mip_gap: float | None = None

    @property
    def has_solution(self) -> bool:
        return self.status in SOLUTION_STATUSES and self.x is not None
