import json

import pytest

from variator.reasoning import compact_simulation, format_facts
from variator.reasoning.context import compact_optimization, round_sig, to_json
from variator.reasoning.grounding import (
    allowed_numbers,
    citation_errors,
    cited_fact_ids,
    numbers_in_text,
    ungrounded_numbers,
)


def test_format_facts_numbers_from_one(facts):
    lines = format_facts(facts).splitlines()
    assert len(lines) == 5
    assert lines[0] == (
        "[F1] The company shipped 400,000 orders in 2026. | value=400000 orders | period=FY2026"
        " | origin=data | source=orders_2026.csv | confidence=0.95"
    )
    assert lines[4].startswith("[F5] Order volume grew") and "origin=user" in lines[4]
    assert format_facts([]) == "(no evidence provided)"


@pytest.mark.parametrize(
    ("value", "digits", "expected"),
    [(3_456_789.123, 4, 3_457_000), (0.031234, 3, 0.0312), (-1234.5, 2, -1200), (0.0, 4, 0.0),
     (12.5, 4, 12.5)],
)
def test_round_sig(value, digits, expected):
    assert round_sig(value, digits) == expected


def test_compact_simulation_drops_bulk_and_rounds(simulation):
    compact = compact_simulation(simulation, top_drivers=2)
    text = json.dumps(compact)
    assert "histogram" not in text and '"var_95"' not in text and "elapsed" not in text
    first = compact["options"][0]
    assert first["mean"] == 3_457_000 and isinstance(first["mean"], int)
    assert first["prob_loss"] == 0.031 and first["prob_best"] == 0.52
    drivers = compact["top_drivers"]["second_warehouse"]
    assert [d["variable"] for d in drivers] == ["margin_per_order", "demand_growth"]
    assert drivers[0] == {"variable": "margin_per_order", "at_p10": 1_500_000, "at_p90": 5_400_000,
                          "swing": 3_900_000}
    assert compact["risk_hit_rates"] == {"construction_delay": 0.25, "tpl_price_hike": 0.301}
    assert len(to_json(compact)) < len(simulation.model_dump_json()) / 3


def test_compact_optimization(optimization):
    compact = compact_optimization(optimization)
    assert compact == {"selected": ["second_warehouse"], "expected_value": 3_457_000, "solver": "highs",
                       "status": "Optimal", "proven_optimal": True, "cvar": 605_000,
                       "budget_used": 2_200_000}
    assert compact_optimization(None) is None


def test_cited_fact_ids():
    assert cited_fact_ids("[F2][F15] and F3") == [2, 15, 3]
    assert cited_fact_ids("FY2025 data, F-1") == []
    assert cited_fact_ids(None) == []


def test_citation_errors(grounded_decision):
    assert citation_errors(grounded_decision, 5) == []
    errors = citation_errors(grounded_decision, 4)
    assert errors == ["variable 'demand_growth' cites non-existent facts ['F5']; valid fact ids: [F1]..[F4]"]


def test_numbers_in_text_parses_money_and_percent():
    found = {raw: (value, sig) for raw, value, sig in numbers_in_text(
        "Profit of $3.2M (1,234,567 USD) with 12% loss risk over 3 years in 2027; "
        "outsource_3pl, F1, p95, 3PL, 0.25 share, 1.5 million, 40k orders"
    )}
    assert found == {
        "3.2M": (3_200_000, 2),
        "1,234,567": (1_234_567, 7),
        "12%": (12, 2),
        "0.25": (0.25, 2),
        "1.5 million": (1_500_000, 2),
        "40k": (40_000, 1),
    }


def test_ungrounded_numbers_match_at_displayed_precision(decision, simulation):
    allowed = allowed_numbers(decision, simulation)
    texts = [
        "Expected profit is $3.46M, about $3.5M, and it wins in 52% of scenarios.",  # mean, prob_best
        "The gap to expanding is roughly $357k.",  # pairwise difference of means
        "Loss probability is 3.1%; the 95% tail matters.",  # prob_loss x100, method constant
        "Demand growth is likely 12%.",  # model parameter (pert mode 0.12)
        "Profit could reach $7.7M.",  # invented
    ]
    assert ungrounded_numbers(texts, allowed) == ["7.7M"]


def test_numbers_from_fact_statements_are_allowed(facts):
    allowed = allowed_numbers(None, None, facts=facts)
    assert ungrounded_numbers(["Volume grew 8-15% a year; 3PL quotes run to $4.6 per order."], allowed) == []
    assert ungrounded_numbers(["Volume grew 60% a year."], allowed) == ["60%"]
