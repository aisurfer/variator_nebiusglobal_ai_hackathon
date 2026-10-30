"""Timeline of the delivery template: time formulas, P(met), inputs in conflict, plan, suggested dates."""

import numpy as np
import pytest
from variator_it_services import plugin
from variator_it_services.timeline import progress_milestone

from variator.domain import Timeline, TimelineItem, with_values
from variator.rules import compile_rules
from variator.simulation import simulate_tensor
from variator.timeline import evaluate, solve_plan, suggest

N = 1000
OLD_DURATION = (
    "ramp_up_months + scope_points * (1 + rework_share) / (team_size * productivity) + risk_delay_months"
)


def _model():
    return plugin.templates["delivery_model"]()


def _rules(model):
    packs = [p for p in plugin.rule_packs if p.templates is None or "delivery_model" in p.templates]
    return compile_rules(
        packs,
        variables=[v.name for v in model.variables],
        options=[o.name for o in model.options],
        criteria=[c.name for c in model.effective_criteria()],
        derived=model.derived_map(),
    )


def _probe(model, formulas, n=N):
    return simulate_tensor(model, rules=_rules(model), n_scenarios=n, seed=3, probes=formulas).probes


def test_probes_give_one_value_per_scenario_and_option():
    probes = _probe(_model(), {"d": "duration", "late": "duration > target_months"})
    assert probes["d"].shape == (N, 3)
    assert set(np.unique(probes["late"])) <= {0.0, 1.0}


def test_neutral_timeline_keeps_the_old_duration():
    probes = _probe(_model(), {"new": "duration", "old": OLD_DURATION})
    assert np.allclose(probes["new"], probes["old"])  # the load-test blocker binds in none of these scenarios


def test_go_live_waits_for_the_load_tests():
    late_env = with_values(_model(), {"env_start": 12})
    probes = _probe(late_env, {"d": "duration", "gate": "env_ready + load_test_months"})
    assert np.all(probes["d"] >= probes["gate"] - 1e-9)
    assert (probes["d"] > _probe(_model(), {"d": "duration"})["d"]).mean() > 0.9


def test_extra_people_shorten_the_work_and_vendor_people_come_sooner():
    base = _probe(_model(), {"d": "duration"})["d"].mean()
    hire = _probe(with_values(_model(), {"add_fte": 4}), {"b": "beta_time", "d": "duration"})
    vendor = _probe(with_values(_model(), {"add_fte": 4, "add_outsourced": 1}), {"b": "beta_time"})
    assert hire["d"].mean() < base
    assert vendor["b"].mean() < hire["b"].mean()


def test_events_change_the_time_only_when_they_come_in_time():
    model = _model()
    base = _probe(model, {"b": "beta_time", "d": "duration"})
    kp = _probe(with_values(model, {"ev_kp": 1, "ev_kp_month": 2}), {"b": "beta_time", "d": "duration"})
    late_scope = _probe(with_values(model, {"ev_scope_points": 100, "ev_scope_month": 40}), {"d": "duration"})
    assert kp["b"].mean() > base["b"].mean() and kp["d"].mean() > base["d"].mean()
    assert np.allclose(late_scope["d"], base["d"])


def test_an_earlier_milestone_is_met_less_often_and_names_the_input_in_conflict():
    model, timeline = _model(), plugin.timelines["delivery_model"]
    rules = _rules(model)
    base = evaluate(model, timeline, rules, n_scenarios=N)
    early = evaluate(model, timeline, rules, n_scenarios=N, moves={"m_env": 2.5})
    env = lambda res: next(i for i in res.items if i.name == "load_test_env")  # noqa: E731
    assert env(early).p_met["in_house"] < env(base).p_met["in_house"]
    assert any(
        c.item == "load_test_env" and c.variable == "env_lead" and c.direction == "too high"
        for c in early.conflicts
    )


