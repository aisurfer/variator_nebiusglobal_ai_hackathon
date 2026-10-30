"""Sampling by inverse CDF: ``x = F^-1(u)`` for uniforms ``u`` in [0, 1].

Driving every distribution from uniforms is what makes correlations (Gaussian copula)
and common random numbers across options work: the same ``u`` column can be pushed
through different marginals and stays rank-aligned.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from variator.domain import Distribution, DistributionKind
from variator.simulation.backend import Backend, get_backend

PERT_LAMBDA = 4.0

# Unbounded marginals clip u away from 0/1 so ndtri stays finite (about +-7 sigma).
_U_TINY = 1e-12

_NUMPY = get_backend("numpy")


def lognormal_log_params(mean: float, std: float) -> tuple[float, float]:
    """Convert the mean/std of a lognormal variable into (mu, sigma) of its logarithm."""
    if mean <= 0:
        raise ValueError("lognormal mean must be > 0")
    sigma2 = math.log1p((std / mean) ** 2)
    return math.log(mean) - sigma2 / 2, math.sqrt(sigma2)


def pert_shape(low: float, mode: float, high: float) -> tuple[float, float]:
    """Beta shape parameters (alpha, beta) of a PERT distribution with lambda=4."""
    span = high - low
    return 1 + PERT_LAMBDA * (mode - low) / span, 1 + PERT_LAMBDA * (high - mode) / span


def is_constant(dist: Distribution) -> bool:
    """True if the distribution has a single possible value."""
    match dist.kind:
        case DistributionKind.FIXED:
            return True
        case DistributionKind.NORMAL | DistributionKind.LOGNORMAL:
            return dist.std == 0
        case DistributionKind.UNIFORM | DistributionKind.TRIANGULAR | DistributionKind.PERT:
            return dist.low == dist.high
        case DistributionKind.BERNOULLI:
            return dist.p in (0.0, 1.0)
    return False


def ppf(dist: Distribution, u: Any, backend: Backend = _NUMPY) -> Any:
    """Inverse CDF of ``dist`` evaluated at uniforms ``u`` (array on ``backend``).

    Returns a float64 array of the same shape. Lognormal ``mean``/``std`` describe the
    variable itself, not its logarithm. Bernoulli maps the top ``p`` share of ``u`` to 1,
    so that higher ``u`` never gives a lower value (keeps rank correlations meaningful).
    """
    xp = backend.xp
    u = xp.asarray(u, dtype=xp.float64)
    match dist.kind:
        case DistributionKind.FIXED:
            return xp.full(u.shape, dist.value, dtype=xp.float64)
        case DistributionKind.NORMAL:
            return dist.mean + dist.std * backend.ndtri(xp.clip(u, _U_TINY, 1 - _U_TINY))
        case DistributionKind.LOGNORMAL:
            mu, sigma = lognormal_log_params(dist.mean, dist.std)
            return xp.exp(mu + sigma * backend.ndtri(xp.clip(u, _U_TINY, 1 - _U_TINY)))
        case DistributionKind.UNIFORM:
            return dist.low + (dist.high - dist.low) * u
        case DistributionKind.TRIANGULAR:
            return _triangular_ppf(dist.low, dist.mode, dist.high, u, xp)
        case DistributionKind.PERT:
            if dist.low == dist.high:
                return xp.full(u.shape, dist.low, dtype=xp.float64)
            alpha, beta = pert_shape(dist.low, dist.mode, dist.high)
            return dist.low + (dist.high - dist.low) * backend.betaincinv(alpha, beta, u)
        case DistributionKind.BERNOULLI:
            return (u > 1 - dist.p).astype(xp.float64)
    raise ValueError(f"unsupported distribution kind {dist.kind!r}")


def quantile(dist: Distribution, q: float) -> float:
    """Scalar quantile of ``dist`` at probability ``q`` (computed on the host)."""
    return float(ppf(dist, np.array([q]))[0])


def analytic_mean(dist: Distribution) -> float:
    """Analytic mean of ``dist`` (used for checks and reporting)."""
    match dist.kind:
        case DistributionKind.FIXED:
            return float(dist.value)
        case DistributionKind.NORMAL | DistributionKind.LOGNORMAL:
            return float(dist.mean)
        case DistributionKind.UNIFORM:
            return (dist.low + dist.high) / 2
        case DistributionKind.TRIANGULAR:
            return (dist.low + dist.mode + dist.high) / 3
        case DistributionKind.PERT:
            return (dist.low + PERT_LAMBDA * dist.mode + dist.high) / (PERT_LAMBDA + 2)
        case DistributionKind.BERNOULLI:
            return float(dist.p)
    raise ValueError(f"unsupported distribution kind {dist.kind!r}")


def _triangular_ppf(low: float, mode: float, high: float, u: Any, xp: Any) -> Any:
    span = high - low
    if span == 0:
        return xp.full(u.shape, low, dtype=xp.float64)
    split = (mode - low) / span
    left = low + xp.sqrt(u * span * (mode - low))
    right = high - xp.sqrt((1 - u) * span * (high - mode))
    return xp.where(u < split, left, right)
