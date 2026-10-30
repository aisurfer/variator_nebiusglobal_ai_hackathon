"""Editable parameters of the catalogue: one JSON-serialisable structure.

``CatalogParams.metrics[<metric id>]`` holds the target, the RAG bands (read with the metric direction), the
window in days and the options of that metric. ``default_params()`` gives the defaults of every metric of the
catalogue; ``merge_params(base, override)`` applies a partial override (a dict, e.g. from ``--params FILE``).
A change of ``target`` or ``bands`` changes only the status; a change of a window or an option changes the
value.
A recomputation reads the history again: no regeneration of data.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

Option = float | int | str | bool | list[str] | dict[str, float] | None


class BandsParam(BaseModel):
    """RAG bands in metric units: higher is better -> value >= good: good, >= warn: warn; lower: mirror."""

    model_config = ConfigDict(extra="forbid")
    good: float
    warn: float


class MetricParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: float | None = None
    bands: BandsParam | None = None
    window_days: int | None = Field(default=None, ge=1, le=3650)
    options: dict[str, Option] = Field(default_factory=dict)


class CatalogParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    currency: str = "USD"
    series_weeks: int = Field(default=52, ge=2, le=53)
    items_limit: int = Field(default=25, ge=1, le=500)
    lineage_records_max: int = Field(default=500, ge=1, le=100_000)
    metrics: dict[str, MetricParams] = Field(default_factory=dict)

    def of(self, metric_id: str) -> MetricParams:
        return self.metrics.get(metric_id) or MetricParams()

    def digest(self) -> str:
        """A short hash of the parameters: the same parameters give the same digest."""
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()[:12]


class ParamsError(ValueError):
    """An override names an unknown metric, field or option."""


def _merge(base: dict[str, Any], over: dict[str, Any], path: str) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        # bands and option values (a dict option too) are replaced as a whole; the levels above merge
        if (
            isinstance(v, dict)
            and isinstance(out.get(k), dict)
            and k != "bands"
            and not path.endswith(".options")
        ):
            out[k] = _merge(out[k], v, f"{path}.{k}")
        else:
            out[k] = copy.deepcopy(v)
    return out


def merge_params(base: CatalogParams, override: dict[str, Any] | None) -> CatalogParams:
    """``base`` with a partial override applied; unknown metric ids and options raise ``ParamsError``."""
    if not override:
        return base.model_copy(deep=True)
    if not isinstance(override, dict):
        raise ParamsError("the parameter override must be a JSON object")
    known = base.model_dump(mode="json")
    for mid, mp in (override.get("metrics") or {}).items():
        if mid not in known["metrics"]:
            raise ParamsError(f"unknown metric in parameters: {mid}")
        for opt in (mp or {}).get("options") or {}:
            if opt not in known["metrics"][mid]["options"]:
                allowed = ", ".join(sorted(known["metrics"][mid]["options"])) or "none"
                raise ParamsError(f"{mid}: unknown option {opt!r} (options: {allowed})")
    merged = _merge(known, override, "params")
    try:
        return CatalogParams.model_validate(merged)
    except ValueError as exc:
        raise ParamsError(str(exc)) from exc
