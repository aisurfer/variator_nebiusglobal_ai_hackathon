"""Meaningful differences: gaps below a criterion's threshold are ties in the score and in each scenario.

Min-max scaling turned tiny gaps (0.06 pp of churn) into full score gaps; a threshold per criterion stops
that. The score uses fixed anchors from the baseline (QA M3), so a change of one fork moves only its score.
"""

import numpy as np
import pytest
from variator_it_services import plugin

from variator.domain import Criterion
from variator.domain.timeline import with_values
from variator.mcda import best_path, normalize, rank, snap, with_ties
from variator.rules import compile_rules
from variator.simulation import TensorRun, simulate_tensor


def _plain_normalize(m, higher):
    """The min-max scaling before meaningful differences (kept here as the reference)."""
    lo, hi = m.min(axis=-2, keepdims=True), m.max(axis=-2, keepdims=True)
    span = hi - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        scaled = np.where(span > 0, (m - lo) / np.where(span > 0, span, 1.0), 1.0)
    return np.where(higher, scaled, np.where(span > 0, 1.0 - scaled, 1.0))


@pytest.fixture(scope="module")
def company_run():
    model = plugin.templates["company_model"]()
    packs = [p for p in plugin.rule_packs if p.templates is None or "company_model" in p.templates]
    rules = compile_rules(
        packs,
        variables=[v.name for v in model.variables],
        options=[o.name for o in model.options],
        criteria=[c.name for c in model.effective_criteria()],
        derived=model.derived_map(),
    )
    return simulate_tensor(model, rules=rules, n_scenarios=2048, seed=3)


def _ranking(run):
    r = rank(run)
    return [(o.option, o.score, o.prob_best, o.topsis, o.robustness, o.pareto) for o in r.options]


def test_snap_groups_close_values_and_takes_the_best_of_each_group():
    m = np.array([[0.0], [0.004], [1.0], [1.003]])
    assert snap(m, np.array([True]), [0.01])[:, 0].tolist() == [0.004, 0.004, 1.003, 1.003]
    assert snap(m, np.array([False]), [0.01])[:, 0].tolist() == [0.0, 0.0, 1.0, 1.0]
    assert snap(m, np.array([True]), [0.0]) is m  # a threshold 0 changes nothing
    # two options closer than the threshold always tie, even when a third one is close to both
    chain = np.array([[0.0], [0.6], [1.2]])
    assert len(set(snap(chain, np.array([True]), [1.0])[:, 0])) == 1


def test_all_thresholds_zero_give_exactly_the_plain_ranking(company_run):
    zero = with_ties(company_run, {c.name: 0.0 for c in company_run.criteria})
    x, higher = zero.values, zero.higher_is_better
    assert np.array_equal(normalize(x, higher, np.zeros(x.shape[-1])), _plain_normalize(x, higher))
    criteria = [Criterion(**{**c.model_dump(), "tie_within": 0, "tie_from": None}) for c in zero.criteria]
    plain = TensorRun(**{**zero.__dict__, "criteria": criteria})
    assert _ranking(zero) == _ranking(plain)
    assert best_path(zero).model_dump() == best_path(plain).model_dump()


def test_a_difference_below_the_threshold_never_changes_the_ranking(company_run):
    k = company_run.criterion_index("churn")
    q = 0.005
    rng = np.random.default_rng(0)
    near, flat = company_run.values.copy(), company_run.values.copy()
    base = company_run.values[:, :1, k]
    near[:, :, k] = base + rng.uniform(0, 0.9 * q, size=near[:, :, k].shape)  # spread below q everywhere
    flat[:, :, k] = base  # no spread at all
    ties = {c.name: 0.0 for c in company_run.criteria} | {"churn": q}
    a = with_ties(TensorRun(**{**company_run.__dict__, "values": near}), ties)
    b = with_ties(TensorRun(**{**company_run.__dict__, "values": flat}), ties)
    ra, rb = rank(a), rank(b)
    assert [o.option for o in ra.options] == [o.option for o in rb.options]
    assert [o.prob_best for o in ra.options] == [o.prob_best for o in rb.options]
    # churn adds the same to every fork (the anchors move a little with the baseline's noise)
    gap = lambda r: [o.score - r.options[-1].score for o in r.options]  # noqa: E731
    assert gap(ra) == pytest.approx(gap(rb), abs=1e-12)
    # without the threshold the same noise moves P(best)
    zero = {c.name: 0.0 for c in company_run.criteria}
    assert _ranking(with_ties(a, zero)) != _ranking(with_ties(b, zero))


def test_a_change_of_one_fork_never_changes_the_score_of_another(company_run):
    """Fixed anchors (QA M3): the score of a fork depends on itself and the baseline only."""
    def others(run):
        scores = {o.option: o.score for o in rank(run).options}
        return scores.pop("hire_5"), scores

    own, before = others(company_run)
    j = company_run.option_index("hire_5")
    for factor in (0.5, 1.5):
        values = company_run.values.copy()
        values[:, j, :] *= factor  # a very different hire_5 on every criterion
        mine, after = others(TensorRun(**{**company_run.__dict__, "values": values}))
        assert mine != own and after == before
    # the same through the model: a later hiring start moves only hire_5
    moved = with_values(plugin.templates["company_model"](), {"m_hire": 6})
    packs = [p for p in plugin.rule_packs if p.templates is None or "company_model" in p.templates]
    rules = compile_rules(
        packs,
        variables=[v.name for v in moved.variables],
        options=[o.name for o in moved.options],
        criteria=[c.name for c in moved.effective_criteria()],
        derived=moved.derived_map(),
    )
    mine, after = others(simulate_tensor(moved, rules=rules, n_scenarios=2048, seed=3))
    assert mine != own and after == before


def _two_forks(churn_a, churn_b, n=200):
    criteria = [
        Criterion(name="cash", unit="USD", weight=1.0, tie_within=100_000),
        Criterion(name="churn", unit="share", higher_is_better=False, weight=1.0, tie_within=0.005),
    ]
    noise = np.random.default_rng(1).normal(0, 0.0005, size=n)
    values = np.empty((n, 2, 2))
    values[:, 0, 0], values[:, 1, 0] = 2_500_000, 2_200_000
    values[:, 0, 1], values[:, 1, 1] = churn_a + noise, churn_b + noise
    return TensorRun(
        criteria=criteria, option_names=["a", "b"], values=values, flags=np.full(values.shape, -1),
        rule_hits={}, check_failures={}, seed=0, backend="numpy", elapsed_s=0.0,
    )  # fmt: skip


def test_tie_in_score_p_best_and_the_texts():
    run = _two_forks(0.170, 0.168)  # b is 0.2 pp better on churn: below the 0.5 pp threshold
    r = rank(run)
    assert [o.option for o in r.options] == ["a", "b"] and r.options[0].prob_best == 1.0
    assert "a is better than b on: cash" in r.tradeoffs
    assert "a and b are equal within the meaningful difference on: churn" in r.tradeoffs
    path = best_path(run)
    assert path.ties == ["churn"]
    assert any("within the meaningful difference on: churn" in s for s in path.stays_best)
    # without the threshold b wins churn in every scenario and takes part of P(best)
    plain = with_ties(run, {"cash": 0.0, "churn": 0.0})
    assert {o.option: o.prob_best for o in rank(plain).options}["b"] > 0 and best_path(plain).ties == []
    # a gap above the threshold still counts
    wide = rank(_two_forks(0.180, 0.168))
    assert "b is better than a on: churn" in wide.tradeoffs
