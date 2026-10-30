import copy

from variator.domain import (
    DecisionModel,
    DecisionOption,
    Distribution,
    DistributionKind,
    Risk,
    Variable,
)
from variator.llm import SUPER
from variator.reasoning import (
    Critique,
    IssueKind,
    Severity,
    critique_decision_model,
    lint_decision_model,
    prompts,
    revise_decision_model,
)

CRITIQUE_REPLY = {
    "summary": "Reasonable structure, but capex risk is understated.",
    "issues": [
        {
            "severity": "high",
            "kind": "overconfident_range",
            "target": "capex",
            "problem": "Range ignores permit costs.",
            "suggestion": "Raise the high end.",
        }
    ],
    "missing_risks": [
        {
            "name": "key_client_loss",
            "description": "Top client leaves",
            "variable": "demand_growth",
            "probability": 0.1,
            "rationale": "Customer concentration.",
        }
    ],
    "quality_score": 6,
}


def _user_turn(request: dict) -> str:
    return next(m["content"] for m in request["messages"] if m["role"] == "user")


def test_lint_is_quiet_on_a_sound_model(grounded_decision):
    assert lint_decision_model(grounded_decision) == []


def test_lint_flags_missing_sources(decision):
    issues = lint_decision_model(decision)
    assert {i.target for i in issues} == {"own_cost_per_order", "capex", "expansion_capex",
                                          "capacity_orders"}
    assert all(i.kind == IssueKind.UNGROUNDED and i.severity == Severity.LOW for i in issues)


def test_lint_detects_structural_problems(grounded_decision):
    pert = Distribution(kind=DistributionKind.PERT, low=5, mode=5, high=5)
    model = grounded_decision.model_copy(deep=True)
    model.variables.append(
        Variable(name="unused_rent", description="Rent", distribution=pert, source="assumption: x")
    )
    model.options.append(
        DecisionOption(name="copy_of_3pl", description="dup", formula=model.options[2].formula)
    )
    model.risks.append(
        Risk(name="rent_hike", description="Rent rises", probability=1.0, variable="capex",
             magnitude=Distribution.fixed(0.1), options=["outsource_3pl"])
    )
    model = DecisionModel.model_validate(model.model_dump())  # references are still valid

    found = {(i.target, i.kind, i.severity) for i in lint_decision_model(model)}
    assert ("unused_rent", IssueKind.STRUCTURE, Severity.MEDIUM) in found
    assert ("unused_rent", IssueKind.OVERCONFIDENT_RANGE, Severity.MEDIUM) in found
    assert ("copy_of_3pl", IssueKind.FORMULA_ERROR, Severity.HIGH) in found
    assert ("rent_hike", IssueKind.STRUCTURE, Severity.HIGH) in found  # 3PL formula has no capex
    assert ("rent_hike", IssueKind.IMPLAUSIBLE_RANGE, Severity.LOW) in found


def test_critique_request_and_merge_with_lint(fake_tf, decision, facts):
    fake_tf.add_json_reply(CRITIQUE_REPLY)
    critique = critique_decision_model(fake_tf.client(), decision, facts)

    request = fake_tf.requests[0]
    assert request["model"] == SUPER.model_id
    assert request["chat_template_kwargs"] == {"enable_thinking": True}
    assert request["response_format"]["json_schema"]["name"] == "CritiqueDraft"
    user = _user_turn(request)
    assert user.startswith(prompts.CRITIC_INSTRUCTIONS)
    assert "[F3] In-house fulfilment" in user
    assert "- [low] capex: Variable has no source." in user  # lint is shown to the LLM
    assert '"name":"second_warehouse"' in user  # compact model JSON

    assert isinstance(critique, Critique)
    assert critique.issues[0].target == "capex" and critique.quality_score == 6
    assert len(critique.lint) == 4 and all(i.kind == IssueKind.UNGROUNDED for i in critique.lint)
    assert len(critique.all_issues) == 5
    assert critique.generated_by == SUPER.model_id
    assert critique.needs_revision()


def test_needs_revision_false_for_clean_review():
    clean = Critique(summary="ok", issues=[], missing_risks=[], quality_score=9)
    assert not clean.needs_revision()
    assert clean.needs_revision(min_score=10)


def test_revise_returns_validated_model_and_keeps_question(fake_tf, grounded_decision, facts,
                                                          model_payload):
    revised = copy.deepcopy(model_payload)
    revised["question"] = "A reworded question"
    revised["variables"][5]["distribution"]["high"] = 4_000_000
    broken = copy.deepcopy(revised)
    broken["risks"][0]["options"] = ["new_site"]
    fake_tf.add_json_reply(broken).add_json_reply(revised)
    critique = Critique(**CRITIQUE_REPLY)

    result = revise_decision_model(fake_tf.client(), grounded_decision, critique, facts)

    assert type(result) is DecisionModel
    assert result.question == grounded_decision.question
    assert result.variable("capex").distribution.high == 4_000_000
    first = fake_tf.requests[0]
    assert first["model"] == SUPER.model_id
    assert first["response_format"]["json_schema"]["name"] == "DecisionModel"
    user = _user_turn(first)
    assert user.startswith(prompts.REVISER_INSTRUCTIONS)
    assert "Range ignores permit costs." in user and "key_client_loss" in user
    assert "references unknown options" in fake_tf.requests[1]["messages"][-1]["content"]
