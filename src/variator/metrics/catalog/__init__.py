"""Metric catalogue of the CEO brief (the one MVP role, every metric) over the company history (no LLM).

See ``README.md``.
"""

from variator.metrics.catalog.artifacts import as_of_violations, compute_all, write_set
from variator.metrics.catalog.datasets import HistoryData, load_history
from variator.metrics.catalog.engine import Evaluation, evaluate, run
from variator.metrics.catalog.params import CatalogParams, MetricParams, ParamsError, merge_params
from variator.metrics.catalog.registry import BY_ID, CATALOG, default_params, metrics_for
from variator.metrics.catalog.sanity import sanity

__all__ = [
    "BY_ID",
    "CATALOG",
    "CatalogParams",
    "Evaluation",
    "HistoryData",
    "MetricParams",
    "ParamsError",
    "as_of_violations",
    "compute_all",
    "default_params",
    "evaluate",
    "load_history",
    "merge_params",
    "metrics_for",
    "run",
    "sanity",
    "write_set",
]
