"""Adapt a Gmail message payload into a provider-neutral mail record."""

from __future__ import annotations


def _identity(event: dict[str, object]) -> tuple[dict[str, str], str]:
    external_id = str(event["external_id"])
    key = {"source_id": str(event["source"]), "object_type": "message", "external_id": external_id}
    return key, f"gmail.message.{external_id}"


def _deletion_record(event: dict[str, object]) -> list[dict[str, object]]:
    tombstone = event.get("tombstone")
    if not isinstance(tombstone, dict) or not isinstance(tombstone.get("effective_acl"), dict):
        raise ValueError("delete requires tombstone.effective_acl")
    key, entity_id = _identity(event)
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "Email",
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
    """Map one Gmail messages.get payload without network or database access."""
    if event.get("op") == "delete":
        return _deletion_record(event)
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Gmail upsert requires an object payload")
    message_id = str(payload.get("id") or event["external_id"])
    key, entity_id = _identity(event)
    headers = (
        payload.get("payload", {}).get("headers", []) if isinstance(payload.get("payload"), dict) else []
    )
    header_map = {
        str(item.get("name", "")).lower(): item.get("value", "") for item in headers if isinstance(item, dict)
    }
    body = payload.get("body") or payload.get("snippet") or ""
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "Email",
            "title": str(header_map.get("subject") or message_id),
            "url": f"https://mail.google.com/mail/u/0/#all/{message_id}",
            "fields": {"thread_id": payload.get("threadId"), "label_ids": payload.get("labelIds", [])},
            "body": str(body),
            "valid_from": event["world_at"],
            "actor": {
                "source_id": event["source"],
                "object_type": "account",
                "external_id": header_map.get("from"),
            },
            "statements": [],
            "causes": [],
            "acl": event.get("acl"),
            "deleted": False,
        }
    ]
