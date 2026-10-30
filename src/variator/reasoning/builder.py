"""Nemotron Super turns a question plus evidence into a validated ``DecisionModel``."""

from __future__ import annotations

from collections.abc import Sequence

from variator.domain import DecisionModel, Fact
from variator.llm import TokenFactoryClient
from variator.llm.models import ROLE_MODEL
from variator.reasoning import prompts
from variator.reasoning.calls import ask_structured
from variator.reasoning.context import format_facts
from variator.reasoning.grounding import as_plain_model, grounded_decision_schema


def build_request(
    question: str,
    facts: Sequence[Fact],
    *,
    options_hint: Sequence[str] = (),
    constraints: Sequence[str] = (),
) -> str:
    """Dynamic part of the builder prompt (facts numbered ``[F1]``, ``[F2]``, ...)."""
    facts_text = format_facts(facts)
    if not facts:
        facts_text += "\nNo evidence: every variable source must be 'assumption: <reason>'."
    return prompts.BUILDER_REQUEST.format(
        question=question.strip(),
        facts=facts_text,
        options=prompts.bullet_list(list(options_hint), empty="(none: propose the realistic options)"),
        constraints=prompts.bullet_list(list(constraints)),
    )


def build_decision_model(
    client: TokenFactoryClient,
    question: str,
    facts: Sequence[Fact],
    *,
    options_hint: Sequence[str] = (),
    constraints: Sequence[str] = (),
    model: str = ROLE_MODEL["decision_model"],
    reasoning: bool = True,
    max_attempts: int = 3,
) -> DecisionModel:
    """Ask the LLM to formalize ``question`` into a ``DecisionModel`` grounded in ``facts``.

    Validation covers the domain rules (identifiers, distribution parameters,
    formula whitelist, references between variables/options/risks) plus fact
    citations in ``Variable.source``. Any violation is fed back to the model by
    ``structured()`` for up to ``max_attempts`` tries; after that
    ``StructuredOutputError`` is raised.
    """
    request = build_request(question, facts, options_hint=options_hint, constraints=constraints)
    result = ask_structured(
        client,
        model,
        prompts.BUILDER_INSTRUCTIONS,
        request,
        grounded_decision_schema(len(facts)),
        reasoning=reasoning,
        max_attempts=max_attempts,
    )
    return as_plain_model(result)
