"""Adapt a Linear issue payload into a provider-neutral work item."""

from __future__ import annotations


def _identity(event: dict[str, object]) -> tuple[dict[str, str], str]:
    external_id = str(event["external_id"])
    key = {"source_id": str(event["source"]), "object_type": "issue", "external_id": external_id}
    return key, f"linear.issue.{external_id}"


def _deletion_record(event: dict[str, object]) -> list[dict[str, object]]:
    tombstone = event.get("tombstone")
    if not isinstance(tombstone, dict) or not isinstance(tombstone.get("effective_acl"), dict):
        raise ValueError("delete requires tombstone.effective_acl")
    key, entity_id = _identity(event)
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "Task",
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
    """Map one Linear issue without network or database access."""
    if event.get("op") == "delete":
        return _deletion_record(event)
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Linear upsert requires an object payload")
    issue_id = str(payload.get("id") or event["external_id"])
    key, entity_id = _identity(event)
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "Task",
            "title": str(payload.get("title") or issue_id),
            "url": str(payload.get("url") or f"https://linear.app/redwood/issue/{issue_id}"),
            "fields": {
                "status": payload.get("status"),
                "team": payload.get("team"),
                "priority": payload.get("priority"),
            },
            "body": str(payload.get("description") or ""),
            "valid_from": event["world_at"],
            "actor": None,
            "statements": [],
            "causes": [],
            "acl": event.get("acl"),
            "deleted": False,
        }
    ]
