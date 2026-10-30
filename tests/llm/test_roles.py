"""Which Nemotron model does each LLM role: one table (``ROLE_MODEL``), the default of every call site.

Slide 9 of the submission says the same: Ultra extracts the actor cards and designs the case; Super builds
the decision model, lists risks and critiques; Lightning writes the year of company life and extracts.
"""

from __future__ import annotations

import inspect

from variator.actors.cards.writer import CardWriters
from variator.agent.loop import _run as agent_loop
from variator.ingestion.facts import facts_from_text
from variator.ingestion.question import parse_question
from variator.llm.models import DEFAULT_MODELS, ROLE_MODEL
from variator.narrative.writers import Writers, nemotron_request
from variator.reasoning.analyst import analyze_results
from variator.reasoning.builder import build_decision_model
from variator.reasoning.critic import critique_decision_model, revise_decision_model
from variator.research.pipeline import gather_external_facts

SLIDE_9 = {
    "actor_cards": "ultra",
    "case_design": "ultra",
    "decision_model": "super",
    "critique": "super",
    "narrative": "lightning",
    "extraction": "lightning",
}


def default(fn, name: str) -> str:
    return inspect.signature(fn).parameters[name].default


def test_the_roles_of_slide_9_map_to_its_models():
    assert {role: ROLE_MODEL[role] for role in SLIDE_9} == SLIDE_9
    assert set(ROLE_MODEL.values()) <= {m.key for m in DEFAULT_MODELS}


def test_every_call_site_takes_its_default_from_the_table():
    assert default(CardWriters, "model") == ROLE_MODEL["actor_cards"]
    assert default(parse_question, "model") == ROLE_MODEL["case_design"]
    assert default(parse_question, "reasoning") is True
    assert default(build_decision_model, "model") == ROLE_MODEL["decision_model"]
    assert default(critique_decision_model, "llm") == ROLE_MODEL["critique"]
    assert default(revise_decision_model, "llm") == ROLE_MODEL["critique"]
    assert default(analyze_results, "llm") == ROLE_MODEL["analysis"]
    assert default(agent_loop, "model") == ROLE_MODEL["agent_loop"]
    assert default(Writers, "model") == default(nemotron_request, "model") == ROLE_MODEL["narrative"]
    assert default(facts_from_text, "model") == ROLE_MODEL["extraction"]
    assert default(gather_external_facts, "model") == ROLE_MODEL["extraction"]
