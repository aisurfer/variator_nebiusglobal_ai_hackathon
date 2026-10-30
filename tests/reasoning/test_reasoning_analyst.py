import pytest

from variator.domain import MetricSpec
from variator.llm import ULTRA
from variator.reasoning import (
    INSUFFICIENT_EVIDENCE,
    DecisionReport,
    analyze_results,
    assemble_report,
    prompts,
)
from variator.reasoning.schemas import ReportNarrative

OPTION_NAMES = ["second_warehouse", "expand_current", "outsource_3pl"]


def _user_turn(request: dict) -> str:
    return next(m["content"] for m in request["messages"] if m["role"] == "user")


def test_request_shape_uses_ultra_with_reasoning(fake_tf, decision, simulation, make_narrative):
    fake_tf.add_json_reply(make_narrative())
    report = analyze_results(fake_tf.client(), decision, simulation)

    request = fake_tf.requests[0]
    assert request["model"] == ULTRA.model_id
    assert request["chat_template_kwargs"] == {"enable_thinking": True}
    assert request["temperature"] == 1.0
    schema = request["response_format"]["json_schema"]
    assert schema["name"] == "ReportNarrative"
    assert schema["schema"]["properties"]["recommendation"]["enum"] == [*OPTION_NAMES, INSUFFICIENT_EVIDENCE]
    assert _user_turn(request).startswith(prompts.ANALYST_INSTRUCTIONS)
    assert isinstance(report, DecisionReport)
    assert report.generated_by == ULTRA.model_id


def test_simulation_is_sent_compactly(fake_tf, decision, simulation, optimization, make_narrative):
    fake_tf.add_json_reply(make_narrative())
    analyze_results(fake_tf.client(), decision, simulation, optimization=optimization)
    user = _user_turn(fake_tf.requests[0])
    for absent in ("histogram", '"left"', '"count"', "elapsed", '"var_95"', "3456789.123", "0.031234"):
        assert absent not in user, absent
    assert '"mean":3457000' in user and '"prob_loss":0.031' in user
    assert '"top_drivers"' in user and '"risk_hit_rates":{"construction_delay":0.25' in user
    assert '"selected":["second_warehouse"]' in user and '"solver":"highs"' in user


def test_without_optimization_says_not_run(fake_tf, decision, simulation, make_narrative):
    fake_tf.add_json_reply(make_narrative())
    analyze_results(fake_tf.client(), decision, simulation)
    assert "Optimization:\n(not run)" in _user_turn(fake_tf.requests[0])


def test_numbers_are_attached_by_code_not_llm(fake_tf, decision, simulation, optimization,
                                              make_narrative):
    fake_tf.add_json_reply(make_narrative(
        rationale=["It yields a mean profit of $9.9M with only 0.7% chance of loss."],
    ))
    report = analyze_results(fake_tf.client(), decision, simulation, optimization=optimization)

    stats = {o.option: o for o in simulation.options}
    assert [c.option for c in report.comparison] == OPTION_NAMES  # ranked by mean
    for row in report.comparison:
        source = stats[row.option]
        assert (row.mean, row.p5, row.p95, row.prob_loss, row.cvar_95, row.prob_best) == (
            source.mean, source.p5, source.p95, source.prob_loss, source.cvar_95, source.prob_best)
    assert [c.rank for c in report.comparison] == [1, 2, 3]
    assert report.comparison[1].delta_mean_vs_recommended == pytest.approx(3_100_000 - 3_456_789.123)
    assert [c.selected_by_optimizer for c in report.comparison] == [True, False, False]
    assert report.optimization == optimization
    assert report.n_scenarios == 10_000
    assert report.ungrounded_numbers == ["9.9M", "0.7%"]
    assert report.checks == []


def test_narrative_numbers_that_match_data_are_not_flagged(decision, simulation, make_narrative):
    narrative = ReportNarrative(**make_narrative(
        headline="Mean profit of about $3.5M, best in 52% of scenarios, 3.1% loss risk.",
    ))
    report = assemble_report(decision, simulation, narrative)
    assert report.ungrounded_numbers == []


