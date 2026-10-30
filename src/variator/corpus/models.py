"""Canonical data contracts for the deterministic Redwood corpus."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from variator.corpus.canonical import immutable_id, sha256_hex

SchemaVersion = Literal["corpus-document/1", "plancy-event/2"]
Layer = Literal["internal", "chain", "external"]
FactorRole = Literal["outcome", "driver", "constraint", "risk", "lever"]
FactorConfidence = Literal["direct", "derived"]
Operation = Literal["upsert", "delete"]


class FactorObservation(BaseModel):
    """A deterministic factor measurement backed by one canonical document."""

    model_config = ConfigDict(extra="forbid")

    factor_id: str
    entity_id: str
    entity_revision: int = Field(ge=1)
    role: FactorRole
    value: float | None = None
    unit: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    polarity: Literal["positive", "negative", "neutral"] | None = None
    confidence: FactorConfidence
    source_doc_id: str
    source_text_start: int | None = Field(default=None, ge=0)
    source_text_end: int | None = Field(default=None, ge=0)
    extraction_rule_id: str

    @model_validator(mode="after")
    def validate_range(self) -> FactorObservation:
        """Require a well-formed optional source text span."""
        if (self.source_text_start is None) != (self.source_text_end is None):
            raise ValueError("source_text_start and source_text_end must be set together")
        if (
            self.source_text_start is not None
            and self.source_text_end is not None
            and self.source_text_end < self.source_text_start
        ):
            raise ValueError("source_text_end must not precede source_text_start")
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        return self


class CorpusDocument(BaseModel):
    """One source-normalized document in the shared Redwood world."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    layer: Layer
    source_type: str
    timestamp: datetime | None
    author: str | None
    content: str
    entities: list[str] = Field(default_factory=list)
    factors: list[FactorObservation] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    chain_refs: list[dict[str, str]] = Field(default_factory=list)
    provenance: dict[str, Any]
    schema_version: Literal["corpus-document/1"] = "corpus-document/1"

    @model_validator(mode="after")
    def validate_factor_provenance(self) -> CorpusDocument:
        """Keep flattened factor evidence attached to its owning document."""
        for observation in self.factors:
            if observation.source_doc_id != self.doc_id:
                raise ValueError("factor source_doc_id must equal document doc_id")
        if len(set(self.entities)) != len(self.entities):
            raise ValueError("entities must not contain duplicates")
        return self


class AclRule(BaseModel):
    """One direct ACL rule for a person or group principal."""

    model_config = ConfigDict(extra="forbid")

    principal: str


class Membership(BaseModel):
    """Time-bounded group membership used at knowledge time."""

    model_config = ConfigDict(extra="forbid")

    principal: str
    group: str
    valid_from: datetime
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def validate_window(self) -> Membership:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        return self

    def is_active_at(self, known_at: datetime) -> bool:
        """Return whether this membership was valid at a requested knowledge time."""
        return self.valid_from <= known_at and (self.valid_to is None or known_at <= self.valid_to)


class Acl(BaseModel):
    """Default-deny ACL with explicit allow and deny rules."""

    model_config = ConfigDict(extra="forbid")

    allow: list[AclRule] = Field(default_factory=list)
    deny: list[AclRule] = Field(default_factory=list)
    inherit: bool = False

    def allows(
        self,
        principal: str,
        known_at: datetime,
        memberships: list[Membership],
        inherited: Acl | None = None,
    ) -> bool:
        """Evaluate direct, time-valid group, then inherited access with deny precedence."""
        principals = {principal}
        for membership in memberships:
            if membership.principal != principal or not membership.is_active_at(known_at):
                continue
            group = membership.group
            principals.update(
                {
                    group,
                    f"group:{group}",
                    f"group:{group.removeprefix('grp:')}",
                    f"grp:{group.removeprefix('group:')}",
                }
            )
        if any(rule.principal in principals for rule in self.deny):
            return False
        if any(rule.principal in principals for rule in self.allow):
            return True
        return self.inherit and inherited is not None and inherited.allows(principal, known_at, memberships)


class DocumentMeta(BaseModel):
    """Provider-neutral metadata carried by every materialized attachment."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    attachment_id: str = Field(alias="attachmentId")
    filename: str
    mime_type: str = Field(alias="mimeType")
    source_document: str = Field(alias="sourceDocument")
    source_document_id: str = Field(alias="sourceDocumentId")
    checksum: str
    parent_event_id: str = Field(alias="parentEventId")
    parent_external_id: str = Field(alias="parentExternalId")


class NorthStarReport(BaseModel):
    """Structured weekly North Star report embedded in a Confluence payload."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    report_period: dict[str, str] = Field(alias="reportPeriod")
    owner: dict[str, str]
    source_records: list[dict[str, Any]] = Field(alias="sourceRecords")
    metrics: list[dict[str, Any]]
    financials: dict[str, float]
    reconciliation: dict[str, Any]


