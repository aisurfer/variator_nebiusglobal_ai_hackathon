"""Data quality score (Q) and Occam's razor simplification of a decision model."""

from variator.quality.core import (
    DEFAULT_THRESHOLD,
    QualityScore,
    Simplification,
    TrendFit,
    fit_distribution,
    score_series,
    select_trend,
    simplify,
    swing_shares,
)

__all__ = [
    "DEFAULT_THRESHOLD",
    "QualityScore",
    "Simplification",
    "TrendFit",
    "fit_distribution",
    "score_series",
    "select_trend",
    "simplify",
    "swing_shares",
]
