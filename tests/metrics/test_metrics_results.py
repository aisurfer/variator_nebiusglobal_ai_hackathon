"""Plancy-shaped results: JSON written from the TypeScript types parses and dumps back unchanged."""

import json
import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from variator.metrics.results import (
    Bands,
    CauseRef,
    DatasetId,
    GenUnit,
    KbAreaId,
    KbBrief,
    KbMetricResult,
    KbRecord,
    KbRoleId,
    KbUnit,
    LineageKind,
    LineageNode,
    MetricResult,
    RecordRef,
    status_of,
)

CONTRACTS_TS = Path(__file__).resolve().parents[2] / "workspace" / "src" / "genui" / "contracts.ts"

# src/kb/types.ts KbMetricResult: required fields only.
KB_MIN = """{
  "id": "eng.cycle_time", "roles": ["ceo"], "title": "Cycle time",
  "question": "How long does a task take?", "definition": "Median days from In Progress to Done.",
  "howCalculated": ["Take the tasks closed in the week.", "Take the median of their days."],
  "unit": "days", "direction": "lower", "target": 3, "bands": {"good": 3, "warn": 5}, "view": "kpi",
  "sources": ["Jira"], "asOf": "2026-10-27", "value": 4.5, "status": "warn", "previous": 5,
  "series": [{"date": "2026-10-13", "value": 5.5}, {"date": "2026-10-20", "value": null},
             {"date": "2026-10-27", "value": 4.5}],
  "breakdown": [{"key": "l.park", "label": "Lena Park", "value": 4}],
  "items": [{"id": "jira.ENG-1", "kind": "Task", "title": "Retry failed card payments", "date": "2026-10-22",
             "facts": {"days": 4, "status": "Done", "sprint": null}}],
  "itemsTotal": 1, "restrictedTotal": 0, "sample": 1, "caveats": []
}"""

# The same type with every optional field (`?`), a null baseline and a null related id.
KB_FULL = """{
  "id": "ceo.decisions_changed", "roles": ["ceo"], "area": "delivery", "title": "Changed decisions",
  "question": "Which decisions changed?", "definition": "Decisions replaced in the last 4 weeks.",
  "howCalculated": ["Count the replaced decisions."], "unit": "count", "direction": "context",
  "target": null, "bands": null, "view": "list", "sources": ["Confluence"],
  "itemsLabel": "Decisions", "asOf": "2026-10-27", "value": null, "status": "unknown", "previous": null,
  "series": [], "breakdown": [],
  "items": [{"id": "confluence.page-1.dec-2", "kind": "Decision", "title": "Release moves to July 15",
             "date": null, "facts": {}, "restricted": true, "relatedId": null,
             "relatedTitle": "Release on July 1", "relatedLabel": "replaces"}],
  "itemsTotal": 1, "restrictedTotal": 1, "sample": null, "caveats": ["The base has two weeks of data."],
  "baseline": null, "custom": true
}"""

RECORD_REF = {
    "dataset": "directives",
    "id": "d1",
    "label": "Ship the billing fix",
    "detail": {"kind": "directive", "id": "d1"},
    "fields": {"dueAt": "2026-10-20", "late": False, "days": 2, "owner": None},
    "evidenceIds": ["ev1"],
}