class MeetingProtocol(BaseModel):
    """Readable structured protocol attached to a meeting transcript."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    attendees: list[dict[str, Any]]
    agenda: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    owners: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    timestamps: dict[str, str]


class Tombstone(BaseModel):
    """Deletion record sufficient to remove all derived index artifacts."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    revision: int = Field(ge=1)
    deleted_at: datetime
    effective_acl: Acl
    reason: str | None = None


class PlancyEvent(BaseModel):
    """Immutable full-snapshot source event for the Plancy delivery timeline."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    entity_id: str
    sequence: int = Field(ge=1)
    source: str
    object_type: str
    external_id: str
    op: Operation
    revision: int = Field(ge=1)
    previous_event_id: str | None = None
    content_sha256: str | None = None
    schema_version: Literal["plancy-event/2"] = "plancy-event/2"
    original_occurred_at: datetime | None = None
    world_at: datetime
    observed_at: datetime
    at: datetime | None = None
    modified_at: datetime | None = None
    version: int | str | None = None
    delivery_id: str | None = None
    time_transform_id: str | None = None
    container: str | None = None
    acl: Acl | None = None
    aliases: list[str] = Field(default_factory=list)
    payload: dict[str, Any] | None
    tombstone: Tombstone | None = None

    @field_serializer("acl")
    def serialize_acl(self, acl: Acl | None) -> list[str] | None:
        """Emit the Plancy contract representation while retaining rich ACLs internally."""
        if acl is None:
            return None
        return [rule.principal for rule in acl.allow]

    @model_validator(mode="after")
    def validate_event(self) -> PlancyEvent:
        """Enforce immutable identity, revision chain, and deletion semantics."""
        expected_entity_id = immutable_id(
            self.entity_id.split(":", maxsplit=1)[0],
            self.source,
            self.object_type,
            self.external_id,
        )
        if self.entity_id != expected_entity_id:
            raise ValueError("entity_id must use tenant:source:object_type:external_id")
        if self.event_id != f"{self.entity_id}:{self.revision}":
            raise ValueError("event_id must equal entity_id plus revision")
        if self.revision == 1 and self.previous_event_id is not None:
            raise ValueError("first revision must not reference a previous event")
        if self.revision > 1 and self.previous_event_id is None:
            raise ValueError("later revisions require previous_event_id")
        if self.observed_at < self.world_at:
            raise ValueError("observed_at must not precede world_at")
        if self.at is not None and self.at != self.observed_at:
            raise ValueError("at must equal observed_at in schema version 2")
        if self.modified_at is not None and self.modified_at != self.world_at:
            raise ValueError("modified_at must equal world_at in normalized events")
        if self.op == "upsert":
            if self.payload is None or self.tombstone is not None:
                raise ValueError("upsert requires payload and forbids tombstone")
            if self.content_sha256 is None:
                raise ValueError("upsert requires content_sha256")
        else:
            if self.payload is not None or self.tombstone is None:
                raise ValueError("delete requires tombstone and null payload")
            if self.tombstone.entity_id != self.entity_id:
                raise ValueError("tombstone entity_id must equal event entity_id")
            if self.tombstone.revision != self.revision:
                raise ValueError("tombstone revision must equal event revision")
            if self.content_sha256 is not None:
                raise ValueError("delete must not contain content_sha256")
        return self

    @classmethod
    def upsert(
        cls,
        *,
        entity_id: str,
        source: str,
        object_type: str,
        external_id: str,
        revision: int,
        sequence: int,
        world_at: datetime,
        observed_at: datetime,
        payload: dict[str, Any],
        previous_event_id: str | None = None,
        original_occurred_at: datetime | None = None,
        time_transform_id: str | None = None,
        container: str | None = None,
        acl: Acl | None = None,
        aliases: list[str] | None = None,
    ) -> PlancyEvent:
        """Build a full-state upsert with canonical normalized-content hashing."""
        normalized_content = {
            "entity_id": entity_id,
            "source": source,
            "object_type": object_type,
            "external_id": external_id,
            "container": container,
            "acl": acl.model_dump(mode="json") if acl else None,
            "payload": payload,
        }
        return cls(
            event_id=f"{entity_id}:{revision}",
            entity_id=entity_id,
            sequence=sequence,
            source=source,
            object_type=object_type,
            external_id=external_id,
            op="upsert",
            revision=revision,
            previous_event_id=previous_event_id,
            content_sha256=sha256_hex(normalized_content),
            original_occurred_at=original_occurred_at,
            world_at=world_at,
            observed_at=observed_at,
            at=observed_at,
            modified_at=world_at,
            version=revision,
            time_transform_id=time_transform_id,
            container=container,
            acl=acl,
            aliases=aliases or [],
            payload=payload,
        )

    @classmethod
    def delete(
        cls,
        *,
        entity_id: str,
        source: str,
        object_type: str,
        external_id: str,
        revision: int,
        sequence: int,
        world_at: datetime,
        observed_at: datetime,
        previous_event_id: str,
        tombstone: Tombstone,
        original_occurred_at: datetime | None = None,
        time_transform_id: str | None = None,
        container: str | None = None,
        aliases: list[str] | None = None,
    ) -> PlancyEvent:
        """Build a tombstone event retaining deletion-time ACL context."""
        return cls(
            event_id=f"{entity_id}:{revision}",
            entity_id=entity_id,
            sequence=sequence,
            source=source,
            object_type=object_type,
            external_id=external_id,
            op="delete",
            revision=revision,
            previous_event_id=previous_event_id,
            original_occurred_at=original_occurred_at,
            world_at=world_at,
            observed_at=observed_at,
            at=observed_at,
            modified_at=world_at,
            version=revision,
            time_transform_id=time_transform_id,
            container=container,
            aliases=aliases or [],
            payload=None,
            tombstone=tombstone,
        )


class ChainTable(BaseModel):
    """A normalized operational-chain table and its primary-key column."""

    model_config = ConfigDict(extra="forbid")

    name: str
    primary_key: str
    rows: list[dict[str, Any]] = Field(default_factory=list)


class CorpusManifest(BaseModel):
    """Provenance and quality metadata for one deterministic assembly run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "corpus-manifest/1"
    seed: int
    generator_version: str
    config_hash: str
    run_id: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    files: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    leakage: dict[str, Any] = Field(default_factory=dict)
    time_transforms: list[dict[str, Any]] = Field(default_factory=list)
    redaction_rules: list[str] = Field(default_factory=list)


