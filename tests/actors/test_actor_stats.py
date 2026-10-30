"""Estimators on small synthetic inputs with known answers (no history needed)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from variator.actors import stats
from variator.actors.nps import _ordinal_pool, nps_of_mean
from variator.actors.stats import Param

SRC = "data:test@2026-01-01"


def _pert_ok(p: Param) -> None:
    assert p.low <= p.mode <= p.high
    assert p.low <= p.point <= p.high
    d = p.to_dict()
    assert {"point", "low", "mode", "high", "n", "source"} <= set(d)


def test_rate_known_answer():
    p = stats.rate(100, 50.0, SRC, "1/year")
    _pert_ok(p)
    assert p.point == pytest.approx(2.0, rel=0.02)
    assert p.low < 2.0 < p.high
    m = stats.rate(42, 252, SRC, "per month", per=21)
    assert m.point == pytest.approx(3.5, rel=0.03)


def test_hazard_is_annual_probability():
    p = stats.hazard(12, 100.0, SRC)
    _pert_ok(p)
    assert p.point == pytest.approx(1 - math.exp(-0.125), rel=0.05)
    assert 0 < p.low < p.high < 1


def test_share_and_zero_events():
    p = stats.share(25, 100, SRC)
    assert p.point == pytest.approx(0.25, abs=0.01)
    z = stats.share(0, 20, SRC)
    _pert_ok(z)
    assert z.mode == z.low < 1e-4 and z.high > 0.05  # the zero mode is clamped to p1 (PERT bounds)


def test_lognormal_recovers_median_and_sigma():
    x = stats.rng("test:ln").lognormal(math.log(3.0), 0.5, 400)
    fit = stats.lognormal(x, SRC, "h", "t")
    assert fit.median.contains(3.0)
    assert fit.sigma.contains(0.5)
    assert fit.mean.contains(3.0 * math.exp(0.125))


def test_censored_normal_handles_unpaid_invoices():
    r = stats.rng("test:cens")
    x = r.normal(60, 8, 300)
    seen = np.minimum(x, 62.0)
    cens = x > 62.0
    mu, sd, se, _, n = stats.censored_normal(seen, cens)
    assert abs(mu - 60) < 3 * se + 0.5
    assert sd == pytest.approx(8, rel=0.2)
    naive = float(np.mean(seen))
    assert naive < mu  # dropping the censoring biases the mean down


def test_pool_poisson_covers_actors_and_group():
    r = stats.rng("test:pool")
    n = 80
    lam = np.exp(r.normal(0.0, 0.3, n))
    expo = np.full(n, 12.0)
    y = r.poisson(lam * expo)
    ids = [f"a{i}" for i in range(n)]
    pooled = stats.pool_poisson(ids, y, expo, SRC, "group:test", label="t")
    assert pooled.group.contains(1.0)
    assert pooled.spread.contains(0.3)
    inside = sum(pooled.actors[a].contains(float(lam[i])) for i, a in enumerate(ids))
    assert inside / n >= 0.9
    for p in pooled.actors.values():
        _pert_ok(p)


def test_short_history_shrinks_more():
    """Two actors, same own rate: the shorter history gets less weight and sits closer to the group."""
    r = stats.rng("test:shrink")
    n = 40
    lam = np.exp(r.normal(0.0, 0.3, n))
    expo = np.full(n, 12.0)
    y = r.poisson(lam * expo).astype(float)
    ids = [f"a{i}" for i in range(n)] + ["long", "short"]
    y = np.concatenate([y, [48.0, 4.0]])  # both have an own rate of 4 per unit
    expo = np.concatenate([expo, [12.0, 1.0]])
    pooled = stats.pool_poisson(ids, y, expo, SRC, "group:test", label="shrink")
    wl, ws = pooled.weights["long"], pooled.weights["short"]
    assert ws < wl
    g = pooled.group.point
    assert abs(math.log(pooled.actors["short"].point / g)) < abs(math.log(pooled.actors["long"].point / g))
    assert pooled.actors["short"].source == "group:test"


def test_pool_normal_shrinkage_and_coverage():
    r = stats.rng("test:pn")
    n = 60
    theta = r.normal(math.log(60), 0.25, n)
    v = np.where(np.arange(n) % 2 == 0, 0.002, 0.2)  # half precise, half noisy
    y = r.normal(theta, np.sqrt(v))
    ids = [f"c{i}" for i in range(n)]
    pooled = stats.pool_normal(ids, y, v, SRC, "group:test", label="pn")
    assert pooled.group.contains(60.0)
    precise = [pooled.weights[f"c{i}"] for i in range(0, n, 2)]
    noisy = [pooled.weights[f"c{i}"] for i in range(1, n, 2)]
    assert min(precise) > max(noisy)
    inside = sum(pooled.actors[a].contains(math.exp(theta[i])) for i, a in enumerate(ids))
    assert inside / n >= 0.9


def test_pool_rate_rare_events_share_one_rate():
    ids = ["a", "b", "c"]
    pooled = stats.pool_rate(ids, np.array([1, 0, 0]), np.array([1.0, 1.0, 1.0]), SRC, "group:test")
    assert all(w == 0.0 for w in pooled.weights.values())
    assert pooled.actors["a"].point == pooled.actors["b"].point == pooled.group.point


def test_covariate_hazard_separates_base_and_effect():
    r = stats.rng("test:cov")
    cells = 200_000
    x = np.where(r.random(cells) < 0.2, r.exponential(2.0, cells), 0.0)
    h0, k = 0.12, 0.8
    expo = np.full(cells, 1 / 252)
    ev = (r.random(cells) < h0 * (1 + k * x) * expo).astype(float)
    base, kp, _ = stats.covariate_hazard(ev, expo, x, SRC)
    assert base.contains(1 - math.exp(-h0))
    assert kp.contains(k)
    crude = stats.hazard(ev.sum(), expo.sum(), SRC)
    assert crude.point > base.point  # the realised hazard is above the base hazard


def test_nps_mapping_and_ordinal_pool():
    assert nps_of_mean(8.0, 1.3) < nps_of_mean(8.5, 1.3) < nps_of_mean(9.0, 1.3)
    r = stats.rng("test:nps")
    mus = r.normal(8.3, 0.5, 120)
    scores = [np.clip(np.round(m + 1.3 * r.normal(size=6)), 0, 10) for m in mus]
    fit = _ordinal_pool([f"c{i}" for i in range(120)], scores)
    assert fit["sigma"] == pytest.approx(1.3, rel=0.15)
    lo, med, hi = fit["group"]
    assert lo < 8.3 < hi


def test_param_rejects_nan_and_orders_bounds():
    with pytest.raises(ValueError):
        Param(float("nan"), 0, 0, 1, 1, SRC)
    p = Param(5, 9, 20, 1, 1, SRC)
    assert p.low == 1 and p.high == 9 and p.mode == 9 and p.point == 5


def test_draws_are_deterministic():
    a = stats.lognormal(np.array([1.0, 2.0, 3.0, 4.0]), SRC, label="same").mean.to_dict()
    b = stats.lognormal(np.array([1.0, 2.0, 3.0, 4.0]), SRC, label="same").mean.to_dict()
    assert a == b