def test_invalid_recommendation_is_withheld(fake_tf, decision, simulation, make_narrative):
    fake_tf.add_json_reply(make_narrative(recommendation="build_a_factory", confidence="high"))
    report = analyze_results(fake_tf.client(), decision, simulation)

    assert report.recommendation == INSUFFICIENT_EVIDENCE
    assert not report.has_recommendation
    assert report.llm_recommendation == "build_a_factory"
    assert report.confidence == "low"
    assert "'build_a_factory'" in report.recommendation_note
    assert report.recommendation_note in report.limitations
    assert all(c.delta_mean_vs_recommended is None for c in report.comparison)
    assert report.key_drivers[0].option == "second_warehouse"  # best by mean is the focal option


def test_recommendation_name_is_normalized(decision, simulation, make_narrative):
    report = assemble_report(decision, simulation, ReportNarrative(**make_narrative(
        recommendation="Expand Current")))
    assert report.recommendation == "expand_current"
    assert "normalized" in report.recommendation_note


def test_insufficient_evidence_is_a_valid_answer(decision, simulation, make_narrative):
    report = assemble_report(decision, simulation, ReportNarrative(**make_narrative(
        recommendation=INSUFFICIENT_EVIDENCE, confidence="medium")))
    assert report.recommendation == INSUFFICIENT_EVIDENCE
    assert report.recommendation_note is None and report.confidence == "medium"


def test_key_drivers_take_numbers_from_sensitivities(decision, simulation, make_narrative):
    narrative = ReportNarrative(**make_narrative(key_drivers=[
        {"variable": "Demand Growth", "explanation": "Growth fills capacity."},
        {"variable": "capex", "explanation": "Upfront spend."},
        {"variable": "weather", "explanation": "Invented."},
    ]))
    report = assemble_report(decision, simulation, narrative)

    drivers = [(d.variable, d.swing, d.explanation) for d in report.key_drivers]
    assert drivers == [
        ("margin_per_order", 3_900_000, None),
        ("demand_growth", 2_400_000, "Growth fills capacity."),
        ("own_cost_per_order", 900_000, None),
        ("capex", None, "Upfront spend."),
    ]
    assert report.key_drivers[0].metric_at_low == 1_500_000
    assert report.key_drivers[0].option == "second_warehouse"
    assert "Dropped driver explanation for unknown variable 'weather'." in report.checks


def test_checks_flag_dominated_recommendation_and_optimizer_disagreement(
    decision, simulation, optimization, make_narrative
):
    narrative = ReportNarrative(**make_narrative(recommendation="outsource_3pl"))
    report = assemble_report(decision, simulation, narrative, optimization=optimization)
    assert any("'second_warehouse' beats the recommended 'outsource_3pl'" in c for c in report.checks)
    assert any("'expand_current' beats" in c for c in report.checks)
    assert any("optimizer (highs" in c for c in report.checks)


def test_lower_is_better_metric_reverses_ranking(decision, simulation, make_narrative):
    costs = simulation.model_copy(update={"metric": MetricSpec(name="3-year cost", higher_is_better=False)})
    report = assemble_report(decision, costs, ReportNarrative(**make_narrative(
        recommendation="outsource_3pl")))
    assert [c.option for c in report.comparison] == list(reversed(OPTION_NAMES))
    assert report.comparison[0].rank == 1


def test_evidence_coverage_from_citations(grounded_decision, simulation, facts, make_narrative):
    report = assemble_report(grounded_decision, simulation, ReportNarrative(**make_narrative()),
                             facts=facts)
    assert report.evidence_coverage == pytest.approx(5 / 8)
    assert not any("cite evidence" in c for c in report.checks)

    no_cites = assemble_report(grounded_decision, simulation, ReportNarrative(**make_narrative()),
                               facts=facts[:1])
    assert no_cites.evidence_coverage == pytest.approx(1 / 8)
    assert "Only 12% of variables cite evidence; the rest are assumptions." in no_cites.checks
