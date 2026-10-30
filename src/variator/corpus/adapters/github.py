"""Adapt a GitHub pull request or issue payload into a source record."""

from __future__ import annotations


def _identity(event: dict[str, object]) -> tuple[dict[str, str], str]:
    external_id = str(event["external_id"])
    key = {"source_id": str(event["source"]), "object_type": "record", "external_id": external_id}
    return key, f"github.record.{external_id}"


def _deletion_record(event: dict[str, object]) -> list[dict[str, object]]:
    tombstone = event.get("tombstone")
    if not isinstance(tombstone, dict) or not isinstance(tombstone.get("effective_acl"), dict):
        raise ValueError("delete requires tombstone.effective_acl")
    key, entity_id = _identity(event)
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "SourceRecord",
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
    """Map one GitHub REST API object without network or database access."""
    if event.get("op") == "delete":
        return _deletion_record(event)
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("GitHub upsert requires an object payload")
    object_id = str(payload.get("id") or event["external_id"])
    key, entity_id = _identity(event)
    kind = "MergeRequest" if "pull_request" in payload or "merged" in payload else "Issue"
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": kind,
            "title": str(payload.get("title") or object_id),
            "url": str(payload.get("html_url") or f"https://github.com/redwood/inference/issues/{object_id}"),
            "fields": {"state": payload.get("state"), "number": payload.get("number")},
            "body": str(payload.get("body") or ""),
            "valid_from": event["world_at"],
            "actor": None,
            "statements": [],
            "causes": [],
            "acl": event.get("acl"),
            "deleted": False,
        }
    ]
