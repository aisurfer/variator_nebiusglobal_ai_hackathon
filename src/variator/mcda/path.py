"""Best path: one plain summary of a ranking for end users.

situation (active presets) -> decision (best option, rule: score x p_feasible) -> conditions
(where it stays best) -> expected result (median, p5..p95 per criterion) -> risks to watch.
The pages compute the same card from the same rule; ``check_model_page.py`` compares them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
from pydantic import BaseModel, Field

from variator.mcda.core import (
    MIN_FEASIBLE,
    criterion_ties,
    decision_matrix,
    feasible_key,
    rank,
    run_anchors,
    score_matrix,
    tensor_stats,
    tied_criteria,
    weight_vector,
)
from variator.simulation.tensor import TensorRun

WEIGHT_FACTORS = (0.5, 2.0)
# A change counts only when the new winner leads by this much (score 0..1). Near-ties are
# Monte Carlo noise: the page and Python use different random streams and disagreed on them.
CLEAR_LEAD = 0.02


class BestPath(BaseModel):
    situation: list[str] = Field(description="Active presets; empty = the template")
    decision: str
    runner_up: str | None
    p_feasible: float
    prob_best: float
    robustness: float
    stays_best: list[str] = Field(description="Conditions in plain sentences")
    breaks_if: list[str] = Field(description="Single changes that give another winner")
    ties: list[str] = Field(
        default_factory=list,
        description="Criteria where the decision and the runner-up differ only within the meaningful difference",  # noqa: E501
    )
    expected: dict[str, dict[str, float]] = Field(
        description="criterion -> {p5, median, p95} of the decision"
    )
    risks: list[str]


def _keys(run: TensorRun, w: np.ndarray, risk_aversion: float, feasible: np.ndarray) -> np.ndarray:
    m = decision_matrix(tensor_stats(run), risk_aversion)
    sm = score_matrix(m, run.higher_is_better, criterion_ties(run), run_anchors(run))
    return (sm @ (w / w.sum())) * feasible


def what_breaks(run: TensorRun, w: np.ndarray, risk_aversion: float) -> list[tuple[str, str]]:
    """(change, new winner) for each single change of a weight (x0.5, x2) or of the risk aversion (0, 1)
    after which another option leads by more than ``CLEAR_LEAD``."""
    feasible = feasible_key(run.feasible_share(), criterion_ties(run))
    base = int(np.argmax(_keys(run, w, risk_aversion, feasible)))

    def clear_winner(keys: np.ndarray) -> int:
        j = int(np.argmax(keys))
        return j if keys[j] - keys[base] > CLEAR_LEAD else base

    out = []
    for k, c in enumerate(run.criteria):
        for f in WEIGHT_FACTORS:
            w2 = w.copy()
            w2[k] *= f
            j = clear_winner(_keys(run, w2, risk_aversion, feasible))
            if j != base:
                out.append((f"weight of {c.name} x{f:g}", run.option_names[j]))
    for a in (0.0, 1.0):
        if a != risk_aversion:
            j = clear_winner(_keys(run, w, a, feasible))
            if j != base:
                out.append((f"risk aversion {a:g}", run.option_names[j]))
    return out


def best_path(
    run: TensorRun,
    weights: Mapping[str, float] | None = None,
    *,
    risk_aversion: float = 0.25,
    situation: Sequence[str] = (),
    red_share: Mapping[str, float] | None = None,
) -> BestPath:
    names = [c.name for c in run.criteria]
    w = weight_vector(names, weights, [c.weight for c in run.criteria])
    ranking = rank(run, weights, risk_aversion=risk_aversion)
    top = ranking.options[0]
    j = run.option_index(top.option)
    stats = tensor_stats(run)
    breaks = what_breaks(run, w, risk_aversion)
    stays = [f"Best in {top.prob_best:.0%} of the scenarios (feasible ones only)."]
    stays.append(f"First for {top.robustness:.0%} of nearby weight sets.")
    if not breaks:
        stays.append("No single weight change (x0.5 or x2) and no risk-aversion change gives another winner.")
    ties: list[str] = []
    if len(ranking.options) > 1:
        runner = run.option_index(ranking.options[1].option)
        m = decision_matrix(stats, risk_aversion)
        ties = [names[k] for k in tied_criteria(m, run.higher_is_better, criterion_ties(run), j, runner)]
        if ties:
            runner_name = ranking.options[1].option
            stays.append(
                f"Equal to {runner_name} within the meaningful difference on: {', '.join(ties)}; "
                "these criteria do not decide."
            )
    risks = [
        f"Check {rule} fails in {shares[j]:.0%} of the scenarios."
        for rule, shares in run.check_failures.items()
        if shares[j] >= 0.005  # below 0.5% the sentence would say "0%"
    ]
    if top.p_feasible < MIN_FEASIBLE:
        risks.append(f"Feasible in only {top.p_feasible:.0%} of the scenarios: treat the result with care.")
    if red_share and red_share.get(top.option, 0) > 0:
        risks.append(f"Red on a weighted criterion in {red_share[top.option]:.0%} of the scenarios.")
    for k, c in enumerate(run.criteria):
        tail = stats["p5"][j, k] if c.higher_is_better else stats["p95"][j, k]
        risks.append(f"Bad 5% of {c.name}: {'below' if c.higher_is_better else 'above'} {tail:.4g} {c.unit}.")
    return BestPath(
        situation=list(situation),
        decision=top.option,
        runner_up=ranking.options[1].option if len(ranking.options) > 1 else None,
        p_feasible=round(top.p_feasible, 4),
        prob_best=round(top.prob_best, 4),
        robustness=round(top.robustness, 4),
        stays_best=stays,
        breaks_if=[f"{change} -> {winner}" for change, winner in breaks],
        ties=ties,
        expected={
            c.name: {
                "p5": float(f"{stats['p5'][j, k]:.4g}"),
                "median": float(f"{stats['p50'][j, k]:.4g}"),
                "p95": float(f"{stats['p95'][j, k]:.4g}"),
            }
            for k, c in enumerate(run.criteria)
        },
        risks=risks,
    )
