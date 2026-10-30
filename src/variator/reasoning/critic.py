"""Nemotron Super reviews a ``DecisionModel`` and revises it from the review."""

from __future__ import annotations

from collections.abc import Sequence

from variator.domain import DecisionModel, Fact
from variator.llm import TokenFactoryClient
from variator.llm.models import ROLE_MODEL
from variator.reasoning import prompts
from variator.reasoning.calls import ask_structured
from variator.reasoning.checks import lint_decision_model
from variator.reasoning.context import compact_model, format_facts, to_json
from variator.reasoning.grounding import as_plain_model, grounded_decision_schema
from variator.reasoning.schemas import Critique, CritiqueDraft, CritiqueIssue


def _format_lint(issues: Sequence[CritiqueIssue]) -> str:
    if not issues:
        return "(none)"
    return "\n".join(f"- [{i.severity}] {i.target}: {i.problem}" for i in issues)


def critique_decision_model(
    client: TokenFactoryClient,
    model: DecisionModel,
    facts: Sequence[Fact] = (),
    *,
    llm: str = ROLE_MODEL["critique"],
    reasoning: bool = True,
    max_attempts: int = 2,
) -> Critique:
    """Review ``model`` for missing risks, bad ranges, double counting and formula errors.

    Deterministic lint runs first; its findings go to the LLM (so it adds judgment
    instead of repeating them) and are returned in ``Critique.lint``.
    """
    lint = lint_decision_model(model)
    request = prompts.CRITIC_REQUEST.format(
        facts=format_facts(facts), lint=_format_lint(lint), model=to_json(compact_model(model))
    )
    draft = ask_structured(
        client, llm, prompts.CRITIC_INSTRUCTIONS, request, CritiqueDraft,
        reasoning=reasoning, max_attempts=max_attempts,
    )
    return Critique(**draft.model_dump(), lint=lint, generated_by=client.models.model_id(llm))


def revise_decision_model(
    client: TokenFactoryClient,
    model: DecisionModel,
    critique: Critique,
    facts: Sequence[Fact] = (),
    *,
    llm: str = ROLE_MODEL["critique"],
    reasoning: bool = True,
    max_attempts: int = 3,
) -> DecisionModel:
    """Return a full revised ``DecisionModel`` that addresses ``critique``.

    The revision passes the same validation and citation checks as the builder.
    The leader's restated question is kept from the original model.
    """
    review = critique.model_dump(mode="json", exclude={"generated_by"})
    request = prompts.REVISER_REQUEST.format(
        facts=format_facts(facts), critique=to_json(review), model=to_json(compact_model(model))
    )
    revised = ask_structured(
        client, llm, prompts.REVISER_INSTRUCTIONS, request, grounded_decision_schema(len(facts)),
        reasoning=reasoning, max_attempts=max_attempts,
    )
    return as_plain_model(revised).model_copy(update={"question": model.question})
