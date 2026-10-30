"""Adapt a HubSpot CRM object payload into a source record."""

from __future__ import annotations


def _identity(event: dict[str, object]) -> tuple[dict[str, str], str]:
    external_id = str(event["external_id"])
    key = {
        "source_id": str(event["source"]),
        "object_type": "crm_object",
        "external_id": external_id,
    }
    return key, f"hubspot.crm_object.{external_id}"


def _deletion_record(event: dict[str, object]) -> list[dict[str, object]]:
    tombstone = event.get("tombstone")
    if not isinstance(tombstone, dict) or not isinstance(tombstone.get("effective_acl"), dict):
        raise ValueError("delete requires tombstone.effective_acl")
    key, entity_id = _identity(event)
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "CRMObject",
            "title": str(event["external_id"]),
            "url": "",
            "fields": {"deletion_reason": tombstone.get("reason")},
            "body": "",
            "valid_from": event["world_at"],
            "actor": None,
            "statements": [],
            "causes": [],
            "acl": tombstone["effective_acl"],
            "deleted": True,
        }
    ]


def adapt(event: dict[str, object]) -> list[dict[str, object]]:
    """Map one HubSpot CRM v3 object without network or database access."""
    if event.get("op") == "delete":
        return _deletion_record(event)
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("HubSpot upsert requires an object payload")
    object_id = str(payload.get("id") or event["external_id"])
    key, entity_id = _identity(event)
    properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "CRMObject",
            "title": str(properties.get("name") or properties.get("subject") or object_id),
            "url": f"https://app.hubspot.com/contacts/redwood/record/{object_id}",
            "fields": properties,
            "body": str(properties.get("description") or ""),
            "valid_from": event["world_at"],
            "actor": None,
            "statements": [],
            "causes": [],
            "acl": event.get("acl"),
            "deleted": bool(payload.get("archived", False)),
        }
    ]
