import pytest
from pydantic import ValidationError

from variator.domain import DecisionModel, Distribution, DistributionKind, formula_names


def test_example_decision_is_valid(decision):
    assert {o.name for o in decision.options} == {
        "second_warehouse",
        "expand_current",
        "outsource_3pl",
    }
    # round-trips through JSON, as it will when produced by an LLM
    assert DecisionModel.model_validate_json(decision.model_dump_json()) == decision


def test_formula_names_extracts_variables():
    assert formula_names("3 * minimum(a, b + c) - capex") == {"a", "b", "c", "capex"}


@pytest.mark.parametrize(
    "formula",
    ["__import__('os')", "a.b", "x[0]", "lambda: 1", "open('f')", "'text'", "max(a, key=b)"],
)
def test_formula_names_rejects_unsafe_syntax(formula):
    with pytest.raises(ValueError):
        formula_names(formula)


def test_distribution_requires_params():
    with pytest.raises(ValidationError):
        Distribution(kind=DistributionKind.TRIANGULAR, low=1, high=2)
    with pytest.raises(ValidationError):
        Distribution(kind=DistributionKind.PERT, low=1, mode=3, high=2)


def test_unknown_variable_in_formula_is_rejected(decision):
    data = decision.model_dump()
    data["options"][0]["formula"] = "ghost_variable * 2"
    with pytest.raises(ValidationError, match="unknown variables"):
        DecisionModel.model_validate(data)


def test_example_has_a_real_trade_off(decision):
    from variator.simulation import simulate

    result = simulate(decision, n_scenarios=5_000, seed=7, sensitivity=False).result
    assert all(0.15 < o.prob_best < 0.6 for o in result.options)


def test_risk_names_must_be_identifiers(decision):
    data = decision.model_dump()
    data["risks"][0]["name"] = "Construction delay"
    with pytest.raises(ValidationError):
        DecisionModel.model_validate(data)


def test_identifier_rules(decision):
    data = decision.model_dump()
    data["variables"][0]["name"] = "Demand Growth"
    with pytest.raises(ValidationError):
        DecisionModel.model_validate(data)


def test_derived_values_expand_and_validate():
    import pytest

    from variator.domain import DecisionModel, DecisionOption, Derived, Distribution, MetricSpec, Variable

    def model(derived, formula="b * 2"):
        return DecisionModel(
            question="q",
            metric=MetricSpec(name="m"),
            variables=[Variable(name="x", description="x", distribution=Distribution.fixed(3))],
            options=[DecisionOption(name="o", description="o", formula=formula)],
            derived=derived,
        )

    m = model([Derived(name="a", formula="x + 1"), Derived(name="b", formula="a * a")])
    assert m.expand("b * 2") == "(x + 1) * (x + 1) * 2"
    with pytest.raises(ValueError, match="cycle"):
        model([Derived(name="a", formula="b"), Derived(name="b", formula="a")])
    with pytest.raises(ValueError, match="clash"):
        model([Derived(name="x", formula="1")], formula="x")
    with pytest.raises(ValueError, match="unknown names"):
        model([Derived(name="b", formula="zz")])
