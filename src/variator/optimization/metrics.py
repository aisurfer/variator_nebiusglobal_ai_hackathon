"""Exact portfolio metrics on a scenario matrix.

Solvers may work on a scenario sample and with scaled coefficients; everything the
user sees is recomputed here on the full matrix in original units.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def cvar_of_loss(losses: np.ndarray, alpha: float) -> float:
    """Return CVaR_alpha of an equally weighted loss sample: the mean of the worst ``1 - alpha`` tail.

    Uses the Rockafellar–Uryasev definition ``min_eta eta + E[(L - eta)+] / (1 - alpha)``. When
    ``(1 - alpha) * n`` is not an integer the boundary scenario enters with a fractional weight,
    so this is exactly the quantity the MILP's linearized CVaR constraint bounds.
    """
    sample = np.asarray(losses, dtype=np.float64).ravel()
    n = sample.size
    if n == 0:
        raise ValueError("loss sample is empty")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    tail = (1.0 - alpha) * n
    # 1e-9 absorbs float noise such as 0.05 * 200 = 10.000000000000002
    k = min(int(np.floor(tail + 1e-9)), n - 1)
    frac = max(tail - k, 0.0)
    boundary = n - k - 1
    part = np.partition(sample, boundary)
    return float((part[boundary + 1 :].sum() + frac * part[boundary]) / tail)


@dataclass(frozen=True)
class PortfolioMetrics:
    expected_value: float
    cvar_loss: float
    budget_used: float | None


def evaluate_portfolio(
    scenarios: np.ndarray,
    selected: np.ndarray,
    *,
    alpha: float,
    costs: np.ndarray | None,
) -> PortfolioMetrics:
    """Compute expected value, CVaR of loss and budget used of a selection on all scenarios.

    ``selected`` is a boolean mask over options; portfolio value in a scenario is the sum of the
    selected options' metrics, and loss is its negation.
    """
    values = scenarios @ selected.astype(np.float64)
    return PortfolioMetrics(
        expected_value=float(values.mean()),
        cvar_loss=cvar_of_loss(-values, alpha),
        budget_used=None if costs is None else float(costs @ selected.astype(np.float64)),
    )