def test_unknown_event_and_unknown_variable_raise():
    model, timeline = _model(), plugin.timelines["delivery_model"]
    with pytest.raises(ValueError, match="no event"):
        evaluate(model, timeline, n_scenarios=10, events=["meteor"])
    bad = Timeline(horizon=12, items=[TimelineItem(name="x", label="x", kind="plan", month="no_such_month")])
    with pytest.raises(ValueError, match="unknown variables"):
        bad.check(model)


def test_with_values_fixes_the_variable_for_every_option():
    model = with_values(_model(), {"team_size": 5})
    assert all("team_size" not in o.overrides for o in model.options)


def test_plan_finds_how_many_people_and_meets_the_milestones():
    model, full = _model(), plugin.timelines["delivery_model"]
    timeline = full.model_copy(update={"levers": [lv for lv in full.levers if lv.variable == "add_fte"]})
    rules = _rules(model)
    plan = solve_plan(model, timeline, "nearshore_tm", rules, n_scenarios=500, seed=7)
    assert plan.met
    step = plan.steps[0]
    assert step.variable == "add_fte" and step.value >= 1 and step.mode in {"hire", "outsource"}
    check = evaluate(
        model,
        timeline,
        rules,
        n_scenarios=500,
        seed=7,
        moves={"add_fte": step.value, "add_outsourced": timeline.levers[0].modes[step.mode]},
    )
    assert all(i.met("nearshore_tm") for i in check.items)


def test_suggested_dates_are_met_in_about_80_percent():
    model, timeline = _model(), plugin.timelines["delivery_model"]
    rules = _rules(model)
    moves = suggest(evaluate(model, timeline, rules, n_scenarios=N), timeline, "offshore_fixed")
    assert set(moves) == {"m_beta", "m_env", "target_months"}
    after = evaluate(model, timeline, rules, n_scenarios=N, moves=moves)
    for item in after.items:
        if item.kind == "milestone":
            assert item.p_met["offshore_fixed"] >= 0.78


def _company():
    model = plugin.templates["company_model"]()
    packs = [p for p in plugin.rule_packs if p.templates is None or "company_model" in p.templates]
    rules = compile_rules(
        packs,
        variables=[v.name for v in model.variables],
        options=[o.name for o in model.options],
        criteria=[c.name for c in model.effective_criteria()],
        derived=model.derived_map(),
    )
    return model, plugin.timelines["company_model"], rules


def test_company_lever_milestones_count_only_for_their_option():
    model, timeline, rules = _company()
    res = {i.name: i for i in evaluate(model, timeline, rules, n_scenarios=N).items}
    assert set(res["gpu_ready"].p_met) == {"reserve_gpu"}
    assert set(res["cash_goal"].p_met) == {o.name for o in model.options}


def test_company_placed_event_and_a_late_start_hurt():
    model, timeline, rules = _company()
    base = {i.name: i for i in evaluate(model, timeline, rules, n_scenarios=N).items}
    shock = {
        i.name: i
        for i in evaluate(
            model, timeline, rules, n_scenarios=N, events=["account_event"], moves={"ev_account_month": 1}
        ).items
    }
    assert shock["cash_goal"].p_met["status_quo"] < base["cash_goal"].p_met["status_quo"]
    late = {i.name: i for i in evaluate(model, timeline, rules, n_scenarios=N, moves={"m_gpu": 4}).items}
    assert late["gpu_ready"].p_met["reserve_gpu"] < base["gpu_ready"].p_met["reserve_gpu"]


def test_company_plan_uses_only_the_lever_of_the_option():
    model, timeline, rules = _company()
    plan = solve_plan(model, timeline, "reserve_gpu", rules, n_scenarios=300, seed=7)
    assert [s.variable for s in plan.steps] == ["m_gpu"]
    assert solve_plan(model, timeline, "status_quo", rules, n_scenarios=300, seed=7).steps == []


def test_own_progress_milestone_matches_the_beta():
    model, timeline = _model(), plugin.timelines["delivery_model"]
    own = timeline.model_copy(update={"items": [*timeline.items, progress_milestone("half", "Half", 0.5, 6)]})
    res = {i.name: i for i in evaluate(model, own, _rules(model), n_scenarios=N).items}
    assert res["half"].p_met == res["beta"].p_met
