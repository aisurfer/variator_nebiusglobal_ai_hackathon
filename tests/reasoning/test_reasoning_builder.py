import copy

import pytest

from variator.domain import DecisionModel
from variator.llm import SUPER, StructuredOutputError
from variator.reasoning import build_decision_model, prompts
from variator.reasoning.builder import build_request

QUESTION = "Should we open a second warehouse, expand the current one, or outsource to a 3PL?"


def _user_turn(request: dict) -> str:
    return next(m["content"] for m in request["messages"] if m["role"] == "user")


def test_request_shape_uses_super_with_reasoning(fake_tf, facts, model_payload, grounded_decision):
    fake_tf.add_json_reply(model_payload)
    result = build_decision_model(
        fake_tf.client(), QUESTION, facts,
        options_hint=["second warehouse", "3PL"], constraints=["Capex budget 2.5M USD"],
    )
    assert type(result) is DecisionModel
    assert result == grounded_decision

    request = fake_tf.requests[0]
    assert request["model"] == SUPER.model_id
    assert request["chat_template_kwargs"] == {"enable_thinking": True}
    assert request["temperature"] == 1.0  # NVIDIA recommendation with reasoning on
    assert request["max_tokens"] == 32_768  # TF default of 8192 can truncate thinking + JSON
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["name"] == "DecisionModel"
    # structured() owns the single system message; our instructions open the user turn
    assert [m["role"] for m in request["messages"]] == ["system", "user"]
    user = _user_turn(request)
    assert user.startswith(prompts.BUILDER_INSTRUCTIONS)
    assert "- second warehouse" in user and "- Capex budget 2.5M USD" in user
    assert QUESTION in user


def test_schema_sent_is_the_domain_schema(fake_tf, facts, model_payload):
    fake_tf.add_json_reply(model_payload)
    build_decision_model(fake_tf.client(), QUESTION, facts)
    sent = fake_tf.requests[0]["response_format"]["json_schema"]["schema"]
    assert sent == DecisionModel.model_json_schema()


def test_facts_are_numbered_for_citation(facts):
    text = build_request(QUESTION, facts)
    lines = [line for line in text.splitlines() if line.startswith("[F")]
    assert [line.split("]")[0] + "]" for line in lines] == ["[F1]", "[F2]", "[F3]", "[F4]", "[F5]"]
    assert lines[0].startswith("[F1] The company shipped 400,000 orders in 2026.")
    assert "value=400000 orders" in lines[0] and "source=orders_2026.csv" in lines[0]
    assert "origin=web" in lines[3]


def test_reasoning_off_uses_greedy_sampling(fake_tf, facts, model_payload):
    fake_tf.add_json_reply(model_payload)
    build_decision_model(fake_tf.client(), QUESTION, facts, reasoning=False)
    request = fake_tf.requests[0]
    assert request["chat_template_kwargs"] == {"enable_thinking": False}
    assert request["temperature"] == 0.0
    assert request["max_tokens"] == 8_192


def _break_formula(p):
    p["options"][0]["formula"] = "base_orders * mystery_factor - capex"


def _break_identifier(p):
    p["variables"][0]["name"] = "Demand Growth"


def _break_pert(p):
    p["variables"][0]["distribution"] = {"kind": "pert", "low": 0.1, "mode": 0.5, "high": 0.2}


def _break_risk(p):
    p["risks"][0]["variable"] = "permit_cost"


def _break_function(p):
    p["options"][1]["formula"] = "pow(base_orders, 2)"


def _break_group(p):
    p["exclusive_groups"] = [["second_warehouse", "do_nothing"]]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_break_formula, "unknown variables: ['mystery_factor']"),
        (_break_identifier, "snake_case"),
        (_break_pert, "mode must be within"),
        (_break_risk, "targets unknown variable 'permit_cost'"),
        (_break_function, "non-whitelisted function"),
        (_break_group, "exclusive group"),
    ],
)
def test_repair_loop_with_domain_validators(fake_tf, facts, model_payload, grounded_decision,
                                            mutate, expected):
    broken = copy.deepcopy(model_payload)
    mutate(broken)
    fake_tf.add_json_reply(broken).add_json_reply(model_payload)

    result = build_decision_model(fake_tf.client(), QUESTION, facts)

    assert result == grounded_decision
    assert len(fake_tf.requests) == 2
    retry = fake_tf.requests[1]["messages"]
    assert retry[-2]["role"] == "assistant"
    assert retry[-1]["role"] == "user" and "invalid" in retry[-1]["content"]
    assert expected in retry[-1]["content"]


def test_repair_loop_rejects_citations_of_missing_facts(fake_tf, facts, model_payload):
    broken = copy.deepcopy(model_payload)
    broken["variables"][1]["source"] = "[F1][F9]"
    fake_tf.add_json_reply(broken).add_json_reply(model_payload)

    result = build_decision_model(fake_tf.client(), QUESTION, facts)

    assert result.variable("base_orders").source == "[F1]"
    feedback = fake_tf.requests[1]["messages"][-1]["content"]
    assert "'base_orders' cites non-existent facts ['F9']" in feedback
    assert "[F1]..[F5]" in feedback


def test_uncited_source_must_say_assumption(fake_tf, facts, model_payload):
    broken = copy.deepcopy(model_payload)
    broken["variables"][4]["source"] = "web survey"
    fake_tf.add_json_reply(broken).add_json_reply(model_payload)
    build_decision_model(fake_tf.client(), QUESTION, facts)
    assert "start with 'assumption:'" in fake_tf.requests[1]["messages"][-1]["content"]


def test_without_facts_every_source_is_an_assumption(fake_tf, model_payload):
    assumed = copy.deepcopy(model_payload)
    for var in assumed["variables"]:
        var["source"] = "assumption: no data provided"
    fake_tf.add_json_reply(model_payload).add_json_reply(assumed)

    result = build_decision_model(fake_tf.client(), QUESTION, [])

    assert all(v.source.startswith("assumption") for v in result.variables)
    user = _user_turn(fake_tf.requests[0])
    assert "(no evidence provided)" in user and "must be 'assumption: <reason>'" in user
    assert "none (no evidence was provided)" in fake_tf.requests[1]["messages"][-1]["content"]


def test_gives_up_after_max_attempts(fake_tf, facts, model_payload):
    broken = copy.deepcopy(model_payload)
    _break_formula(broken)
    fake_tf.add_json_reply(broken).add_json_reply(broken)
    with pytest.raises(StructuredOutputError, match="DecisionModel"):
        build_decision_model(fake_tf.client(), QUESTION, facts, max_attempts=2)
    assert fake_tf.pending == 0


def test_prompt_teaches_schema_semantics():
    text = prompts.BUILDER_INSTRUCTIONS
    for needle in ("snake_case", "pert", "fixed", "bernoulli", "minimum", "where", "overrides",
                   "multiply", "(1 + magnitude)", "exclusive_groups", "correlations", "assumptions",
                   '"[F3]"', "assumption: <short reason>"):
        assert needle in text, needle
