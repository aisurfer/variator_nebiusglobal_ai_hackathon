"""Data contracts of the reasoning layer.

Two kinds of models live here:

* ``*Draft`` / ``ReportNarrative``: the JSON the LLM fills via structured output.
  They contain narrative only, never computed statistics.
* ``Critique``, ``DecisionReport``, ``RedTeamReview``: what callers receive. They
  extend the LLM output with fields that deterministic code attaches (numbers
  from ``SimulationResult`` / ``OptimizationResult``, lint findings, checks).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from variator.domain import MetricSpec, OptimizationResult

INSUFFICIENT_EVIDENCE = "insufficient_evidence"

Confidence = Literal["low", "medium", "high"]


# --- critique -------------------------------------------------------------------------------


class Severity(StrEnum):
    LOW = "low"  # cosmetic, does not move results
    MEDIUM = "medium"  # materially shifts risk numbers
    HIGH = "high"  # could change which option looks best


class IssueKind(StrEnum):
    MISSING_RISK = "missing_risk"
    OVERCONFIDENT_RANGE = "overconfident_range"
    IMPLAUSIBLE_RANGE = "implausible_range"
    DOUBLE_COUNTING = "double_counting"
    FORMULA_ERROR = "formula_error"
    UNGROUNDED = "ungrounded"
    MISSING_CORRELATION = "missing_correlation"
    MISSING_OPTION = "missing_option"
    STRUCTURE = "structure"
    OTHER = "other"


class CritiqueIssue(BaseModel):
    severity: Severity
    kind: IssueKind
    target: str = Field(description="Exact variable, option or risk name, or 'model'")
    problem: str
    suggestion: str


class MissingRisk(BaseModel):
    name: str = Field(description="snake_case name of the risk event")
    description: str
    variable: str = Field(description="Existing variable the event would shift")
    probability: float = Field(ge=0, le=1, description="Chance the event happens over the horizon")
    rationale: str


class CritiqueDraft(BaseModel):
    """What the critic LLM returns."""

    summary: str = Field(description="Two or three sentences on overall model quality")
    issues: list[CritiqueIssue]
    missing_risks: list[MissingRisk]
    quality_score: int = Field(ge=1, le=10, description="10 = ready to simulate as is")


class Critique(CritiqueDraft):
    """LLM critique plus issues found by deterministic checks (``lint``)."""

    lint: list[CritiqueIssue] = Field(
        default_factory=list, description="Issues found by code, not by the LLM"
    )
    generated_by: str = ""

    @property
    def all_issues(self) -> list[CritiqueIssue]:
        return [*self.lint, *self.issues]

    def needs_revision(self, *, min_score: int = 8) -> bool:
        """True when the model should go through ``revise_decision_model``."""
        serious = any(i.severity in (Severity.MEDIUM, Severity.HIGH) for i in self.all_issues)
        return serious or bool(self.missing_risks) or self.quality_score < min_score


# --- analysis report ------------------------------------------------------------------------


class DriverNote(BaseModel):
    variable: str = Field(description="Variable name exactly as in the decision model")
    explanation: str = Field(description="Why this driver moves the outcome, in business terms")


class ReportNarrative(BaseModel):
    """What the analyst LLM returns: interpretation only, no computed statistics."""

    recommendation: str = Field(description="One option name, or 'insufficient_evidence'")
    headline: str = Field(description="One sentence a CEO reads first")
    rationale: list[str] = Field(description="3-5 bullets: why this option")
    key_drivers: list[DriverNote]
    what_must_be_true: list[str] = Field(description="Conditions for the recommendation to hold")
    early_warning_signals: list[str] = Field(
        description="Observable indicators that would trigger re-evaluation"
    )
    risks_to_watch: list[str]
    limitations: list[str]
    confidence: Confidence


class OptionComparison(BaseModel):
    """Per-option numbers, copied by code from ``SimulationResult``."""

    option: str
    rank: int = Field(description="1 = best expected value (direction-aware)")
    mean: float
    std: float
    p5: float
    p50: float
    p95: float
    prob_loss: float
    cvar_95: float
    prob_best: float
    delta_mean_vs_recommended: float | None = None
    selected_by_optimizer: bool | None = None


class KeyDriver(BaseModel):
    """Tornado entry for the focal option (numbers from code) with the LLM explanation."""

    variable: str
    option: str | None = None
    metric_at_low: float | None = None
    metric_at_high: float | None = None
    swing: float | None = None
    explanation: str | None = None


class DecisionReport(BaseModel):
    question: str
    metric: MetricSpec
    recommendation: str = Field(description="Option name or 'insufficient_evidence' (validated)")
    llm_recommendation: str = Field(description="Raw value the LLM returned")
    recommendation_note: str | None = Field(
        default=None, description="Set when code had to correct the LLM recommendation"
    )
    headline: str
    confidence: Confidence
    rationale: list[str]
    key_drivers: list[KeyDriver]
    what_must_be_true: list[str]
    early_warning_signals: list[str]
    risks_to_watch: list[str]
    limitations: list[str]
    comparison: list[OptionComparison]
    optimization: OptimizationResult | None = None
    n_scenarios: int
    evidence_coverage: float | None = Field(
        default=None, description="Share of variables whose source cites a fact; None without facts"
    )
    checks: list[str] = Field(default_factory=list, description="Consistency warnings from code")
    ungrounded_numbers: list[str] = Field(
        default_factory=list,
        description="Numbers in the narrative not traceable to the simulation, model or facts",
    )
    generated_by: str = ""

    @property
    def has_recommendation(self) -> bool:
        return self.recommendation != INSUFFICIENT_EVIDENCE


# --- red team -------------------------------------------------------------------------------


class Challenge(BaseModel):
    target: str = Field(description="Assumption, variable, risk or 'recommendation' being challenged")
    challenge: str
    why_it_matters: str
    how_to_test: str = Field(description="Cheapest way to check it before committing")


class AlternativeOption(BaseModel):
    name: str = Field(description="snake_case name")
    description: str
    why_consider: str


class FlipCondition(BaseModel):
    condition: str = Field(description="What would have to happen for the recommendation to change")
    variable: str | None = Field(default=None, description="Model variable involved, if any")
    flips_to: str | None = Field(
        default=None, description="Option that would become better, or the alternative option name"
    )
    plausibility: Confidence


class RedTeamDraft(BaseModel):
    """What the red-team LLM returns."""

    robustness: Literal["robust", "fragile", "unsupported"]
    summary: str
    challenges: list[Challenge]
    blind_spots: list[str]
    alternative_option: AlternativeOption
    flip_conditions: list[FlipCondition]


class SensitivityFlip(BaseModel):
    """One-at-a-time flip found by code from the tornado data."""

    variable: str
    at: Literal["low", "high"] = Field(description="low = variable at its p10, high = at its p90")
    recommended: str
    challenger: str
    recommended_metric: float
    challenger_metric: float


class RedTeamReview(RedTeamDraft):
    sensitivity_flips: list[SensitivityFlip] = Field(default_factory=list)
    generated_by: str = ""
