"""company_model: cause-and-effect chains, shocks, levers and rules."""

import numpy as np
import pytest
from variator_it_services import plugin
from variator_it_services.company import DERIVED, RISKS, VARIABLES, company_model

from variator.expr import compile_with_derived
from variator.mcda import rank
from variator.rules import compile_rules
from variator.simulation import simulate, simulate_tensor


def _rules(model):
    packs = [p for p in plugin.rule_packs if p.templates is None or "company_model" in p.templates]
    return compile_rules(
        packs,
        variables=[v.name for v in model.variables],
        options=[o.name for o in model.options],
        criteria=[c.name for c in model.criteria],
        derived=model.derived_map(),
    )


def test_all_factor_blocks_are_present():
    blocks = {b for b, *_ in VARIABLES}
    assert {"finance", "clients", "product", "operations", "people", "market", "external"} <= blocks
    names = {n for _, n, *_ in VARIABLES} | {n for n, *_ in DERIVED}
    for factor in (
        "cac",
        "ltv_cac",
        "ar_days",
        "budget",
        "renewal_due",
        "key_account_lost",
        "roadmap",
        "blockers",
        "deploy_freq",
        "skills",
        "vendor_price",
        "new_tech_gain",
        "gpu_shortage",
        "fx_shock",
    ):
        assert factor in names, factor
    assert len(RISKS) >= 9


def test_deep_chain_compiles_linearly():
    model = company_model()
    f = compile_with_derived("cash_end", model.derived_map())
    assert len(f.steps) >= 15  # cash_end needs most of the chain, each step one time
    assert "gpu_shortage" in f.names and "cash_end" not in f.names


def test_gpu_shortage_chain_moves_every_link():
    model = company_model()
    derived = model.derived_map()
    env = {
        v.name: np.full(2, v.distribution.value if v.distribution.kind == "fixed" else v.distribution.mode)
        for v in model.variables
    }
    env = {k: np.repeat(v[:1], 3) for k, v in env.items()}
    # 0 = no shortage; 0.6 = SLA falls but stays above the target; 1.0 = SLA breach
    env["gpu_shortage"] = np.array([0.0, 0.6, 1.0])
    out = {
        name: compile_with_derived(name, derived).evaluate(env, size=3)
        for name in ("infra_cost", "latency", "sla", "churn", "revenue", "cash_end")
    }
    assert out["infra_cost"][0] < out["infra_cost"][1] < out["infra_cost"][2]
    assert out["latency"][0] < out["latency"][1] < out["latency"][2]
    assert out["sla"][0] > out["sla"][1] > 0.995 > out["sla"][2]
    assert out["churn"][1] == out["churn"][0]  # no SLA breach: clients do not leave
    assert out["churn"][2] > out["churn"][0]  # breach: churn grows
    assert out["revenue"][2] < out["revenue"][0]
    assert out["cash_end"][0] > out["cash_end"][1] > out["cash_end"][2]


def test_levers_give_real_trade_offs():
    model = company_model()
    run = simulate_tensor(model, rules=_rules(model), n_scenarios=6000, seed=3)
    cash_only = rank(run, {"cash": 1, "runway": 0, "churn": 0, "sla": 0, "nps": 0})
    clients = rank(run, {"cash": 0.2, "runway": 0.2, "churn": 1, "sla": 1, "nps": 1})
    assert cash_only.options[0].option == "raise_prices_8"
    assert clients.options[0].option != "raise_prices_8"
    j = run.option_names.index
    sla = run.values[:, :, run.criterion_index("sla")]
    assert sla[:, j("pay_tech_debt")].mean() > sla[:, j("status_quo")].mean()
    assert run.check_failures["runway_low"][j("hire_5")] >= run.check_failures["runway_low"][j("status_quo")]


def test_single_metric_engine_and_tornado_use_the_chain():
    result = simulate(company_model(), n_scenarios=3000, seed=1).result
    drivers = {s.variable for s in result.sensitivities}
    assert "opex" in drivers and drivers <= {v.name for v in company_model().variables}


def test_rule_pack_templates_keep_cases_clean():
    packs = {p.pack: p for p in plugin.rule_packs}
    assert packs["company_basic"].templates == ["company_model"]
    assert packs["outsourcing_basic"].templates == ["delivery_model"]


