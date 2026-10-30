"""Nemotron Ultra interprets simulation results; code attaches every number.

The LLM writes narrative fields only (``ReportNarrative``). Code then:
validates the recommended option, copies per-option statistics and tornado
drivers from ``SimulationResult``, runs consistency checks, and flags numbers
in the narrative that do not trace back to computed data.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field, create_model

from variator.domain import DecisionModel, Fact, OptimizationResult, OptionStats, SimulationResult
from variator.llm import TokenFactoryClient
from variator.llm.models import ROLE_MODEL
from variator.reasoning import prompts
from variator.reasoning.calls import ask_structured
from variator.reasoning.checks import (
    dominance_warnings,
    match_name,
    optimizer_warnings,
    rank_options,
)
from variator.reasoning.context import (
    compact_model,
    compact_optimization,
    compact_simulation,
    format_facts,
    to_json,
)
from variator.reasoning.grounding import allowed_numbers, evidence_coverage, ungrounded_numbers
from variator.reasoning.schemas import (
    INSUFFICIENT_EVIDENCE,
    DecisionReport,
    KeyDriver,
    OptionComparison,
    ReportNarrative,
)

TOP_DRIVERS = 5


def narrative_schema(option_names: Sequence[str]) -> type[ReportNarrative]:
    """``ReportNarrative`` whose JSON schema enumerates the allowed recommendations.

    The enum steers constrained decoding on the server, while Python-side the
    field stays a plain string so an off-list value is corrected by code instead
    of failing the whole call.
    """
    allowed = [*option_names, INSUFFICIENT_EVIDENCE]
    return create_model(
        "ReportNarrative",
        __base__=ReportNarrative,
        recommendation=(
            str,
            Field(description="One option name, or 'insufficient_evidence'",
                  json_schema_extra={"enum": allowed}),
        ),
    )


def build_analysis_request(
    model: DecisionModel,
    simulation: SimulationResult,
    optimization: OptimizationResult | None = None,
    facts: Sequence[Fact] = (),
) -> str:
    """Dynamic part of the analyst prompt, with the compact (histogram-free) results."""
    names = [o.name for o in model.options]
    opt = compact_optimization(optimization)
    return prompts.ANALYST_REQUEST.format(
        allowed=", ".join([*names, INSUFFICIENT_EVIDENCE]),
        model=to_json(compact_model(model)),
        facts=format_facts(facts),
        simulation=to_json(compact_simulation(simulation, top_drivers=TOP_DRIVERS)),
        optimization=to_json(opt) if opt is not None else "(not run)",
    )


def analyze_results(
    client: TokenFactoryClient,
    model: DecisionModel,
    simulation: SimulationResult,
    *,
    optimization: OptimizationResult | None = None,
    facts: Sequence[Fact] = (),
    llm: str = ROLE_MODEL["analysis"],
    reasoning: bool = True,
    max_attempts: int = 2,
) -> DecisionReport:
    """Turn simulation (and optional optimization) results into a ``DecisionReport``.

    Narrative comes from the LLM; ``comparison``, ``key_drivers`` numbers,
    ``checks`` and ``ungrounded_numbers`` come from code.
    """
    names = [o.name for o in model.options]
    narrative = ask_structured(
        client,
        llm,
        prompts.ANALYST_INSTRUCTIONS,
        build_analysis_request(model, simulation, optimization, facts),
        narrative_schema(names),
        reasoning=reasoning,
        max_attempts=max_attempts,
    )
    return assemble_report(
        model, simulation, narrative, optimization=optimization, facts=facts,
        generated_by=client.models.model_id(llm),
    )


def assemble_report(
    model: DecisionModel,
    simulation: SimulationResult,
    narrative: ReportNarrative,
    *,
    optimization: OptimizationResult | None = None,
    facts: Sequence[Fact] = (),
    generated_by: str = "",
) -> DecisionReport:
    """Merge LLM narrative with numbers and checks computed by code. Never calls the LLM."""
    simulated = [o.option for o in simulation.options]
    candidates = [n for n in (o.name for o in model.options) if n in simulated] or simulated
    checks: list[str] = []
    limitations = list(narrative.limitations)
    confidence = narrative.confidence
    note = None

    recommendation = match_name(narrative.recommendation, [*candidates, INSUFFICIENT_EVIDENCE])
    if recommendation is None:
        note = (
            f"The analyst recommended {narrative.recommendation!r}, which is not one of the "
            f"simulated options {candidates}; the recommendation was withheld."
        )
        recommendation, confidence = INSUFFICIENT_EVIDENCE, "low"
        limitations.append(note)
    elif recommendation != narrative.recommendation:
        note = f"Recommendation {narrative.recommendation!r} was normalized to {recommendation!r}."

    ranked = rank_options(simulation)
    focal = recommendation if recommendation != INSUFFICIENT_EVIDENCE else ranked[0].option
    comparison = _comparison(simulation, ranked, recommendation, optimization)

    if recommendation != INSUFFICIENT_EVIDENCE:
        checks.extend(dominance_warnings(simulation, recommendation))
        checks.extend(optimizer_warnings(optimization, recommendation))

    key_drivers, dropped = _key_drivers(model, simulation, narrative, focal)
    checks.extend(f"Dropped driver explanation for unknown variable {v!r}." for v in dropped)

    coverage = evidence_coverage(model, len(facts))
    if coverage is not None and coverage < 0.5:
        checks.append(
            f"Only {coverage:.0%} of variables cite evidence; the rest are assumptions."
        )

    claims = [narrative.headline, *narrative.rationale, *narrative.risks_to_watch,
              *narrative.limitations, *(d.explanation for d in narrative.key_drivers)]
    flagged = ungrounded_numbers(claims, allowed_numbers(model, simulation, optimization, facts))

    return DecisionReport(
        question=model.question,
        metric=simulation.metric,
        recommendation=recommendation,
        llm_recommendation=narrative.recommendation,
        recommendation_note=note,
        headline=narrative.headline,
        confidence=confidence,
        rationale=narrative.rationale,
        key_drivers=key_drivers,
        what_must_be_true=narrative.what_must_be_true,
        early_warning_signals=narrative.early_warning_signals,
        risks_to_watch=narrative.risks_to_watch,
        limitations=limitations,
        comparison=comparison,
        optimization=optimization,
        n_scenarios=simulation.n_scenarios,
        evidence_coverage=coverage,
        checks=checks,
        ungrounded_numbers=flagged,
        generated_by=generated_by,
    )


def _comparison(
    simulation: SimulationResult,
    ranked: Sequence[OptionStats],
    recommendation: str,
    optimization: OptimizationResult | None,
) -> list[OptionComparison]:
    rank = {o.option: i + 1 for i, o in enumerate(ranked)}
    by_name = {o.option: o for o in simulation.options}
    rec = by_name.get(recommendation)
    selected = set(optimization.selected) if optimization is not None else None
    return [
        OptionComparison(
            option=o.option,
            rank=rank[o.option],
            mean=o.mean,
            std=o.std,
            p5=o.p5,
            p50=o.p50,
            p95=o.p95,
            prob_loss=o.prob_loss,
            cvar_95=o.cvar_95,
            prob_best=o.prob_best,
            delta_mean_vs_recommended=(o.mean - rec.mean) if rec is not None else None,
            selected_by_optimizer=(o.option in selected) if selected is not None else None,
        )
        for o in ranked
    ]


def _key_drivers(
    model: DecisionModel, simulation: SimulationResult, narrative: ReportNarrative, focal: str
) -> tuple[list[KeyDriver], list[str]]:
    """Top tornado entries of the focal option, annotated with the LLM's explanations."""
    variables = [v.name for v in model.variables]
    explanations: dict[str, str] = {}
    dropped: list[str] = []
    for note in narrative.key_drivers:
        name = match_name(note.variable, variables)
        if name is None:
            dropped.append(note.variable)
        else:
            explanations.setdefault(name, note.explanation)

    sens = {s.variable: s for s in simulation.sensitivities if s.option == focal}
    top = sorted(sens.values(), key=lambda s: s.swing, reverse=True)[:TOP_DRIVERS]
    order = [s.variable for s in top] + [v for v in explanations if v not in {s.variable for s in top}]
    drivers = []
    for name in order:
        s = sens.get(name)
        drivers.append(KeyDriver(
            variable=name,
            option=focal if s is not None else None,
            metric_at_low=s.metric_at_low if s is not None else None,
            metric_at_high=s.metric_at_high if s is not None else None,
            swing=s.swing if s is not None else None,
            explanation=explanations.get(name),
        ))
    return drivers, dropped