# src/genui/contracts.ts MetricResult with a LineageNode tree.
GENUI = {
    "metricId": "m-on-time",
    "specVersion": 2,
    "asOf": "2026-10-27",
    "value": 64.3,
    "unit": "percent",
    "status": "good",
    "previous": {"day": 60.0, "week": None},
    "series": [{"date": "2026-10-26", "value": 60.0}, {"date": "2026-10-27", "value": 64.3}],
    "breakdown": [
        {"key": "p1", "label": "Billing", "value": 70.0, "detail": {"kind": "project", "id": "p1"}},
        {"key": "p2", "label": "Other", "value": None, "detail": None},
    ],
    "highlight": [RECORD_REF],
    "changes": {
        "comparedTo": "2026-10-26",
        "added": [RECORD_REF],
        "removed": [],
        "addedTotal": 1,
        "removedTotal": 0,
    },
    "lineage": {
        "id": "root",
        "label": "On-time rate",
        "kind": "metric",
        "value": 64.3,
        "unit": "percent",
        "explanation": "9 of 14 directives were on time",
        "formulaText": "9 / 14 × 100",
        "children": [
            {
                "id": "root.numerator",
                "label": "On time",
                "kind": "aggregate",
                "value": 9,
                "unit": "count",
                "explanation": "Directives done by the due date: 9",
                "formulaText": None,
                "children": [],
                "records": [RECORD_REF],
                "recordsTotal": 9,
                "excluded": [{"count": 2, "reason": "still open", "sample": [RECORD_REF]}],
            },
            {
                "id": "root.component.0",
                "label": "Speed",
                "kind": "component",
                "value": None,
                "unit": None,
                "explanation": "Too few records",
                "formulaText": None,
                "weight": 0.5,
                "normalized": None,
                "unknownReason": "2 records, 5 needed",
                "sample": 2,
                "children": [
                    {
                        "id": "root.component.0.r",
                        "label": "d1",
                        "kind": "record",
                        "value": 1,
                        "unit": "count",
                        "explanation": "One record",
                        "formulaText": None,
                        "children": [],
                    }
                ],
            },
        ],
    },
    "coverage": {"records": 14, "unknownComponents": ["Speed"], "historyAvailable": True},
    "computedAt": "2026-10-27T07:00:00.000Z",
    "engineVersion": "genui-1",
}


# A simulation result: records of simulation datasets, an event below an aggregate and its declared causes.
SIM_RECORD = {
    "dataset": "invoices",
    "id": "inv-0042",
    "label": "Invoice 42 to Acme",
    "detail": None,
    "fields": {"amount": 12500.0, "paidAt": None, "daysLate": 31},
    "evidenceIds": [],
}
SIM_EVENT = {
    "id": "root.late.e0",
    "label": "invoice_overdue: inv-0042",
    "kind": "event",
    "value": None,
    "unit": None,
    "explanation": "Invoice 42 became overdue on 2027-03-14",
    "formulaText": None,
    "children": [
        {
            "id": "root.late.e0.c0",
            "label": "Payment terms moved to 60 days",
            "kind": "cause",
            "value": None,
            "unit": None,
            "explanation": "Declared cause of the event",
            "formulaText": None,
            "children": [],
            "cause": {"type": "decision", "id": "dec-terms-60"},
        },
        {
            "id": "root.late.e0.c1",
            "label": "Acme pays late before quarter end",
            "kind": "cause",
            "value": None,
            "unit": None,
            "explanation": "",
            "formulaText": None,
            "children": [],
            "cause": {"type": "habit", "id": "client:acme.late_payer"},
        },
    ],
}
GENUI_SIM = GENUI | {
    "metricId": "money.overdue",
    "unit": "count",
    "highlight": [SIM_RECORD, RECORD_REF | {"dataset": "events", "id": "ev-7", "detail": None}],
    "changes": GENUI["changes"] | {"added": [SIM_RECORD]},
    "lineage": {
        "id": "root",
        "label": "Overdue invoices",
        "kind": "metric",
        "value": 1,
        "unit": "count",
        "explanation": "1 invoice is overdue",
        "formulaText": None,
        "children": [
            {
                "id": "root.late",
                "label": "Overdue",
                "kind": "aggregate",
                "value": 1,
                "unit": "count",
                "explanation": "Invoices past their due date: 1",
                "formulaText": None,
                "children": [SIM_EVENT],
                "records": [SIM_RECORD],
                "recordsTotal": 1,
            }
        ],
    },
}


@pytest.mark.parametrize("text", [KB_MIN, KB_FULL])
def test_kb_metric_result_round_trip(text):
    src = json.loads(text)
    model = KbMetricResult.model_validate(src)
    assert model.model_dump(mode="json") == src
    assert json.loads(model.model_dump_json()) == src


def test_genui_metric_result_round_trip():
    model = MetricResult.model_validate(GENUI)
    assert model.model_dump(mode="json") == GENUI
    assert model.lineage.children[1].children[0].kind == "record"
    assert json.loads(model.model_dump_json()) == GENUI


def test_simulation_result_round_trip():
    model = MetricResult.model_validate(GENUI_SIM)
    assert model.model_dump(mode="json") == GENUI_SIM
    assert json.loads(model.model_dump_json()) == GENUI_SIM
    event = model.lineage.children[0].children[0]
    assert event.kind == "event" and event.cause is None
    assert [c.cause for c in event.children] == [
        CauseRef(type="decision", id="dec-terms-60"),
        CauseRef(type="habit", id="client:acme.late_payer"),
    ]
    assert [r.dataset for r in model.highlight] == ["invoices", "events"]