def test_swing_shares_see_small_drivers():
    from variator.quality import swing_shares

    shares = swing_shares(company_model(), n_scenarios=800, seed=1)
    # outside the top 10 of each tornado; a cut list gave them 0
    assert shares["demand_growth"] > 0 and shares["renewal_due"] > 0


@pytest.mark.parametrize("name", ["status_quo", "raise_prices_8"])
def test_options_only_change_levers(name):
    option = company_model().option(name)
    assert set(option.overrides) <= {"lever_debt", "lever_hire", "lever_gpu", "price_change"}


SIZES = {
    "hire_count": 5,
    "debt_share": 0.15,  # WP16: as the fork text says ("15% of capacity"); 0.12 before
    "gpu_reserve_cost": 300_000,
    "ev_gpu_size": 0.4,
    "ev_vendor_size": 0.1,
    "ev_account_size": 0.1,
}
# WP16 (Plancy 03-REQUIREMENTS §8): people, delivery cost, the limits of the rules and of the runway RAG
SETTINGS = {
    "headcount": 20,
    "engineer_cost": 150_000,
    "delivery_payroll_share": 0.85,
    "burnout_load": 1.05,
    "burnout_attrition": 0.05,
    "runway_cap": 60,
    "runway_green": 18,
    "runway_red": 6,
}


def _defaults(model, **changes):
    env = {
        v.name: np.full(1, v.distribution.value if v.distribution.kind == "fixed" else v.distribution.mode)
        for v in model.variables
    }
    env.update({k: np.full(1, float(x)) for k, x in changes.items()})
    return env


def test_lever_event_sizes_and_limits_are_variables():
    model = company_model()
    values = {v.name: v.distribution.value for v in model.variables if v.name in SIZES | SETTINGS}
    assert values == SIZES | SETTINGS
    formulas = " ".join(f for *_, f in DERIVED)
    assert all(name in formulas for name in SIZES | {"headcount": 0, "engineer_cost": 0, "runway_cap": 0})
    for constant in (
        "0.06 *",
        "0.012",
        "750000",
        "150000",
        "300000",
        "* 0.4 *",
        "ev_vendor * 0.1",
        "ev_account * 0.1",
        "0.12 * lever",
        ", 60)",
    ):
        assert constant not in formulas, constant
    pack = next(p for p in plugin.rule_packs if p.pack == "company_basic")
    rules = " ".join(str(r.model_dump()) for r in pack.rules)
    assert "1.05" not in rules and "+ 0.05" not in rules and "< 6" not in rules


def test_an_edited_size_moves_its_chain():
    model = company_model()
    derived = model.derived_map()

    def value(name, **changes):
        on = {"lever_hire": 1, "lever_debt": 1, "lever_gpu": 1, "ev_gpu": 1, "ev_vendor": 1, "ev_account": 1}
        return float(compile_with_derived(name, derived).evaluate(_defaults(model, **on, **changes))[0])

    # share of the year the hires work; each costs 150k a year; the cost has no other hiring term
    hired = value("hire_effect")
    assert value("hire_cost") == pytest.approx(150_000 * 5 * hired)
    assert value("hire_cost", hire_count=10, engineer_cost=200_000) == pytest.approx(200_000 * 10 * hired)
    assert value("cost", engineer_cost=0) == pytest.approx(value("cost") - value("hire_cost"))
    assert value("load", hire_count=10) < value("load") < value("load", hire_count=0)
    assert value("capacity", debt_share=0.24) < value("capacity")
    more = value("infra_cost", gpu_reserve_cost=600_000) - value("infra_cost")
    assert more == pytest.approx(300_000)  # ordered at month 0: the whole year
    assert value("gpu_total", ev_gpu_size=0.8) == pytest.approx(2 * value("gpu_total"))
    assert value("vendor_total", ev_vendor_size=0.2) == pytest.approx(2 * value("vendor_total"))
    assert value("account_total", ev_account_size=0.2) == pytest.approx(2 * value("account_total"))


# ---------------------------------------------------------------- WP16: the agreed fixes (03-REQUIREMENTS §8)


