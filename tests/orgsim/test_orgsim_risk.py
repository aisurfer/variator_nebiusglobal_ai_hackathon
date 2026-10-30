"""Time to risk on hand-made masks [scenario, option, period]."""

import numpy as np
import pytest

from variator.orgsim.risk import (
    NEVER,
    all_time,
    any_time,
    failed_by,
    first_failure_distribution,
    first_failure_period,
    median_first_failure,
    p_ever,
    p_failed_by,
)

# 4 scenarios, 2 options, 5 periods. Option 0 fails in s0 at t1, s1 at t3, s2 at t1, s3 never.
# Option 1 fails in every scenario from t0, and in all periods in s0.
M = np.zeros((4, 2, 5), dtype=bool)
M[0, 0, [1, 2]] = True
M[1, 0, [3]] = True
M[2, 0, [1, 4]] = True
M[:, 1, 0] = True
M[0, 1, :] = True


def test_first_failure_period():
    assert first_failure_period(M).tolist() == [[1, 0], [3, 0], [1, 0], [NEVER, 0]]


def test_p_failed_by_is_the_risk_calendar():
    p = p_failed_by(M)
    assert p.shape == (2, 5)
    assert p[0].tolist() == [0.0, 0.5, 0.5, 0.75, 0.75]
    assert p[1].tolist() == [1.0] * 5
    assert p_ever(M).tolist() == [0.75, 1.0]
    assert failed_by(M)[1, 0].tolist() == [False, False, False, True, True]


def test_distribution_of_the_first_failure():
    d = first_failure_distribution(M)
    assert d.shape == (2, 6)
    assert d[0].tolist() == [0.0, 0.5, 0.0, 0.25, 0.0, 0.25]  # the last column is "never"
    assert np.allclose(d.sum(axis=1), 1)


def test_median_ignores_scenarios_that_never_fail():
    assert median_first_failure(M).tolist() == [1.0, 0.0]
    none = np.zeros((3, 2, 4), dtype=bool)
    none[0, 1, 2] = True
    out = median_first_failure(none)
    assert np.isnan(out[0]) and out[1] == 2.0


def test_any_and_all_time_with_windows():
    assert any_time(M)[:, 0].tolist() == [True, True, True, False]
    assert any_time(M, (2, 4))[:, 0].tolist() == [True, True, False, False]
    assert all_time(M)[:, 1].tolist() == [True, False, False, False]
    assert all_time(M, slice(0, 1))[:, 1].tolist() == [True, True, True, True]
    with pytest.raises(ValueError):
        any_time(M, (5, 9))


def test_masks_must_be_boolean():
    with pytest.raises(TypeError):
        first_failure_period(M.astype(int))
    with pytest.raises(ValueError):
        p_failed_by(np.zeros(4, dtype=bool))


@pytest.mark.parametrize("seed", range(10))
def test_curve_properties_on_random_masks(seed):
    rng = np.random.default_rng(seed)
    m = rng.random((200, 3, 12)) < rng.uniform(0.01, 0.2)
    p = p_failed_by(m)
    assert (np.diff(p, axis=1) >= 0).all() and np.allclose(p[:, -1], p_ever(m))
    d = first_failure_distribution(m)
    assert np.allclose(np.cumsum(d[:, :-1], axis=1), p)
    first = first_failure_period(m)
    for s, o in [(0, 0), (5, 2), (199, 1)]:
        assert first[s, o] == (np.flatnonzero(m[s, o])[0] if m[s, o].any() else NEVER)
