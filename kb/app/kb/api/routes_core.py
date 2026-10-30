"""Core REST: ingestion, entities/versions/history/diff/timeline, ontology, sources, admin."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from kb.contracts import IngestResult, SourceEnvelope
from kb.ingest.pipeline import UnknownSource, ingest, tenant_sources
from kb.ingest.replay import replay
from kb.journal import reader
from kb.journal.writer import journal_tx
from kb.api.deps import Conn, KnownAt, Principal, Tenant, not_found, parse_time, view_time
from kb.settings import get_settings
from kb.worker.framework import drain_until_idle, reset, watermarks

router = APIRouter(prefix="/api/v1")


@router.post("/ingest/envelopes", response_model=list[IngestResult], tags=["ingest"])
def ingest_envelopes(conn: Conn, p: Principal, envelopes: Annotated[list[SourceEnvelope], Body()]) -> list[IngestResult]:
    """Connector push endpoint: a batch of envelopes, acknowledged after the inbox row is durable."""
    if not p.is_admin and not p.principal_id.startswith("service:"):
        raise HTTPException(403, "connector or admin principal required")
    out = []
    for env in envelopes:
        if env.tenant_id != p.tenant_id:
            raise HTTPException(422, "envelope tenant differs from the request tenant")
        try:
            out.append(ingest(conn, env))
        except UnknownSource as exc:
            raise HTTPException(422, f"unknown source {exc}") from None
    return out


class ReplayRequest(BaseModel):
    until: str | None = Field(None, description="replay events up to this ISO time")
    fixtures: str | None = None


@router.post("/ingest/replay", tags=["ingest"])
def ingest_replay(conn: Conn, p: Principal, req: ReplayRequest | None = None) -> dict[str, Any]:
    """Dev only: feed the simulated source history (fixtures timeline) through the ingest path."""
    if get_settings().env == "prod" or not p.is_admin:
        raise HTTPException(403, "replay is a dev/admin operation")
    req = req or ReplayRequest()
    return replay(conn, p.tenant_id, fixtures=req.fixtures, until=parse_time(req.until, "until"))


@router.get("/entities", tags=["journal"])
def list_entities(
    conn: Conn, p: Principal, kind: str | None = None, q: str | None = None, source_id: str | None = None,
    include_deleted: bool = False, limit: Annotated[int, Query(le=500)] = 100, offset: int = 0,
    known_at: KnownAt = None,
) -> dict[str, Any]:
    """Entities (as known at K: those with a version recorded by K, with their kind, title and deletion then)."""
    acl_sql, acl_params = p.sql_filter("e.acl")
    if known_at is None:
        head = "SELECT e.kind_id, e.title, e.head_seq AS versions, e.tombstone_tx IS NOT NULL AS deleted"
    else:
        head = """SELECT v.kind_id, coalesce(v.title, e.title) AS title, v.seq AS versions, v.op = 'delete' AS deleted
                  FROM entity_version v WHERE v.tenant_id = e.tenant_id AND v.entity_id = e.entity_id
                    AND lower(v.tx) <= %s ORDER BY v.seq DESC LIMIT 1"""
    rows = conn.execute(
        f"""SELECT e.entity_id, h.kind_id AS kind, h.title, e.source_id, e.url, h.versions, h.deleted
            FROM entity e JOIN LATERAL ({head}) h ON true
            WHERE e.tenant_id = %s AND {acl_sql}
              AND (%s::text IS NULL OR h.kind_id = %s) AND (%s::text IS NULL OR e.source_id = %s)
              AND (%s::text IS NULL OR h.title ILIKE '%%' || %s || '%%' OR e.entity_id ILIKE '%%' || %s || '%%')
              AND (%s OR NOT h.deleted)
            ORDER BY h.kind_id, e.entity_id LIMIT %s OFFSET %s""",
        (*(() if known_at is None else (known_at,)), p.tenant_id, *acl_params, kind, kind, source_id, source_id,
         q, q, q, include_deleted, limit, offset),
    ).fetchall()
    return {"items": rows, "limit": limit, "offset": offset, "known_at": known_at}


@router.get("/entities/{entity_id}", tags=["journal"])
def get_entity(conn: Conn, p: Principal, entity_id: str, known_at: KnownAt = None) -> dict[str, Any]:
    card = reader.entity_card(conn, p, entity_id, known_at=known_at)
    if card is None:
        raise not_found()
    return card


@router.get("/entities/{entity_id}/history", tags=["journal"])
def entity_history(
    conn: Conn, p: Principal, entity_id: str, start: str | None = None, end: str | None = None,
    fields: Annotated[list[str] | None, Query()] = None, known_at: KnownAt = None,
) -> dict[str, Any]:
    h = reader.history(conn, p, entity_id, start=parse_time(start, "start"), end=parse_time(end, "end"), fields=fields,
                       known_at=known_at)
    if h is None:
        raise not_found()
    return {"entity_id": entity_id, "known_at": known_at, "versions": h}


@router.get("/entities/{entity_id}/as-of", tags=["journal"])
def entity_as_of(conn: Conn, p: Principal, entity_id: str, valid_at: str | None = None,
                 known_at: KnownAt = None) -> dict[str, Any]:
    """State at business time `valid_at` as known at `known_at` (default: the head at the end of the data; a
    business time alone is read as known at the end of the data)."""
    va = parse_time(valid_at, "valid_at")
    rows = reader.as_of(conn, p, [entity_id], valid_at=va, known_at=known_at)
    if not rows:
        raise not_found()
    return {"query": {"valid_at": va, "known_at": known_at}, "version": rows[0]}


@router.get("/entities/{entity_id}/diff", tags=["journal"])
def entity_diff(conn: Conn, p: Principal, entity_id: str, from_seq: int | None = None,
                to_seq: int | None = None, known_at: KnownAt = None) -> dict[str, Any]:
    d = reader.diff(conn, p, entity_id, from_seq, to_seq, known_at=known_at)
    if d is None:
        raise not_found()
    return d


@router.get("/entities/{entity_id}/statements", tags=["journal"])
def entity_statements(conn: Conn, p: Principal, entity_id: str, known_at: KnownAt = None,
                      direction: str = "both") -> dict[str, Any]:
    if reader.get_entity(conn, p, entity_id) is None:
        raise not_found()
    return {"entity_id": entity_id, "known_at": known_at,
            "statements": reader.statements(conn, p, entity_id, known_at=known_at, direction=direction)}


@router.get("/versions/{version_id}", tags=["journal"])
def get_version(conn: Conn, p: Principal, version_id: str, known_at: KnownAt = None) -> dict[str, Any]:
    row = reader.get_version(conn, p, version_id, known_at=known_at)
    if row is None:
        raise not_found()
    return reader.version_out(row, with_body=True)


@router.get("/timeline", tags=["journal"])
def get_timeline(
    conn: Conn, p: Principal, ids: Annotated[list[str] | None, Query()] = None, start: str | None = None,
    end: str | None = None, kinds: Annotated[list[str] | None, Query()] = None, axis: str = "valid",
    limit: Annotated[int, Query(le=2000)] = 500, known_at: KnownAt = None,
) -> dict[str, Any]:
    """Versions in [start, end) on the valid or the tx axis; as known at K only those recorded by K."""
    if axis not in ("valid", "tx"):
        raise HTTPException(422, "axis must be valid or tx")
    events = reader.timeline(conn, p, entity_ids=ids, start=parse_time(start, "start"), end=parse_time(end, "end"),
                             kinds=kinds, axis=axis, limit=limit, known_at=known_at)
    return {"axis": axis, "known_at": known_at, "events": events}


@router.get("/bounds", tags=["journal"])
def data_bounds(conn: Conn, p: Principal) -> dict[str, Any]:
    """The span of the tenant's data: the dates of view a read accepts end at `data_end` (Docs/KB/AS-OF.md)."""
    from kb.asof import bounds

    return bounds(conn, p.tenant_id).as_dict()


