"""Smoke tests against the real Token Factory. Skipped unless ``pytest --live`` with NEBIUS_API_KEY."""

import pytest

from variator.config import Settings
from variator.domain import DecisionModel
from variator.llm import TokenFactoryClient
from variator.reasoning import (
    INSUFFICIENT_EVIDENCE,
    analyze_results,
    build_decision_model,
    citation_errors,
    critique_decision_model,
    red_team,
)

pytestmark = pytest.mark.live


@pytest.fixture
def client() -> TokenFactoryClient:
    return TokenFactoryClient(Settings.from_env())


def test_live_super_builds_a_valid_grounded_model(client, facts):
    """Verifies json_schema + enable_thinking=True together on Super (see README assumptions)."""
    model = build_decision_model(
        client,
        "Should we open a second warehouse, expand the current one, or outsource growth to a 3PL?",
        facts,
        options_hint=["second warehouse", "expand current site", "3PL"],
        constraints=["Capex budget 2.5M USD"],
    )
    assert type(model) is DecisionModel
    assert len(model.options) >= 2
    assert citation_errors(model, len(facts)) == []


def test_live_super_critiques(client, decision, facts):
    critique = critique_decision_model(client, decision, facts)
    assert 1 <= critique.quality_score <= 10
    assert critique.summary


def test_live_ultra_analyzes_and_red_teams(client, decision, simulation, optimization, facts):
    report = analyze_results(client, decision, simulation, optimization=optimization, facts=facts)
    assert report.recommendation in {*(o.name for o in decision.options), INSUFFICIENT_EVIDENCE}
    assert report.headline and report.rationale
    review = red_team(client, decision, report, simulation)
    assert review.challenges and review.flip_conditions
