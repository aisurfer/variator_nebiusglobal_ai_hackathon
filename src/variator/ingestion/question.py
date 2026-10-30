"""Design the case of the leader's question as a ``DecisionBrief`` with Nemotron 3 Ultra.

The brief is the case design and the hand-off to the decision-model builder (Nemotron 3 Super):
candidate options (with names that are valid ``DecisionOption`` identifiers), the metric, the
horizon, hard constraints, the drivers to model and what to ask the user.
"""

from __future__ import annotations

import keyword
import math
import re
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from variator.domain import FORMULA_FUNCTIONS, Fact, MetricSpec
from variator.ingestion import prompts
from variator.llm import TokenFactoryClient, user_message
from variator.llm.models import ROLE_MODEL

MAX_HORIZON_MONTHS = 600


def to_identifier(text: str, *, fallback: str = "option") -> str:
    """Coerce free text into a snake_case identifier that ``DecisionOption`` accepts."""
    name = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    name = re.sub(r"_+", "_", name)[:60].rstrip("_")
    if not name:
        name = fallback
    if not name[0].isalpha():
        name = f"{fallback}_{name}"
    if keyword.iskeyword(name) or name in FORMULA_FUNCTIONS:
        name = f"{name}_{fallback}"
    return name


class ConstraintKind(StrEnum):
    BUDGET = "budget"
    DEADLINE = "deadline"
    CAPACITY = "capacity"
    RISK = "risk"
    POLICY = "policy"
    OTHER = "other"


class OptionCandidate(BaseModel):
    name: str = Field(description="snake_case identifier, e.g. open_second_warehouse")
    description: str

    @field_validator("name", mode="before")
    @classmethod
    def _identifier(cls, v: object) -> str:
        return to_identifier(str(v))


class Constraint(BaseModel):
    kind: ConstraintKind = Field(description="budget, deadline, capacity, risk, policy or other")
    description: str
    value: float | None = None
    unit: str | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, v: object) -> str:
        text = str(v).strip().lower()
        return text if text in ConstraintKind._value2member_map_ else ConstraintKind.OTHER.value


class DecisionBriefDraft(BaseModel):
    """What the LLM fills in; ``DecisionBrief`` adds the original question."""

    restated_question: str
    options: list[OptionCandidate] = Field(min_length=1)
    metric: str = Field(description='e.g. "3-year profit contribution", "NPV"')
    metric_unit: str = Field(default="USD", description="Currency code or unit")
    higher_is_better: bool = True
    horizon_months: int = Field(description="Evaluation horizon in months")
    constraints: list[Constraint] = Field(default_factory=list)
    key_uncertainties: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list, description="Questions for the user")

    @field_validator("horizon_months", mode="before")
    @classmethod
    def _horizon(cls, v: object) -> int:
        try:
            months = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("horizon_months must be a number") from exc
        if math.isnan(months):
            raise ValueError("horizon_months must be a number")
        return int(min(max(round(months), 1), MAX_HORIZON_MONTHS))

    @model_validator(mode="after")
    def _unique_option_names(self) -> DecisionBriefDraft:
        seen: dict[str, int] = {}
        for option in self.options:
            base = option.name
            seen[base] = seen.get(base, 0) + 1
            if seen[base] > 1:
                option.name = f"{base}_{seen[base]}"
        return self


class DecisionBrief(DecisionBriefDraft):
    question: str = Field(description="The leader's original question")

    def metric_spec(self) -> MetricSpec:
        return MetricSpec(name=self.metric, unit=self.metric_unit, higher_is_better=self.higher_is_better)

    def budget(self) -> float | None:
        """The first budget constraint with a number, if any."""
        return next(
            (c.value for c in self.constraints if c.kind == ConstraintKind.BUDGET and c.value is not None),
            None,
        )


def _render_facts(facts: Sequence[Fact], limit: int) -> str:
    lines = []
    for fact in facts[:limit]:
        where = f"{fact.origin.value}: {fact.source}" if fact.source else fact.origin.value
        lines.append(f"- {fact.statement} [{where}]")
    return "\n".join(lines)


def parse_question(
    client: TokenFactoryClient,
    question: str,
    *,
    context_facts: Sequence[Fact] = (),
    model: str = ROLE_MODEL["case_design"],
    max_context_facts: int = 40,
    reasoning: bool = True,
) -> DecisionBrief:
    """Restate the question and design the case: the options (forks), metric, horizon, constraints
    and follow-up questions (one Ultra call, reasoning on: it decides everything downstream).

    ``context_facts`` (e.g. from ``facts_from_profile``) are shown before the
    question so the model does not ask for what is already known. Raises
    ``ValueError`` for an empty question and ``StructuredOutputError`` if the
    model never returns valid JSON.
    """
    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    parts = [prompts.QUESTION_INSTRUCTIONS]
    if context_facts:
        parts.append(prompts.QUESTION_FACTS_TEMPLATE.format(facts=_render_facts(context_facts,
                                                                                 max_context_facts)))
    parts.append(prompts.QUESTION_TEMPLATE.format(question=question))
    messages = [user_message("\n\n".join(parts))]
    draft = client.structured(model, messages, DecisionBriefDraft, reasoning=reasoning)
    return DecisionBrief(question=question, **draft.model_dump())
