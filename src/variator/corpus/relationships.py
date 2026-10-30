"""Materialize evidence-backed factor relationships."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from variator.corpus.canonical import sha256_hex


def materialize_relationships(
    observations: list[dict[str, Any]], rules: dict[tuple[str, str], tuple[str, str]]
) -> list[dict[str, Any]]:
    """Return only relationships explicitly allowed by deterministic rules."""
    rels: list[dict[str, Any]] = []
    for i, a in enumerate(observations):
        for b in observations[i + 1 :]:
            if a.get("entity_id") == b.get("entity_id"):
                rule = rules.get((str(a.get("factor_id")), str(b.get("factor_id"))))
                source_observation, target_observation = a, b
                if rule is None:
                    rule = rules.get((str(b.get("factor_id")), str(a.get("factor_id"))))
                    source_observation, target_observation = b, a
                if rule is not None:
                    predicate, rule_id = rule
                    world_at = (
                        source_observation.get("world_at")
                        or target_observation.get("world_at")
                        or datetime(1970, 1, 1, tzinfo=UTC)
                    )
                    identity = {
                        "a": source_observation.get("factor_id"),
                        "b": target_observation.get("factor_id"),
                        "entity": source_observation.get("entity_id"),
                        "rule": rule_id,
                    }
                    rels.append(
                        {
                            "relationship_id": f"relationship:{sha256_hex(identity)[:16]}",
                            "source": source_observation.get("factor_id"),
                            "target": target_observation.get("factor_id"),
                            "source_factor_id": source_observation.get("factor_id"),
                            "target_factor_id": target_observation.get("factor_id"),
                            "entity_id": source_observation.get("entity_id"),
                            "evidence_entity_ids": [str(source_observation.get("entity_id"))],
                            "evidence_revisions": [int(source_observation.get("entity_revision", 1))],
                            "predicate": predicate,
                            "rule_id": rule_id,
                            "world_at": world_at,
                            "lag_seconds": None,
                            "confidence": source_observation.get("confidence", "derived"),
                        }
                    )
    return rels
