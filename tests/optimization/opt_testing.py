"""Helpers for optimization tests: brute force, reference CVaR, a fake ``cuopt`` module."""

from __future__ import annotations

import itertools
import types
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import numpy as np
import scipy.sparse as sp
from scipy.optimize import Bounds, LinearConstraint, milp


def ru_cvar(losses: np.ndarray, alpha: float) -> float:
    """Reference CVaR: minimize the Rockafellar–Uryasev objective over eta directly.

    The objective is convex and piecewise linear in eta with kinks at the sample values, so the
    minimum over the sample values is exact. Independent of ``metrics.cvar_of_loss``.
    """
    losses = np.asarray(losses, dtype=float)
    return min(eta + np.maximum(losses - eta, 0.0).mean() / (1 - alpha) for eta in losses)


@dataclass
class BruteForce:
    selected: np.ndarray
    expected_value: float
    cvar: float


def brute_force(
    scenarios: np.ndarray,
    *,
    costs: Sequence[float] | None = None,
    budget: float | None = None,
    groups: Sequence[Sequence[int]] = (),
    alpha: float = 0.95,
    max_cvar_loss: float | None = None,
) -> BruteForce | None:
    """Enumerate every subset; return the feasible one with the highest mean, or None."""
    k = scenarios.shape[1]
    best: BruteForce | None = None
    for bits in itertools.product((False, True), repeat=k):
        mask = np.array(bits)
        if budget is not None and costs is not None and float(np.dot(costs, mask)) > budget + 1e-9:
            continue
        if any(mask[list(g)].sum() > 1 for g in groups):
            continue
        values = scenarios @ mask.astype(float)
        cvar = ru_cvar(-values, alpha)
        if max_cvar_loss is not None and cvar > max_cvar_loss + 1e-9:
            continue
        ev = float(values.mean())
        if best is None or ev > best.expected_value:
            best = BruteForce(mask, ev, cvar)
    return best


def solve_canonical(
    c: np.ndarray,
    a: sp.csr_matrix,
    row_lower: np.ndarray,
    row_upper: np.ndarray,
    var_lower: np.ndarray,
    var_upper: np.ndarray,
    integer: np.ndarray,
    maximize: bool,
) -> np.ndarray | None:
    """Solve whatever a fake solver received with HiGHS, proving the encoding is faithful."""
    sign = -1.0 if maximize else 1.0
    res = milp(
        sign * np.asarray(c, dtype=float),
        integrality=np.asarray(integer, dtype=int),
        bounds=Bounds(var_lower, var_upper),
        constraints=LinearConstraint(a, row_lower, row_upper),
        options={"mip_rel_gap": 1e-9},
    )
    return res.x if res.status == 0 else None


class FakeMILPStatus(IntEnum):
    """Mirrors cuopt.linear_programming.solver.solver_wrapper.MILPTerminationStatus."""

    NoTermination = 0
    Optimal = 1
    FeasibleFound = 8
    Infeasible = 2
    Unbounded = 3
    TimeLimit = 5
    UnboundedOrInfeasible = 11


@dataclass
class FakeCuopt:
    """Stand-in for ``cuopt.linear_programming`` that records calls and solves with HiGHS.

    ``status`` forces the termination status; ``primal`` overrides the returned vector.
    """

    status: str = "Optimal"
    primal: list[float] | None = None
    error_status: int = 0
    error_message: str = ""
    raise_on_solve: Exception | None = None
    data_models: list[Any] = field(default_factory=list)
    settings: list[Any] = field(default_factory=list)

    def module(self) -> types.ModuleType:
        fake = self

        class DataModel:
            def __init__(self) -> None:
                self.calls: dict[str, tuple[Any, ...]] = {}
                fake.data_models.append(self)

            def __getattr__(self, name: str) -> Any:
                if not name.startswith("set_"):
                    raise AttributeError(name)

                def setter(*args: Any) -> None:
                    self.calls[name] = args

                return setter

        class SolverSettings:
            def __init__(self) -> None:
                self.params: dict[str, Any] = {}
                fake.settings.append(self)

            def set_parameter(self, name: str, value: Any) -> None:
                self.params[name] = value

        class Solution:
            def __init__(self, status: FakeMILPStatus, primal: np.ndarray | None) -> None:
                self._status = status
                self._primal = primal

            def get_termination_status(self) -> FakeMILPStatus:
                return self._status

            def get_primal_solution(self) -> np.ndarray | None:
                return self._primal

            def get_milp_stats(self) -> dict[str, float]:
                return {"mip_gap": 0.0 if self._status.name == "Optimal" else 0.05}

            def get_error_status(self) -> int:
                return fake.error_status

            def get_error_message(self) -> str:
                return fake.error_message

        def Solve(dm: DataModel, settings: SolverSettings) -> Solution:  # noqa: N802 - cuOpt's name
            if fake.raise_on_solve is not None:
                raise fake.raise_on_solve
            values, indices, offsets = dm.calls["set_csr_constraint_matrix"]
            (c,) = dm.calls["set_objective_coefficients"]
            a = sp.csr_matrix((values, indices, offsets), shape=(len(offsets) - 1, len(c)))
            x = solve_canonical(
                c,
                a,
                dm.calls["set_constraint_lower_bounds"][0],
                dm.calls["set_constraint_upper_bounds"][0],
                dm.calls["set_variable_lower_bounds"][0],
                dm.calls["set_variable_upper_bounds"][0],
                dm.calls["set_variable_types"][0] == "I",
                dm.calls["set_maximize"][0],
            )
            status = FakeMILPStatus[fake.status]
            if fake.primal is not None:
                x = np.asarray(fake.primal, dtype=float)
            elif status.name not in ("Optimal", "FeasibleFound"):
                x = None
            return Solution(status, x)

        lp = types.ModuleType("cuopt.linear_programming")
        lp.DataModel = DataModel  # type: ignore[attr-defined]
        lp.SolverSettings = SolverSettings  # type: ignore[attr-defined]
        lp.Solve = Solve  # type: ignore[attr-defined]
        return lp


def random_instance(seed: int, n_scenarios: int = 120, n_options: int = 6) -> dict[str, Any]:
    """A small random problem with a binding-ish budget and two exclusive pairs."""
    rng = np.random.default_rng(seed)
    means = rng.uniform(-20, 40, n_options)
    stds = rng.uniform(5, 80, n_options)
    scenarios = rng.normal(means, stds, size=(n_scenarios, n_options))
    costs = rng.uniform(1, 10, n_options).round(2)
    return {
        "scenarios": scenarios,
        "names": [f"opt_{i}" for i in range(n_options)],
        "costs": costs,
        "budget": float(costs.sum() * 0.5),
        "groups": [[0, 1], [2, 3]],
    }
