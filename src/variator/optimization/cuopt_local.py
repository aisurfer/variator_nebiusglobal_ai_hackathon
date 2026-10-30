"""GPU solver: NVIDIA cuOpt Python API, in-process.

``cuopt`` is imported lazily: it is an optional extra (``cuopt-cu12``) that needs an NVIDIA GPU.
The matrix-level ``DataModel`` API is used because our formulation already is a CSR matrix.
API verified against https://github.com/NVIDIA/cuopt/tree/v26.10.00/python/cuopt/cuopt/linear_programming
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from types import ModuleType

import numpy as np

from variator.optimization.formulation import MilpFormulation
from variator.optimization.outcome import SolverOutcome, cuopt_status

# Solver parameter name, see CUOPT_TIME_LIMIT in
# cpp/include/cuopt/mathematical_optimization/constants.h (v26.10.00).
TIME_LIMIT_PARAM = "time_limit"


class CuoptUnavailableError(RuntimeError):
    """The cuOpt Python package cannot be imported (not installed, or no CUDA runtime)."""


def gpu_available() -> bool:
    """Best-effort check for a visible NVIDIA GPU without importing CUDA libraries."""
    if os.environ.get("CUDA_VISIBLE_DEVICES", None) in ("", "-1"):
        return False
    return _nvidia_smi_lists_gpu()


@functools.lru_cache(maxsize=1)
def _nvidia_smi_lists_gpu() -> bool:
    # ASSUMPTION: GPU containers (Nebius Serverless, NVIDIA Container Toolkit) expose nvidia-smi,
    # see https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        return False
    try:
        out = subprocess.run(
            [nvidia_smi, "-L"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and "GPU" in out.stdout


def _cuopt_installed() -> bool:
    if "cuopt" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("cuopt") is not None
    except (ImportError, ValueError):
        return False


def is_available() -> bool:
    """True when the ``cuopt`` package is installed and a GPU is visible."""
    return _cuopt_installed() and gpu_available()


def _linear_programming() -> ModuleType:
    try:
        return importlib.import_module("cuopt.linear_programming")
    except Exception as exc:  # ImportError, or CUDA/driver errors raised while importing
        raise CuoptUnavailableError(f"cannot import cuopt.linear_programming: {exc}") from exc


def build_data_model(lp: ModuleType, formulation: MilpFormulation) -> object:
    """Fill a ``cuopt.linear_programming.DataModel`` from the canonical formulation."""
    f = formulation
    dm = lp.DataModel()
    # DataModel docs: CSR values float64, indices/offsets int32.
    dm.set_csr_constraint_matrix(
        f.a.data.astype(np.float64), f.a.indices.astype(np.int32), f.a.indptr.astype(np.int32)
    )
    # Row bounds rather than b + row types: the same calls the cuOpt server makes
    # (python/cuopt_server/cuopt_server/utils/linear_programming/conversion.py).
    dm.set_constraint_lower_bounds(f.row_lower.astype(np.float64))
    dm.set_constraint_upper_bounds(f.row_upper.astype(np.float64))
    dm.set_objective_coefficients(f.c.astype(np.float64))
    dm.set_maximize(False)
    dm.set_variable_lower_bounds(f.var_lower.astype(np.float64))
    dm.set_variable_upper_bounds(f.var_upper.astype(np.float64))
    dm.set_variable_types(np.where(f.integrality == 1, "I", "C"))
    return dm


def solve_cuopt_local(formulation: MilpFormulation, *, time_limit_s: float) -> SolverOutcome:
    """Solve on the local GPU. MILP is beta in cuOpt: only its "Optimal" status counts as proven.

    Raises ``CuoptUnavailableError`` when cuOpt cannot be imported; cuOpt's own exceptions
    propagate so the caller can fall back to HiGHS.
    """
    lp = _linear_programming()
    dm = build_data_model(lp, formulation)
    settings = lp.SolverSettings()
    settings.set_parameter(TIME_LIMIT_PARAM, float(time_limit_s))
    solution = lp.Solve(dm, settings)

    error = _error_message(solution)
    if error is not None:
        return SolverOutcome(solver="cuopt", status="error", x=None, proven_optimal=False, message=error)

    raw_status = solution.get_termination_status()
    name = getattr(raw_status, "name", str(raw_status))
    status = cuopt_status(name)
    x = None
    if status in ("optimal", "feasible"):
        primal = solution.get_primal_solution()
        x = None if primal is None else np.asarray(primal, dtype=np.float64)
        if x is None or x.size != formulation.n_vars:
            size = None if x is None else x.size
            return SolverOutcome(
                solver="cuopt",
                status="error",
                x=None,
                proven_optimal=False,
                message=f"cuOpt returned {size} values for {formulation.n_vars} variables",
            )
    return SolverOutcome(
        solver="cuopt",
        status=status,
        x=x,
        proven_optimal=status == "optimal",
        message=f"cuOpt termination status {name}",
        mip_gap=_mip_gap(solution),
    )


def _error_message(solution: object) -> str | None:
    get_status = getattr(solution, "get_error_status", None)
    if get_status is None:
        return None
    err = get_status()
    # ErrorStatus.Success is 0 (solver_wrapper.pyx); plain ints appear when no error happened.
    if err is None or int(err) == 0:
        return None
    get_message = getattr(solution, "get_error_message", None)
    detail = get_message() if get_message is not None else ""
    return f"cuOpt error {getattr(err, 'name', err)}: {detail}".rstrip(": ")


def _mip_gap(solution: object) -> float | None:
    try:
        gap = solution.get_milp_stats().get("mip_gap")  # type: ignore[attr-defined]
    except Exception:  # stats are optional diagnostics; never fail a solve over them
        return None
    return None if gap is None else float(gap)
