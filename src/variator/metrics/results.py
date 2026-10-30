"""Metric results in the exact JSON shape of the VARIATOR workspace (from the Plancy UI).

The models mirror these TypeScript types of ``workspace/``:

- ``src/kb/types.ts``: ``KbBrief`` (with ``KbRole`` and ``KbBriefArea``), ``KbMetricResult``
  (with ``KbMetricDef`` and ``KbRecord``);
- ``src/genui/contracts.ts``: ``MetricResult``, ``LineageNode`` and ``RecordRef``.

Python names are snake_case; ``model_dump()`` gives camelCase keys. A TypeScript
optional field (``name?: T``) is left out of the JSON when it is not set, so a parsed
document dumps back to the same JSON. ``DatasetId`` holds the Plancy datasets and the
datasets of a simulation; ``LineageNode`` has the simulation levels ``event`` and
``cause`` (a cause node names its declared cause in ``cause``).

The MVP has one user role, the CEO (``KbRoleId``): every metric has ``roles=["ceo"]``.
A record that opens a knowledge-base entity has ``Detail(kind="kbEntity", id=<entity id>)``.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, model_serializer
from pydantic.alias_generators import to_camel

KbRoleId = Literal["ceo"]
KbAreaId = Literal["money", "clients", "delivery"]
# index: a number on its own scale, not 0–100 (NPS runs from −100 to 100)
KbUnit = Literal["money", "count", "hours", "days", "months", "ms", "percent", "score", "index"]
Status = Literal["good", "warn", "bad", "neutral", "unknown"]
Direction = Literal["higher", "lower", "context"]
KbView = Literal["kpi", "list", "breakdown"]
GenUnit = Literal[
    "count", "percent", "ratio", "usd", "days", "weeks", "months", "hours", "ms", "score", "index", "multiple"
]
DatasetId = Literal[
    # Plancy GenUI datasets
    "projects",
    "employees",
    "risks",
    "commitments",
    "deals",
    "processes",
    "quarters",
    "finance",
    "evidence",
    "directives",
    "issues",
    "followUps",
    "sectorMetrics",
    # datasets of a VARIATOR simulation
    "contracts",
    "invoices",
    "payments",
    "bank",
    "people",
    "requisitions",
    "payroll",
    "incidents",
    "deploys",
    "support_tickets",
    "cloud_bills",
    "tax_payments",
    "decisions",
    "events",
    "nps",
    "monitoring",
]
LineageKind = Literal["metric", "operation", "component", "aggregate", "record", "event", "cause"]
EntityKind = Literal[
    "kbEntity",  # a knowledge-base entity: the id is the KB entity id (the workspace opens its dossier)
    "project",
    "risk",
    "employee",
    "evidence",
    "deal",
    "process",
    "quarter",
    "metric",
    "gmetric",
    "directive",
    "issue",
    "followUp",
]
Scalar = str | bool | int | float | None


class TsModel(BaseModel):
    """camelCase JSON; optional TypeScript fields are left out when they are not set.

    ``ts_optional``: fields typed ``name?: T`` (left out when unset or None).
    ``ts_optional_nullable``: fields typed ``name?: T | null`` (left out only when unset).
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True, extra="forbid"
    )
    ts_optional: ClassVar[frozenset[str]] = frozenset()
    ts_optional_nullable: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _drop_unset_optional(self, handler: Any, info: Any) -> dict[str, Any]:
        data = handler(self)
        by_alias = info.by_alias is not False
        for name in self.ts_optional | self.ts_optional_nullable:
            key = type(self).model_fields[name].alias if by_alias else name
            unset = name not in self.model_fields_set
            if unset or (name in self.ts_optional and getattr(self, name) is None):
                data.pop(key, None)
        return data

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------- KB brief (src/kb/types.ts)
class Bands(TsModel):
    good: float
    warn: float


class SeriesPoint(TsModel):
    date: str
    value: float | None


class KbBreakdownItem(TsModel):
    key: str
    label: str
    value: float


class KbRecord(TsModel):
    ts_optional: ClassVar[frozenset[str]] = frozenset({"restricted"})
    ts_optional_nullable: ClassVar[frozenset[str]] = frozenset(
        {"related_id", "related_title", "related_label"}
    )

    id: str
    kind: str
    title: str
    date: str | None
    facts: dict[str, str | int | float | None]
    restricted: bool | None = None
    related_id: str | None = None
    related_title: str | None = None
    related_label: str | None = None


class KbMetricRef(TsModel):
    """The source of a metric's definition (``KbMetricRef`` of ``src/kb/types.ts``): a public page, or a note."""

    name: str
    url: str | None
    note: str = ""


class KbMetricDef(TsModel):
    ts_optional: ClassVar[frozenset[str]] = frozenset({"items_label", "area", "reference"})

    id: str
    roles: list[KbRoleId]
    area: KbAreaId | None = None
    title: str
    question: str
    definition: str
    how_calculated: list[str]
    unit: KbUnit
    direction: Direction
    target: float | None
    bands: Bands | None
    view: KbView
    sources: list[str]
    items_label: str | None = None
    reference: KbMetricRef | None = None


