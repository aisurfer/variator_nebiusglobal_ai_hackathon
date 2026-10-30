"""Event envelope: validation, total order, timeline line and canonical record."""

from __future__ import annotations

import itertools
import random
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from variator.orgsim.events import CAUSE_PREDICATES, Cause, Event, content_sha256, sort_events

TZ = ZoneInfo("Europe/Berlin")
T0 = datetime(2026, 10, 27, 10, 0, tzinfo=TZ)


# A copy of the parts of Plancy's kb/app/kb/contracts.py that a record uses (extra fields forbidden).
class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ref(_M):
    entity_id: str | None = None
    source_id: str | None = None
    object_type: str | None = None
    external_id: str | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _identity(self) -> Ref:
        if not self.entity_id and not (self.source_id and self.object_type and self.external_id):
            raise ValueError("Ref needs entity_id or (source_id, object_type, external_id)")
        return self


class Evidence(_M):
    quote: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)


class StatementSpec(_M):
    predicate: str
    object: Ref | None = None
    literal: Any = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    evidence: Evidence | None = None
    confidence: float = 1.0


class CausalSpec(_M):
    predicate: str
    cause: Ref
    sensitivity: list[str] | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    evidence: Evidence | None = None
    confidence: float = 1.0
    cause_version_seq: int | None = None


class CanonicalRecord(_M):
    key: Ref
    entity_id: str
    kind: str
    title: str
    url: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    body: str | None = None
    valid_from: datetime | None = None
    actor: Ref | None = None
    aliases: list[dict[str, Any]] = Field(default_factory=list)
    statements: list[StatementSpec] = Field(default_factory=list)
    causes: list[CausalSpec] = Field(default_factory=list)
    acl: list[str] | None = None
    deleted: bool = False


def ev(seq: int, ts: datetime = T0, **kw: Any) -> Event:
    base = dict(
        run="r1",
        option="hire_5",
        scenario=3,
        ts=ts,
        process="churn",
        kind="churned",
        entity_id="contract-042",
        fields={"arr": 120000},
        causes=(Cause(type="decision", id="raise_prices_8"),),
        seq=seq,
    )
    return Event(**(base | kw))


def test_event_needs_an_aware_time_and_known_cause_types():
    with pytest.raises(ValidationError):
        ev(0, ts=datetime(2026, 10, 27, 10, 0))
    with pytest.raises(ValidationError):
        Cause(type="rumour", id="x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ev(-1)
    assert ev(0).event_id == "r1:hire_5:3:0"


def test_total_order_is_deterministic_for_any_input_order():
    events = [
        ev(2, T0),
        ev(1, T0),
        ev(0, T0 + timedelta(hours=1)),
        ev(5, T0, scenario="actual"),
        ev(0, T0, scenario=1),
        ev(3, T0, option="base"),
        ev(4, T0.astimezone(ZoneInfo("UTC")), run="r0"),
    ]
    want = sort_events(events)
    for perm in itertools.islice(itertools.permutations(events), 0, 400, 7):
        assert sort_events(perm) == want
    assert [(e.run, e.option, e.scenario, e.seq) for e in want] == [
        ("r0", "hire_5", 3, 4),
        ("r1", "base", 3, 3),
        ("r1", "hire_5", 1, 0),
        ("r1", "hire_5", 3, 1),
        ("r1", "hire_5", 3, 2),
        ("r1", "hire_5", 3, 0),
        ("r1", "hire_5", "actual", 5),
    ]
    assert sorted(events) == want


def test_order_uses_the_instant_not_the_offset():
    berlin = ev(0, datetime(2026, 10, 27, 10, 0, tzinfo=TZ))  # 09:00 UTC
    utc = ev(1, datetime(2026, 10, 27, 8, 30, tzinfo=ZoneInfo("UTC")))
    assert sort_events([berlin, utc]) == [utc, berlin]


def test_timeline_line_has_the_mandatory_fields():
    line = ev(7).to_timeline_line(tenant="acme", source="sim-acme", observe_lag=timedelta(minutes=2))
    for k in (
        "at",
        "world_at",
        "observed_at",
        "source",
        "object_type",
        "external_id",
        "op",
        "modified_at",
        "schema_version",
        "payload",
        "content_sha256",
    ):
        assert line[k] is not None, k
    assert line["at"] == line["observed_at"] == "2026-10-27T10:02:00+01:00"
    assert line["world_at"] == line["modified_at"] == "2026-10-27T10:00:00+01:00"
    assert line["object_type"] == "churn" and line["external_id"] == "contract-042"
    assert line["payload"]["causes"] == [{"type": "decision", "id": "raise_prices_8"}]
    assert line["event_id"] is None and line["sequence"] is None  # the writer fills them
    versioned = ev(7).to_timeline_line(tenant="acme", source="sim-acme", version=2)
    assert versioned["event_id"] == "acme:sim-acme:churn:contract-042:2"
    with pytest.raises(ValueError):
        ev(7).to_timeline_line(tenant="acme", source="s", observe_lag=timedelta(minutes=6))


def test_content_hash_ignores_key_order_and_sees_payload_changes():
    a = ev(1).to_timeline_line(tenant="t", source="s", payload={"x": 1, "y": [1, 2]})
    b = ev(1).to_timeline_line(tenant="t", source="s", payload={"y": [1, 2], "x": 1})
    c = ev(1).to_timeline_line(tenant="t", source="s", payload={"x": 2, "y": [1, 2]})
    assert a["content_sha256"] == b["content_sha256"] != c["content_sha256"]
    assert content_sha256(a) == a["content_sha256"]
    gone = ev(1).to_timeline_line(tenant="t", source="s", op="delete")
    assert gone["payload"] is None and "content_sha256" not in gone


def test_canonical_record_validates_and_declares_every_cause():
    causes = tuple(Cause(type=t, id=f"{t}-1") for t in CAUSE_PREDICATES)
    e = ev(12, causes=causes, entity_id="person:anna smith/x")
    rec = CanonicalRecord.model_validate(e.to_canonical_record())
    assert rec.entity_id == "sim.r1.hire_5.s3.e12" and " " not in rec.entity_id and "/" not in rec.entity_id
    assert rec.kind == "SimEvent" and rec.valid_from == e.ts
    assert [(c.predicate, c.cause.object_type, c.cause.external_id) for c in rec.causes] == [
        (CAUSE_PREDICATES[t], t, f"{t}-1") for t in CAUSE_PREDICATES
    ]
    assert (
        rec.fields["arr"] == 120000
        and rec.fields["option"] == "hire_5"
        and rec.fields["event_kind"] == "churned"
    )
    assert rec.statements[0].object.external_id == "person:anna smith/x"
    custom = e.to_canonical_record(predicates={"decision": "caused_by"}, kind="Churn", acl=["grp:management"])
    assert custom["causes"][0]["predicate"] == "caused_by" and custom["kind"] == "Churn"


def test_random_events_round_trip_through_json():
    rng = random.Random(3)
    for i in range(30):
        e = ev(
            i,
            ts=T0 + timedelta(minutes=rng.randint(0, 10_000)),
            scenario=rng.choice([0, 5, "actual"]),
            fields={"amount": rng.random(), "tags": ["a", "b"]},
        )
        assert Event.model_validate(e.envelope()) == e
