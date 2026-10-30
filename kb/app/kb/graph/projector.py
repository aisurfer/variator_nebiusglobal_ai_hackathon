"""Graph projector (K10): outbox consumer `graph` that maintains the Memgraph projection of the journal.

Projection model (research §4.4, KB element 6-7: one generic edge type with `type` instead of one relationship
type per predicate):

  (:Entity  {tenant, id, kind, title, url, source_id, acl, deleted, head_seq, head_vid, created_at, first_valid,
             deleted_at})          created_at/deleted_at: tx_from of the first/tombstone version; first_valid: min valid_from
  (:Version {tenant, vid, entity_id, seq, kind, op, title, acl, tx_from, tx_to, tx_seq, valid_from, is_current})
  (:Entity)-[:HAS_VERSION {tenant}]->(:Version)
  (:Version)-[:NEXT {tenant}]->(:Version)                       seq n -> seq n+1
  (:Entity)-[:REL {tenant, type, stmt_id, origin, confidence, review_status, subject_version,
                   tx_from, tx_to, valid_from, valid_to}]->(:Entity)          statements with an entity object
  (:Version)-[:CAUSED_BY {tenant, link_id, kind, predicate, origin, review_status, confidence, sensitivity,
                          tx_from, tx_to, valid_from, valid_to}]->(:Version)   effect version -> cause version
  (:Entity)-[:CAUSAL {... same as CAUSED_BY, type = predicate, effect_version, cause_version}]->(:Entity)
                                                                 the causal link projected to entity level

Times are integer epoch microseconds; a missing tx_to means open. Closed statements and links keep their edges
with tx_to set, so known_at queries work on the graph. For statements and links tx_from/tx_to are their knowledge
interval (`known`, Docs/KB/AS-OF.md): a fact derived late from earlier data is in the graph from the moment it was
derivable; a withdrawn candidate has an empty interval (tx_from = tx_to). Literal statements are not projected (they live in the
journal and in search). Version.acl mirrors the entity's current ACL, like the journal reader does.

The consumer is state-based and idempotent: it re-reads the rows named in the event payloads from PostgreSQL
and MERGEs them by journal id, so redelivery and replays change nothing.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

import psycopg
from neo4j import ManagedTransaction

from kb.contracts import TOPIC_CAUSAL, TOPIC_ENTITY_ACL, TOPIC_ENTITY_VERSION, TOPIC_STATEMENTS
from kb.graph import memgraph as mg
from kb.graph.memgraph import to_us
from kb.worker.framework import OutboxEvent, register_consumer

log = logging.getLogger(__name__)
CHUNK = 1000

_Q_ENTITIES = """
UNWIND $rows AS r
MERGE (e:Entity {tenant: $t, id: r.id})
SET e.kind = r.kind, e.title = r.title, e.url = r.url, e.source_id = r.source_id, e.acl = r.acl,
    e.deleted = r.deleted, e.head_seq = r.head_seq, e.head_vid = r.head_vid, e.created_at = r.created_at,
    e.first_valid = r.first_valid, e.deleted_at = r.deleted_at
"""

_Q_VERSIONS = """
UNWIND $rows AS r
MATCH (e:Entity {tenant: $t, id: r.entity_id})
MERGE (v:Version {tenant: $t, vid: r.vid})
SET v.entity_id = r.entity_id, v.seq = r.seq, v.kind = r.kind, v.op = r.op, v.title = r.title, v.acl = r.acl,
    v.tx_from = r.tx_from, v.tx_to = r.tx_to, v.tx_seq = r.tx_seq, v.valid_from = r.valid_from,
    v.is_current = r.is_current