def _value(model, name, **changes):
    return float(compile_with_derived(name, model.derived_map()).evaluate(_defaults(model, **changes))[0])


def test_debt_share_matches_the_fork_text():
    model = company_model()
    assert model.variable("debt_share").distribution.value == 0.15
    assert "15%" in model.option("pay_tech_debt").description
    assert "15%" in model.variable("lever_debt").description


def test_hiring_cost_starts_when_the_hires_join():
    model = company_model()
    # hiring starts in month 2; with a 3-month lead the people join in month 5: 7 of 12 months are paid
    assert _value(model, "hire_ready", lever_hire=1, m_hire=2, hiring_lead=3) == 5
    paid = _value(model, "hire_cost", lever_hire=1, m_hire=2, hiring_lead=3)
    assert paid == pytest.approx(150_000 * 5 * 7 / 12)
    assert _value(model, "hire_cost", lever_hire=1, m_hire=10, hiring_lead=3) == 0  # they join after the year
    assert "join" in model.option("hire_5").description


def test_hiring_relief_uses_the_headcount():
    model = company_model()
    whole_year = {"lever_hire": 1, "m_hire": 0, "hiring_lead": 0, "team_load": 1.0}
    assert _value(model, "load", **whole_year) == pytest.approx(20 / 25)  # 5 people share the work of 20
    assert _value(model, "load", **whole_year, headcount=40) == pytest.approx(40 / 45)
    assert _value(model, "load", **whole_year, hire_count=100) > 0  # never negative


def test_burnout_uses_the_load_after_the_hires():
    model = company_model()
    rules = _rules(model)
    base = float(_defaults(model)["attrition"][0])

    def attrition(**changes):
        env = _defaults(model, team_load=1.1, m_hire=0, hiring_lead=2, **changes)
        return float(rules.apply(env, "status_quo", np, 1).env["attrition"][0])

    assert attrition() == pytest.approx(base + 0.05)  # no hires: the load 1.1 is above 1.05
    assert attrition(lever_hire=1) == pytest.approx(base)  # 5 hires from month 2: 1.1 x 20 / 24.2 = 0.91
    assert attrition(burnout_load=1.2) == pytest.approx(base)  # the threshold is a variable
    assert attrition(burnout_attrition=0.1) == pytest.approx(base + 0.1)  # and the attrition bump


def test_gross_margin_subtracts_the_delivery_payroll():
    model = company_model()
    v = {n: _value(model, n) for n in ("revenue", "infra_cost", "delivery_payroll", "gross_margin")}
    assert v["delivery_payroll"] == pytest.approx(0.85 * 8_800_000)  # opex mode, no hires, no FX shock
    margin = (v["revenue"] - v["infra_cost"] - v["delivery_payroll"]) / v["revenue"]
    assert v["gross_margin"] == pytest.approx(margin)
    assert 0.1 < v["gross_margin"] < 0.4  # a services margin, not a SaaS one
    assert _value(model, "gross_margin", delivery_payroll_share=0.5) > v["gross_margin"]


def test_runway_rag_uses_fixed_limits_from_variables():
    from variator.domain.timeline import with_values
    from variator.rag import resolve_thresholds

    model = company_model()
    run = simulate_tensor(model, rules=_rules(model), n_scenarios=2000, seed=5)
    runway = run.criterion_index("runway")
    # the baseline median is the 60-month cap: bands from it would have no amber
    assert np.median(run.values[:, 0, runway]) == 60
    limits = resolve_thresholds(run)["runway"]
    assert (limits.green, limits.red) == (18, 6)
    edited = with_values(model, {"runway_green": 24, "runway_red": 9, "runway_cap": 36})
    run2 = simulate_tensor(edited, rules=_rules(edited), n_scenarios=2000, seed=5)
    limits = resolve_thresholds(run2)["runway"]
    assert (limits.green, limits.red) == (24, 9)
    assert run2.values[:, :, runway].max() <= 36
    # the runway check uses the red limit: a higher limit fails more often
    failed = [r.check_failures.get("runway_low", [0.0] * 5) for r in (run, run2)]
    assert sum(failed[1]) > sum(failed[0])
