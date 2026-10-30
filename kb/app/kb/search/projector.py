"""Consumer `search` (K08): journal -> kb-chunks, kb-versions, kb-entities.

State-based and idempotent: for every entity named in a batch the projector re-reads the entity, all its versions
and chunk rows from PostgreSQL and upserts the full documents. Writers of a tenant are serialised
(opensearch.writer_lock), so a slower concurrent drain never overwrites a newer state; re-projecting the same
state rewrites identical documents.

- chunk docs: one per chunk row; text is immutable, only version_to_seq/tx_to close. Vectors come from the
  (encoder, sha256) cache, so a chunk is embedded once in its life (also across reindexes);
- version docs: one per entity_version (title, flattened fields, body head, recorded/superseded, effective_from,
  is_current, op);
- entity docs: the current head; deleted when the entity is tombstoned (its chunk/version docs get is_current=false);
- entity.acl events re-project the entity, which rewrites `acl` on all of its documents.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import psycopg

from kb.contracts import TOPIC_CHUNKS, TOPIC_ENTITY_ACL, TOPIC_ENTITY_VERSION
from kb.search import docs as D
from kb.search.embeddings import cached_embed, get_embedder
from kb.search.opensearch import Indices, bulk, delete_tenant, ensure_indices, refresh, writer_lock
from kb.worker.framework import OutboxEvent, register_consumer

log = logging.getLogger(__name__)

_VERSION_COLS = """version_id, entity_id, seq, op, kind_id, title, content, body, changed_fields, valid_from,
  lower(tx) AS recorded_at, upper(tx) AS superseded_at, tx_seq, actor_id"""


def project_entities(conn: psycopg.Connection, tenant_id: str, entity_ids: list[str], *, do_refresh: bool = True) -> int:
    """(Re)project the given entities into the chunk/version/entity indices. Returns the number of documents."""
    if not entity_ids:
        return 0
    emb = get_embedder()
    idx: Indices = ensure_indices(conn, emb)
    ents = {r["entity_id"]: r for r in conn.execute(
        "SELECT * FROM entity WHERE tenant_id = %s AND entity_id = ANY(%s)", (tenant_id, entity_ids)).fetchall()}
    versions: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in conn.execute(
        f"SELECT {_VERSION_COLS} FROM entity_version WHERE tenant_id = %s AND entity_id = ANY(%s) ORDER BY entity_id, seq",
        (tenant_id, entity_ids),
    ).fetchall():
        versions[r["entity_id"]][r["seq"]] = r
    chunks: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute(
        "SELECT * FROM chunk WHERE tenant_id = %s AND entity_id = ANY(%s) ORDER BY entity_id, version_from_seq, ordinal",
        (tenant_id, entity_ids),
    ).fetchall():
        chunks[r["entity_id"]].append(r)
    keys: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute(
        """SELECT entity_id, alias FROM alias WHERE tenant_id = %s AND entity_id = ANY(%s) AND alias_kind = 'key'
             AND upper_inf(tx)""",
        (tenant_id, entity_ids),
    ).fetchall():
        keys[r["entity_id"]].append(r["alias"])

    # one embedding call for the whole batch (cache misses only)
    texts: list[str] = []
    slots: list[tuple[str, str, Any]] = []
    for eid, ent in ents.items():
        vs = versions.get(eid, {})
        for c in chunks.get(eid, []):
            creating = vs.get(c["version_from_seq"]) or {}
            texts.append(D.chunk_embed_text(c, creating.get("title") or ent.get("title")))
            slots.append(("c", eid, c))
        for v in vs.values():
            texts.append(D.version_embed_text(v))
            slots.append(("v", eid, v))
    vectors = cached_embed(conn, emb, texts)

    actions: list[dict[str, Any]] = []
    for (typ, eid, row), vec in zip(slots, vectors):
        ent = ents[eid]
        if typ == "c":
            src = D.chunk_doc(tenant_id, ent, row, versions[eid], vec, emb.name)
            actions.append({"_op_type": "index", "_index": idx.chunks, "_id": D.doc_id(tenant_id, row["chunk_id"]),
                            "_source": src})
        else:
            src = D.version_doc(tenant_id, ent, row, vec, emb.name)
            actions.append({"_op_type": "index", "_index": idx.versions, "_id": D.doc_id(tenant_id, row["version_id"]),
                            "_source": src})
    for eid, ent in ents.items():
        _id = D.doc_id(tenant_id, eid)
        vs = versions.get(eid, {})
        head = vs.get(ent["head_seq"])
        if ent["tombstone_tx"] is not None or head is None or head["op"] == "delete":
            actions.append({"_op_type": "delete", "_index": idx.entities, "_id": _id})
        else:
            first = vs[min(vs)]
            src = D.entity_doc(tenant_id, ent, head, first, keys.get(eid, []))
            actions.append({"_op_type": "index", "_index": idx.entities, "_id": _id, "_source": src})
    n = bulk(actions)
    if do_refresh:
        refresh([idx.chunks, idx.versions, idx.entities])
    return n


class SearchProjector:
    name = "search"
    batch = 2000  # an entity is projected once per batch with all its versions: big batches project it once
    topics = frozenset({TOPIC_ENTITY_VERSION, TOPIC_ENTITY_ACL, TOPIC_CHUNKS})

    def handle(self, conn: psycopg.Connection, tenant_id: str, events: list[OutboxEvent],
               part: tuple[int, int] | None = None) -> None:
        """`part` (k, n): a part of a parallel drain projects the entities of part k of n (its own writer lock)."""
        from kb.worker.framework import part_name, part_of

        ids = list(dict.fromkeys(e.payload["entity_id"] for e in events if e.payload.get("entity_id")))
        if part:
            ids = [i for i in ids if part_of(i, part[1]) == part[0]]
        with writer_lock(conn, part_name(self.name, *part) if part else self.name, tenant_id):
            for i in range(0, len(ids), 100):
                project_entities(conn, tenant_id, ids[i : i + 100])

    def reset(self, conn: psycopg.Connection, tenant_id: str) -> None:
        idx = ensure_indices(conn)
        with writer_lock(conn, self.name, tenant_id):
            n = delete_tenant([idx.pattern("chunks"), idx.pattern("versions"), idx.pattern("entities")], tenant_id)
        log.info("search projection of %s reset (%d docs)", tenant_id, n)


register_consumer(SearchProjector())