MERGE (e)-[h:HAS_VERSION]->(v)
SET h.tenant = $t
"""

_Q_NEXT = """
UNWIND $rows AS r
MATCH (a:Version {tenant: $t, vid: r.a}), (b:Version {tenant: $t, vid: r.b})
MERGE (a)-[n:NEXT]->(b)
SET n.tenant = $t
"""

_Q_REL = """
UNWIND $rows AS r
MATCH (s:Entity {tenant: $t, id: r.s}), (o:Entity {tenant: $t, id: r.o})
MERGE (s)-[e:REL {stmt_id: r.stmt_id}]->(o)
SET e.tenant = $t, e.type = r.type, e.origin = r.origin, e.confidence = r.confidence,
    e.review_status = 'accepted', e.subject_version = r.subject_version, e.tx_from = r.tx_from, e.tx_to = r.tx_to,
    e.valid_from = r.valid_from, e.valid_to = r.valid_to
"""

_Q_CAUSED_BY = """
UNWIND $rows AS r
MATCH (ev:Version {tenant: $t, vid: r.effect_version}), (cv:Version {tenant: $t, vid: r.cause_version})
MERGE (ev)-[c:CAUSED_BY {link_id: r.link_id}]->(cv)
SET c.tenant = $t, c.kind = r.kind, c.predicate = r.predicate, c.origin = r.origin,
    c.review_status = r.review_status, c.confidence = r.confidence, c.sensitivity = r.sensitivity,
    c.tx_from = r.tx_from, c.tx_to = r.tx_to, c.valid_from = r.valid_from, c.valid_to = r.valid_to
"""

_Q_CAUSAL = """
UNWIND $rows AS r
MATCH (ee:Entity {tenant: $t, id: r.effect_entity_id}), (ce:Entity {tenant: $t, id: r.cause_entity_id})
MERGE (ee)-[c:CAUSAL {link_id: r.link_id}]->(ce)
SET c.tenant = $t, c.type = r.predicate, c.kind = r.kind, c.origin = r.origin,
    c.review_status = r.review_status, c.confidence = r.confidence, c.sensitivity = r.sensitivity,
    c.effect_version = r.effect_version, c.cause_version = r.cause_version,
    c.tx_from = r.tx_from, c.tx_to = r.tx_to, c.valid_from = r.valid_from, c.valid_to = r.valid_to
