"""Nemotron Ultra attacks a recommendation; code supplies the sensitivity flips."""

from __future__ import annotations

from typing import Any

from variator.domain import DecisionModel, SimulationResult
from variator.llm import TokenFactoryClient
from variator.llm.models import ROLE_MODEL
from variator.reasoning import prompts
from variator.reasoning.calls import ask_structured
from variator.reasoning.checks import match_name, rank_options, sensitivity_flips
from variator.reasoning.context import compact_model, compact_simulation, to_json
from variator.reasoning.schemas import (
    INSUFFICIENT_EVIDENCE,
    DecisionReport,
    FlipCondition,
    RedTeamDraft,
    RedTeamReview,
    SensitivityFlip,
)


def _report_summary(report: DecisionReport) -> dict[str, Any]:
    return {
        "recommendation": report.recommendation,
        "headline": report.headline,
        "confidence": report.confidence,
        "rationale": report.rationale,
        "what_must_be_true": report.what_must_be_true,
        "key_drivers": [d.variable for d in report.key_drivers],
        "checks": report.checks,
    }


def _format_flips(flips: list[SensitivityFlip]) -> str:
    if not flips:
        return "(none found)"
    return "\n".join(
        f"- {f.variable} at its {'p10' if f.at == 'low' else 'p90'}: {f.challenger} "
        f"({f.challenger_metric:,.4g}) beats {f.recommended} ({f.recommended_metric:,.4g})"
        for f in flips
    )


def red_team(
    client: TokenFactoryClient,
    model: DecisionModel,
    report: DecisionReport,
    simulation: SimulationResult,
    *,
    llm: str = ROLE_MODEL["analysis"],
    reasoning: bool = True,
    max_attempts: int = 2,
) -> RedTeamReview:
    """Challenge the report's recommendation: weak assumptions, blind spots, an
    unconsidered alternative and the conditions under which the choice flips.

    When the report has no recommendation, the best option by expected value is
    stress-tested instead. Flip conditions are cleaned by code: unknown variables
    and options are set to ``None``.
    """
    target = (
        report.recommendation
        if report.recommendation != INSUFFICIENT_EVIDENCE
        else rank_options(simulation)[0].option
    )
    flips = sensitivity_flips(simulation, target)
    request = prompts.RED_TEAM_REQUEST.format(
        report=to_json(_report_summary(report) | {"stress_tested_option": target}),
        flips=_format_flips(flips),
        model=to_json(compact_model(model)),
        simulation=to_json(compact_simulation(simulation)),
    )
    draft = ask_structured(
        client, llm, prompts.RED_TEAM_INSTRUCTIONS, request, RedTeamDraft,
        reasoning=reasoning, max_attempts=max_attempts,
    )
    return RedTeamReview(
        **draft.model_dump(exclude={"flip_conditions"}),
        flip_conditions=_clean_flips(draft, model),
        sensitivity_flips=flips,
        generated_by=client.models.model_id(llm),
    )


def _clean_flips(draft: RedTeamDraft, model: DecisionModel) -> list[FlipCondition]:
    variables = [v.name for v in model.variables]
    options = [o.name for o in model.options] + [draft.alternative_option.name]
    return [
        flip.model_copy(update={
            "variable": match_name(flip.variable, variables),
            "flips_to": match_name(flip.flips_to, options),
        })
        for flip in draft.flip_conditions
    ]
