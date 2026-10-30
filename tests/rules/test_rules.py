import numpy as np
import pytest

from variator.domain import Criterion, DecisionModel, DecisionOption, Distribution, MetricSpec, Variable
from variator.rules import RuleError, RuleKind, compile_rules, load_pack
from variator.simulation import simulate_tensor


def _model() -> DecisionModel:
    return DecisionModel(
        question="q",
        metric=MetricSpec(name="profit"),
        variables=[
            Variable(name="x", description="x", distribution=Distribution(kind="uniform", low=0, high=10)),
            Variable(name="penalty", description="helper", distribution=Distribution.fixed(0)),
        ],
        options=[
            DecisionOption(name="a", description="a", formula="x - penalty", metrics={"risk": "x"}),
            DecisionOption(name="b", description="b", formula="5 - penalty", metrics={"risk": "5"}),
        ],
        criteria=[Criterion(name="profit"), Criterion(name="risk", higher_is_better=False)],
    )


PACK = """
pack: p
rules:
  - id: tiers
    kind: effect
    if: x > 8
    then: {set: {penalty: 3}}
    elif:
      - if: x > 5
        then: {set: {penalty: 1}}
    else: {set: {penalty: 0}}
  - id: too_low
    kind: check
    if: x < 2
    then: {fail: "x is too low"}
    options: [a]
  - id: red_flag
    kind: flag
    if: x > 9
    then: {flag: red, criterion: profit}
"""


def _rules(model: DecisionModel, text: str = PACK):
    return compile_rules(
        [load_pack(text)],
        variables=[v.name for v in model.variables],
        options=[o.name for o in model.options],
        criteria=[c.name for c in model.criteria],
    )


def test_if_elif_else_effect_is_vectorized():
    model = _model()
    rules = _rules(model)
    env = {"x": np.array([1.0, 6.0, 9.5]), "penalty": np.zeros(3)}
    out = rules.apply(env, "a", np, 3)
    assert out.env["penalty"].tolist() == [0.0, 1.0, 3.0]
    assert env["penalty"].tolist() == [0.0, 0.0, 0.0]  # input env is not changed
    assert out.failed["too_low"].tolist() == [True, False, False]
    assert out.flags["profit"].tolist() == [-1, -1, 2]


def test_rule_scope_limits_options():
    rules = _rules(_model())
    out = rules.apply({"x": np.array([1.0]), "penalty": np.zeros(1)}, "b", np, 1)
    assert "too_low" not in out.failed


def test_tensor_applies_rules_checks_and_flags():
    model = _model()
    run = simulate_tensor(model, rules=_rules(model), n_scenarios=20_000, seed=3)
    assert run.values.shape == (20_000, 2, 2)
    assert run.check_failures["too_low"][0] == pytest.approx(0.2, abs=0.02)
    assert run.check_failures["too_low"][1] == 0.0
    # option b: profit = 5 - penalty, mean penalty = 0.3*1 + 0.2*3 = 0.9
    assert run.values[:, 1, 0].mean() == pytest.approx(4.1, abs=0.05)
    assert (run.flags[:, :, 0] == 2).mean(axis=0) == pytest.approx([0.1, 0.1], abs=0.02)
    assert (run.flags[:, :, 1] == -1).all()


@pytest.mark.parametrize(
    "text, message",
    [
        ("pack: p\nrules:\n  - id: r\n    if: y > 1\n    then: {set: {x: 1}}", "unknown variables"),
        ("pack: p\nrules:\n  - id: r\n    if: x > 1\n    then: {set: {nope: 1}}", "unknown variable"),
        ("pack: p\nrules:\n  - id: r\n    if: __import__('os')\n    then: {set: {x: 1}}", "r condition"),
        (
            "pack: p\nrules:\n  - id: r\n    kind: flag\n    if: x > 1\n    then: {flag: red, criterion: zz}",
            "unknown criterion",
        ),
    ],
)
def test_bad_rules_fail_at_compile_time(text, message):
    with pytest.raises(RuleError, match=message):
        _rules(_model(), text)


def test_kind_restricts_actions():
    with pytest.raises(ValueError, match="check rule can only"):
        load_pack("pack: p\nrules:\n  - id: r\n    kind: check\n    if: x > 1\n    then: {set: {x: 1}}")
    assert load_pack(PACK).rules[1].kind == RuleKind.CHECK


def test_rules_use_derived_values_and_effects_flow_into_them():
    from variator.domain import Derived

    data = _model().model_dump()
    data["derived"] = [Derived(name="doubled", formula="x * 2").model_dump()]
    data["options"][0]["formula"] = "doubled - penalty"
    model = DecisionModel.model_validate(data)
    text = """
pack: d
rules:
  - id: big
    kind: effect
    if: doubled > 10
    then: {set: {x: 0}}
"""
    rules = compile_rules(
        [load_pack(text)], variables=[v.name for v in model.variables], derived={"doubled": "x * 2"}
    )
    run = simulate_tensor(model, rules=rules, n_scenarios=5_000, seed=1)
    # x in [0, 10]: where x > 5 the rule sets x = 0, so `doubled` (the metric) is 0 there
    assert run.values[:, 0, 0].max() <= 10.0 + 1e-9
    assert (run.values[:, 0, 0] == 0).mean() == pytest.approx(0.5, abs=0.03)
    with pytest.raises(RuleError, match="cannot change derived"):
        compile_rules(
            [load_pack("pack: e\nrules:\n  - id: r\n    if: x > 1\n    then: {set: {doubled: 1}}")],
            variables=["x", "penalty"],
            derived={"doubled": "x * 2"},
        )