"""


def _chunks(rows: list[dict], n: int = CHUNK) -> Iterable[list[dict]]:
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


# ---------------------------------------------------------------- reads from the journal
def _read_statements(conn: psycopg.Connection, tenant: str, stmt_ids: list[int] | None) -> list[dict]:
    rows = conn.execute(
        """SELECT stmt_id, subject_id, subject_version_id, predicate_id, object_id, origin, confidence,
                  lower(valid) AS vf, upper(valid) AS vt,
                  CASE WHEN isempty(known) THEN lower(tx) ELSE lower(known) END AS tf, CASE WHEN isempty(known) THEN lower(tx) ELSE upper(known) END AS tt
           FROM statement
           WHERE tenant_id = %s AND object_id IS NOT NULL AND (%s::bigint[] IS NULL OR stmt_id = ANY(%s))""",
        (tenant, stmt_ids, stmt_ids),
    ).fetchall()
    return [{"stmt_id": r["stmt_id"], "s": r["subject_id"], "o": r["object_id"], "type": r["predicate_id"],
             "origin": r["origin"], "confidence": float(r["confidence"]), "subject_version": r["subject_version_id"],
             "tx_from": to_us(r["tf"]), "tx_to": to_us(r["tt"]), "valid_from": to_us(r["vf"]),
             "valid_to": to_us(r["vt"])} for r in rows]


def _read_links(conn: psycopg.Connection, tenant: str, link_ids: list[str] | None) -> list[dict]:
    rows = conn.execute(
        """SELECT link_id, effect_entity_id, effect_version, cause_entity_id, cause_version, causal_kind, predicate_id,
                  origin, review_status, confidence, sensitivity, lower(valid) AS vf, upper(valid) AS vt,
                  CASE WHEN isempty(known) THEN lower(tx) ELSE lower(known) END AS tf, CASE WHEN isempty(known) THEN lower(tx) ELSE upper(known) END AS tt
           FROM causal_link WHERE tenant_id = %s AND (%s::text[] IS NULL OR link_id = ANY(%s))""",
        (tenant, link_ids, link_ids),
    ).fetchall()
    return [{"link_id": r["link_id"], "effect_entity_id": r["effect_entity_id"], "effect_version": r["effect_version"],
             "cause_entity_id": r["cause_entity_id"], "cause_version": r["cause_version"], "kind": r["causal_kind"],
             "predicate": r["predicate_id"], "origin": r["origin"], "review_status": r["review_status"],
             "confidence": float(r["confidence"]), "sensitivity": list(r["sensitivity"]) if r["sensitivity"] else None,
             "tx_from": to_us(r["tf"]), "tx_to": to_us(r["tt"]), "valid_from": to_us(r["vf"]),
             "valid_to": to_us(r["vt"])} for r in rows]


def _read_entities(conn: psycopg.Connection, tenant: str, entity_ids: list[str] | None) -> tuple[list[dict], list[dict]]:
    ents = conn.execute(
        """SELECT entity_id, kind_id, title, url, source_id, acl, tombstone_tx, head_seq, head_version_id
           FROM entity WHERE tenant_id = %s AND (%s::text[] IS NULL OR entity_id = ANY(%s))""",
        (tenant, entity_ids, entity_ids),
    ).fetchall()
    vers = conn.execute(
        """SELECT version_id, entity_id, seq, op, kind_id, title, valid_from, lower(tx) AS tf, upper(tx) AS tt, tx_seq
           FROM entity_version WHERE tenant_id = %s AND (%s::text[] IS NULL OR entity_id = ANY(%s))
           ORDER BY entity_id, seq""",
        (tenant, entity_ids, entity_ids),
    ).fetchall()
    acl = {e["entity_id"]: sorted(e["acl"] or []) for e in ents}
    first: dict[str, int] = {}
    first_valid: dict[str, int] = {}
    head_from: dict[str, int] = {}
    for v in vers:
        eid, vf = v["entity_id"], to_us(v["valid_from"])
        first.setdefault(eid, to_us(v["tf"]))  # type: ignore[arg-type]
        first_valid[eid] = min(first_valid.get(eid, vf), vf)  # type: ignore[type-var]
        head_from[eid] = to_us(v["tf"])  # type: ignore[assignment]
    ent_rows = [{
        "id": e["entity_id"], "kind": e["kind_id"], "title": e["title"], "url": e["url"], "source_id": e["source_id"],
        "acl": acl[e["entity_id"]], "deleted": e["tombstone_tx"] is not None, "head_seq": e["head_seq"],
        "head_vid": e["head_version_id"], "created_at": first.get(e["entity_id"]),
        "first_valid": first_valid.get(e["entity_id"]),
        "deleted_at": head_from.get(e["entity_id"]) if e["tombstone_tx"] is not None else None,
    } for e in ents]
    ver_rows = [{
        "vid": v["version_id"], "entity_id": v["entity_id"], "seq": v["seq"], "op": v["op"], "kind": v["kind_id"],
        "title": v["title"], "acl": acl.get(v["entity_id"], []), "tx_from": to_us(v["tf"]), "tx_to": to_us(v["tt"]),
        "tx_seq": v["tx_seq"], "valid_from": to_us(v["valid_from"]), "is_current": v["tt"] is None,
    } for v in vers if v["entity_id"] in acl]
    return ent_rows, ver_rows


def _next_pairs(ver_rows: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for v in ver_rows:
        by.setdefault(v["entity_id"], []).append(v)
    pairs = []
    for vs in by.values():
        vs.sort(key=lambda x: x["seq"])
        pairs += [{"a": a["vid"], "b": b["vid"]} for a, b in zip(vs, vs[1:]) if b["seq"] == a["seq"] + 1]
    return pairs


# ---------------------------------------------------------------- writes to Memgraph
def project(
    conn: psycopg.Connection, tenant: str, *, entity_ids: Iterable[str] | None = (),
    stmt_ids: Iterable[int] | None = (), link_ids: Iterable[str] | None = (),
) -> dict[str, int]:
    """Upsert the given journal rows (None for a kind = all rows of the tenant). Endpoint entities of statements
    and links are upserted with all their versions, so edges never wait for nodes from a later batch."""
    stmt_list = None if stmt_ids is None else sorted(set(stmt_ids))
    link_list = None if link_ids is None else sorted(set(link_ids))
    stmts = _read_statements(conn, tenant, stmt_list) if stmt_list is None or stmt_list else []
    links = _read_links(conn, tenant, link_list) if link_list is None or link_list else []
    if entity_ids is None:
        ents, vers = _read_entities(conn, tenant, None)
    else:
        want = set(entity_ids or ())
        want.update(s["s"] for s in stmts)
        want.update(s["o"] for s in stmts)
        want.update(lk["effect_entity_id"] for lk in links)
        want.update(lk["cause_entity_id"] for lk in links)
        ents, vers = _read_entities(conn, tenant, sorted(want)) if want else ([], [])
    nexts = _next_pairs(vers)

    def work(tx: ManagedTransaction) -> None:
        for q, rows in ((_Q_ENTITIES, ents), (_Q_VERSIONS, vers), (_Q_NEXT, nexts), (_Q_REL, stmts),
                        (_Q_CAUSED_BY, links), (_Q_CAUSAL, links)):
            for part in _chunks(rows):
                tx.run(q, t=tenant, rows=part).consume()

    if ents or stmts or links:
        mg.write_tx(work)
    return {"entities": len(ents), "versions": len(vers), "statements": len(stmts), "links": len(links)}


def reset_tenant(tenant: str) -> int:
    """Delete the tenant's subgraph (nodes and, with them, every edge). Other tenants are untouched."""
    removed = 0
    for label in ("Version", "Entity"):
        while True:
            rows = mg.write(f"MATCH (n:{label} {{tenant: $t}}) WITH n LIMIT 5000 DETACH DELETE n RETURN count(*) AS c",
                            t=tenant)
            c = rows[0]["c"] if rows else 0
            removed += c
            if c == 0:
                break
    return removed


