"""NPS habits (v2 datasets): the NPS level of each client, of the cohorts and of the company.

Model: a 0-10 score is a rounded normal around the client's mean score (latent sd from the
within-client spread). The NPS of a client is the expected promoters - detractors of its
score distribution. Client mean scores are pooled to the cohort (``stats.pool_normal``),
so a client with one or two answers is close to the cohort. The company NPS of a survey is
a Dirichlet posterior of the promoters, passives and detractors. Active only when an NPS
dataset exists.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from variator.actors import stats
from variator.actors.context import Context
from variator.actors.stats import Param


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def nps_of_mean(mu: float, sd: float) -> float:
    """Expected NPS points (-100..100) of scores round(mu + sd x N(0, 1)) clipped to 0..10."""
    return 100.0 * ((1.0 - _phi((8.5 - mu) / sd)) - _phi((6.5 - mu) / sd))


@dataclass
class NpsResult:
    clients: dict[str, Param] = field(default_factory=dict)
    company: dict[str, Param] = field(default_factory=dict)
    cohorts: dict[str, dict[str, Param]] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)
    responses: list[dict[str, Any]] = field(default_factory=list)


def _responses(ctx: Context) -> list[dict[str, Any]]:
    h = ctx.h
    name = h.optional.get("nps")
    if not name:
        return []
    rows: list[dict[str, Any]] = []
    if name.startswith("sim_"):
        latest: dict[str, dict] = {}
        for r in h.records.get(name[4:], []):
            latest[r["id"]] = r
        for r in latest.values():
            rows.append(
                {
                    "id": r["id"],
                    "client": r.get("client"),
                    "survey": r.get("survey"),
                    "score": r.get("score"),
                    "contact": r.get("contact"),
                    "date": str(r.get("responded_at") or "")[:10],
                }
            )
    else:
        for r in h.table(name):
            rows.append(
                {
                    "id": r.get("response_id") or r.get("id"),
                    "client": r.get("client"),
                    "survey": r.get("survey"),
                    "score": r.get("score"),
                    "contact": r.get("contact"),
                    "date": str(r.get("date") or "")[:10],
                }
            )
    return [r for r in rows if r["score"] is not None and r["client"]]


def learn_nps(ctx: Context) -> NpsResult:
    h = ctx.h
    res = NpsResult()
    rows = _responses(ctx)
    if len(rows) < 5:
        return res
    name = h.optional["nps"]
    tag = ctx.tag(name)
    res.responses = rows
    has_sim = "nps_response" in h.records
    kb = (lambda i: h.kb("sim", i)) if has_sim else (lambda i: f"{name}:{i}")
    # company NPS per survey and for the whole history
    by_s: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_s[str(r["survey"])].append(float(r["score"]))

    def dirichlet_nps(sc: np.ndarray, label: str) -> Param:
        k = np.array([np.sum(sc >= 9), np.sum((sc >= 7) & (sc <= 8)), np.sum(sc <= 6)], float)
        d = stats.rng(f"nps:{label}").dirichlet(k + 0.5, stats.DRAWS)
        return stats.from_draws(
            100 * (d[:, 0] - d[:, 2]),
            int(sc.size),
            tag,
            "NPS points",
            "Dirichlet posterior of promoters, passives and detractors",
            promoters=int(k[0]),
            passives=int(k[1]),
            detractors=int(k[2]),
        )

    for s, sc in sorted(by_s.items()):
        res.company[f"nps_survey_{s}"] = dirichlet_nps(np.array(sc), s)
    allsc = np.array([float(r["score"]) for r in rows])
    res.company["nps_history"] = dirichlet_nps(allsc, "all")
    last = sorted(by_s)[-1]
    res.company["nps_now"] = res.company[f"nps_survey_{last}"]
    res.info["surveys"] = {s: len(v) for s, v in sorted(by_s.items())}
    # client mean scores, pooled
    per: dict[str, list[float]] = defaultdict(list)
    ev: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        per[r["client"]].append(float(r["score"]))
        ev[r["client"]].append(kb(r["id"]))
    ids = sorted(c for c in per if c in h.clients)
    if len(ids) < 3:
        return res
    fit = _ordinal_pool(ids, [np.array(per[c]) for c in ids])
    sd_latent = fit["sigma"]
    res.company["nps_score_sd_latent"] = stats.fixed(
        sd_latent,
        tag,
        "score points",
        "latent sd of a score around the client mean (marginal ML)",
        n=len(rows),
    )
    f = lambda m: nps_of_mean(m, sd_latent)  # noqa: E731 - monotone in m: quantiles map
    glo, gmed, ghi = fit["group"]
    res.company["nps_level_typical_client"] = Param(
        f(gmed),
        f(glo),
        f(gmed),
        f(ghi),
        len(ids),
        ctx.group("clients"),
        "NPS points",
        "the NPS of the cohort mean score",
        {"spread_score_points": fit["tau"]},
    )
    for c in ids:
        lo, med, mode, hi, w = fit["actors"][c]
        src = tag if w >= 0.5 else ctx.group("clients")
        res.clients[c] = Param(
            f(med),
            f(lo),
            f(mode),
            f(hi),
            len(per[c]),
            src,
            "NPS points",
            "ordinal latent-normal model of the scores, pooled to the cohort (Bayesian grid)",
            {"shrinkage_weight": w, "responses": len(per[c]), "mean_score": float(np.mean(per[c]))},
        )
        res.evidence[c] = ev[c][-4:]
    for coh in ("key", "standard"):
        mem = [c for c in ids if h.clients[c].key == (coh == "key")]
        if mem:
            sc = np.concatenate([per[c] for c in mem])
            res.cohorts.setdefault(coh, {})["nps"] = dirichlet_nps(sc, f"cohort:{coh}")
            res.cohorts[coh]["nps_mean_client_level"] = stats.fixed(
                float(np.mean([res.clients[c].point for c in mem])),
                ctx.group(f"clients:{coh}"),
                "NPS points",
                "mean of the pooled client levels",
                n=len(mem),
            )
    res.info["latent_sd"] = sd_latent
    res.info["spread_score_points"] = fit["tau"]
    return res


_MU = np.linspace(2.0, 13.0, 441)


def _category_logp(sigma: float) -> np.ndarray:
    """log P(score = k | mu) on the grid, k = 0..10: score = round(mu + sigma z) clipped to 0..10."""
    from scipy.special import ndtr

    edges = np.arange(-0.5, 11.0, 1.0)  # k - 0.5 for k = 0..11
    cdf = ndtr((edges[:, None] - _MU[None, :]) / sigma)  # [12, grid]
    cdf[0, :] = 0.0
    cdf[-1, :] = 1.0
    return np.log(np.maximum(np.diff(cdf, axis=0), 1e-300))  # [11, grid]


def _ordinal_pool(ids: list[str], scores: list[np.ndarray], tau_scale: float = 1.0) -> dict[str, Any]:
    """Random-effects ordinal model: mu_i ~ N(m, tau^2); sigma by marginal ML; (m, tau) on a grid."""
    from scipy import optimize
    from scipy.stats import norm

    dmu = _MU[1] - _MU[0]
    counts = np.array([np.bincount(np.clip(s.astype(int), 0, 10), minlength=11) for s in scores], float)

    def lik_mat(sigma: float) -> tuple[np.ndarray, np.ndarray]:
        L = counts @ _category_logp(sigma)  # [actor, grid]
        return np.exp(L - L.max(axis=1, keepdims=True)), L.max(axis=1)

    def nll(th: np.ndarray) -> float:
        m, lt, ls = th
        L, off = lik_mat(math.exp(ls))
        prior = norm.pdf(_MU, m, math.exp(lt)) * dmu
        return -float(np.sum(np.log(np.maximum(L @ prior, 1e-300)) + off))

    allsc = np.concatenate(scores)
    best = optimize.minimize(
        nll,
        np.array([float(np.mean(allsc)), math.log(0.5), math.log(1.2)]),
        method="Nelder-Mead",
        options={"xatol": 1e-4, "fatol": 1e-6, "maxiter": 600},
    )
    sigma = float(math.exp(best.x[2]))
    L, _ = lik_mat(sigma)
    ms = np.linspace(best.x[0] - 1.0, best.x[0] + 1.0, 41)
    taus = np.concatenate([[0.02], np.geomspace(0.03, 3.0, 40)])
    lp = np.empty((ms.size, taus.size))
    priors = {}
    for j, t in enumerate(taus):
        for i, m in enumerate(ms):
            pr = norm.pdf(_MU, m, t) * dmu
            priors[(i, j)] = pr
            lp[i, j] = float(np.sum(np.log(np.maximum(L @ pr, 1e-300))))
    lp += norm.logpdf(taus, 0, tau_scale)[None, :] + np.log(np.gradient(taus))[None, :]
    w = np.exp(lp - lp.max())
    w /= w.sum()
    cells = [(i, j) for i in range(ms.size) for j in range(taus.size) if w[i, j] > w.max() * 1e-5]
    pm = w.sum(axis=1)
    c = np.cumsum(pm / pm.sum())
    group = tuple(float(np.interp(q, c, ms)) for q in (stats.P_LO, 0.5, stats.P_HI))
    pt = w.sum(axis=0)
    ct = np.cumsum(pt / pt.sum())
    tau_med = float(np.interp(0.5, ct, taus))
    P = np.array([priors[x] for x in cells])  # [cell, grid]
    cw = np.array([w[x] for x in cells])
    actors = {}
    for k, a in enumerate(ids):
        z = P @ L[k]
        post = L[k] * ((cw / np.maximum(z, 1e-300)) @ P)
        post /= post.sum()
        cc = np.cumsum(post)
        lo, med, hi = (float(np.interp(q, cc, _MU)) for q in (stats.P_LO, 0.5, stats.P_HI))
        mode = float(_MU[int(np.argmax(post))])
        v_own = sigma * sigma / len(scores[k])
        wt = float(
            np.sum(cw * (taus[[x[1] for x in cells]] ** 2 / (taus[[x[1] for x in cells]] ** 2 + v_own)))
            / cw.sum()
        )
        actors[a] = (lo, med, mode, hi, wt)
    return {"sigma": sigma, "group": group, "tau": tau_med, "actors": actors}
