"""Multi-criteria decision analysis: weights, TOPSIS, Pareto front, weight robustness."""

from variator.mcda.core import (
    CriterionStats,
    RankedOption,
    Ranking,
    criterion_ties,
    decision_matrix,
    normalize,
    pareto_mask,
    rank,
    snap,
    tensor_stats,
    tied_criteria,
    topsis,
    weight_robustness,
    weight_vector,
    weighted_scores,
    with_ties,
)
from variator.mcda.path import BestPath, best_path, what_breaks

__all__ = [
    "BestPath",
    "best_path",
    "what_breaks",
    "CriterionStats",
    "RankedOption",
    "Ranking",
    "criterion_ties",
    "decision_matrix",
    "normalize",
    "pareto_mask",
    "rank",
    "snap",
    "tensor_stats",
    "tied_criteria",
    "topsis",
    "weight_robustness",
    "weight_vector",
    "weighted_scores",
    "with_ties",
]