@pytest.mark.parametrize(
    "dataset", ["contracts", "support_tickets", "cloud_bills", "tax_payments", "decisions"]
)
def test_record_ref_accepts_simulation_datasets(dataset):
    ref = RecordRef.model_validate(SIM_RECORD | {"dataset": dataset})
    assert ref.model_dump()["dataset"] == dataset


def test_cause_is_dumped_only_when_set():
    base = {"id": "e", "label": "Event", "value": None, "unit": None, "explanation": "", "formula_text": None}
    assert "cause" not in LineageNode(kind="event", children=[], **base).model_dump()
    # `cause?:` is never null in the JSON
    assert "cause" not in LineageNode(kind="cause", children=[], cause=None, **base).model_dump()
    cause = CauseRef(type="life_event", id="person:lena.leave")
    node = LineageNode(kind="cause", children=[], cause=cause, **base)
    assert node.model_dump()["cause"] == {"type": "life_event", "id": "person:lena.leave"}


def test_python_names_give_camel_case_and_skip_unset_optionals():
    rec = KbRecord(id="x", kind="Invoice", title="INV-1", date="2026-10-01", facts={"amount": 1500.0})
    assert rec.model_dump() == {
        "id": "x",
        "kind": "Invoice",
        "title": "INV-1",
        "date": "2026-10-01",
        "facts": {"amount": 1500.0},
    }
    node = LineageNode(
        id="root",
        label="Cash",
        kind="metric",
        value=None,
        unit=None,
        explanation="Unknown",
        formula_text=None,
        children=[],
        normalized=None,
        unknown_reason=None,
    )
    out = node.model_dump()
    assert out["formulaText"] is None and out["normalized"] is None  # `normalized?: number | null` was set
    assert "unknownReason" not in out and "weight" not in out  # `unknownReason?: string` is never null
    assert node.model_dump(by_alias=False)["formula_text"] is None


def test_wrong_shapes_are_refused():
    bad = json.loads(KB_MIN) | {"extra": 1}
    with pytest.raises(ValidationError):
        KbMetricResult.model_validate(bad)
    with pytest.raises(ValidationError):
        KbMetricResult.model_validate(json.loads(KB_MIN) | {"status": "red"})
    with pytest.raises(ValidationError):
        MetricResult.model_validate(GENUI | {"unit": "money"})  # a KB unit, not a GenUI unit
    with pytest.raises(ValidationError):
        KbMetricResult.model_validate(json.loads(KB_MIN) | {"roles": ["finance"]})  # one MVP role: ceo
    with pytest.raises(ValidationError):
        RecordRef.model_validate(SIM_RECORD | {"dataset": "invoice"})  # not a dataset id
    with pytest.raises(ValidationError):
        LineageNode.model_validate(SIM_EVENT | {"kind": "shock"})  # not a lineage kind
    with pytest.raises(ValidationError):
        LineageNode.model_validate(SIM_EVENT["children"][0] | {"cause": {"type": "decision"}})  # no id


def test_unions_match_the_typescript_contract():
    """DatasetId, the GenUI units and the LineageNode kinds are the lists of workspace/src/genui/contracts.ts;
    the role, area and unit ids of the brief are the ones of workspace/src/kb/types.ts."""
    if not CONTRACTS_TS.exists():
        pytest.skip("workspace/ is not in this checkout")
    text = CONTRACTS_TS.read_text(encoding="utf-8")
    enum = re.search(r"export const DatasetId = z\.enum\(\[(.*?)\]\);", text, re.S)
    units = re.search(r"export const Unit = z\.enum\(\[(.*?)\]\);", text, re.S)
    kinds = re.search(r"^\s*kind: ('metric'[^;]*);", text, re.M)
    assert enum and units and kinds
    assert re.findall(r"'([^']+)'", enum.group(1)) == list(get_args(DatasetId))
    assert re.findall(r"'([^']+)'", units.group(1)) == list(get_args(GenUnit))
    assert re.findall(r"'([^']+)'", kinds.group(1)) == list(get_args(LineageKind))
    kb = (CONTRACTS_TS.parents[1] / "kb" / "types.ts").read_text(encoding="utf-8")
    for name, union in (("KbRoleId", KbRoleId), ("KbAreaId", KbAreaId), ("KbUnit", KbUnit)):
        found = re.search(rf"export type {name} = ([^;]*);", kb)
        assert found and re.findall(r"'([^']+)'", found.group(1)) == list(get_args(union)), name


