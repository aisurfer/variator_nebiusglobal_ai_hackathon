from variator.llm import ULTRA
from variator.reasoning import (
    INSUFFICIENT_EVIDENCE,
    RedTeamReview,
    assemble_report,
    prompts,
    red_team,
    sensitivity_flips,
)
from variator.reasoning.schemas import ReportNarrative

RED_TEAM_REPLY = {
    "robustness": "fragile",
    "summary": "The case rests on demand growth continuing.",
    "challenges": [
        {
            "target": "demand_growth",
            "challenge": "Growth range is anchored on a boom period.",
            "why_it_matters": "It is the second largest driver.",
            "how_to_test": "Ask the top 10 clients for 2027 volume commitments.",
        }
    ],
    "blind_spots": ["Competitor price war in the south region"],
    "alternative_option": {
        "name": "hybrid_3pl",
        "description": "Lease a small site and overflow to a 3PL",
        "why_consider": "Keeps optionality while demand is uncertain.",
    },
    "flip_conditions": [
        {"condition": "Margins fall below trend", "variable": "Margin Per Order",
         "flips_to": "outsource_3pl", "plausibility": "medium"},
        {"condition": "Growth stalls", "variable": "demand_growth", "flips_to": "hybrid_3pl",
         "plausibility": "medium"},
        {"condition": "A hurricane", "variable": "weather", "flips_to": "build_in_mexico",
         "plausibility": "low"},
    ],
}


def _report(decision, simulation, make_narrative, **overrides):
    return assemble_report(decision, simulation, ReportNarrative(**make_narrative(**overrides)))


def _user_turn(request: dict) -> str:
    return next(m["content"] for m in request["messages"] if m["role"] == "user")


def test_sensitivity_flips_are_computed_by_code(simulation):
    flips = [(f.variable, f.at, f.challenger) for f in sensitivity_flips(simulation, "second_warehouse")]
    assert flips == [
        ("margin_per_order", "low", "expand_current"),
        ("margin_per_order", "low", "outsource_3pl"),
        ("demand_growth", "low", "expand_current"),
    ]


def test_red_team_request_and_cleanup(fake_tf, decision, simulation, make_narrative):
    report = _report(decision, simulation, make_narrative)
    fake_tf.add_json_reply(RED_TEAM_REPLY)

    review = red_team(fake_tf.client(), decision, report, simulation)

    request = fake_tf.requests[0]
    assert request["model"] == ULTRA.model_id
    assert request["chat_template_kwargs"] == {"enable_thinking": True}
    assert request["response_format"]["json_schema"]["name"] == "RedTeamDraft"
    user = _user_turn(request)
    assert user.startswith(prompts.RED_TEAM_INSTRUCTIONS)
    assert '"recommendation":"second_warehouse"' in user
    assert "- margin_per_order at its p10: outsource_3pl (1.9e+06) beats second_warehouse" in user
    assert "histogram" not in user

    assert isinstance(review, RedTeamReview)
    assert review.robustness == "fragile"
    assert len(review.sensitivity_flips) == 3
    cleaned = [(f.variable, f.flips_to) for f in review.flip_conditions]
    assert cleaned == [
        ("margin_per_order", "outsource_3pl"),
        ("demand_growth", "hybrid_3pl"),  # the alternative option is a valid flip target
        (None, None),  # unknown variable and option are dropped by code
    ]
    assert review.generated_by == ULTRA.model_id


def test_red_team_without_recommendation_stress_tests_best_option(
    fake_tf, decision, simulation, make_narrative
):
    report = _report(decision, simulation, make_narrative, recommendation=INSUFFICIENT_EVIDENCE)
    fake_tf.add_json_reply(RED_TEAM_REPLY)
    review = red_team(fake_tf.client(), decision, report, simulation, llm="super", reasoning=False)
    request = fake_tf.requests[0]
    assert request["chat_template_kwargs"] == {"enable_thinking": False}
    assert '"stress_tested_option":"second_warehouse"' in _user_turn(request)
    assert all(f.recommended == "second_warehouse" for f in review.sensitivity_flips)
