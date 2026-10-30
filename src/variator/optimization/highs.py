"""CPU solver: HiGHS through ``scipy.optimize.milp``. Always available, exact for our sizes."""

from __future__ import annotations

from scipy.optimize import Bounds, LinearConstraint, milp

from variator.optimization.formulation import MilpFormulation
from variator.optimization.outcome import SolverOutcome, SolveStatus

# Tighter than the HiGHS default (1e-4) so near-ties between portfolios are resolved; cheap at
# the handful of binaries a decision has.
MIP_REL_GAP = 1e-6

# scipy.optimize.milp status codes, see its docstring.
_SCIPY_STATUS: dict[int, SolveStatus] = {0: "optimal", 2: "infeasible", 3: "unbounded"}


def solve_highs(formulation: MilpFormulation, *, time_limit_s: float) -> SolverOutcome:
    """Solve the MILP with HiGHS; ``proven_optimal`` only when HiGHS reports optimal."""
    f = formulation
    res = milp(
        c=f.c,
        integrality=f.integrality,
        bounds=Bounds(f.var_lower, f.var_upper),
        constraints=LinearConstraint(f.a, f.row_lower, f.row_upper),
        options={"time_limit": float(time_limit_s), "mip_rel_gap": MIP_REL_GAP, "disp": False},
    )
    if res.status == 1:  # time or iteration limit: keep the incumbent when there is one
        status: SolveStatus = "feasible" if res.x is not None else "time_limit"
    else:
        status = _SCIPY_STATUS.get(int(res.status), "error")
    has_x = status in ("optimal", "feasible") and res.x is not None
    return SolverOutcome(
        solver="highs",
        status=status,
        x=res.x if has_x else None,
        proven_optimal=status == "optimal",
        message=str(res.message),
        mip_gap=None if res.get("mip_gap") is None else float(res.mip_gap),
    )