# src/kb/types.ts KbBrief: the CEO brief with a top row and areas.
BRIEF = {
    "organization": {"id": "company", "tenant": "company", "name": "Larchwood Systems", "roles": []},
    "role": {
        "id": "ceo",
        "label": "CEO",
        "title": "Chief Executive Officer",
        "name": "Avery Collins",
        "personId": "person.lw-e01",
        "principal": "user:admin",
        "metadata": True,
    },
    "asOf": "2026-10-26",
    "range": {"start": "2025-10-27", "end": "2026-10-26"},
    "weeks": ["2026-10-19", "2026-10-26"],
    "headline": ["Money: cash $2.00M."],
    "metrics": [json.loads(KB_MIN), json.loads(KB_FULL)],
    "top": ["eng.cycle_time"],
    "areas": [
        {"id": "money", "title": "Money", "question": "Is there enough cash?", "metrics": []},
        {
            "id": "delivery",
            "title": "Delivery and service",
            "question": "Do we deliver?",
            "metrics": ["ceo.decisions_changed"],
        },
    ],
    "builtAt": "2026-10-27T00:00:00+00:00",
    "watermark": None,
}


def test_kb_brief_round_trip_and_the_one_role():
    model = KbBrief.model_validate(BRIEF)
    assert model.model_dump(mode="json") == BRIEF
    assert model.role.person_id == "person.lw-e01" and model.areas[1].metrics == ["ceo.decisions_changed"]
    with pytest.raises(ValidationError):
        KbBrief.model_validate(BRIEF | {"role": BRIEF["role"] | {"id": "finance"}})
    with pytest.raises(ValidationError):
        KbBrief.model_validate(BRIEF | {"areas": [BRIEF["areas"][0] | {"id": "sales"}]})


def test_display_hint_round_trips_and_only_set_keys_are_written():
    capped = json.loads(KB_MIN) | {"unit": "months", "display": {"value": "60+ mo", "note": "Capped."}}
    assert KbMetricResult.model_validate(capped).model_dump(mode="json") == capped
    inflow = json.loads(KB_MIN) | {"display": {"label": "Net cash flow per month", "negate": True}}
    assert KbMetricResult.model_validate(inflow).model_dump(mode="json") == inflow
    nps = json.loads(KB_MIN) | {"unit": "index"}
    assert "display" not in KbMetricResult.model_validate(nps).model_dump(mode="json")
    with pytest.raises(ValidationError):
        KbMetricResult.model_validate(json.loads(KB_MIN) | {"display": {"phrase": "x"}})


def test_new_units_datasets_and_the_kb_entity_detail():
    for unit in ("months", "ms"):
        assert KbMetricResult.model_validate(json.loads(KB_MIN) | {"unit": unit}).unit == unit
        assert MetricResult.model_validate(GENUI | {"unit": unit}).unit == unit
    for dataset in ("nps", "monitoring"):
        assert RecordRef.model_validate(SIM_RECORD | {"dataset": dataset}).dataset == dataset
    ref = RecordRef.model_validate(SIM_RECORD | {"detail": {"kind": "kbEntity", "id": "sim.INC-0042"}})
    assert ref.model_dump()["detail"] == {"kind": "kbEntity", "id": "sim.INC-0042"}
    with pytest.raises(ValidationError):
        RecordRef.model_validate(SIM_RECORD | {"detail": {"kind": "entity", "id": "x"}})


@pytest.mark.parametrize(
    ("value", "direction", "bands", "want"),
    [
        (None, "higher", Bands(good=3, warn=1), "unknown"),
        (5, "context", Bands(good=3, warn=1), "neutral"),
        (5, "higher", None, "neutral"),
        (3, "higher", Bands(good=3, warn=1), "good"),
        (2, "higher", Bands(good=3, warn=1), "warn"),
        (0.5, "higher", Bands(good=3, warn=1), "bad"),
        (3, "lower", Bands(good=3, warn=5), "good"),
        (5, "lower", Bands(good=3, warn=5), "warn"),
        (6, "lower", Bands(good=3, warn=5), "bad"),
    ],
)
def test_status_of_reads_bands_like_plancy(value, direction, bands, want):
    assert status_of(value, direction, bands) == want