class EntityMap(BaseModel):
    """Closed local audit map for reversible external entity substitution."""

    model_config = ConfigDict(extra="forbid")

    seed: int
    rules_version: str
    replacements: dict[str, str] = Field(default_factory=dict)


class ExternalObjectIdentity(BaseModel):
    """Stable provider-native object key and its immutable tenant identity."""

    model_config = ConfigDict(extra="forbid")

    tenant: str
    source: str
    object_type: str
    external_id: str

    @property
    def entity_id(self) -> str:
        """Return the normative immutable object identity."""
        return immutable_id(self.tenant, self.source, self.object_type, self.external_id)


class DirectoryEntry(BaseModel):
    """A person, organization, group, product, or account in the common directory."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    kind: Literal[
        "person",
        "organization",
        "group",
        "product",
        "repository",
        "project",
        "space",
        "channel",
        "account",
    ]
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    accounts: list[ExternalObjectIdentity] = Field(default_factory=list)
    memberships: list[Membership] = Field(default_factory=list)


class ProviderAdapterRecord(BaseModel):
    """Offline adapter declaration for a provider-specific payload mapping."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    object_type: str
    payload_schema_version: str
    identity_rule: str
    acl_rule: str
    version_rule: str
    link_rule: str
    deletion_rule: str


SUPPORTED_ADAPTER_PROVIDERS = {"slack", "gmail", "linear", "github", "hubspot", "fireflies", "google_drive"}


class GoldLink(BaseModel):
    """Evidence-backed typed relationship delivered to Plancy."""

    model_config = ConfigDict(extra="forbid")

    link_id: str
    predicate: Literal[
        "part_of",
        "blocked_by",
        "caused_by",
        "implements",
        "informed_by",
        "closes",
        "decided_in",
        "supersedes",
        "replies_to",
    ]
    source_entity_id: str
    target_entity_id: str
    evidence_entity_ids: list[str] = Field(min_length=1)
    explicit: bool
    expressed_as: Literal[
        "remote_link",
        "issue_link",
        "parent",
        "page_property",
        "decision_item",
        "mr_text",
        "reply",
        "text",
    ]
    evidence_quote: str | None = None
    rule_id: str | None = None


class EvaluationCase(BaseModel):
    """One deterministic, access-aware Plancy evaluation case."""

    model_config = ConfigDict(extra="forbid")

    id: str
    split: Literal["dev", "holdout"]
    category: Literal[
        "lookup",
        "why",
        "history",
        "as_of",
        "conflict",
        "stale",
        "impact",
        "multi_hop",
        "acl",
        "negative",
        "implicit_ref",
        "numeric",
        "people",
    ]
    difficulty: Literal["trivial", "tricky"]
    language: Literal["en", "ru"]
    question: str
    principal: str
    known_at: datetime | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    roots: list[dict[str, Any]] = Field(default_factory=list)
    forbidden: list[dict[str, Any]] = Field(default_factory=list)
    answer_facts: list[dict[str, Any]] = Field(default_factory=list)
    acl_scenario: str | None = None
    acl_context: list[dict[str, Any]] = Field(default_factory=list)
    expect_not_found: bool = False
    rationale: str


class FactorRelationship(BaseModel):
    """A deterministic causal relationship between supported observations."""

    model_config = ConfigDict(extra="forbid")

    relationship_id: str
    predicate: Literal["causes", "increases", "decreases", "blocks", "constrains"]
    source_factor_id: str
    target_factor_id: str
    evidence_entity_ids: list[str] = Field(min_length=1)
    evidence_revisions: list[int] = Field(min_length=1)
    rule_id: str
    world_at: datetime
    lag_seconds: int | None = Field(default=None, ge=0)
    confidence: FactorConfidence
