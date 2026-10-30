"""Estimators with uncertainty (04 §1.4.2): each gives a ``Param`` with p1, mode, p99.

| Kind | Estimator | Function |
|---|---|---|
| Rate of events | Gamma posterior (Jeffreys prior) of events / exposure | ``rate`` |
| Annual hazard | exponential survival: rate posterior, p = 1 - exp(-rate) | ``hazard`` |
| Share | Beta posterior (Jeffreys prior) of k of n | ``share`` |
| Duration | log-normal: Student t (log mean), scaled inverse chi2 (log variance) | ``lognormal`` |
| Level | Student t for the mean | ``normal_mean`` |
| Censored duration | normal with right censoring (maximum likelihood) | ``censored_normal`` |
| Actor = group + deviation | normal-normal, Bayesian over the spread | ``pool_normal`` |
| Actor = group + deviation, counts | Poisson-log-normal, Bayesian over the spread | ``pool_poisson`` |
| Hazard with a covariate | survival with hazard h0 x (1 + k x(t)), grid posterior | ``covariate_hazard`` |

low and high are p1 and p99 of the posterior; mode is the most likely value (PERT inputs of
``variator.domain``); point is the posterior median. Monte Carlo draws use ``derived_rng``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import optimize, special, stats

from variator.synthetic.company import derived_rng

P_LO, P_HI = 0.01, 0.99
DRAWS = 20_000
SEED = 20261027
TAU_PRIOR = 0.5  # half-normal scale of the between-actor log-sd (weakly informative)


@dataclass
class Param:
    """One estimated parameter: PERT-ready (low <= mode <= high) with its source tag."""

    point: float
    low: float
    mode: float
    high: float
    n: int
    source: str
    unit: str = ""
    method: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        vals = [self.low, self.mode, self.high, self.point]
        if any(not math.isfinite(float(v)) for v in vals):
            raise ValueError(f"non-finite parameter values: {vals}")
        lo, hi = min(self.low, self.high), max(self.low, self.high)
        self.low, self.high = float(lo), float(hi)
        self.mode = float(min(max(self.mode, lo), hi))
        self.point = float(min(max(self.point, lo), hi))

    def to_dict(self, digits: int = 4) -> dict[str, Any]:
        out: dict[str, Any] = {
            "point": _round(self.point, digits),
            "low": _round(self.low, digits),
            "mode": _round(self.mode, digits),
            "high": _round(self.high, digits),
            "n": int(self.n),
            "source": self.source,
        }
        if self.unit:
            out["unit"] = self.unit
        if self.method:
            out["method"] = self.method
        if self.extra:
            out["extra"] = {k: _clean(v, digits) for k, v in self.extra.items() if v is not None}
        return out

    def scaled(self, k: float, unit: str | None = None) -> Param:
        """The same parameter in other units (k > 0)."""
        return Param(
            self.point * k,
            self.low * k,
            self.mode * k,
            self.high * k,
            self.n,
            self.source,
            unit if unit is not None else self.unit,
            self.method,
            dict(self.extra),
        )

    def contains(self, x: float) -> bool:
        return self.low <= x <= self.high


def fixed(
    value: float, source: str, unit: str = "", method: str = "value at T", n: int = 1, **extra: Any
) -> Param:
    """A known value (a state at T, a contract term): low = mode = high."""
    v = float(value)
    return Param(v, v, v, v, n, source, unit, method, extra)


def _round(x: float, digits: int) -> float:
    if x == 0 or not math.isfinite(x):
        return float(x)
    mag = int(math.floor(math.log10(abs(x))))
    return round(float(x), max(digits - 1 - mag, 0))


def _clean(v: Any, digits: int) -> Any:
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (float, np.floating)):
        return _round(float(v), digits)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, dict):
        return {k: _clean(x, digits) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x, digits) for x in v]
    return v


def from_draws(
    draws: np.ndarray, n: int, source: str, unit: str = "", method: str = "", **extra: Any
) -> Param:
    """A parameter from posterior draws: quantiles and a histogram mode."""
    x = np.asarray(draws, dtype=float)
    x = x[np.isfinite(x)]
    lo, med, hi = np.quantile(x, [P_LO, 0.5, P_HI])
    return Param(float(med), float(lo), draws_mode(x), float(hi), n, source, unit, method, extra)


def draws_mode(x: np.ndarray) -> float:
    """The mode of a sample: the peak of a smoothed histogram between p0.5 and p99.5."""
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return float("nan")
    a, b = np.quantile(x, [0.005, 0.995])
    if b <= a:
        return float(np.median(x))
    hist, edges = np.histogram(x, bins=60, range=(a, b))
    k = int(np.argmax(np.convolve(hist, [1, 2, 3, 2, 1], mode="same")))
    return float((edges[k] + edges[k + 1]) / 2)


def rng(label: str) -> np.random.Generator:
    return derived_rng(SEED, label)


def _grid_quantiles(grid: np.ndarray, p: np.ndarray) -> tuple[float, float, float, float]:
    """(p1, median, mode, p99) of a density given on a grid (weights p, any scale)."""
    p = np.asarray(p, dtype=float)
    p = p / p.sum()
    c = np.cumsum(p)
    lo, med, hi = (float(np.interp(z, c, grid)) for z in (P_LO, 0.5, P_HI))
    return lo, med, float(grid[int(np.argmax(p))]), hi


# ---------------------------------------------------------------------------------------------- basics
def rate(
    events: float,
    exposure: float,
    source: str,
    unit: str = "",
    *,
    prior: tuple[float, float] = (0.5, 0.0),
    per: float = 1.0,
) -> Param:
    """Events per unit of exposure: Gamma(a0 + k, b0 + e) posterior; ``per`` rescales (e.g. per month)."""
    a = prior[0] + float(events)
    b = prior[1] + float(exposure)
    if b <= 0:
        raise ValueError("exposure must be positive")
    g = stats.gamma(a, scale=1.0 / b)
    lo, med, hi = g.ppf([P_LO, 0.5, P_HI])
    mode = max(a - 1.0, 0.0) / b
    return Param(
        med * per,
        lo * per,
        mode * per,
        hi * per,
        int(events),
        source,
        unit,
        "gamma-poisson",
        {"events": float(events), "exposure": float(exposure), "shape": a, "rate": b / per},
    )


def hazard(events: float, years: float, source: str) -> Param:
    """Annual probability of a one-time event: exponential survival, p = 1 - exp(-rate)."""
    r = rate(events, years, source, "1/year")
    return to_probability(r, "exponential survival", events=float(events), exposure_years=float(years))


def to_probability(r: Param, method: str = "", **extra: Any) -> Param:
    """An annual rate as an annual probability: p = 1 - exp(-rate) (monotone, quantiles map)."""
    f = lambda x: 1.0 - math.exp(-x)  # noqa: E731
    ex = dict(r.extra)
    ex.update(extra)
    ex["rate_point"] = r.point
    return Param(
        f(r.point),
        f(r.low),
        f(r.mode),
        f(r.high),
        r.n,
        r.source,
        "probability per year",
        method or (r.method + ", annual probability"),
        ex,
    )


def share(k: float, n: float, source: str, *, prior: tuple[float, float] = (0.5, 0.5)) -> Param:
    """k of n: Beta posterior with the Jeffreys prior."""
    a, b = prior[0] + k, prior[1] + (n - k)
    d = stats.beta(a, b)
    lo, med, hi = d.ppf([P_LO, 0.5, P_HI])
    if a > 1 and b > 1:
        mode = (a - 1) / (a + b - 2)
    else:
        mode = 0.0 if a <= b else 1.0
    return Param(med, lo, mode, hi, int(n), source, "share", "beta", {"k": float(k), "n": float(n)})


def normal_mean(x: np.ndarray, source: str, unit: str = "", *, sd_floor: float = 0.0) -> Param:
    """Mean of a level: Student t posterior (Jeffreys prior)."""
    x = np.asarray(x, dtype=float)
    n = x.size
    if n == 0:
        raise ValueError("no observations")
    m = float(np.mean(x))
    s = max(float(np.std(x, ddof=1)) if n > 1 else abs(m) * 0.5 + 1e-9, sd_floor)
    se = s / math.sqrt(n)
    t = stats.t(max(n - 1, 1), loc=m, scale=se)
    lo, hi = t.ppf([P_LO, P_HI])
    return Param(m, lo, m, hi, n, source, unit, "student-t", {"sd": s, "se": se})


@dataclass
class LogNormalFit:
    n: int
    mu: float  # mean of the logs
    s: float  # sd of the logs
    median: Param
    mean: Param
    sigma: Param


def lognormal(x: np.ndarray, source: str, unit: str = "", label: str = "lognormal") -> LogNormalFit:
    """Log-normal durations: the median exp(mu), the mean exp(mu + sigma^2 / 2) and sigma, with posteriors."""
    x = np.asarray(x, dtype=float)
    x = x[x > 0]
    n = x.size
    if n < 2:
        raise ValueError("a log-normal fit needs two observations or more")
    y = np.log(x)
    mu, s = float(np.mean(y)), float(np.std(y, ddof=1))
    s = max(s, 1e-6)
    r = rng(f"lognormal:{label}")
    sig2 = (n - 1) * s * s / r.chisquare(n - 1, DRAWS)
    mus = r.normal(mu, np.sqrt(sig2 / n))
    q = np.quantile(np.exp(mus), [P_LO, P_HI])
    med = Param(
        math.exp(mu),
        float(q[0]),
        math.exp(mu),
        float(q[1]),
        n,
        source,
        unit,
        "log-normal (median)",
        {"log_mu": mu, "log_sd": s},
    )
    mean = from_draws(np.exp(mus + sig2 / 2), n, source, unit, "log-normal (mean)", log_mu=mu, log_sd=s)
    qs = np.quantile(np.sqrt(sig2), [P_LO, P_HI])
    sigma = Param(s, float(qs[0]), s, float(qs[1]), n, source, "log-sd", "log-normal (sigma)")
    return LogNormalFit(n, mu, s, med, mean, sigma)


# ---------------------------------------------------------------------------------------------- censoring
def censored_normal(
    values: np.ndarray, censored: np.ndarray, *, sd_floor: float = 1.0
) -> tuple[float, float, float, float, int]:
    """Normal MLE with right censoring: (mu, sigma, se_mu, se_log_sigma, uncensored count).

    ``censored[i]`` is True when ``values[i]`` is only a lower bound (for example: not paid by T).
    """
    v = np.asarray(values, dtype=float)
    c = np.asarray(censored, dtype=bool)
    obs = v[~c]
    if obs.size == 0:
        raise ValueError("no uncensored values")
    m0 = float(np.mean(obs))
    s0 = max(float(np.std(obs, ddof=1)) if obs.size > 1 else 5.0, sd_floor)

    def nll(th: np.ndarray) -> float:
        mu, ls = th
        sd = math.exp(ls)
        ll = stats.norm.logpdf(obs, mu, sd).sum()
        if c.any():
            ll += stats.norm.logsf(v[c], mu, sd).sum()
        return -float(ll)

    res = optimize.minimize(
        nll,
        np.array([m0, math.log(s0)]),
        method="Nelder-Mead",
        options={"xatol": 1e-6, "fatol": 1e-8, "maxiter": 2000},
    )
    mu, ls = res.x
    if math.exp(ls) < sd_floor:
        ls = math.log(sd_floor)
    hess = _num_hessian(nll, np.array([mu, ls]))
    try:
        cov = np.linalg.inv(hess)
        se_mu = math.sqrt(max(cov[0, 0], 1e-12))
        se_ls = math.sqrt(max(cov[1, 1], 1e-12))
    except np.linalg.LinAlgError:
        se_mu, se_ls = float("nan"), float("nan")
    if not math.isfinite(se_mu):
        se_mu = math.exp(ls) / math.sqrt(max(obs.size, 1))
    if not math.isfinite(se_ls):
        se_ls = 1 / math.sqrt(2 * max(obs.size - 1, 1))
    return float(mu), float(math.exp(ls)), float(se_mu), float(se_ls), int(obs.size)


def _num_hessian(f, x: np.ndarray, h: float = 1e-4) -> np.ndarray:
    k = x.size
    out = np.zeros((k, k))
    for i in range(k):
        for j in range(i, k):
            e_i = np.zeros(k)
            e_j = np.zeros(k)
            e_i[i] = h * max(1.0, abs(x[i]))
            e_j[j] = h * max(1.0, abs(x[j]))
            v = (f(x + e_i + e_j) - f(x + e_i - e_j) - f(x - e_i + e_j) + f(x - e_i - e_j)) / (
                4 * e_i[i] * e_j[j]
            )
            out[i, j] = out[j, i] = v
    return out


# ---------------------------------------------------------------------------------------------- pooling
@dataclass
class Pooled:
    """Partial pooling result: group parameters and per-actor posteriors (on the natural scale)."""

    mu: float  # posterior median of the group mean on the pooled scale (log scale for log models)
    tau: float  # posterior median of the between-actor sd
    group: Param  # the cohort value (the median of the cohort on the natural scale)
    group_mean: Param  # the mean of the cohort (natural scale; differs from the median for log models)
    spread: Param  # tau with its uncertainty
    actors: dict[str, Param]
    weights: dict[str, float]  # shrinkage weight of the actor's own data (1 = own data only)


def _reml_nll(tau: float, y: np.ndarray, v: np.ndarray) -> float:
    w = 1.0 / (v + tau * tau)
    mu = float(np.sum(w * y) / np.sum(w))
    return 0.5 * float(np.sum(np.log(v + tau * tau)) + math.log(np.sum(w)) + np.sum(w * (y - mu) ** 2))


def pool_normal(
    ids: list[str],
    y: np.ndarray,
    v: np.ndarray,
    source_actor: str,
    source_group: str,
    *,
    log: bool = True,
    unit: str = "",
    tau_scale: float | None = None,
    label: str = "pool",
) -> Pooled:
    """Normal-normal partial pooling, Bayesian over the spread: actor = group + shrunk deviation.

    ``y`` are the actor estimates (log values when ``log``) and ``v`` their sampling variances.
    Priors: flat on the group mean, half-normal(``tau_scale``) on the spread. The shrinkage
    weight is E[tau^2 / (tau^2 + v)]: a short history (large v) shrinks more to the group.
    """
    y = np.asarray(y, dtype=float)
    v = np.maximum(np.asarray(v, dtype=float), 1e-12)
    if tau_scale is None:
        tau_scale = TAU_PRIOR if log else float(np.std(y) + np.sqrt(np.mean(v)) + 1e-9)
    taus = np.concatenate([[0.0], np.geomspace(tau_scale * 0.003, tau_scale * 6, 160)])
    dt = np.gradient(taus)
    lp = np.array([-_reml_nll(t, y, v) for t in taus]) + stats.norm.logpdf(taus, 0, tau_scale) + np.log(dt)
    wt = np.exp(lp - lp.max())
    wt /= wt.sum()
    r = rng(f"pool_normal:{label}")
    ti = r.choice(taus.size, size=DRAWS, p=wt)
    tau_d = taus[ti]
    mu_hat = np.empty(taus.size)
    se_hat = np.empty(taus.size)
    for k, t in enumerate(taus):
        w = 1.0 / (v + t * t)
        mu_hat[k] = np.sum(w * y) / np.sum(w)
        se_hat[k] = math.sqrt(1.0 / np.sum(w))
    mu_d = r.normal(mu_hat[ti], se_hat[ti])
    f = np.exp if log else (lambda x: x)
    tlo, tmed, tmode, thi = _grid_quantiles(taus, wt)
    group = from_draws(
        f(mu_d),
        int(y.size),
        source_group,
        unit,
        "partial pooling (normal-normal, Bayesian spread), group " + ("median" if log else "mean"),
        tau=tmed,
    )
    group_mean = from_draws(
        f(mu_d + (tau_d**2) / 2) if log else mu_d,
        int(y.size),
        source_group,
        unit,
        "partial pooling, group mean",
    )
    spread = Param(
        tmed,
        tlo,
        tmode,
        thi,
        int(y.size),
        source_group,
        "log-sd" if log else unit,
        "posterior of the spread (half-normal prior)",
        {"prior_scale": tau_scale},
    )
    actors: dict[str, Param] = {}
    weights: dict[str, float] = {}
    for i, a in enumerate(ids):
        b = tau_d**2 / (tau_d**2 + v[i])
        th = r.normal(b * y[i] + (1 - b) * mu_d, np.sqrt(b * v[i]))
        w = float(np.mean(b))
        src = source_actor if w >= 0.5 else source_group
        prm = from_draws(
            f(th), 0, src, unit, "partial pooling (normal-normal)", shrinkage_weight=w, own=float(f(y[i]))
        )
        actors[a] = prm
        weights[a] = w
    return Pooled(float(np.median(mu_d)), tmed, group, group_mean, spread, actors, weights)


_GH_X, _GH_W = np.polynomial.hermite_e.hermegauss(32)
_GH_W = _GH_W / _GH_W.sum()


def pool_poisson(
    ids: list[str],
    counts: np.ndarray,
    exposure: np.ndarray,
    source_actor: str,
    source_group: str,
    *,
    unit: str = "",
    per: float = 1.0,
    tau_scale: float = TAU_PRIOR,
    label: str = "pool",
) -> Pooled:
    """Poisson-log-normal partial pooling, Bayesian over (mu, tau) on a grid.

    log rate_i ~ N(mu, tau^2), counts_i ~ Poisson(rate_i x e_i). Priors: flat on mu,
    half-normal(``tau_scale``) on tau. The marginal likelihood uses Gauss-Hermite nodes; each
    actor posterior mixes over the (mu, tau) grid, so a poorly known spread widens the actor
    intervals. ``per`` rescales the rate (per month etc.).
    """
    y = np.asarray(counts, dtype=float)
    e = np.asarray(exposure, dtype=float)
    keep = e > 0
    if keep.sum() == 0:
        raise ValueError("no exposure")
    yk, ek = y[keep], e[keep]
    tot_y, tot_e = yk.sum(), ek.sum()
    m0 = math.log(max(tot_y, 0.5) / tot_e)
    half = 4.0 / math.sqrt(max(tot_y, 1.0)) + 1.5 * tau_scale + 0.5
    mus = np.linspace(m0 - half, m0 + half, 81)
    taus = np.concatenate([[0.0], np.geomspace(tau_scale * 0.01, tau_scale * 5, 50)])
    # marginal log-likelihood on the grid: [mu, tau]
    lg = special.gammaln(yk + 1)
    ll = np.empty((mus.size, taus.size))
    for j, t in enumerate(taus):
        loglam = mus[:, None] + t * _GH_X[None, :]  # [mu, node]
        lam = np.exp(loglam)[None, :, :] * ek[:, None, None]  # [actor, mu, node]
        lik = yk[:, None, None] * (loglam[None, :, :] + np.log(ek)[:, None, None]) - lam - lg[:, None, None]
        m = lik.max(axis=2, keepdims=True)
        ll[:, j] = np.sum(m[:, :, 0] + np.log(np.exp(lik - m) @ _GH_W), axis=0)
    lp = ll + stats.norm.logpdf(taus, 0, tau_scale)[None, :] + np.log(np.gradient(taus))[None, :]
    wgt = np.exp(lp - lp.max())
    wgt /= wgt.sum()
    pm = wgt.sum(axis=1)
    pt = wgt.sum(axis=0)
    mlo, mmed, mmode, mhi = _grid_quantiles(mus, pm)
    tlo, tmed, tmode, thi = _grid_quantiles(taus, pt)
    group = Param(
        math.exp(mmed) * per,
        math.exp(mlo) * per,
        math.exp(mmode) * per,
        math.exp(mhi) * per,
        int(yk.size),
        source_group,
        unit,
        "Poisson-log-normal pooling (Bayesian grid), group median",
        {"tau": tmed, "events": float(tot_y), "exposure": float(tot_e)},
    )
    # the group mean exp(mu + tau^2 / 2)
    gm = np.add.outer(mus, taus**2 / 2)
    order = np.argsort(gm.ravel())
    gv, gw = gm.ravel()[order], wgt.ravel()[order]
    glo, gmed, gmode, ghi = _grid_quantiles(gv, gw)
    group_mean = Param(
        math.exp(gmed) * per,
        math.exp(glo) * per,
        math.exp(gmed) * per,
        math.exp(ghi) * per,
        int(yk.size),
        source_group,
        unit,
        "Poisson-log-normal pooling, group mean",
    )
    spread = Param(
        tmed,
        tlo,
        tmode,
        thi,
        int(yk.size),
        source_group,
        "log-sd",
        "posterior of the spread (half-normal prior)",
        {"prior_scale": tau_scale},
    )
    # actor posteriors: mixture over the grid cells with weight
    cells = np.argwhere(wgt > wgt.max() * 1e-6)
    cw = wgt[cells[:, 0], cells[:, 1]]
    cmu = mus[cells[:, 0]]
    ctau = np.maximum(taus[cells[:, 1]], 1e-3)
    lo_g = mus[0] - 4 * taus[-1] - 3
    hi_g = mus[-1] + 4 * taus[-1] + 3
    grid = np.linspace(lo_g, hi_g, 700)
    dens = stats.norm.pdf(grid[None, :], cmu[:, None], ctau[:, None])  # [cell, grid]
    dens /= np.maximum(dens.sum(axis=1, keepdims=True), 1e-300)
    actors: dict[str, Param] = {}
    weights: dict[str, float] = {}
    for i, a in enumerate(ids):
        yi, ei = float(y[i]), float(e[i])
        if ei > 0:
            lik = yi * (grid + math.log(ei)) - np.exp(grid) * ei
            lik = np.exp(lik - lik.max())
        else:
            lik = np.ones_like(grid)
        z = dens @ lik  # [cell]
        p = lik * ((cw / np.maximum(z, 1e-300)) @ dens)
        lo, med, mode, hi = _grid_quantiles(grid, p)
        expected = ei * np.exp(cmu)
        b = ctau**2 / (ctau**2 + 1.0 / np.maximum(expected, 1e-9)) if ei > 0 else np.zeros_like(ctau)
        w = float(np.sum(cw * b) / np.sum(cw))
        own = yi / ei * per if ei > 0 else None
        src = source_actor if w >= 0.5 else source_group
        actors[a] = Param(
            math.exp(med) * per,
            math.exp(lo) * per,
            math.exp(mode) * per,
            math.exp(hi) * per,
            int(yi),
            src,
            unit,
            "Poisson-log-normal pooling (Bayesian grid)",
            {"shrinkage_weight": w, "events": yi, "exposure": ei, "own": own},
        )
        weights[a] = w
    return Pooled(mmed, tmed, group, group_mean, spread, actors, weights)


RARE_EVENTS = 5


def pool_rate(
    ids: list[str],
    counts: np.ndarray,
    exposure: np.ndarray,
    source_actor: str,
    source_group: str,
    *,
    unit: str = "",
    per: float = 1.0,
    label: str = "pool",
) -> Pooled:
    """``pool_poisson``, but with fewer than ``RARE_EVENTS`` events one shared rate (Gamma posterior).

    With so few events the spread between actors is not identifiable: every actor takes the
    cohort rate (shrinkage weight 0).
    """
    y = np.asarray(counts, dtype=float)
    e = np.asarray(exposure, dtype=float)
    if y.sum() >= RARE_EVENTS:
        return pool_poisson(ids, y, e, source_actor, source_group, unit=unit, per=per, label=label)
    g = rate(float(y.sum()), float(e.sum()), source_group, unit, per=per)
    g.method = f"one shared rate (fewer than {RARE_EVENTS} events)"
    actors = {}
    for i, a in enumerate(ids):
        p = Param(
            g.point,
            g.low,
            g.mode,
            g.high,
            int(y[i]),
            source_group,
            unit,
            g.method,
            {"shrinkage_weight": 0.0, "events": float(y[i]), "exposure": float(e[i])},
        )
        actors[a] = p
    spread = Param(0.0, 0.0, 0.0, 0.0, int(y.size), source_group, "log-sd", "not identifiable")
    return Pooled(math.log(max(g.point, 1e-12) / per), 0.0, g, g, spread, actors, {a: 0.0 for a in ids})


# ------------------------------------------------------------------------------- hazards with a covariate
def covariate_hazard(
    events: np.ndarray,
    exposure: np.ndarray,
    x: np.ndarray,
    source: str,
    *,
    k_max: float = 5.0,
) -> tuple[Param, Param, float]:
    """Grid posterior of hazard = h0 x (1 + k x) on exposure cells (Jeffreys prior on h0, flat on k >= 0).

    ``events``, ``exposure`` (years) and ``x`` are per cell (for example one client-day).
    Returns the base annual probability (x = 0), k and the maximum log-likelihood.
    """
    ev = np.asarray(events, dtype=float)
    ex = np.asarray(exposure, dtype=float)
    xx = np.asarray(x, dtype=float)
    n_ev = ev.sum()
    tot = ex.sum()
    h_hat = max(n_ev, 0.5) / tot
    hs = np.geomspace(h_hat / 30, h_hat * 6, 240)
    ks = np.concatenate([[0.0], np.geomspace(0.01, k_max, 159)])
    sx = float(np.sum(ex * xx))
    pos = ev > 0
    ll = np.empty((hs.size, ks.size))
    for j, k in enumerate(ks):
        mult = 1.0 + k * xx[pos]
        ll[:, j] = n_ev * np.log(hs) + float(np.sum(ev[pos] * np.log(mult))) - hs * (tot + k * sx)
    # Jeffreys prior h^-1/2; the h grid is log-spaced (cell width ~ h); flat prior on k with the grid widths
    lp = ll + 0.5 * np.log(hs)[:, None] + np.log(np.gradient(ks))[None, :]
    p = np.exp(lp - lp.max())
    p /= p.sum()
    hlo, hmed, hmode, hhi = _grid_quantiles(hs, p.sum(axis=1))
    klo, kmed, kmode, khi = _grid_quantiles(ks, p.sum(axis=0))
    f = lambda r: 1.0 - math.exp(-r)  # noqa: E731
    base = Param(
        f(hmed),
        f(hlo),
        f(hmode),
        f(hhi),
        int(n_ev),
        source,
        "probability per year",
        "survival with a time-varying covariate (grid posterior)",
        {"events": float(n_ev), "exposure_years": float(tot), "rate_point": hmed},
    )
    kp = Param(kmed, klo, kmode, khi, int(n_ev), source, "multiplier per unit of x", "grid posterior")
    return base, kp, float(ll.max())
