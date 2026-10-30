"""Core types: a metric definition, its context as of a date, what a compute returns, lineage helpers.

A compute function takes a ``Ctx`` (the history, the parameters, the as-of date and its cut) and returns a
``Computation``: the value (None = unknown, with a reason), the records behind it (``Item``, each with its KB
entity id), the breakdown, caveats and the lineage children (operations -> aggregates -> events -> causes).
The engine (``engine.py``) turns it into the UI contracts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from variator.metrics.catalog.datasets import HistoryData
from variator.metrics.catalog.ids import cause_ref
from variator.metrics.catalog.params import CatalogParams, MetricParams
from variator.metrics.results import (
    CauseRef,
    DatasetId,
    Detail,
    Direction,
    GenUnit,
    KbAreaId,
    KbUnit,
    KbView,
    LineageNode,
    RecordRef,
)

RoleId = Literal["ceo"]  # the one user role of the MVP: the CEO covers every metric
MVP_ROLE: RoleId = "ceo"
Fact = str | int | float | None
DETAIL_KIND = "kbEntity"  # results.EntityKind of a source record; the id is the KB entity id
# The area of the brief that shows a metric, by its family.
AREA_OF_FAMILY: dict[str, KbAreaId] = {
    "finance": "money",
    "clients": "clients",
    "operations": "delivery",
    "delivery": "delivery",
    "people": "delivery",
}


@dataclass
class Item:
    """One record behind a number, with its knowledge-base entity id."""

    kb_id: str
    dataset: DatasetId
    kind: str
    title: str
    date: str | None
    facts: dict[str, Fact] = field(default_factory=dict)
    related_id: str | None = None
    related_title: str | None = None
    related_label: str | None = None
    event: tuple[str, str] | None = None  # (event kind, ISO time) of the version that makes it count
    causes: tuple[str, ...] = ()  # declared causes of that version, "type:id"


@dataclass
class Breakdown:
    key: str
    label: str
    value: float | None
    detail_id: str | None = None


@dataclass
class Computation:
    value: float | None
    items: list[Item] = field(default_factory=list)
    breakdown: list[Breakdown] = field(default_factory=list)
    sample: int | None = None
    caveats: list[str] = field(default_factory=list)
    unknown_reason: str | None = None
    lineage: list[LineageNode] = field(default_factory=list)
    formula_text: str | None = None
    baseline: float | None = None
    records_total: int | None = None
    unknown_components: list[str] = field(default_factory=list)
    # How to show the value when the number alone misleads: "value", "label", "note", "negate" go to
    # results.KbDisplay; "phrase" is the value in the brief headline («runway over 5 years»).
    display: dict[str, Any] | None = None

    @classmethod
    def unknown(
        cls, reason: str, *, caveats: Sequence[str] = (), components: Sequence[str] = ()
    ) -> Computation:
        return cls(
            value=None,
            unknown_reason=reason,
            caveats=[reason, *caveats],
            unknown_components=list(components),
        )


@dataclass
class Ctx:
    data: HistoryData
    params: CatalogParams
    metric_id: str
    as_of: date
    cut: datetime

    @property
    def p(self) -> MetricParams:
        return self.params.of(self.metric_id)

    @property
    def window(self) -> int:
        return int(self.p.window_days or 30)

    def opt(self, name: str, default: Any = None) -> Any:
        return self.p.options.get(name, default)

    def end_of(self, d: date) -> datetime:
        return self.data.clock.end_of_day(d)

    def window_start(self, days: int | None = None) -> date:
        """The first day inside the trailing window of ``days`` days that ends at the as-of date."""
        return self.as_of - timedelta(days=(days or self.window) - 1)

    def in_window(self, d: date | None, days: int | None = None) -> bool:
        return d is not None and self.window_start(days) <= d <= self.as_of

    @property
    def records_max(self) -> int:
        return self.params.lineage_records_max

    def local(self, ts: datetime) -> date:
        return self.data.clock.local_date(ts)


@dataclass(frozen=True)
class MetricRef:
    """Where the definition of a metric comes from, for people to check a number against it.

    Every metric of every module has one (Rule 11): a public page (`url`), or, for a metric that VARIATOR defines
    itself, `url=None` and a `note` that says why no public definition applies.
    """

    name: str
    url: str | None
    note: str = ""

    def __post_init__(self) -> None:
        if self.url is not None and not self.url.startswith("https://"):
            raise ValueError(f"metric reference {self.name!r}: the url must start with https://")
        if self.url is None and not self.note.strip():
            raise ValueError(f"metric reference {self.name!r}: without a url, a note must say why")


@dataclass(frozen=True)
class MetricDef:
    """A catalogue metric. It has no role list: the one MVP role (the CEO) sees every metric."""

    id: str
    title: str
    question: str
    definition: str
    how_calculated: tuple[str, ...]
    unit: KbUnit
    gen_unit: GenUnit
    direction: Direction
    view: KbView
    sources: tuple[str, ...]
    needs: tuple[str, ...]
    defaults: MetricParams
    compute: Callable[[Ctx], Computation]
    #: the source of the definition (required: a metric without one does not load)
    reference: MetricRef
    items_label: str | None = None
    digits: int = 2
    family: str = ""

    @property
    def area(self) -> KbAreaId:
        return AREA_OF_FAMILY[self.family]


# ---------------------------------------------------------------------------------------------- numbers
def D(x: Any) -> Decimal:
    """Decimal of a money value (floats go through their shortest text form)."""
    if isinstance(x, Decimal):
        return x
    if x is None or x == "":
        return Decimal(0)
    return Decimal(str(x))


def money(x: Decimal | float | int) -> float:
    return float(D(x).quantize(Decimal("0.01")))


def num(x: Any) -> float | None:
    if x is None or x == "":
        return None
    return float(x)


def rnd(x: float | Decimal | None, digits: int = 2) -> float | None:
    return None if x is None else round(float(x), digits)


def _num(v: float, digits: int) -> str:
    """At most `digits` decimals, no trailing zeros, thousands separated: 62.0 -> «62», 9.75 -> «9.75»."""
    text = f"{abs(v):,.{digits}f}"
    if digits:
        text = text.rstrip("0").rstrip(".")
    return ("−" if v < 0 and text.strip("0.,") else "") + text


def tile_value(value: float | None, unit: str) -> str:
    """A value as the brief's tile shows it (workspace src/kb/api.ts formatValue), in words for a sentence:
    the headline and the tiles give the same numbers («ARR $12.0M», «62 days», «deploys per week 9.75»,
    a true minus sign)."""
    if value is None:
        return "unknown"
    v = float(value)
    if unit in ("money", "usd"):
        a = abs(v)
        body = f"{a / 1_000_000:.{1 if a >= 10_000_000 else 2}f}M" if a >= 1_000_000 else f"{round(a):,}"
        return f"{'−' if v < 0 else ''}${body}"
    if unit == "percent":
        return f"{_num(v, 3 if 99 <= v < 100 else 1)}%"  # 99.991% is not 100%
    if unit == "days":
        return f"{_num(v, 1)} days"
    if unit == "hours":
        return f"{_num(v, 1)} h"
    if unit == "months":
        return f"{_num(v, 1)} months"
    if unit == "ms":
        return f"{_num(v, 0)} ms"
    if unit == "index":
        return _num(round(v), 0)
    return _num(v, 3)


def fmt_money(x: float | Decimal | None) -> str:
    if x is None:
        return "unknown"
    v = float(x)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:.2f}M"
    if a >= 10_000:
        return f"{sign}${a / 1_000:.0f}k"
    return f"{sign}${a:,.0f}"


def top_breakdown(
    rows: Iterable[tuple[str, str, float, str | None]], n: int = 12, other: str = "Other"
) -> list[Breakdown]:
    """Sorted by value (largest first), the tail summed into one "Other" row."""
    rows = sorted(rows, key=lambda r: (-abs(r[2]), r[1]))
    out = [Breakdown(k, label, round(v, 2), det) for k, label, v, det in rows[:n]]
    tail = rows[n:]
    if tail:
        out.append(Breakdown("other", f"{other} ({len(tail)})", round(sum(r[2] for r in tail), 2)))
    return out


# ---------------------------------------------------------------------------------------------- lineage
def record_ref(item: Item) -> RecordRef:
    fields = {k: v for k, v in item.facts.items() if v is None or isinstance(v, (str, int, float, bool))}
    return RecordRef(
        dataset=item.dataset,
        id=item.kb_id,
        label=item.title,
        detail=Detail(kind=DETAIL_KIND, id=item.kb_id),
        fields=fields,
        evidence_ids=[item.kb_id],
    )


def aggregate(
    label: str,
    value: float | None,
    unit: GenUnit | None,
    explanation: str,
    items: Sequence[Item],
    *,
    limit: int,
    formula: str | None = None,
    unknown_reason: str | None = None,
    children: Sequence[LineageNode] = (),
) -> LineageNode:
    node = LineageNode(
        id="",
        label=label,
        kind="aggregate",
        value=value,
        unit=unit,
        explanation=explanation,
        formula_text=formula,
        children=list(children),
        records=[record_ref(i) for i in items[:limit]],
        records_total=len(items),
    )
    if unknown_reason:
        node.unknown_reason = unknown_reason
    return node


def operation(
    label: str,
    value: float | None,
    unit: GenUnit | None,
    explanation: str,
    formula: str | None,
    children: Sequence[LineageNode],
    *,
    unknown_reason: str | None = None,
) -> LineageNode:
    node = LineageNode(
        id="",
        label=label,
        kind="operation",
        value=value,
        unit=unit,
        explanation=explanation,
        formula_text=formula,
        children=list(children),
    )
    if unknown_reason:
        node.unknown_reason = unknown_reason
    return node


def _cause_title(data: HistoryData, kb: str) -> str:
    if kb.startswith("sim."):
        rid = kb[4:]
        for table in data.sim.values():
            if rid in table:
                v = table.history(rid)[-1]
                t = v.f.get("title") or v.f.get("statement") or v.f.get("reference")
                if t:
                    return str(t)
    return data.titles.get(kb, kb)


def _event_subject(item: Item) -> str:
    """Who an event is about: the client of the record, else its title."""
    client = item.facts.get("client")
    return str(client) if client else item.title


def chains(items: Sequence[Item], data: HistoryData, n: int = 5) -> list[LineageNode]:
    """Event nodes (the version that makes a record count) with their declared causes, first ``n`` items."""
    out: list[LineageNode] = []
    for item in [i for i in items if i.event is not None][:n]:
        kind, at = item.event  # type: ignore[misc]
        causes = []
        for raw in item.causes:
            ctype, kb = cause_ref(raw)
            causes.append(
                LineageNode(
                    id="",
                    label=_cause_title(data, kb),
                    kind="cause",
                    value=None,
                    unit=None,
                    explanation=f"Recorded cause of {item.title}",
                    formula_text=None,
                    children=[],
                    cause=CauseRef(type=ctype, id=kb),
                )
            )
        out.append(
            LineageNode(
                id="",
                label=f"{item.title}: {kind.replace('_', ' ')}",
                kind="event",
                value=None,
                unit=None,
                # words for people: who, what happened, the day it became known (no record id, no code, no time)
                explanation=f"{_event_subject(item)} · {kind.replace('_', ' ').capitalize()} · {at[:10]}",
                formula_text=None,
                children=causes,
                records=[record_ref(item)],
                records_total=1,
            )
        )
    return out
