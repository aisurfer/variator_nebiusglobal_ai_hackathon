"""Materialize versioned Plancy events, ACLs, typed gold links, and provider adapters."""

from __future__ import annotations

from collections.abc import Iterable

from variator.corpus.models import PlancyEvent, ProviderAdapterRecord, Tombstone


def provider_adapters(providers: Iterable[str] | None = None) -> tuple[ProviderAdapterRecord, ...]:
    """Return deterministic offline adapter declarations for required providers."""
    selected = (
        tuple(providers)
        if providers is not None
        else (
            "slack",
            "gmail",
            "linear",
            "github",
            "hubspot",
            "fireflies",
            "google_drive",
        )
    )
    return tuple(
        ProviderAdapterRecord(
            provider=provider,
            object_type="record",
            payload_schema_version="provider-payload/1",
            identity_rule="tenant:source:object_type:external_id",
            acl_rule="source-or-container-acl",
            version_rule="source-revision",
            link_rule="explicit-source-reference",
            deletion_rule="emit-tombstone",
        )
        for provider in selected
    )


def make_event(
    *,
    entity_id: str,
    source: str,
    object_type: str,
    external_id: str,
    revision: int,
    sequence: int,
    world_at,
    observed_at,
    payload: dict,
) -> PlancyEvent:
    """Build a Plancy upsert event."""
    return PlancyEvent.upsert(
        entity_id=entity_id,
        source=source,
        object_type=object_type,
        external_id=external_id,
        revision=revision,
        sequence=sequence,
        world_at=world_at,
        observed_at=observed_at,
        payload=payload,
    )


def make_delete_event(
    *,
    entity_id: str,
    source: str,
    object_type: str,
    external_id: str,
    revision: int,
    sequence: int,
    world_at,
    observed_at,
    previous_event_id: str,
    tombstone: Tombstone,
) -> PlancyEvent:
    """Build a versioned tombstone event with retained deletion context."""
    return PlancyEvent.delete(
        entity_id=entity_id,
        source=source,
        object_type=object_type,
        external_id=external_id,
        revision=revision,
        sequence=sequence,
        world_at=world_at,
        observed_at=observed_at,
        previous_event_id=previous_event_id,
        tombstone=tombstone,
    )
