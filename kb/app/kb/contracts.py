"""Contracts between connectors, adapters and the journal (K02).

SourceEnvelope   — what a connector delivers (02 §11.1): identity, operation, revision marker, payload, ACL.
CanonicalRecord  — what an adapter produces from one envelope: one entity state with its declared
                   statements and causal references. One envelope may yield several records (a Confluence
                   meeting page yields the Meeting plus one Decision per decision item); the first record is
                   the primary, the others are derived children of it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CAUSAL_KINDS = (
    "caused_by",
    "decided_because_of",
    "depends_on",
    "based_on",
    "supersedes",
    "precondition",
    "informed_by",
)
CausalKind = Literal[
    "caused_by", "decided_because_of", "depends_on", "based_on", "supersedes", "precondition", "informed_by"
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ref(_Model):
    """A reference to an object: an internal entity id, or an external identity in some source instance."""

    entity_id: str | None = None
    source_id: str | None = None
    object_type: str | None = None
    external_id: str | None = None
    label: str | None = None  # display hint ("FP-101", a page title) for unresolved refs and grounding

    @model_validator(mode="after")
    def _identity(self) -> "Ref":
        if not self.entity_id and not (self.source_id and self.object_type and self.external_id):
            raise ValueError("Ref needs entity_id or (source_id, object_type, external_id)")
        return self

    def external_key(self) -> str | None:
        if self.source_id and self.object_type and self.external_id:
            return f"{self.source_id}/{self.object_type}/{self.external_id}"
        return None


class Evidence(_Model):
    quote: str | None = None  # exact text from the source that supports the statement/link
    locator: dict[str, Any] = Field(default_factory=dict)  # e.g. {"field": "description", "start": 10, "end": 42}


class AliasSpec(_Model):
    alias: str
    kind: Literal["key", "title", "name", "email", "username", "label"] = "label"


class StatementSpec(_Model):
    """A declared non-causal relation or attribute of the record's entity (subject)."""

    predicate: str
    object: Ref | None = None
    literal: Any = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    evidence: Evidence | None = None
    confidence: float = 1.0

    @model_validator(mode="after")
    def _object_or_literal(self) -> "StatementSpec":
        if self.object is None and self.literal is None:
            raise ValueError("StatementSpec needs object or literal")
        return self


class CausalSpec(_Model):
    """A declared causal/dependency reference: this record's entity (effect) <- cause.

    The cause version is bound at save time to the head version of the cause entity (research §3.1),
    unless the source names the version explicitly (cause_version_seq).
    """

    predicate: str
    cause: Ref
    sensitivity: list[str] | None = None  # None -> predicate.typical_sensitivity
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    evidence: Evidence | None = None
    confidence: float = 1.0
    cause_version_seq: int | None = None


class CanonicalRecord(_Model):
    key: Ref  # external identity of the object (source_id, object_type, external_id)
    entity_id: str  # proposed stable id, e.g. "jira.FP-101"; the journal keeps the first mapping forever
    kind: str
    title: str
    url: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    body: str | None = None  # readable text for chunking and search (markdown headings allowed)
    valid_from: datetime | None = None  # business effective time; default = envelope.source_modified_at
    actor: Ref | None = None  # author of this revision (account/person)
    aliases: list[AliasSpec] = Field(default_factory=list)
    statements: list[StatementSpec] = Field(default_factory=list)
    causes: list[CausalSpec] = Field(default_factory=list)
    acl: list[str] | None = None  # None -> container ACL of the envelope
    deleted: bool = False

    @model_validator(mode="after")
    def _key(self) -> "CanonicalRecord":
        if not self.key.external_key():
            raise ValueError("CanonicalRecord.key must be an external identity")
        return self


class SourceEnvelope(_Model):
    tenant_id: str
    source_id: str
    delivery_id: str  # dedupe identity of this delivery
    operation: Literal["upsert", "delete", "acl"] = "upsert"
    object_type: str
    external_id: str
    container: str | None = None  # Jira project key, Confluence space, GitLab project path
    provider_version: str | None = None
    source_modified_at: datetime | None = None
    observed_at: datetime | None = None  # simulation only: becomes tx time when allowed (KB_ALLOW_SIMULATED_TIME)
    payload: dict[str, Any] | None = None
    acl: list[str] | None = None  # object ACL from the connector's ACL sync; None -> container ACL


class IngestResult(_Model):
    delivery_id: str
    status: Literal["processed", "duplicate", "unchanged", "failed"]
    tx_seq: int | None = None
    entities: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    error: str | None = None
    empty: dict[str, int] = Field(default_factory=dict)  # values the journal filled in (ApplyResult.empty)


# Outbox topics. Payloads are thin: consumers re-read the state from PostgreSQL.
TOPIC_ENTITY_VERSION = "entity.version"  # {entity_id, version_id, seq, op, kind, changed_fields}
TOPIC_ENTITY_ACL = "entity.acl"  # {entity_id, acl}
TOPIC_STATEMENTS = "statements.changed"  # {subject_id, opened: [stmt_id], closed: [stmt_id]}
TOPIC_CAUSAL = "causal.changed"  # {effect_entity_id, opened: [link_id], closed: [link_id]}
TOPIC_CHUNKS = "chunks.changed"  # {entity_id, opened: [chunk_id], closed: [chunk_id]}
TOPIC_PREDICATE = "predicate.changed"  # {predicate_id}