class KbDisplay(TsModel):
    """How to show a value when the number alone misleads (``KbDisplay`` of ``src/kb/types.ts``).

    ``value``: the text that replaces the number («60+ mo» for a runway at its cap).
    ``label``: the title to show instead; with ``negate`` the number, its series and its change are shown
    negated (a negative burn as the net cash flow). ``note``: why, for a tooltip.
    """

    ts_optional: ClassVar[frozenset[str]] = frozenset({"value", "label", "note", "negate"})

    value: str | None = None
    label: str | None = None
    note: str | None = None
    negate: bool | None = None


class KbMetricResult(KbMetricDef):
    ts_optional: ClassVar[frozenset[str]] = frozenset(
        {"items_label", "area", "reference", "custom", "display"}
    )
    ts_optional_nullable: ClassVar[frozenset[str]] = frozenset({"baseline"})

    as_of: str
    value: float | None
    status: Status
    previous: float | None
    series: list[SeriesPoint]
    breakdown: list[KbBreakdownItem]
    items: list[KbRecord]
    items_total: int
    restricted_total: int
    sample: int | None
    caveats: list[str]
    baseline: float | None = None
    custom: bool | None = None
    display: KbDisplay | None = None


class KbRole(TsModel):
    """The user role of a brief (``KbRole``): the MVP has one, the CEO."""

    ts_optional: ClassVar[frozenset[str]] = frozenset({"metadata"})

    id: KbRoleId
    label: str
    title: str
    name: str
    person_id: str
    principal: str
    metadata: bool | None = None


class KbBriefArea(TsModel):
    """One area of the brief: its metric ids in order (the top-row metrics are not repeated)."""

    id: KbAreaId
    title: str
    question: str
    metrics: list[str]


class BriefRange(TsModel):
    start: str
    end: str


class KbBrief(TsModel):
    """``KbBrief`` of ``src/kb/types.ts``: the brief the workspace reads.

    ``metrics`` holds every metric once; ``top`` (the key numbers) and ``areas`` place them on the page.
    ``organization`` is the company card (``KbOrganization``) as a plain object.
    """

    organization: dict[str, Any]
    role: KbRole
    as_of: str
    range: BriefRange
    weeks: list[str]
    headline: list[str]
    metrics: list[KbMetricResult]
    top: list[str]
    areas: list[KbBriefArea]
    built_at: str
    watermark: int | None


# ---------------------------------------------------------------------- GenUI (src/genui/contracts.ts)
class Detail(TsModel):
    kind: EntityKind
    id: str


class RecordRef(TsModel):
    dataset: DatasetId
    id: str
    label: str
    detail: Detail | None
    fields: dict[str, Scalar]
    evidence_ids: list[str]


class Excluded(TsModel):
    count: int
    reason: str
    sample: list[RecordRef]


class CauseRef(TsModel):
    """The declared cause of an event (``orgsim.events.Cause``): decision, hypothesis, habit..."""

    type: str
    id: str


class LineageNode(TsModel):
    ts_optional: ClassVar[frozenset[str]] = frozenset(
        {"weight", "unknown_reason", "records", "records_total", "excluded", "sample", "cause"}
    )
    ts_optional_nullable: ClassVar[frozenset[str]] = frozenset({"normalized"})

    id: str
    label: str
    kind: LineageKind
    value: float | None
    unit: GenUnit | None
    explanation: str
    formula_text: str | None
    weight: float | None = None
    normalized: float | None = None
    unknown_reason: str | None = None
    children: list[LineageNode]
    records: list[RecordRef] | None = None
    records_total: int | None = None
    excluded: list[Excluded] | None = None
    sample: int | None = None
    cause: CauseRef | None = None


class Previous(TsModel):
    day: float | None
    week: float | None


class GenBreakdownItem(TsModel):
    key: str
    label: str
    value: float | None
    detail: Detail | None


class Changes(TsModel):
    compared_to: str
    added: list[RecordRef]
    removed: list[RecordRef]
    added_total: int
    removed_total: int


class Coverage(TsModel):
    records: int
    unknown_components: list[str]
    history_available: bool


class MetricResult(TsModel):
    metric_id: str
    spec_version: int
    as_of: str
    value: float | None
    unit: GenUnit
    status: Status
    previous: Previous
    series: list[SeriesPoint]
    breakdown: list[GenBreakdownItem]
    highlight: list[RecordRef]
    changes: Changes
    lineage: LineageNode
    coverage: Coverage
    computed_at: str
    engine_version: str


def status_of(value: float | None, direction: Direction, bands: Bands | None) -> Status:
    """Status of a value by its bands, read with the direction (as Plancy reads ``bands``).

    higher: value >= good -> good, >= warn -> warn, else bad; lower: the mirror.
    No value -> unknown; no bands or direction ``context`` -> neutral.
    """
    if value is None:
        return "unknown"
    if bands is None or direction == "context":
        return "neutral"
    if direction == "higher":
        return "good" if value >= bands.good else "warn" if value >= bands.warn else "bad"
    return "good" if value <= bands.good else "warn" if value <= bands.warn else "bad"


def status_with_target(
    value: float | None, direction: Direction, bands: Bands | None, target: float | None
) -> Status:
    """The status a card shows next to its target: on track (good) exactly when the value meets the target.

    A value that misses the target is never good: the bands then give warn or bad (good becomes warn).
    Without a target, or with direction ``context``, it is ``status_of``.
    """
    status = status_of(value, direction, bands)
    if value is None or target is None or direction == "context":
        return status
    meets = value >= target if direction == "higher" else value <= target
    if meets:
        return "good"
    return "warn" if status in ("good", "neutral") else status
