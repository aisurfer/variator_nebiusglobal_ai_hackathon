"""Contract tests for canonical corpus and Plancy projection models."""

from datetime import UTC, datetime, timedelta

import pytest

from variator.corpus.canonical import sha256_hex
from variator.corpus.models import (
    Acl,
    AclRule,
    CorpusDocument,
    FactorObservation,
    Membership,
    PlancyEvent,
    Tombstone,
)
from variator.corpus.plancy_events import make_delete_event, make_event, provider_adapters
from variator.corpus.validation import (
    check_event_hash,
    check_identity,
    validate_provenance_chain,
    validate_unique_aliases,
)

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def test_document_requires_factor_evidence_to_reference_its_own_document():
    """Evidence indexes cannot point to a different canonical document."""
    observation = FactorObservation(
        factor_id="financial.revenue",
        entity_id="redwood:hubspot:deal:deal-1",
        entity_revision=1,
        role="outcome",
        source_doc_id="doc-1",
        extraction_rule_id="hubspot.amount.v1",
        confidence="direct",
    )
    document = CorpusDocument(
        doc_id="doc-1",
        layer="internal",
        source_type="hubspot_deal",
        timestamp=NOW,
        author=None,
        content="Annual contract is worth 120000 USD.",
        entities=["redwood:hubspot:deal:deal-1"],
        factors=[observation],
        provenance={"source": "enterprise_rag", "locator": "hubspot/doc-1"},
    )

    assert document.factors[0].source_doc_id == document.doc_id

    with pytest.raises(ValueError, match="source_doc_id"):
        CorpusDocument(
            **document.model_dump(exclude={"factors"}),
            factors=[observation.model_copy(update={"source_doc_id": "doc-2"})],
        )


def test_plancy_revision_and_tombstone_preserve_replay_contract():
    """A revision chain has immutable event identity and deletion context."""
    entity_id = "redwood:jira:issue:ENG-241"
    created = PlancyEvent.upsert(
        entity_id=entity_id,
        source="jira",
        object_type="issue",
        external_id="ENG-241",
        revision=1,
        sequence=1,
        world_at=NOW,
        observed_at=NOW,
        payload={"key": "ENG-241", "fields": {"status": "To Do"}},
    )
    deleted = PlancyEvent.delete(
        entity_id=entity_id,
        source="jira",
        object_type="issue",
        external_id="ENG-241",
        revision=2,
        sequence=2,
        world_at=NOW + timedelta(days=1),
        observed_at=NOW + timedelta(days=1),
        previous_event_id=created.event_id,
        tombstone=Tombstone(
            entity_id=entity_id,
            revision=2,
            deleted_at=NOW + timedelta(days=1),
            effective_acl=Acl(),
            reason="source deletion",
        ),
    )

    assert created.event_id == f"{entity_id}:1"
    assert deleted.previous_event_id == created.event_id
    assert deleted.payload is None
    assert deleted.tombstone.entity_id == entity_id


def test_acl_is_default_deny_and_uses_membership_validity_interval():
    """Access is evaluated at known_at rather than current group membership."""
    acl = Acl(
        allow=[AclRule(principal="group:engineering")],
        deny=[AclRule(principal="person:alex")],
    )
    membership = Membership(
        principal="person:maya",
        group="engineering",
        valid_from=NOW - timedelta(days=1),
        valid_to=NOW + timedelta(days=1),
    )

    assert acl.allows("person:maya", NOW, [membership])
    assert not acl.allows("person:alex", NOW, [])
    assert not acl.allows("person:maya", NOW + timedelta(days=2), [membership])


def test_plancy_materializers_require_full_upserts_and_tombstones():
    entity_id = "redwood:jira:issue:ENG-242"
    created = make_event(
        entity_id=entity_id,
        source="jira",
        object_type="issue",
        external_id="ENG-242",
        revision=1,
        sequence=1,
        world_at=NOW,
        observed_at=NOW,
        payload={"key": "ENG-242"},
    )
    deleted = make_delete_event(
        entity_id=entity_id,
        source="jira",
        object_type="issue",
        external_id="ENG-242",
        revision=2,
        sequence=2,
        world_at=NOW,
        observed_at=NOW,
        previous_event_id=created.event_id,
        tombstone=Tombstone(entity_id=entity_id, revision=2, deleted_at=NOW, effective_acl=Acl()),
    )
    assert deleted.tombstone.entity_id == entity_id


def test_required_provider_adapters_are_declared():
    assert {adapter.provider for adapter in provider_adapters()} == {
        "slack",
        "gmail",
        "linear",
        "github",
        "hubspot",
        "fireflies",
        "google_drive",
    }


def test_identity_and_rfc8785_event_hash_validation():
    assert check_identity("redwood", "jira", "issue", "ENG-1", "redwood:jira:issue:ENG-1")
    assert check_event_hash({"b": 2, "a": 1}, sha256_hex({"a": 1, "b": 2}))
    assert validate_unique_aliases({"ENG-1": "redwood:jira:issue:ENG-1"})
    assert validate_provenance_chain([{"source_record": "jira/ENG-1", "transformation": "adapter.v1"}])
    assert not validate_provenance_chain([{"source_record": "jira/ENG-1"}])
