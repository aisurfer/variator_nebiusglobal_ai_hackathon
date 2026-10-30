"""Time to risk over a check mask ``[scenario, option, period]`` (FR13, 04-ANSWERS §2.3.2).

A check is evaluated in every period. ``mask[s, o, t]`` is True when the check fails
in scenario ``s``, option ``o`` and period ``t``. From the mask:

- ``first_failure_period``: the period of the first failure (``NEVER`` = -1 if none);
- ``p_failed_by``: P(first failure <= t) per option, the curve of the risk calendar;
- ``first_failure_distribution``: P(first failure = t), with "never" as the last column;
- ``median_first_failure``: the median period among scenarios that fail;
- ``any_time`` / ``all_time``: path predicates over a window of periods.

Axes between the first (scenario) and the last (period) can be any shape; the
docstrings say "option" for them.
"""

from __future__ import annotations

import numpy as np

NEVER = -1


def _mask(mask: np.ndarray) -> np.ndarray:
    m = np.asarray(mask)
    if m.dtype != bool:
        raise TypeError("the mask must be boolean")
    if m.ndim < 2:
        raise ValueError("the mask needs at least the axes [scenario, period]")
    return m


def _window(m: np.ndarray, window: tuple[int | None, int | None] | slice | None) -> np.ndarray:
    if window is None:
        return m
    w = window if isinstance(window, slice) else slice(*window)
    part = m[..., w]
    if part.shape[-1] == 0:
        raise ValueError("the window selects no period")
    return part


def first_failure_period(mask: np.ndarray) -> np.ndarray:
    """Index of the first failed period per [scenario, option]; ``NEVER`` (-1) if none."""
    m = _mask(mask)
    return np.where(m.any(axis=-1), m.argmax(axis=-1), NEVER).astype(np.int64)


def failed_by(mask: np.ndarray) -> np.ndarray:
    """True from the first failure on: [scenario, option, period]."""
    return np.logical_or.accumulate(_mask(mask), axis=-1)


def p_failed_by(mask: np.ndarray) -> np.ndarray:
    """P(first failure <= t) over scenarios: shape [option, period], non-decreasing in t."""
    return failed_by(mask).mean(axis=0)


def p_ever(mask: np.ndarray) -> np.ndarray:
    """P(the check fails at least once in the horizon): shape [option]."""
    return _mask(mask).any(axis=-1).mean(axis=0)


def first_failure_distribution(mask: np.ndarray) -> np.ndarray:
    """P(first failure = t) per option: shape [option, T + 1]; the last column is "never"."""
    m = _mask(mask)
    first = first_failure_period(m)
    n_periods = m.shape[-1]
    bins = np.where(first == NEVER, n_periods, first)
    onehot = bins[..., None] == np.arange(n_periods + 1)
    return onehot.mean(axis=0)


def median_first_failure(mask: np.ndarray) -> np.ndarray:
    """Median first-failure period per option among the scenarios that fail; NaN when none fails."""
    first = first_failure_period(mask).astype(np.float64)
    first[first == NEVER] = np.nan
    out = np.full(first.shape[1:], np.nan)
    failed = ~np.isnan(first)
    has = failed.any(axis=0)
    out[has] = np.nanmedian(first[:, has], axis=0)
    return out


def any_time(mask: np.ndarray, window: tuple[int | None, int | None] | slice | None = None) -> np.ndarray:
    """True where the check fails in at least one period of the window: [scenario, option]."""
    return _window(_mask(mask), window).any(axis=-1)


def all_time(mask: np.ndarray, window: tuple[int | None, int | None] | slice | None = None) -> np.ndarray:
    """True where the check fails in every period of the window: [scenario, option]."""
    return _window(_mask(mask), window).all(axis=-1)
