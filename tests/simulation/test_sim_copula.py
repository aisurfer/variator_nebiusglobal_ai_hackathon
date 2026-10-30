import math

import numpy as np
import pytest
from scipy import stats

from variator.domain import Correlation
from variator.simulation import (
    CorrelationRepairWarning,
    build_copula,
    get_backend,
    nearest_positive_definite,
    spearman_to_pearson,
)
from variator.simulation.copula import is_positive_definite

NUMPY = get_backend("numpy")


def test_spearman_to_pearson():
    assert spearman_to_pearson(0.0) == 0.0
    assert spearman_to_pearson(1.0) == pytest.approx(1.0)
    assert spearman_to_pearson(-0.5) == pytest.approx(-2 * math.sin(math.pi / 12))


def test_no_correlations_means_no_copula():
    assert build_copula(["a", "b"], []) is None


def test_achieved_spearman_matches_target():
    names = ["a", "b", "c", "d"]
    corrs = [Correlation(a="a", b="b", rho=0.7), Correlation(a="c", b="a", rho=-0.4)]
    copula = build_copula(names, corrs)
    assert copula.names == ["a", "b", "c"]
    u = np.random.default_rng(7).random((100_000, 4))
    out = copula.apply(u, NUMPY)
    rho = stats.spearmanr(out).statistic
    assert rho[0, 1] == pytest.approx(0.7, abs=0.01)
    assert rho[0, 2] == pytest.approx(-0.4, abs=0.01)
    # b-c is implied by the others (about -0.28); d is untouched and independent.
    np.testing.assert_array_equal(out[:, 3], u[:, 3])
    assert abs(rho[0, 3]) < 0.01
    assert ((out > 0) & (out < 1)).all()


def test_non_positive_definite_matrix_is_repaired_with_warning():
    corrs = [
        Correlation(a="a", b="b", rho=0.9),
        Correlation(a="b", b="c", rho=0.9),
        Correlation(a="a", b="c", rho=-0.9),
    ]
    with pytest.warns(CorrelationRepairWarning):
        copula = build_copula(["a", "b", "c"], corrs)
    assert copula.notes and "positive definite" in copula.notes[0]
    assert is_positive_definite(copula.pearson)
    np.testing.assert_allclose(np.diag(copula.pearson), 1.0)
    np.testing.assert_allclose(copula.pearson, copula.pearson.T)
    np.testing.assert_allclose(copula.lower @ copula.lower.T, copula.pearson, atol=1e-12)


def test_nearest_positive_definite_keeps_valid_matrices_and_fixes_bad_ones():
    good = np.array([[1.0, 0.3], [0.3, 1.0]])
    np.testing.assert_allclose(nearest_positive_definite(good), good, atol=1e-9)
    bad = np.array([[1.0, 0.95, -0.95], [0.95, 1.0, 0.95], [-0.95, 0.95, 1.0]])
    assert not is_positive_definite(bad)
    fixed = nearest_positive_definite(bad)
    assert is_positive_definite(fixed)
    assert np.abs(fixed).max() <= 1.0 + 1e-12


def test_duplicate_pairs_keep_last_value_with_note():
    corrs = [Correlation(a="a", b="b", rho=0.2), Correlation(a="b", b="a", rho=0.6)]
    copula = build_copula(["a", "b"], corrs)
    assert copula.target[0, 1] == 0.6
    assert any("given twice" in note for note in copula.notes)
