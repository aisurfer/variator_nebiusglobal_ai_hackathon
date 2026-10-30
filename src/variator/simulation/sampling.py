"""Uniform input samples for the engines: pseudo-random or scrambled Sobol.

Sobol points are a low-discrepancy (quasi Monte Carlo) sequence. They fill the unit
cube more evenly than random points, so means and quantiles converge faster: the same
accuracy needs about 4 to 8 times fewer scenarios. Variable columns and risk columns
are parts of one joint sequence; with Sobol, a new risk can change the variable samples.
"""

from __future__ import annotations

import warnings
from typing import Any, Literal

import numpy as np

from variator.simulation.backend import Backend

Sampling = Literal["random", "sobol"]


def is_power_of_two(n: int) -> bool:
    return n > 0 and n & (n - 1) == 0


def draw_uniforms(
    be: Backend, seed: int | None, n: int, n_variables: int, n_risk_columns: int, sampling: Sampling
) -> tuple[Any, Any, list[str]]:
    """Return (variable uniforms, risk uniforms, notes) as arrays on ``be``."""
    if sampling == "random":
        rng = be.rng(seed)
        uniforms = rng.random((n, n_variables), dtype=be.xp.float64)
        # Drawn after the variables so adding a risk does not reshuffle variable samples.
        risk_uniforms = rng.random((n, n_risk_columns), dtype=be.xp.float64)
        return uniforms, risk_uniforms, []
    if sampling != "sobol":
        raise ValueError(f"unknown sampling {sampling!r}; use 'random' or 'sobol'")
    notes = [] if is_power_of_two(n) else [f"sobol: n={n} is not a power of 2; the balance is weaker"]
    points = _sobol(n, n_variables + n_risk_columns, seed)
    return be.asarray(points[:, :n_variables]), be.asarray(points[:, n_variables:]), notes


def _sobol(n: int, d: int, seed: int | None) -> np.ndarray:
    # One joint sequence: two samplers would reuse the same Sobol dimensions, and
    # their columns would correlate (measured: 13% bias of p5 on it_delivery).
    if d == 0:
        return np.empty((n, 0), dtype=np.float64)
    from scipy.stats import qmc

    sampler = qmc.Sobol(d=d, scramble=True, rng=np.random.default_rng(seed))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # balance warning: reported as a note
        return sampler.random(n)
