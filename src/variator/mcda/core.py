"""Multi-criteria ranking over the scenario tensor, in NumPy only.

Matrix shapes: ``M`` is (options, criteria); ``X`` is (scenarios, options, criteria).
``higher`` is a boolean vector (criteria,) of directions. Weights need not sum to 1;
they are normalized here. Nothing in this module calls an LLM, so a weight change
costs one matrix product.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

import numpy as np
from pydantic import BaseModel, Field

from variator.simulation.tensor import TensorRun

TAIL_SHARE = 0.05
DEFAULT_RISK_AVERSION = 0.25
DEFAULT_CONCENTRATION = 20.0  # Dirichlet: higher means weights stay closer to the user's
MIN_FEASIBLE = 0.8  # below it, rank adds a warning (not a hard cut)
Ties = np.ndarray | Sequence[float] | None  # meaningful difference per criterion (K,); None = no ties
FEASIBLE_TIE = 0.01  # feasible shares closer than 1 point are equal in the order key (``feasible_key``)
ANCHOR_TAIL = 0.01  # anchors: the baseline's 1st and 99th percentile of a criterion (``anchors``)
ANCHOR_WIDTH = 4.0  # an anchor range spans at least this many meaningful differences


def snap(m: np.ndarray, higher: np.ndarray, tie: Ties) -> np.ndarray:
    """Ties within the meaningful difference ``tie`` (per criterion, last axis) over the options axis (-2).

    Sort the options on a criterion; neighbours closer than ``tie`` join one group (so two options closer
    than ``tie`` always tie); each option takes the best value of its group. A tie of 0 changes nothing,
    so all ties 0 give the plain min-max ranking. The pages do the same (``snapTies``).
    """
    if tie is None:
        return m
    tie = np.asarray(tie, dtype=np.float64)
    if m.shape[-2] < 2 or not (tie > 0).any():
        return m
    order = np.argsort(m, axis=-2, kind="stable")
    s = np.take_along_axis(m, order, axis=-2)
    joined = np.diff(s, axis=-2) < tie  # [..., i, k]: sorted positions i and i + 1 are one group
    n = s.shape[-2]
    top, bottom = s.copy(), s.copy()  # the group maximum and minimum at each sorted position
    for i in range(n - 2, -1, -1):
        top[..., i, :] = np.where(joined[..., i, :], top[..., i + 1, :], s[..., i, :])
    for i in range(1, n):
        bottom[..., i, :] = np.where(joined[..., i - 1, :], bottom[..., i - 1, :], s[..., i, :])
    out = np.empty_like(m)
    np.put_along_axis(out, order, np.where(higher, top, bottom), axis=-2)
    return out


def criterion_ties(run: TensorRun) -> np.ndarray:
    """Meaningful difference per criterion (K,) of a run (``Criterion.tie_within``)."""
    return np.array([c.tie_within for c in run.criteria], dtype=np.float64)


def feasible_key(feasible: np.ndarray, tie: Ties) -> np.ndarray:
    """Feasible shares for the order key (score x feasible): a share within ``FEASIBLE_TIE`` of the baseline
    (the first option, last axis) counts as the baseline's.

    Only when a criterion has a meaningful difference: else two forks that tie on every criterion would be
    ordered by a 0.02-point gap of the feasible share. Each fork against the baseline only, so a change of one
    fork never moves another one. All ties 0 keep the shares as they are."""
    if tie is None or not (np.asarray(tie) > 0).any():
        return feasible
    base = feasible[..., :1]
    return np.where(np.abs(feasible - base) < FEASIBLE_TIE, base, feasible)


@dataclass(frozen=True)
class Anchors:
    """Fixed scale of each criterion (K,): 0 at ``lo``, 1 at ``lo + span`` (before the direction).

    From the baseline fork only, so the score of a fork does not depend on the other forks (no rank reversal
    when a fork changes or leaves)."""

    lo: np.ndarray
    span: np.ndarray


def anchors(base: np.ndarray, tie: Ties = None) -> Anchors:
    """Anchors from the baseline's values over the scenarios ``base`` (S, K): its 1st to 99th percentile,
    widened around the middle to at least ``ANCHOR_WIDTH`` meaningful differences (a tiny range cannot blow
    up a tiny gap). The pages compute the same (``anchorsFor``)."""
    tie = np.zeros(base.shape[-1]) if tie is None else np.asarray(tie, dtype=np.float64)
    p_lo, p_hi = np.quantile(base, [ANCHOR_TAIL, 1 - ANCHOR_TAIL], axis=0)
    centre = (p_lo + p_hi) / 2
    width = np.maximum(p_hi - p_lo, ANCHOR_WIDTH * tie)
    width = np.where(width > 0, width, np.maximum(np.abs(centre), 1.0) * 1e-9)
    return Anchors(lo=centre - width / 2, span=width)


def run_anchors(run: TensorRun) -> Anchors:
    """The anchors of a run: its first option is the baseline."""
    return anchors(run.values[:, 0, :], criterion_ties(run))


def scaled(m: np.ndarray, higher: np.ndarray, a: Anchors) -> np.ndarray:
    """Each criterion on its fixed anchors, clamped to [0, 1], 1 = best."""
    v = np.clip((m - a.lo) / a.span, 0.0, 1.0)
    return np.where(higher, v, 1.0 - v)


def score_matrix(m: np.ndarray, higher: np.ndarray, tie: Ties, a: Anchors) -> np.ndarray:
    """The decision matrix (O, K) scaled for the score: only the part of a gap to the baseline (row 0) beyond
    the meaningful difference counts (a smaller gap is a tie; no jump at the threshold), then the fixed
    anchors. Each row depends on itself and the baseline only."""
    if tie is not None:
        tie = np.asarray(tie, dtype=np.float64)
        base = m[..., :1, :]  # options on axis -2
        d = m - base
        m = np.where(tie > 0, base + np.sign(d) * np.maximum(np.abs(d) - tie, 0.0), m)
    return scaled(m, higher, a)


def scenario_scores(x: np.ndarray, higher: np.ndarray, w: np.ndarray, tie: Ties, a: Anchors) -> np.ndarray:
    """Weighted score per scenario and option (S, O) for P(best): in each scenario the options within the
    meaningful difference of each other tie (``snap``), then the same fixed anchors as the score."""
    return scaled(snap(x, higher, tie), higher, a) @ w


def with_ties(run: TensorRun, ties: Mapping[str, float]) -> TensorRun:
    """A copy of ``run`` whose criteria have these meaningful differences (criterion name -> value).

    For runs built outside ``simulate_tensor`` (for example ``metrics.cube.to_tensor_run``)."""
    unknown = set(ties) - {c.name for c in run.criteria}
    if unknown:
        raise ValueError(f"meaningful differences for unknown criteria {sorted(unknown)}")
    criteria = [
        c.model_copy(update={"tie_within": float(ties[c.name])}) if c.name in ties else c
        for c in run.criteria
    ]
    return replace(run, criteria=criteria)


def normalize(m: np.ndarray, higher: np.ndarray, tie: Ties = None) -> np.ndarray:
    """Min-max scale each criterion over the options axis (-2) to [0, 1], 1 = best.

    A criterion where all options are equal gets 1.0 for all, so it does not change the ranking.
    ``tie``: options within the meaningful difference count as equal first (``snap``).
    """
    m = snap(m, higher, tie)
    lo = m.min(axis=-2, keepdims=True)
    hi = m.max(axis=-2, keepdims=True)
    span = hi - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        scaled = np.where(span > 0, (m - lo) / np.where(span > 0, span, 1.0), 1.0)
    return np.where(higher, scaled, np.where(span > 0, 1.0 - scaled, 1.0))


def weight_vector(
    criteria: Sequence[str], weights: Mapping[str, float] | None, defaults: Sequence[float]
) -> np.ndarray:
    """Weights in criterion order, normalized to sum 1; unknown names raise ``ValueError``."""
    w = np.array(defaults, dtype=np.float64)
    if weights:
        unknown = set(weights) - set(criteria)
        if unknown:
            raise ValueError(f"weights for unknown criteria {sorted(unknown)}")
        for i, name in enumerate(criteria):
            if name in weights:
                w[i] = float(weights[name])
    if (w < 0).any():
        raise ValueError("weights must be >= 0")
    total = w.sum()
    if total <= 0:
        raise ValueError("at least one weight must be > 0")
    return w / total


def weighted_scores(
    x: np.ndarray, higher: np.ndarray, w: np.ndarray, tie: Ties = None
) -> np.ndarray:
    """Score per scenario and option (S, O): options normalized inside each scenario."""
    return normalize(x, higher, tie) @ w


def topsis(
    m: np.ndarray, higher: np.ndarray, w: np.ndarray, tie: Ties = None
) -> np.ndarray:
    """TOPSIS closeness (O,) in [0, 1]: distance to the anti-ideal over total distance."""
    m = snap(m, higher, tie)
    norms = np.linalg.norm(m, axis=0)
    v = m / np.where(norms > 0, norms, 1.0) * w
    ideal = np.where(higher, v.max(axis=0), v.min(axis=0))
    anti = np.where(higher, v.min(axis=0), v.max(axis=0))
    d_plus = np.linalg.norm(v - ideal, axis=1)
    d_minus = np.linalg.norm(v - anti, axis=1)
    total = d_plus + d_minus
    return np.where(total > 0, d_minus / np.where(total > 0, total, 1.0), 1.0)


def pareto_mask(m: np.ndarray, higher: np.ndarray, tie: Ties = None) -> np.ndarray:
    """True for options that no other option dominates (better or equal on all, better on one)."""
    signed = np.where(higher, snap(m, higher, tie), -snap(m, higher, tie))  # larger is better on every column
    ge = (signed[:, None, :] >= signed[None, :, :]).all(axis=2)
    gt = (signed[:, None, :] > signed[None, :, :]).any(axis=2)
    dominated = (ge & gt).any(axis=0)  # [i, j]: i dominates j
    return ~dominated


def weight_robustness(
    m: np.ndarray,
    higher: np.ndarray,
    w: np.ndarray,
    *,
    concentration: float = DEFAULT_CONCENTRATION,
    samples: int = 4_000,
    seed: int | None = 0,
    feasible: np.ndarray | None = None,
    tie: Ties = None,
) -> np.ndarray:
    """Share of Dirichlet weight samples around ``w`` for which each option is first (O,).

    ``feasible`` (O,) multiplies the scores, as in ``rank`` (rule A of task 6)."""
    rng = np.random.default_rng(seed)
    alpha = np.maximum(w * concentration * len(w), 1e-3)
    draws = rng.dirichlet(alpha, size=samples)  # (samples, K)
    scores = normalize(m, higher, tie) @ draws.T  # (O, samples)
    if feasible is not None:
        scores = scores * feasible[:, None]
    winners = scores.argmax(axis=0)
    return np.bincount(winners, minlength=m.shape[0]) / samples


def anchored_robustness(
    sm: np.ndarray,
    w: np.ndarray,
    *,
    concentration: float = DEFAULT_CONCENTRATION,
    samples: int = 4_000,
    seed: int | None = 0,
    feasible: np.ndarray | None = None,
) -> np.ndarray:
    """``weight_robustness`` on a scaled score matrix ``sm`` (O, K) (``score_matrix``)."""
    rng = np.random.default_rng(seed)
    alpha = np.maximum(w * concentration * len(w), 1e-3)
    scores = sm @ rng.dirichlet(alpha, size=samples).T  # (O, samples)
    if feasible is not None:
        scores = scores * feasible[:, None]
    return np.bincount(scores.argmax(axis=0), minlength=sm.shape[0]) / samples


class CriterionStats(BaseModel):
    mean: float
    p5: float
    p50: float
    p95: float
    cvar: float = Field(description="Mean of the worst 5% (direction-aware)")


class RankedOption(BaseModel):
    option: str
    rank: int
    score: float = Field(description="Weighted score of the decision matrix, 0..1")
    topsis: float
    prob_best: float = Field(description="Share of scenarios with the highest weighted score (feasible only)")
    robustness: float = Field(description="Share of nearby weight vectors where this option is first")
    pareto: bool
    p_feasible: float = Field(default=1.0, description="Share of scenarios where no check rule fails")
    stats: dict[str, CriterionStats]


class Ranking(BaseModel):
    weights: dict[str, float]
    risk_aversion: float
    options: list[RankedOption]
    tradeoffs: list[str] = Field(default_factory=list)
    reversals: list[str] = Field(
        default_factory=list,
        description="Options whose removal changes the order of the others (min-max rank reversal)",
    )
    warnings: list[str] = Field(
        default_factory=list, description="For example options that often fail a check"
    )


def rank_reversals(
    m: np.ndarray,
    higher: np.ndarray,
    w: np.ndarray,
    options: Sequence[str],
    feasible: np.ndarray | None = None,
    tie: Ties = None,
) -> list[str]:
    """Remove each option in turn; return the options whose removal reorders the others.

    Min-max scales over the options, so one option can move the range of a criterion.
    Absolute anchors were tested as a fix and rejected (2026-10-26): 32-58% of the cells
    saturate at 0 or 1, the effective weights change, and default anchors also depend
    on an option (the baseline). This check shows the risk instead.
    """
    f = np.ones(len(options)) if feasible is None else feasible
    full = np.argsort(-(normalize(m, higher, tie) @ w) * f, kind="stable")
    out = []
    for drop in range(len(options)):
        keep = [j for j in range(len(options)) if j != drop]
        sub = np.argsort(-(normalize(m[keep], higher, tie) @ w) * f[keep], kind="stable")
        if [keep[j] for j in sub] != [j for j in full if j != drop]:
            out.append(options[drop])
    return out


def tensor_stats(run: TensorRun) -> dict[str, np.ndarray]:
    """Statistics per option and criterion, each an (O, K) array."""
    x = run.values
    k_tail = max(1, int(np.ceil(TAIL_SHARE * x.shape[0])))
    ordered = np.sort(x, axis=0)
    worst_low = ordered[:k_tail].mean(axis=0)
    worst_high = ordered[-k_tail:].mean(axis=0)
    p5, p50, p95 = np.quantile(x, [0.05, 0.5, 0.95], axis=0)
    return {
        "mean": x.mean(axis=0),
        "p5": p5,
        "p50": p50,
        "p95": p95,
        "cvar": np.where(run.higher_is_better, worst_low, worst_high),
    }


def decision_matrix(stats: Mapping[str, np.ndarray], risk_aversion: float) -> np.ndarray:
    """Blend of the mean and the bad tail: (1 - a) * mean + a * cvar."""
    if not 0 <= risk_aversion <= 1:
        raise ValueError("risk_aversion must be in [0, 1]")
    return (1 - risk_aversion) * stats["mean"] + risk_aversion * stats["cvar"]


def rank(
    run: TensorRun,
    weights: Mapping[str, float] | None = None,
    *,
    risk_aversion: float = DEFAULT_RISK_AVERSION,
    seed: int | None = 0,
) -> Ranking:
    """Rank the options of ``run`` for the given weights (criterion defaults if None).

    Failed check rules (task 6, rule A, chosen by measurement on 2026-10-26): the order uses
    ``score * p_feasible``; in each scenario only feasible options compete for P(best).
    Rejected: a hard threshold (a cliff; when all options fail often it ignores the checks)
    and a per-scenario score (it also changes rankings where no check fails).

    Scaling (QA M3, 2026-10-28): fixed anchors from the baseline (``anchors``), not min-max over the
    forks, so an edit of one fork changes only its own score. Equal keys keep the option order (the
    baseline first), as on the pages.
    """
    names = [c.name for c in run.criteria]
    higher = run.higher_is_better
    w = weight_vector(names, weights, [c.weight for c in run.criteria])
    tie = criterion_ties(run)  # gaps below the meaningful difference are ties, here and per scenario
    a = run_anchors(run)
    stats = tensor_stats(run)
    m = decision_matrix(stats, risk_aversion)
    sm = score_matrix(m, higher, tie, a)
    score = sm @ w
    feasible = run.feasible_share()
    close = topsis(m, higher, w, tie)
    bad = run.infeasible_mask()
    per_scenario = np.where(bad, -np.inf, scenario_scores(run.values, higher, w, tie, a))
    best = per_scenario.max(axis=1, keepdims=True)
    ties = ((per_scenario == best) & ~bad).astype(np.float64)
    counted = ties.sum(axis=1) > 0  # a scenario where every option fails does not count
    prob_best = (ties[counted] / ties[counted].sum(axis=1, keepdims=True)).sum(axis=0) / max(counted.sum(), 1)
    fkey = feasible_key(feasible, tie)
    robust = anchored_robustness(sm, w, seed=seed, feasible=fkey)
    front = pareto_mask(
        np.concatenate([stats["mean"], stats["cvar"]], axis=1),
        np.concatenate([higher, higher]),
        np.concatenate([tie, tie]),
    )
    order = np.argsort(-(score * fkey), kind="stable")
    ranked = []
    for position, j in enumerate(order, start=1):
        ranked.append(
            RankedOption(
                option=run.option_names[j],
                rank=position,
                score=float(score[j]),
                topsis=float(close[j]),
                prob_best=float(prob_best[j]),
                robustness=float(robust[j]),
                pareto=bool(front[j]),
                p_feasible=float(feasible[j]),
                stats={
                    name: CriterionStats(**{key: float(stats[key][j, k]) for key in stats})
                    for k, name in enumerate(names)
                },
            )
        )
    tradeoffs = _tradeoffs(ranked[:2], names, higher, m, run.option_names, tie) if len(ranked) > 1 else []
    return Ranking(
        weights={name: float(v) for name, v in zip(names, w, strict=True)},
        risk_aversion=risk_aversion,
        options=ranked,
        tradeoffs=tradeoffs,
        reversals=[],  # fixed anchors: removing a fork never reorders the others

        warnings=[
            f"{o.option} fails a check in {1 - o.p_feasible:.0%} of scenarios"
            for o in ranked
            if o.p_feasible < MIN_FEASIBLE
        ],
    )


def tied_criteria(m: np.ndarray, higher: np.ndarray, tie: np.ndarray, a: int, b: int) -> list[int]:
    """Criteria where options ``a`` and ``b`` differ, but by less than the meaningful difference."""
    return [k for k in range(m.shape[1]) if 0 < abs(m[a, k] - m[b, k]) < tie[k]]


def _better(m: np.ndarray, higher: np.ndarray, tie: np.ndarray, a: int, b: int, k: int) -> bool:
    """``a`` is better than ``b`` on criterion ``k`` by at least the meaningful difference."""
    gap = (m[a, k] - m[b, k]) if higher[k] else (m[b, k] - m[a, k])
    return gap > 0 and gap >= tie[k]


def _tradeoffs(
    top: list[RankedOption],
    names: list[str],
    higher: np.ndarray,
    m: np.ndarray,
    options: list[str],
    tie: np.ndarray | None = None,
) -> list[str]:
    a, b = (options.index(o.option) for o in top)
    tie = np.zeros(len(names)) if tie is None else tie
    a_wins = [n for k, n in enumerate(names) if _better(m, higher, tie, a, b, k)]
    b_wins = [n for k, n in enumerate(names) if _better(m, higher, tie, b, a, k)]
    lines = []
    if a_wins:
        lines.append(f"{options[a]} is better than {options[b]} on: {', '.join(a_wins)}")
    if b_wins:
        lines.append(f"{options[b]} is better than {options[a]} on: {', '.join(b_wins)}")
    equal = [names[k] for k in tied_criteria(m, higher, tie, a, b)]
    if equal:
        lines.append(
            f"{options[a]} and {options[b]} are equal within the meaningful difference on: {', '.join(equal)}"
        )
    return lines
