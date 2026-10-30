import pytest

from variator.domain import DecisionOption, Fact, FactOrigin
from variator.ingestion import ConstraintKind, DecisionBrief, parse_question, to_identifier
from variator.llm import ULTRA, StructuredOutputError

QUESTION = "Should we open a second warehouse, expand the current one or outsource to a 3PL? Budget is $2.5M."

BRIEF = {
    "restated_question": "Choose how to handle 2027 demand growth: new warehouse, expansion or 3PL.",
    "options": [
        {"name": "Open Second Warehouse", "description": "Build a second site in the south"},
        {"name": "expand_current", "description": "Expand the current site"},
        {"name": "3PL outsourcing", "description": "Send growth to a 3PL"},
        {"name": "expand_current", "description": "Duplicate name from the model"},
    ],
    "metric": "3-year profit contribution",
    "metric_unit": "USD",
    "higher_is_better": True,
    "horizon_months": 36.4,
    "constraints": [
        {"kind": "budget", "description": "Capex budget", "value": 2_500_000, "unit": "USD"},
        {"kind": "regulatory", "description": "Permits needed", "value": None, "unit": None},
    ],
    "key_uncertainties": ["demand growth", "3PL fees"],
    "missing_information": ["What is the expected demand growth for 2027?"],
}


def test_parse_question_request_shape_and_brief(fake_tf):
    fake_tf.add_json_reply(BRIEF)
    facts = [
        Fact(statement="Orders were 400,000 in 2026.", value=400_000, origin=FactOrigin.DATA,
             source="orders.csv:orders"),
        Fact(statement="3PL fees average $3.90 per order.", value=3.9, origin=FactOrigin.WEB),
    ]
    brief = parse_question(fake_tf.client(), f"  {QUESTION}\n", context_facts=facts)

    request = fake_tf.requests[0]
    assert request["model"] == ULTRA.model_id  # Ultra designs the case (slide 9), with reasoning on
    assert request["chat_template_kwargs"] == {"enable_thinking": True}
    assert request["response_format"]["json_schema"]["name"] == "DecisionBriefDraft"
    user = request["messages"][1]["content"]
    assert user.startswith("You help a business leader frame a decision")
    # facts come before the question: more stable prefix for caching
    assert user.index("Known facts:") < user.index("Leader's question:")
    assert "- Orders were 400,000 in 2026. [data: orders.csv:orders]" in user
    assert "- 3PL fees average $3.90 per order. [web]" in user
    assert user.rstrip().endswith(QUESTION)

    assert isinstance(brief, DecisionBrief) and brief.question == QUESTION
    assert [o.name for o in brief.options] == [
        "open_second_warehouse", "expand_current", "option_3pl_outsourcing", "expand_current_2"
    ]
    for option in brief.options:  # names are valid DecisionOption identifiers
        DecisionOption(name=option.name, description=option.description, formula="1")
    assert brief.horizon_months == 36
    assert [c.kind for c in brief.constraints] == [ConstraintKind.BUDGET, ConstraintKind.OTHER]
    assert brief.budget() == 2_500_000
    spec = brief.metric_spec()
    assert (spec.name, spec.unit, spec.higher_is_better) == ("3-year profit contribution", "USD", True)


def test_parse_question_without_facts_has_no_facts_block(fake_tf):
    fake_tf.add_json_reply(BRIEF)
    parse_question(fake_tf.client(), "Raise prices by 8%?")
    assert "Known facts" not in fake_tf.requests[0]["messages"][1]["content"]


def test_parse_question_limits_context_facts(fake_tf):
    fake_tf.add_json_reply(BRIEF)
    facts = [Fact(statement=f"Fact number {i}.", origin=FactOrigin.USER) for i in range(10)]
    parse_question(fake_tf.client(), "Q?", context_facts=facts, max_context_facts=3)
    user = fake_tf.requests[0]["messages"][1]["content"]
    assert "Fact number 2." in user and "Fact number 3." not in user


def test_parse_question_empty_raises_without_call(fake_tf):
    with pytest.raises(ValueError):
        parse_question(fake_tf.client(), "   ")
    assert fake_tf.requests == []


def test_parse_question_repairs_then_fails(fake_tf):
    bad = {**BRIEF, "options": []}  # min_length=1 violated
    fake_tf.add_json_reply(bad).add_json_reply(BRIEF)
    assert parse_question(fake_tf.client(), "Q?").options
    assert "invalid" in fake_tf.requests[1]["messages"][-1]["content"]

    fake_tf.add_json_reply(bad).add_json_reply({**BRIEF, "horizon_months": "soon"})
    with pytest.raises(StructuredOutputError):
        parse_question(fake_tf.client(), "Q?")


def test_horizon_is_clamped(fake_tf):
    fake_tf.add_json_reply({**BRIEF, "horizon_months": 0}).add_json_reply({**BRIEF, "horizon_months": 5000})
    client = fake_tf.client()
    assert parse_question(client, "Q?").horizon_months == 1
    assert parse_question(client, "Q?").horizon_months == 600


@pytest.mark.parametrize(
    ("text", "name"),
    [("Open 2nd Warehouse!", "open_2nd_warehouse"), ("  ", "option"), ("2027 plan", "option_2027_plan"),
     ("min", "min_option"), ("class", "class_option"), ("Raise prices +8%", "raise_prices_8")],
)
def test_to_identifier(text, name):
    assert to_identifier(text) == name