@router.get("/ontology/kinds", tags=["ontology"])
def kinds(conn: Conn, t: Tenant) -> dict[str, Any]:
    rows = conn.execute("SELECT kind_id, label, description, status FROM kind WHERE tenant_id = %s ORDER BY kind_id",
                        (t,)).fetchall()
    return {"items": rows}


@router.get("/ontology/predicates", tags=["ontology"])
def predicates(conn: Conn, t: Tenant, status: str | None = None) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT predicate_id, label, inverse_label, aliases, description, causal_kind, typical_sensitivity,
                  domain_kinds, range_kinds, status, canonical_predicate_id
           FROM predicate WHERE tenant_id = %s AND (%s::text IS NULL OR status = %s) ORDER BY predicate_id""",
        (t, status, status),
    ).fetchall()
    return {"items": rows}


class PredicateIn(BaseModel):
    predicate_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    label: str
    inverse_label: str | None = None
    aliases: list[str] = []
    description: str | None = None
    causal_kind: str | None = None
    typical_sensitivity: list[str] | None = None
    canonical_predicate_id: str | None = None


@router.post("/ontology/predicates", tags=["ontology"])
def upsert_predicate(conn: Conn, p: Principal, body: PredicateIn) -> dict[str, Any]:
    """Open schema: a new relation type is an insert, not a migration. Admin only."""
    if not p.is_admin:
        raise HTTPException(403, "admin only")
    with journal_tx(conn, p.tenant_id, "admin", actor_id=p.principal_id, ref={"predicate": body.predicate_id}) as j:
        conn.execute(
            """INSERT INTO predicate (tenant_id, predicate_id, label, inverse_label, aliases, description, causal_kind,
                 typical_sensitivity, canonical_predicate_id, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
               ON CONFLICT (tenant_id, predicate_id) DO UPDATE SET label = EXCLUDED.label,
                 inverse_label = EXCLUDED.inverse_label, aliases = EXCLUDED.aliases, description = EXCLUDED.description,
                 causal_kind = EXCLUDED.causal_kind, typical_sensitivity = EXCLUDED.typical_sensitivity,
                 canonical_predicate_id = EXCLUDED.canonical_predicate_id, status = 'active'""",
            (p.tenant_id, body.predicate_id, body.label, body.inverse_label, body.aliases, body.description,
             body.causal_kind, body.typical_sensitivity, body.canonical_predicate_id),
        )
        j._event("predicate.changed", body.predicate_id, {"predicate_id": body.predicate_id})
    return {"predicate_id": body.predicate_id, "status": "active"}


@router.get("/sources", tags=["sources"])
def sources(conn: Conn, p: Principal, known_at: KnownAt = None) -> dict[str, Any]:
    """Source instances with their deliveries (as known at K: the deliveries observed by K)."""
    ts = tenant_sources(conn, p.tenant_id)
    counts = {r["source_id"]: r for r in conn.execute(
        """SELECT source_id, count(*) AS deliveries, count(*) FILTER (WHERE status = 'failed') AS failed,
                  max(observed_at) AS last_observed_at
           FROM ingest_event WHERE tenant_id = %s AND (%s::timestamptz IS NULL OR observed_at <= %s)
           GROUP BY source_id""", (p.tenant_id, known_at, known_at)).fetchall()}
    items = []
    for s in ts.sources.values():
        c = counts.get(s.id, {})
        items.append({"source_id": s.id, "provider": s.provider, "name": s.name, "base_url": s.base_url,
                      "containers": {k: {"name": v.name, "acl": list(v.acl)} for k, v in s.containers.items()},
                      "deliveries": c.get("deliveries", 0), "failed": c.get("failed", 0),
                      "last_observed_at": c.get("last_observed_at")})
    return {"items": items, "known_at": known_at}


@router.get("/admin/watermarks", tags=["admin"])
def admin_watermarks(conn: Conn, p: Principal) -> dict[str, Any]:
    return watermarks(conn, p.tenant_id)


@router.get("/admin/stats", tags=["admin"])
def admin_stats(conn: Conn, p: Principal, known_at: KnownAt = None) -> dict[str, Any]:
    """Counts of the tenant's journal (as known at K: entities by kind and deletion, versions, statements, links by
    status and origin, chunks, unresolved references and deliveries then)."""
    t = p.tenant_id
    if known_at is None:
        q = lambda sql: conn.execute(sql, (t,)).fetchall()  # noqa: E731
        return {
            "tenant": t,
            "known_at": None,
            "entities_by_kind": {r["kind_id"]: r["n"] for r in q(
                "SELECT kind_id, count(*) AS n FROM entity WHERE tenant_id = %s AND tombstone_tx IS NULL GROUP BY 1 ORDER BY 1")},
            "deleted_entities": q("SELECT count(*) AS n FROM entity WHERE tenant_id = %s AND tombstone_tx IS NOT NULL")[0]["n"],
            "versions": q("SELECT count(*) AS n FROM entity_version WHERE tenant_id = %s")[0]["n"],
            "statements_open": q("SELECT count(*) AS n FROM statement WHERE tenant_id = %s AND upper_inf(tx)")[0]["n"],
            "causal_links_by_status": {r["status"]: r["n"] for r in q(
                """SELECT status || CASE WHEN review_status = 'pending' THEN ' (pending)' ELSE '' END AS status, count(*) AS n
                   FROM causal_link_status WHERE tenant_id = %s AND NOT isempty(known) GROUP BY 1 ORDER BY 1""")},
            "causal_links_by_origin": {r["origin"]: r["n"] for r in q(
                "SELECT origin, count(*) AS n FROM causal_link WHERE tenant_id = %s AND upper_inf(tx) GROUP BY 1 ORDER BY 1")},
            "chunks_open": q("SELECT count(*) AS n FROM chunk WHERE tenant_id = %s AND version_to_seq IS NULL")[0]["n"],
            "unresolved_refs": q("SELECT count(*) AS n FROM unresolved_ref WHERE tenant_id = %s AND status = 'pending'")[0]["n"],
            "ingest_events": {r["status"]: r["n"] for r in q(
                "SELECT status, count(*) AS n FROM ingest_event WHERE tenant_id = %s GROUP BY 1")},
            "watermarks": watermarks(conn, t),
        }
    from kb.acl import system_principal
    from kb.causal.status import status_rows

    k = known_at
    qk = lambda sql: conn.execute(sql, {"t": t, "k": k}).fetchall()  # noqa: E731
    heads = reader.heads_at(conn, t, None, k).values()
    by_kind: dict[str, int] = {}
    for h in heads:
        if h["tombstone_tx"] is None:
            by_kind[h["kind_id"]] = by_kind.get(h["kind_id"], 0) + 1
    seq_k = qk("SELECT coalesce(max(tx_seq), 0) AS s FROM tx_log WHERE tenant_id = %(t)s AND tx_at <= %(k)s")[0]["s"]
    ids = [r["link_id"] for r in qk("SELECT link_id FROM causal_link WHERE tenant_id = %(t)s AND lower(known) <= %(k)s "
                                    "AND NOT isempty(known)")]
    by_status: dict[str, int] = {}
    for r in status_rows(conn, system_principal(t), ids, known_at=k).values():
        key = r["status"] + (" (pending)" if r["review_status"] == "pending" else "")
        by_status[key] = by_status.get(key, 0) + 1
    return {
        "tenant": t,
        "known_at": k,
        "entities_by_kind": dict(sorted(by_kind.items())),
        "deleted_entities": sum(1 for h in heads if h["tombstone_tx"] is not None),
        "versions": qk("SELECT count(*) AS n FROM entity_version WHERE tenant_id = %(t)s AND lower(tx) <= %(k)s")[0]["n"],
        "statements_open": qk("SELECT count(*) AS n FROM statement WHERE tenant_id = %(t)s AND known @> %(k)s::timestamptz")[0]["n"],
        "causal_links_by_status": dict(sorted(by_status.items())),
        "causal_links_by_origin": {r["origin"]: r["n"] for r in qk(
            """SELECT origin, count(*) AS n FROM causal_link WHERE tenant_id = %(t)s AND known @> %(k)s::timestamptz
               GROUP BY 1 ORDER BY 1""")},
        "chunks_open": qk("""SELECT count(*) AS n FROM chunk WHERE tenant_id = %(t)s AND tx_from <= %(k)s
                               AND (tx_to IS NULL OR tx_to > %(k)s)""")[0]["n"],
        "unresolved_refs": conn.execute(
            """SELECT count(*) AS n FROM unresolved_ref WHERE tenant_id = %s AND created_tx <= %s
                 AND (closed_tx IS NULL OR closed_tx > %s)""", (t, seq_k, seq_k)).fetchone()["n"],
        "ingest_events": {r["status"]: r["n"] for r in qk(
            "SELECT status, count(*) AS n FROM ingest_event WHERE tenant_id = %(t)s AND observed_at <= %(k)s GROUP BY 1")},
        "watermarks": watermarks(conn, t),
    }


class ReindexRequest(BaseModel):
    consumers: list[str] | None = Field(None, description="search | descriptors | graph (default: all)")


@router.post("/admin/reindex", tags=["admin"])
def admin_reindex(conn: Conn, p: Principal, req: ReindexRequest | None = None) -> dict[str, Any]:
    """Drop and rebuild the tenant's projections from the journal (KB-2). Synchronous; admin only."""
    if not p.is_admin:
        raise HTTPException(403, "admin only")
    consumers = (req or ReindexRequest()).consumers
    reset(conn, p.tenant_id, consumers)
    processed = drain_until_idle(conn, p.tenant_id, consumers=consumers, timeout_s=900)
    return {"processed": processed, "watermarks": watermarks(conn, p.tenant_id)}
