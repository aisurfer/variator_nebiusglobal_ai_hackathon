"""Canonical values used by deterministic corpus artifacts."""

from __future__ import annotations

import hashlib
from typing import Any

import rfc8785


def canonical_json(value: Any) -> bytes:
    """Return RFC 8785 canonical JSON bytes for JSON-compatible data."""
    return rfc8785.dumps(value)


def sha256_hex(value: Any) -> str:
    """Return the SHA-256 digest of canonical JSON bytes."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def immutable_id(tenant: str, source: str, object_type: str, external_id: str) -> str:
    """Build the globally unique object identity required by the Plancy contract."""
    parts = (tenant, source, object_type, external_id)
    if any(not part.strip() for part in parts):
        raise ValueError("tenant, source, object_type, and external_id must be non-empty")
    if any(":" in part for part in parts):
        raise ValueError("identity components must not contain ':'")
    return ":".join(parts)
