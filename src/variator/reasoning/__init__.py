"""Reasoning layer: Nemotron formalizes decisions and explains results; code computes numbers.

Pipeline: ``build_decision_model`` (Super) -> ``critique_decision_model`` /
``revise_decision_model`` (Super) -> simulation -> ``analyze_results`` (Ultra)
-> ``red_team`` (Ultra).
"""

from variator.reasoning.analyst import analyze_results, assemble_report, narrative_schema
from variator.reasoning.builder import build_decision_model
from variator.reasoning.checks import (
    dominance_warnings,
    lint_decision_model,
    rank_options,
    sensitivity_flips,
)
from variator.reasoning.context import compact_simulation, format_facts
from variator.reasoning.critic import critique_decision_model, revise_decision_model
from variator.reasoning.grounding import citation_errors, evidence_coverage, ungrounded_numbers
from variator.reasoning.red_team import red_team
from variator.reasoning.schemas import (
    INSUFFICIENT_EVIDENCE,
    AlternativeOption,
    Challenge,
    Critique,
    CritiqueIssue,
    DecisionReport,
    DriverNote,
    FlipCondition,
    IssueKind,
    KeyDriver,
    MissingRisk,
    OptionComparison,
    RedTeamReview,
    ReportNarrative,
    SensitivityFlip,
    Severity,
)

__all__ = [
    "INSUFFICIENT_EVIDENCE",
    "AlternativeOption",
    "Challenge",
    "Critique",
    "CritiqueIssue",
    "DecisionReport",
    "DriverNote",
    "FlipCondition",
    "IssueKind",
    "KeyDriver",
    "MissingRisk",
    "OptionComparison",
    "RedTeamReview",
    "ReportNarrative",
    "SensitivityFlip",
    "Severity",
    "analyze_results",
    "assemble_report",
    "build_decision_model",
    "citation_errors",
    "compact_simulation",
    "critique_decision_model",
    "dominance_warnings",
    "evidence_coverage",
    "format_facts",
    "lint_decision_model",
    "narrative_schema",
    "rank_options",
    "red_team",
    "revise_decision_model",
    "sensitivity_flips",
    "ungrounded_numbers",
]
