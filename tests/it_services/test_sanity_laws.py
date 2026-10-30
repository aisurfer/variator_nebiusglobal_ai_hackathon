"""Sanity laws (task 21): paired runs with one changed input must move the output the right way.

Each pair uses the same seed, so each variable and risk keeps its own random column. The laws
hold scenario by scenario, not only on the mean.
"""

import numpy as np
import pytest
from variator_it_services import delivery_model, plugin
from variator_it_services.company import company_model

from variator.domain.scenarios import Scenario, ScenarioOp, apply_scenarios
from variator.domain.timeline import with_values
from variator.rules import compile_rules
from variator.simulation import simulate_tensor

N = 2000
SEED = 11
TOL = 1e-6


def _rules(model, template):
    packs = [p for p in plugin.rule_packs if p.templates is None or template in p.templates]
    return compile_rules(
        packs,
        variables=[v.name for v in model.variables],
        options=[o.name for o in model.options],
        criteria=[c.name for c in model.criteria],
        derived=model.derived_map(),
    )


def _run(model, template, probes=None):
    return simulate_tensor(
        model, rules=_rules(model, template), n_scenarios=N, seed=SEED, sampling="random", probes=probes
    )


def _values(run, criterion):
    return run.values[:, :, run.criterion_index(criterion)]


def _not_more(worse, base, scale, strict=True):
    """Each scenario of each option: ``worse`` is at most ``base`` (relative tolerance).

    ``strict``: the mean must also fall, so a change that has no effect cannot pass.
    """
    gap = worse - base
    assert gap.max() <= TOL * scale, f"law broken in {(gap > TOL * scale).mean():.1%} of scenario-options"
    if strict:
        assert gap.mean() < -TOL * scale, "the change had no effect"


# shock variable, bigger value: cash at year end never grows, churn never falls
SHOCKS = {
    "gpu_shortage": 0.4,
    "vendor_price": 0.15,
    "fx_shock": 0.1,
    "regulation_cost": 300_000,
    "competitor_cut": 0.1,
    "key_person_exit": 1,
    "key_account_lost": 0.1,
    "reputation_hit": 0.03,
}


@pytest.fixture(scope="module")
def company_base():
    model = company_model()
    return model, _run(model, "company_model")


@pytest.mark.parametrize("shock", sorted(SHOCKS))
def test_bigger_shock_never_gives_more_cash(company_base, shock):
    model, base = company_base
    hit = _run(with_values(model, {shock: SHOCKS[shock]}), "company_model")
    _not_more(_values(hit, "cash"), _values(base, "cash"), scale=1e7)
    # cost-only shocks (vendor, FX, regulation) leave churn as it is
    _not_more(-_values(hit, "churn"), -_values(base, "churn"), scale=1.0, strict=False)


def test_more_demand_never_gives_less_cash(company_base):
    model, base = company_base
    up = _run(with_values(model, {"demand_growth": 0.25}), "company_model")
    down = _run(with_values(model, {"demand_growth": -0.10}), "company_model")
    _not_more(_values(down, "cash"), _values(up, "cash"), scale=1e7)


def test_more_people_never_give_less_capacity(company_base):
    model, _ = company_base
    probes = {"capacity": "capacity"}
    without = _run(with_values(model, {"lever_hire": 0}), "company_model", probes)
    hired = _run(with_values(model, {"lever_hire": 1}), "company_model", probes)
    _not_more(without.probes["capacity"], hired.probes["capacity"], scale=1.0)


def test_earlier_gpu_reservation_never_gives_a_bigger_hit(company_base):
    model, _ = company_base
    probes = {"gpu_hit": "gpu_hit"}
    shock = {"gpu_shortage": 0.5, "lever_gpu": 1}
    early = _run(with_values(model, {**shock, "m_gpu": 0}), "company_model", probes)
    late = _run(with_values(model, {**shock, "m_gpu": 6}), "company_model", probes)
    _not_more(early.probes["gpu_hit"], late.probes["gpu_hit"], scale=1.0)


@pytest.mark.parametrize(
    ("template", "build"), [("company_model", company_model), ("delivery_model", delivery_model)]
)
def test_more_likely_risks_never_help(template, build):
    model = build()
    primary = model.effective_criteria()[0]
    criterion, higher_is_better = primary.name, primary.higher_is_better
    likely = Scenario(
        name="likely_risks",
        label="All risks more likely",
        description="Each risk probability grows: 1 - (1 - p) * (1 - 0.3)",
        ops=[ScenarioOp(kind="risk", target=r.name, probability=0.3) for r in model.risks],
    )
    base = _run(model, template)
    worse = _run(apply_scenarios(model, [likely], ["likely_risks"]), template)
    sign = 1.0 if higher_is_better else -1.0
    a, b = sign * _values(worse, criterion), sign * _values(base, criterion)
    _not_more(a, b, scale=float(np.abs(b).max()))
