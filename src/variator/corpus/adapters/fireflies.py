"""Adapt a Fireflies transcript payload into a meeting record."""

from __future__ import annotations


def _identity(event: dict[str, object]) -> tuple[dict[str, str], str]:
    external_id = str(event["external_id"])
    key = {
        "source_id": str(event["source"]),
        "object_type": "transcript",
        "external_id": external_id,
    }
    return key, f"fireflies.transcript.{external_id}"


def _deletion_record(event: dict[str, object]) -> list[dict[str, object]]:
    tombstone = event.get("tombstone")
    if not isinstance(tombstone, dict) or not isinstance(tombstone.get("effective_acl"), dict):
        raise ValueError("delete requires tombstone.effective_acl")
    key, entity_id = _identity(event)
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "Meeting",
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
    """Map one Fireflies transcript without network or database access."""
    if event.get("op") == "delete":
        return _deletion_record(event)
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Fireflies upsert requires an object payload")
    transcript_id = str(payload.get("id") or event["external_id"])
    key, entity_id = _identity(event)
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "Meeting",
            "title": str(payload.get("title") or transcript_id),
            "url": str(payload.get("url") or f"https://app.fireflies.ai/view/{transcript_id}"),
            "fields": {"participants": payload.get("participants", []), "duration": payload.get("duration")},
            "body": str(payload.get("transcript") or payload.get("summary") or ""),
            "valid_from": event["world_at"],
            "actor": None,
            "statements": [],
            "causes": [],
            "acl": event.get("acl"),
            "deleted": False,
        }
    ]
