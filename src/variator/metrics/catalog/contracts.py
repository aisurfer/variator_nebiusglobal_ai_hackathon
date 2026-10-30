"""The file contracts of the catalogue output, on top of ``variator.metrics.results``.

The brief is ``results.KbBrief`` and a card is ``results.KbMetricResult``: the MVP has one user role, the CEO,
and ``roles`` is ``["ceo"]`` on every metric (the CEO covers all). The brief places a metric by its ``area``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from variator.metrics.results import KbMetricDef, KbMetricResult, MetricResult, TsModel


class MetricArtifact(TsModel):
    """One metric file: the brief card (``KbMetricResult``), the drill-down (``MetricResult``) and the
    current parameters of the metric (target, bands, window, options; snake_case, as people edit them)."""

    ts_optional: ClassVar[frozenset[str]] = frozenset({"params"})

    metric: KbMetricResult
    result: MetricResult
    params: dict[str, Any] | None = None


class CatalogEntry(TsModel):
    """A catalogue entry: the definition as the UI reads it, the GenUI unit, the inputs and the parameters."""

    definition: KbMetricDef
    gen_unit: str
    family: str
    needs: list[str]
    params: dict[str, Any]
