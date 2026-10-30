import numpy as np
import pytest
from scipy import stats

from variator.domain import Distribution, DistributionKind
from variator.simulation import analytic_mean, lognormal_log_params, pert_shape, ppf, quantile

N = 200_000
U = np.random.default_rng(42).random(N)
QS = np.array([0.05, 0.25, 0.5, 0.75, 0.95])


def dist(kind: DistributionKind, **params: float) -> Distribution:
    return Distribution(kind=kind, **params)


CASES = [
    (dist(DistributionKind.NORMAL, mean=9.0, std=1.2), stats.norm(9.0, 1.2)),
    (dist(DistributionKind.UNIFORM, low=90_000, high=130_000), stats.uniform(90_000, 40_000)),
    (
        dist(DistributionKind.TRIANGULAR, low=1.0, mode=2.0, high=5.0),
        stats.triang(c=0.25, loc=1.0, scale=4.0),
    ),
    (
        dist(DistributionKind.PERT, low=2.6, mode=3.0, high=3.8),
        stats.beta(*pert_shape(2.6, 3.0, 3.8), loc=2.6, scale=1.2),
    ),
    (
        dist(DistributionKind.LOGNORMAL, mean=100.0, std=40.0),
        stats.lognorm(s=lognormal_log_params(100, 40)[1], scale=np.exp(lognormal_log_params(100, 40)[0])),
    ),
]


@pytest.mark.parametrize(("d", "reference"), CASES, ids=lambda c: getattr(c, "kind", ""))
def test_inverse_cdf_matches_scipy_exactly(d, reference):
    grid = np.linspace(0.001, 0.999, 999)
    np.testing.assert_allclose(ppf(d, grid), reference.ppf(grid), rtol=1e-9)


@pytest.mark.parametrize(("d", "reference"), CASES, ids=lambda c: getattr(c, "kind", ""))
def test_sample_mean_and_quantiles_within_tolerance(d, reference):
    x = ppf(d, U)
    scale = reference.std()
    assert abs(x.mean() - analytic_mean(d)) < 0.02 * scale
    assert abs(x.mean() - reference.mean()) < 0.02 * scale
    np.testing.assert_allclose(np.quantile(x, QS), reference.ppf(QS), atol=0.02 * scale)


def test_lognormal_is_parameterized_by_its_own_mean_and_std():
    x = ppf(dist(DistributionKind.LOGNORMAL, mean=50.0, std=20.0), U)
    assert x.min() > 0
    assert x.mean() == pytest.approx(50.0, rel=0.01)
    assert x.std() == pytest.approx(20.0, rel=0.02)
    mu, sigma = lognormal_log_params(50.0, 20.0)
    assert np.exp(mu + sigma**2 / 2) == pytest.approx(50.0)


def test_pert_stays_within_bounds_and_has_pert_mean():
    d = dist(DistributionKind.PERT, low=1_800_000, mode=2_200_000, high=3_200_000)
    edges = ppf(d, np.array([0.0, 1.0]))
    np.testing.assert_allclose(edges, [1_800_000, 3_200_000])
    x = ppf(d, U)
    assert x.min() >= 1_800_000 and x.max() <= 3_200_000
    assert x.mean() == pytest.approx((1_800_000 + 4 * 2_200_000 + 3_200_000) / 6, rel=0.002)


def test_pert_shape_uses_lambda_four():
    assert pert_shape(0, 0.5, 1) == (3.0, 3.0)
    assert pert_shape(0, 0, 1) == (1.0, 5.0)


def test_bernoulli_share_and_monotonicity():
    d = dist(DistributionKind.BERNOULLI, p=0.3)
    x = ppf(d, U)
    assert set(np.unique(x)) == {0.0, 1.0}
    assert x.mean() == pytest.approx(0.3, abs=0.005)
    ordered = ppf(d, np.sort(U))
    assert (np.diff(ordered) >= 0).all()


def test_fixed_and_degenerate_distributions_are_constant():
    for d in [
        Distribution.fixed(7.0),
        dist(DistributionKind.NORMAL, mean=7.0, std=0.0),
        dist(DistributionKind.LOGNORMAL, mean=7.0, std=0.0),
        dist(DistributionKind.UNIFORM, low=7.0, high=7.0),
        dist(DistributionKind.TRIANGULAR, low=7.0, mode=7.0, high=7.0),
        dist(DistributionKind.PERT, low=7.0, mode=7.0, high=7.0),
    ]:
        np.testing.assert_allclose(ppf(d, U[:100]), 7.0)


def test_extreme_uniforms_stay_finite():
    u = np.array([0.0, 1e-300, 1.0 - 1e-17, 1.0])
    for d in [dist(DistributionKind.NORMAL, mean=0, std=1), dist(DistributionKind.LOGNORMAL, mean=1, std=1)]:
        assert np.isfinite(ppf(d, u)).all()


def test_triangular_with_mode_at_an_edge():
    d = dist(DistributionKind.TRIANGULAR, low=0.0, mode=0.0, high=1.0)
    grid = np.linspace(0, 1, 101)
    np.testing.assert_allclose(ppf(d, grid), stats.triang(c=0, loc=0, scale=1).ppf(grid), atol=1e-12)


def test_scalar_quantile():
    assert quantile(dist(DistributionKind.UNIFORM, low=0, high=10), 0.1) == pytest.approx(1.0)
    assert quantile(dist(DistributionKind.NORMAL, mean=0, std=1), 0.5) == pytest.approx(0.0)
