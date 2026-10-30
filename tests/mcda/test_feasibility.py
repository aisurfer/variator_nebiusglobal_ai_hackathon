"""Task 6: failed check rules lower the rank (score x p_feasible), P(best) and RAG."""

import numpy as np
from variator_it_services import delivery_model, plugin

from variator.mcda import rank
from variator.rag import rag_report, resolve_thresholds
from variator.rules import compile_rules
from variator.simulation import simulate_tensor


def _run():
    model = delivery_model()
    packs = [p for p in plugin.rule_packs if p.templates is None or "delivery_model" in p.templates]
    rules = compile_rules(
        packs,
        variables=[v.name for v in model.variables],
        options=[o.name for o in model.options],
        criteria=[c.name for c in model.effective_criteria()],
        derived=model.derived_map(),
    )
    return simulate_tensor(model, rules=rules, n_scenarios=4096, seed=11, sampling="sobol")


def test_option_that_often_fails_a_check_does_not_win():
    run = _run()
    feas = dict(zip(run.option_names, run.feasible_share(), strict=True))
    assert feas["nearshore_tm"] < 0.3 and feas["offshore_fixed"] == 1.0  # the known case (budget check)
    ranking = rank(run)
    assert ranking.options[0].option == "offshore_fixed"
    near = next(o for o in ranking.options if o.option == "nearshore_tm")
    assert near.p_feasible < 0.3 and any("nearshore_tm" in w for w in ranking.warnings)
    bad = run.infeasible_mask()
    assert bad.shape == run.values.shape[:2] and np.isclose(1 - bad.mean(axis=0), run.feasible_share()).all()


def test_failed_scenarios_are_red_and_not_best():
    run = _run()
    report = rag_report(run, resolve_thresholds(run))
    near = next(o for o in report.options if o.option == "nearshore_tm")
    assert near.overall.red >= 1 - run.feasible_share()[run.option_index("nearshore_tm")] - 1e-9
    ranking = rank(run)
    assert abs(sum(o.prob_best for o in ranking.options) - 1) < 1e-9