def rebuild(conn: psycopg.Connection, tenant: str) -> dict[str, int]:
    """Drop and re-project the tenant straight from the journal tables (bypasses the outbox)."""
    reset_tenant(tenant)
    return project(conn, tenant, entity_ids=None, stmt_ids=None, link_ids=None)


class GraphProjector:
    name = "graph"
    batch = 2000
    topics = frozenset({TOPIC_ENTITY_VERSION, TOPIC_ENTITY_ACL, TOPIC_STATEMENTS, TOPIC_CAUSAL})

    def handle(self, conn: psycopg.Connection, tenant_id: str, events: list[OutboxEvent]) -> None:
        entity_ids: set[str] = set()
        stmt_ids: set[int] = set()
        link_ids: set[str] = set()
        for ev in events:
            p: dict[str, Any] = ev.payload or {}
            if ev.topic in (TOPIC_ENTITY_VERSION, TOPIC_ENTITY_ACL):
                if p.get("entity_id"):
                    entity_ids.add(p["entity_id"])
            elif ev.topic == TOPIC_STATEMENTS:
                stmt_ids.update(int(x) for x in (p.get("opened") or []) + (p.get("closed") or []))
            elif ev.topic == TOPIC_CAUSAL:
                link_ids.update(str(x) for x in (p.get("opened") or []) + (p.get("closed") or []))
        if entity_ids or stmt_ids or link_ids:
            project(conn, tenant_id, entity_ids=entity_ids, stmt_ids=stmt_ids, link_ids=link_ids)

    def reset(self, conn: psycopg.Connection, tenant_id: str) -> None:
        if mg.enabled():
            reset_tenant(tenant_id)


PROJECTOR = GraphProjector()
if mg.enabled():  # disabled: events stay in the outbox and are projected once the graph is switched on
    register_consumer(PROJECTOR)
