"""Evaluate catalogue metrics as of a date into the UI contracts.

For one metric and one as-of date ``evaluate``:

1. runs the compute function at the date, one week and one day earlier, and at each weekly date of the series
   (every run applies the as-of rule: only what was known at the end of that date);
2. reads the status from the bands and the target of the current parameters (``results.status_with_target``:
   on track exactly when the value meets the target the card shows);
3. builds ``KbMetricResult`` (the brief card) and ``MetricResult`` with the lineage tree
   metric -> operations -> aggregates -> records, and event -> cause nodes for the main records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from variator.metrics.catalog.asof import week_dates
from variator.metrics.catalog.core import (
    DETAIL_KIND,
    MVP_ROLE,
    Computation,
    Ctx,
    Item,
    MetricDef,
    fmt_money,
    record_ref,
)
from variator.metrics.catalog.datasets import HistoryData
from variator.metrics.catalog.params import CatalogParams, MetricParams
from variator.metrics.results import (
    Bands,
    Changes,
    Coverage,
    Detail,
    GenBreakdownItem,
    KbBreakdownItem,
    KbDisplay,
    KbMetricDef,
    KbMetricRef,
    KbMetricResult,
    KbRecord,
    LineageNode,
    MetricResult,
    Previous,
    SeriesPoint,
    status_with_target,
)

ENGINE_VERSION = "variator.metrics.catalog/1.0"
SPEC_VERSION = 1
HIGHLIGHT = 5
CHANGES_MAX = 25
_FIELD = re.compile(r"\{(opt\.)?([a-z0-9_]+)\}")


# ---------------------------------------------------------------------------------------------- texts
def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and abs(value) >= 10_000:
        return f"{value:,}"
    return str(value)


def fill(text: str | None, p: MetricParams) -> str | None:
    """Replace ``{window_days}``, ``{target}`` and ``{opt.<name>}`` with the current parameters."""
    if text is None:
        return None
    values: dict[str, Any] = {"window_days": p.window_days, "target": p.target}

    def sub(m: re.Match) -> str:
        if m.group(1):
            return _show(p.options.get(m.group(2), m.group(0)))
        v = values.get(m.group(2))
        return m.group(0) if v is None else _show(v)

    return _FIELD.sub(sub, text)


def show_value(value: float | None, unit: str) -> str:
    if value is None:
        return "unknown"
    if unit in ("money", "usd"):
        return fmt_money(value)
    if unit == "percent":
        return f"{value:.3f}%" if 99 <= value < 100 else f"{value:.1f}%"  # 99.991% is not 100.0%
    if unit == "days":
        return f"{value:.1f} days"
    if unit == "hours":
        return f"{value:.2f} h"
    if unit == "months":
        return f"{value:.1f} months"
    if unit == "ms":
        return f"{value:.0f} ms"
    if unit == "index":
        return f"{value:.0f}"
    return f"{value:,.1f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------------------------- runs
def run(defn: MetricDef, data: HistoryData, params: CatalogParams, as_of: date) -> Computation:
    """One compute as of a date (the as-of rule is in the context: the cut at the end of that local day)."""
    if as_of < data.start:
        return Computation.unknown(f"{as_of} is before the history ({data.start}).")
    if as_of >= data.anchor + timedelta(days=1):
        return Computation.unknown(f"{as_of} is after T ({data.anchor}): the history has no data for it.")
    ctx = Ctx(data=data, params=params, metric_id=defn.id, as_of=as_of, cut=data.clock.end_of_day(as_of))
    c = defn.compute(ctx)
    if c.value is not None:
        c.value = round(float(c.value), defn.digits)
    return c


@dataclass
class Evaluation:
    defn: MetricDef
    as_of: date
    computation: Computation
    kb: KbMetricResult
    result: MetricResult


def _bands(p: MetricParams) -> Bands | None:
    return Bands(good=p.bands.good, warn=p.bands.warn) if p.bands is not None else None


def kb_record(item: Item) -> KbRecord:
    extra: dict[str, Any] = {}
    if item.related_id is not None or item.related_title is not None:
        extra = {
            "related_id": item.related_id,
            "related_title": item.related_title,
            "related_label": item.related_label,
        }
    return KbRecord(
        id=item.kb_id, kind=item.kind, title=item.title, date=item.date, facts=dict(item.facts), **extra
    )


def definition(defn: MetricDef, p: MetricParams) -> KbMetricDef:
    kw: dict[str, Any] = {}
    if defn.items_label:
        kw["items_label"] = fill(defn.items_label, p)
    return KbMetricDef(
        id=defn.id,
        roles=[MVP_ROLE],
        area=defn.area,
        title=fill(defn.title, p),
        question=defn.question,
        definition=fill(defn.definition, p),
        how_calculated=[fill(x, p) for x in defn.how_calculated],
        unit=defn.unit,
        direction=defn.direction,
        target=p.target,
        bands=_bands(p),
        view=defn.view,
        sources=list(defn.sources),
        reference=KbMetricRef(name=defn.reference.name, url=defn.reference.url, note=defn.reference.note),
        **kw,
    )


def _assign_ids(node: LineageNode, path: str) -> None:
    node.id = path
    for k, child in enumerate(node.children):
        _assign_ids(child, f"{path}.{k}")


def evaluate(
    defn: MetricDef,
    data: HistoryData,
    params: CatalogParams,
    as_of: date | None = None,
    *,
    computed_at: str | None = None,
) -> Evaluation:
    as_of = as_of or data.anchor
    p = params.of(defn.id)
    weeks = week_dates(as_of, params.series_weeks)
    runs = {d: run(defn, data, params, d) for d in weeks}
    cur = runs[as_of]
    week_ago = as_of - timedelta(days=7)
    prev = runs.get(week_ago) or run(defn, data, params, week_ago)
    day = run(defn, data, params, as_of - timedelta(days=1))
    # the card shows «target X» next to the status: on track exactly when the value meets the target
    status = status_with_target(cur.value, defn.direction, _bands(p), p.target)
    series = [SeriesPoint(date=d.isoformat(), value=runs[d].value) for d in weeks]
    d = definition(defn, p)
    items = cur.items
    kw: dict[str, Any] = {}
    if cur.baseline is not None:
        kw["baseline"] = cur.baseline
    if cur.display:
        shown = {k: cur.display[k] for k in ("value", "label", "note", "negate") if k in cur.display}
        if shown:
            kw["display"] = KbDisplay(**shown)
    kb = KbMetricResult(
        **d.model_dump(by_alias=False, exclude_unset=True),
        as_of=as_of.isoformat(),
        value=cur.value,
        status=status,
        previous=prev.value,
        series=series,
        breakdown=[
            KbBreakdownItem(key=b.key, label=b.label, value=b.value)
            for b in cur.breakdown
            if b.value is not None
        ],
        items=[kb_record(i) for i in items[: params.items_limit]],
        items_total=len(items),
        restricted_total=0,
        sample=cur.sample,
        caveats=list(cur.caveats),
        **kw,
    )
    root = LineageNode(
        id="root",
        label=d.title,
        kind="metric",
        value=cur.value,
        unit=defn.gen_unit,
        explanation=f"{d.title} as of {as_of}: {show_value(cur.value, defn.unit)}",
        formula_text=cur.formula_text,
        children=list(cur.lineage),
    )
    if cur.value is None:
        root.unknown_reason = cur.unknown_reason or "unknown"
    _assign_ids(root, "root")
    now_ids = {i.kb_id: i for i in items}
    old_ids = {i.kb_id: i for i in prev.items}
    added = [record_ref(i) for k, i in now_ids.items() if k not in old_ids]
    removed = [record_ref(i) for k, i in old_ids.items() if k not in now_ids]
    unknown_parts = list(
        dict.fromkeys([*cur.unknown_components, *(n for n in defn.needs if not data.has(n))])
    )
    result = MetricResult(
        metric_id=defn.id,
        spec_version=SPEC_VERSION,
        as_of=as_of.isoformat(),
        value=cur.value,
        unit=defn.gen_unit,
        status=status,
        previous=Previous(day=day.value, week=prev.value),
        series=series,
        breakdown=[
            GenBreakdownItem(
                key=b.key,
                label=b.label,
                value=b.value,
                detail=Detail(kind=DETAIL_KIND, id=b.detail_id) if b.detail_id else None,
            )
            for b in cur.breakdown
        ],
        highlight=[record_ref(i) for i in items[:HIGHLIGHT]],
        changes=Changes(
            compared_to=week_ago.isoformat(),
            added=added[:CHANGES_MAX],
            removed=removed[:CHANGES_MAX],
            added_total=len(added),
            removed_total=len(removed),
        ),
        lineage=root,
        coverage=Coverage(
            records=cur.records_total if cur.records_total is not None else len(items),
            unknown_components=unknown_parts,
            history_available=sum(1 for s in series if s.value is not None) >= 2,
        ),
        computed_at=computed_at or datetime.now(UTC).isoformat(timespec="seconds"),
        engine_version=ENGINE_VERSION,
    )
    return Evaluation(defn=defn, as_of=as_of, computation=cur, kb=kb, result=result)
