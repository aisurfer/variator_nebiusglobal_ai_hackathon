"""Validate canonical event hashes, immutable IDs, aliases, and manifest provenance chains."""

from __future__ import annotations

from variator.corpus.canonical import immutable_id, sha256_hex


def check_event_hash(payload: dict, expected: str) -> bool:
    """Compare a payload's canonical hash to an expected value."""
    return sha256_hex(payload) == expected


def check_identity(tenant: str, source: str, object_type: str, external_id: str, value: str) -> bool:
    """Verify an immutable identity string."""
    return immutable_id(tenant, source, object_type, external_id) == value


def validate_unique_aliases(aliases: dict[str, str]) -> bool:
    """Ensure each display alias resolves to one immutable entity."""
    if not all(alias.strip() and entity.strip() and ":" in entity for alias, entity in aliases.items()):
        return False
    by_alias = {alias.casefold(): entity for alias, entity in aliases.items()}
    return len(by_alias) == len(aliases) and len(set(by_alias.values())) <= len(by_alias)


def validate_provenance_chain(records: list[dict]) -> bool:
    """Require each emitted record to name a source and transformation activity."""
    return all(
        isinstance(record.get("source_record"), str)
        and record["source_record"]
        and isinstance(record.get("transformation"), str)
        and record["transformation"]
        for record in records
    )
