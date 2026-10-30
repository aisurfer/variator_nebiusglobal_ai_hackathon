"""End-to-end check that the modules fit together, fully offline.

question + facts -> Super builds the DecisionModel -> Monte Carlo -> portfolio
optimization -> Ultra report -> Ultra red team; plus the Serverless job entrypoint
running the real simulation and optimization in-process.
"""

from variator.cloud.job_entry import execute
from variator.cloud.job_io import JobInput
from variator.domain import Fact, FactOrigin
from variator.llm import SUPER, ULTRA
from variator.optimization import optimize_decision
from variator.reasoning import analyze_results, build_decision_model, red_team
from variator.simulation import simulate

FACTS = [
    Fact(statement="The company shipped 401,613 orders in the last 12 months.", value=401_613,
         unit="orders", origin=FactOrigin.DATA, source="monthly_orders:orders"),
    Fact(statement="3PL quotes range from $3.62 to $4.35 per order.", value=3.9, unit="USD",
         origin=FactOrigin.DATA, source="tpl_quotes:fee_per_order"),
]


def _llm_decision_payload(decision) -> dict:
    """What Super would return: the example model with fact citations."""
    payload = decision.model_dump(mode="json")
    for var in payload["variables"]:
        var["source"] = {
            "base_orders": "[F1]",
            "tpl_cost_per_order": "[F2]",
        }.get(var["name"], "assumption: management estimate")
    return payload


def _narrative(recommendation: str) -> dict:
    return {
        "recommendation": recommendation,
        "headline": "Expand the current site: best expected profit, 3PL absorbs the overflow.",
        "rationale": ["Highest mean contribution.", "Downside close to the 3PL option."],
        "key_drivers": [{"variable": "demand_growth", "explanation": "Growth decides utilization."}],
        "what_must_be_true": ["Growth stays near the historical pace."],
        "early_warning_signals": ["Two quarters of below-trend orders."],
        "risks_to_watch": ["3PL price hikes on overflow volume."],
        "limitations": ["Capex ranges are contractor estimates."],
        "confidence": "medium",
    }


RED_TEAM = {
    "robustness": "fragile",
    "summary": "Options are close; the call hinges on growth and margin.",
    "challenges": [{"target": "demand_growth", "challenge": "Range may be optimistic.",
                    "why_it_matters": "Drives every option.", "how_to_test": "Check Q4 pipeline."}],
    "blind_spots": ["No financing cost."],
    "alternative_option": {"name": "phased_expansion", "description": "Expand in two steps.",
                           "why_consider": "Defers capex until growth is confirmed."},
    "flip_conditions": [{"condition": "Growth above 20%", "variable": "demand_growth",
                         "flips_to": "second_warehouse", "plausibility": "low"}],
}


def test_full_decision_pipeline(fake_tf, decision):
    client = fake_tf.client()

    fake_tf.add_json_reply(_llm_decision_payload(decision))
    model = build_decision_model(client, decision.question, FACTS)
    assert [o.name for o in model.options] == [o.name for o in decision.options]

    run = simulate(model, n_scenarios=5_000, seed=1)
    optimization = optimize_decision(run, model, solver="highs")
    assert len(optimization.selected) == 1  # all three options are mutually exclusive
    assert optimization.status == "optimal"

    best = run.result.best_by_mean().option
    fake_tf.add_json_reply(_narrative(best))
    report = analyze_results(client, model, run.result, optimization=optimization, facts=FACTS)
    assert report.recommendation == best
    by_option = {row.option: row for row in report.comparison}
    for stats in run.result.options:  # numbers come from the simulation, not the LLM
        assert by_option[stats.option].mean == stats.mean

    fake_tf.add_json_reply(RED_TEAM)
    review = red_team(client, model, report, run.result)
    assert review.alternative_option.name == "phased_expansion"

    assert [r["model"] for r in fake_tf.requests] == [SUPER.model_id, ULTRA.model_id, ULTRA.model_id]
    assert client.ledger.total_cost() > 0
    assert fake_tf.pending == 0


def test_serverless_job_entry_runs_real_engine(decision):
    output = execute(JobInput(task="simulate_and_optimize", decision=decision, n_scenarios=3_000,
                              seed=2, backend="numpy", solver="highs"))
    assert output.ok, output.errors
    assert output.simulation.n_scenarios == 3_000
    assert output.optimization.selected
