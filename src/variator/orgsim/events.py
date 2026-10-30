"""The event envelope of the organisation simulator and its two projections.

An ``Event`` is one dated fact of one run, option and scenario, with its declared
causes. Two projections exist:

- ``to_timeline_line``: a line of ``fixtures/timeline.jsonl.gz`` in the corpus
  contract (``plancy-kb-corpus/1`` §2.1);
- ``to_canonical_record``: a dict with the fields of Plancy's ``CanonicalRecord``,
  where each declared cause is a ``CausalSpec`` entry (completeness 1.0, no
  extraction). This is the reference adapter for the simulation provider (§2.4).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, timedelta
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

CauseType = Literal["decision", "hypothesis", "life_event", "habit", "rule", "process"]
CAUSE_TYPES: tuple[CauseType, ...] = ("decision", "hypothesis", "life_event", "habit", "rule", "process")

# Predicates of Plancy's ontology (kb/config/ontology.yaml) for each cause type.
CAUSE_PREDICATES: dict[str, str] = {
    "decision": "implements",
    "hypothesis": "based_on",
    "life_event": "caused_by",
    "habit": "motivated_by",
    "rule": "governed_by",
    "process": "caused_by",
}
SIM_SOURCE = "variator-sim"
SCHEMA_VERSION = "plancy-event/2"
MAX_OBSERVE_LAG = timedelta(minutes=5)
_UNSAFE = re.compile(r"[^0-9A-Za-z_.@-]+")


def canonical_json(obj: Any) -> str:
    """Sorted keys, no spaces, UTF-8 text: the RFC 8785 form for strings, integers and lists."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


def slug(text: object) -> str:
    """An id part without spaces and '/', as Plancy entity ids need."""
    return _UNSAFE.sub("-", str(text)).strip("-") or "x"


class Cause(BaseModel):
    """A declared cause of an event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: CauseType
    id: str = Field(min_length=1)


class Event(BaseModel):
    """One dated fact of a run.

    ``seq`` is the number of the event in its (run, option, scenario); it must be
    unique there. It also sets the order of events with the same ``ts`` (the question
    comes before the reply).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run: str = Field(min_length=1)
    option: str = Field(min_length=1)
    scenario: int | str
    ts: AwareDatetime
    process: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    fields: dict[str, Any] = Field(default_factory=dict)
    causes: tuple[Cause, ...] = ()
    seq: int = Field(ge=0)

    # ------------------------------------------------------------------ identity and order
    @property
    def event_id(self) -> str:
        return f"{self.run}:{self.option}:{self.scenario}:{self.seq}"

    def envelope(self) -> dict[str, Any]:
        """The event as JSON-ready data."""
        return self.model_dump(mode="json")

    def digest(self) -> str:
        return sha256_hex(canonical_json(self.envelope()))

    def order_key(self) -> tuple:
        """Total order: run, option, scenario (numbers before names), time, seq, then content."""
        s = self.scenario
        scenario = (0, s, "") if isinstance(s, int) else (1, 0, s)
        return (
            self.run,
            self.option,
            scenario,
            self.ts.astimezone(UTC),
            self.seq,
            self.process,
            self.kind,
            self.entity_id,
            self.digest(),
        )

    def __lt__(self, other: Event) -> bool:
        return self.order_key() < other.order_key()

    # ------------------------------------------------------------------ projections
    def to_timeline_line(
        self,
        *,
        tenant: str,
        source: str,
        object_type: str | None = None,
        external_id: str | None = None,
        container: str | None = None,
        acl: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        op: Literal["upsert", "delete"] = "upsert",
        version: int | None = None,
        previous_event_id: str | None = None,
        observe_lag: timedelta = timedelta(0),
        sequence: int | None = None,
    ) -> dict[str, Any]:
        """A timeline line of the corpus contract (§2.1).

        ``payload`` is the provider object (a Jira issue, a Graph message); the default
        is the event envelope, for the simulation provider. The delivery writer fills
        ``sequence``, ``version``, ``previous_event_id`` and ``event_id`` when they are None.
        """
        if not timedelta(0) <= observe_lag <= MAX_OBSERVE_LAG:
            raise ValueError("observe_lag must be in [0, 5 minutes] (§2.1)")
        object_type = object_type or self.process
        external_id = str(external_id or self.entity_id)
        at = (self.ts + observe_lag).isoformat()
        body = None if op == "delete" else (self.envelope() if payload is None else payload)
        line: dict[str, Any] = {
            "event_id": (f"{tenant}:{source}:{object_type}:{external_id}:{version}" if version else None),
            "sequence": sequence,
            "at": at,
            "world_at": self.ts.isoformat(),
            "observed_at": at,
            "source": source,
            "object_type": object_type,
            "external_id": external_id,
            "container": container,
            "op": op,
            "modified_at": self.ts.isoformat(),
            "acl": acl,
            "version": version,
            "previous_event_id": previous_event_id,
            "schema_version": SCHEMA_VERSION,
            "payload": body,
        }
        if op == "upsert":
            line["content_sha256"] = content_sha256(line)
        return line

    def entity_record_id(self) -> str:
        """The Plancy entity id of the event record: ``sim.<run>.<option>.s<scenario>.e<seq>``."""
        return "sim." + ".".join(
            slug(p) for p in (self.run, self.option, f"s{self.scenario}", f"e{self.seq}")
        )

    def to_canonical_record(
        self,
        *,
        source: str = SIM_SOURCE,
        kind: str = "SimEvent",
        title: str | None = None,
        body: str | None = None,
        acl: list[str] | None = None,
        predicates: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """A dict that ``CanonicalRecord.model_validate`` accepts.

        The record is the event itself (a row of an events dataset). It relates to the
        entity it changed, and each declared cause is a ``CausalSpec`` (effect: this
        event; cause: the decision, hypothesis, life event, habit, rule or process).
        """
        pred = {**CAUSE_PREDICATES, **(predicates or {})}
        meta = {
            "process": self.process,
            "event_kind": self.kind,
            "entity": self.entity_id,
            "run": self.run,
            "option": self.option,
            "scenario": self.scenario,
            "seq": self.seq,
        }
        return {
            "key": {"source_id": source, "object_type": "event", "external_id": self.event_id},
            "entity_id": self.entity_record_id(),
            "kind": kind,
            "title": title or f"{self.kind}: {self.entity_id}",
            "fields": {**self.envelope()["fields"], **meta},
            "body": body,
            "valid_from": self.ts.isoformat(),
            "statements": [
                {
                    "predicate": "relates_to",
                    "object": {"source_id": source, "object_type": "entity", "external_id": self.entity_id},
                }
            ],
            "causes": [
                {
                    "predicate": pred[c.type],
                    "cause": {"source_id": source, "object_type": c.type, "external_id": c.id},
                    "valid_from": self.ts.isoformat(),
                }
                for c in self.causes
            ],
            "acl": acl,
            "deleted": False,
        }


def content_sha256(line: dict[str, Any]) -> str:
    """Hash of the normalized content of a line: payload, acl, container, object type and op."""
    keys = ("acl", "container", "object_type", "op", "payload")
    return sha256_hex(canonical_json({k: line.get(k) for k in keys}))


def sort_events(events: Iterable[Event]) -> list[Event]:
    """Events in their total order (``Event.order_key``)."""
    return sorted(events, key=Event.order_key)
