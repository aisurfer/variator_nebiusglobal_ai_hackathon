"""Deterministic entity-and-time cross-layer linking."""

from __future__ import annotations

from datetime import datetime


def link_by_entity_time(
    left: list[dict], right: list[dict], entity_key: str = "entity_id", time_key: str = "world_at"
) -> list[tuple[dict, dict]]:
    """Pair records sharing an entity where right time >= left time."""
    pairs: list[tuple[dict, dict]] = []
    for left_record in left:
        for right_record in right:
            if left_record.get(entity_key) == right_record.get(entity_key):
                lt = left_record.get(time_key)
                rt = right_record.get(time_key)
                if isinstance(lt, datetime) and isinstance(rt, datetime) and rt >= lt:
                    pairs.append((left_record, right_record))
    return pairs


def ranked_links(documents: list[dict], window_seconds: float, top_k: int) -> dict[str, list[str]]:
    """Return deterministic same-entity links inside a symmetric time window."""
    links: dict[str, list[str]] = {}
    for document in documents:
        doc_id = str(document["doc_id"])
        timestamp = document.get("timestamp")
        entities = set(document.get("entities", []))
        candidates: list[tuple[float, str]] = []
        if isinstance(timestamp, datetime):
            for other in documents:
                other_id = str(other["doc_id"])
                other_time = other.get("timestamp")
                if other_id == doc_id or not isinstance(other_time, datetime):
                    continue
                shared = entities.intersection(other.get("entities", []))
                distance = abs((other_time - timestamp).total_seconds())
                if shared and distance <= window_seconds:
                    candidates.append((distance, other_id))
        links[doc_id] = [candidate_id for _, candidate_id in sorted(candidates)[:top_k]]
    return links
