"""Search REST (K08/K09): /search, /search/versions, /ground, /admin/consistency/search.

Every response carries the projection watermarks and `stale_projection`: true when the journal holds changes
known at the requested time (known_at, default now) that the projector has not indexed yet.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, HTTPException, Query

from kb.api.deps import Conn, KnownAt, Principal, parse_time
from kb.search import service
from kb.search.descriptors import DescriptorBuilder
from kb.search.projector import SearchProjector
from kb.worker.framework import watermarks

router = APIRouter(prefix="/api/v1")


def _stale(conn: psycopg.Connection, tenant_id: str, consumer: str, topics: frozenset[str], wm: dict[str, int],
           at: datetime | None) -> bool:
    """Is there an outbox event for this consumer, recorded by `at` (default: ever), beyond the consumer's watermark?"""
    row = conn.execute(
        """SELECT EXISTS (
             SELECT 1 FROM outbox o JOIN tx_log t ON t.tenant_id = o.tenant_id AND t.tx_seq = o.tx_seq
             WHERE o.tenant_id = %s AND o.tx_seq > %s AND o.topic = ANY(%s)
               AND (%s::timestamptz IS NULL OR t.tx_at <= %s)) AS stale""",
        (tenant_id, wm.get(consumer, 0), sorted(topics), at, at),
    ).fetchone()
    return bool(row["stale"])


def _meta(conn: psycopg.Connection, tenant_id: str, consumer: str, topics: frozenset[str], at: datetime | None,
          t0: float) -> dict[str, Any]:
    wm = watermarks(conn, tenant_id)
    return {"watermarks": wm, "stale_projection": _stale(conn, tenant_id, consumer, topics, wm, at),
            "took_ms": round((time.perf_counter() - t0) * 1000, 1)}


@router.get("/search", tags=["search"])
def search(
    conn: Conn, p: Principal,
    q: Annotated[str, Query(min_length=1, max_length=1000)],
    kinds: Annotated[list[str] | None, Query()] = None,
    mode: Annotated[str, Query(pattern="^(hybrid|lexical|vector)$")] = "hybrid",
    valid_at: str | None = None, known_at: KnownAt = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    per_entity: Annotated[int, Query(ge=1, le=20)] = 1,
) -> dict[str, Any]:
    """Hybrid (BM25 + kNN, RRF) search over chunks in the (valid_at, known_at) slice."""
    t0 = time.perf_counter()
    va, ka = parse_time(valid_at, "valid_at"), known_at
    hits = service.search(conn, p, q, kinds=kinds, mode=mode, valid_at=va, known_at=ka, limit=limit,  # type: ignore[arg-type]
                          per_entity=per_entity)
    return {"query": {"q": q, "kinds": kinds, "mode": mode, "valid_at": va, "known_at": ka, "limit": limit,
                      "per_entity": per_entity},
            "hits": hits, **_meta(conn, p.tenant_id, SearchProjector.name, SearchProjector.topics, ka, t0)}


@router.get("/search/versions", tags=["search"])
def search_versions(
    conn: Conn, p: Principal,
    q: Annotated[str, Query(min_length=1, max_length=1000)],
    start: str | None = None, end: str | None = None,
    kinds: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20, known_at: KnownAt = None,
) -> dict[str, Any]:
    """Hybrid search over versions recorded in [start, end) (transaction axis), as known at `known_at`."""
    t0 = time.perf_counter()
    s, e = parse_time(start, "start"), parse_time(end, "end")
    hits = service.search_versions(conn, p, q, start=s, end=e, kinds=kinds, limit=limit, known_at=known_at)
    return {"query": {"q": q, "start": s, "end": e, "kinds": kinds, "limit": limit, "known_at": known_at},
            "hits": hits, **_meta(conn, p.tenant_id, SearchProjector.name, SearchProjector.topics, known_at or e, t0)}


@router.get("/ground", tags=["search"])
def ground(
    conn: Conn, p: Principal,
    text: Annotated[str, Query(min_length=1, max_length=500)],
    kind: Annotated[str | None, Query(pattern="^(entity|predicate)$")] = None,
    entity_kinds: Annotated[list[str] | None, Query()] = None,
    known_at: KnownAt = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 5,
) -> dict[str, Any]:
    """Ground a mention to entity/predicate candidates (descriptor index). `ambiguous` flags a near tie. With the
    date of view (`known_at`, alias `as_of`) the aliases valid then."""
    t0 = time.perf_counter()
    ao = known_at
    cands = service.ground(conn, p, text, kind=kind, entity_kinds=entity_kinds, as_of=ao, limit=limit)  # type: ignore[arg-type]
    return {"query": {"text": text, "kind": kind, "entity_kinds": entity_kinds, "as_of": ao, "limit": limit},
            "candidates": cands, "ambiguous": service.is_ambiguous(cands),
            **_meta(conn, p.tenant_id, DescriptorBuilder.name, DescriptorBuilder.topics, None, t0)}


@router.get("/admin/consistency/search", tags=["admin"])
def consistency(conn: Conn, p: Principal, samples: Annotated[int, Query(ge=1, le=500)] = 20,
                seed: int | None = None) -> dict[str, Any]:
    """Journal vs kb-versions: the same version_id for random (entity, known_at) pairs (research §5.7)."""
    if not p.is_admin:
        raise HTTPException(403, "admin only")
    t0 = time.perf_counter()
    res = service.consistency_sample(conn, p, samples, seed=seed)
    return {**res, **_meta(conn, p.tenant_id, SearchProjector.name, SearchProjector.topics, None, t0)}
