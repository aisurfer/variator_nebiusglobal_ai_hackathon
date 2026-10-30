"""Adapt a Slack conversations.history payload into a canonical Plancy record."""

from __future__ import annotations


def _identity(event: dict[str, object]) -> tuple[dict[str, str], str]:
    external_id = str(event["external_id"])
    key = {"source_id": str(event["source"]), "object_type": "message", "external_id": external_id}
    return key, f"slack.message.{external_id}"


def _deletion_record(event: dict[str, object]) -> list[dict[str, object]]:
    tombstone = event.get("tombstone")
    if not isinstance(tombstone, dict) or not isinstance(tombstone.get("effective_acl"), dict):
        raise ValueError("delete requires tombstone.effective_acl")
    key, entity_id = _identity(event)
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "ChatMessage",
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
    """Map one Slack message event without network or database access."""
    if event.get("op") == "delete":
        return _deletion_record(event)
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Slack upsert requires an object payload")
    channel = str(payload["channel"])
    timestamp = str(payload["ts"])
    key, entity_id = _identity(event)
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "ChatMessage",
            "title": f"#{channel}: {str(payload.get('text', ''))[:80]}",
            "url": f"https://redwood.slack.com/archives/{channel}/p{timestamp.replace('.', '')}",
            "fields": {"channel": channel, "thread_ts": payload.get("thread_ts")},
            "body": str(payload.get("text", "")),
            "valid_from": event["world_at"],
            "actor": {
                "source_id": event["source"],
                "object_type": "account",
                "external_id": payload.get("user"),
            },
            "statements": [
                {
                    "predicate": "author",
                    "object": {
                        "source_id": event["source"],
                        "object_type": "account",
                        "external_id": payload.get("user"),
                    },
                }
            ],
            "causes": [],
            "acl": event.get("acl"),
            "deleted": False,
        }
    ]
