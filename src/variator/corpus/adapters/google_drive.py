"""Adapt a Google Drive files.list/files.get payload into a file record."""

from __future__ import annotations


def _identity(event: dict[str, object]) -> tuple[dict[str, str], str]:
    external_id = str(event["external_id"])
    key = {"source_id": str(event["source"]), "object_type": "file", "external_id": external_id}
    return key, f"google_drive.file.{external_id}"


def _deletion_record(event: dict[str, object]) -> list[dict[str, object]]:
    tombstone = event.get("tombstone")
    if not isinstance(tombstone, dict) or not isinstance(tombstone.get("effective_acl"), dict):
        raise ValueError("delete requires tombstone.effective_acl")
    key, entity_id = _identity(event)
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "Document",
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
    """Map one Google Drive file without network or database access."""
    if event.get("op") == "delete":
        return _deletion_record(event)
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Google Drive upsert requires an object payload")
    file_id = str(payload.get("id") or event["external_id"])
    key, entity_id = _identity(event)
    return [
        {
            "key": key,
            "entity_id": entity_id,
            "kind": "Document",
            "title": str(payload.get("name") or file_id),
            "url": str(payload.get("webViewLink") or f"https://drive.google.com/open?id={file_id}"),
            "fields": {"mime_type": payload.get("mimeType"), "parents": payload.get("parents", [])},
            "body": str(payload.get("text") or payload.get("description") or ""),
            "valid_from": event["world_at"],
            "actor": None,
            "statements": [],
            "causes": [],
            "acl": event.get("acl"),
            "deleted": False,
        }
    ]
