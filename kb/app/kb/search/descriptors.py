"""Consumer `descriptors` (K09): the descriptor index over entities AND predicates (research KG elements 3-4).

Entity descriptor (one per entity, ACL of the entity): labels — current title, current aliases and keys, plus
historical (closed) aliases with their validity interval — kind, a short description (head fields + body excerpt
of the last live version), embedding. Tombstoned entities keep a descriptor flagged `deleted` (their aliases are
all historical), so "the deleted draft" can still be grounded.

Predicate descriptor (one per predicate, tenant-wide schema, visible to every principal of the tenant): label,
inverse label, aliases, description, causal_kind, typical_sensitivity, declared domain/range kinds, embedding.
Usage statistics aggregated from statement/causal_link — counts, endpoint-kind pairs such as Decision→Meeting,
link status shares — are stored unindexed in `stats` and returned to admins only; they never enter the scored
text or the embedding (hidden instances must not influence what a principal sees), and no instance titles are
ever stored in a predicate descriptor.

Seeded predicates have no outbox event of their own: every batch also builds descriptors of predicates that are
missing from the index (and of predicates registered on the fly by adapters as `proposed`).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import psycopg

from kb.contracts import TOPIC_CAUSAL, TOPIC_ENTITY_ACL, TOPIC_ENTITY_VERSION, TOPIC_PREDICATE, TOPIC_STATEMENTS
from kb.search import docs as D
from kb.search.embeddings import cached_embed, get_embedder
from kb.search.opensearch import Indices, bulk, client, delete_tenant, ensure_indices, refresh, writer_lock
from kb.worker.framework import OutboxEvent, register_consumer

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------------- entities
def build_entity_descriptors(conn: psycopg.Connection, tenant_id: str, entity_ids: list[str]) -> list[dict[str, Any]]:
    """Index actions for the entity descriptors (embedding included)."""
    if not entity_ids:
        return []
    emb = get_embedder()
    idx = ensure_indices(conn, emb)
    ents = conn.execute(
        """SELECT e.*, k.label AS kind_label FROM entity e
           LEFT JOIN kind k ON k.tenant_id = e.tenant_id AND k.kind_id = e.kind_id
           WHERE e.tenant_id = %s AND e.entity_id = ANY(%s)""",
        (tenant_id, entity_ids),
    ).fetchall()
    live = {r["entity_id"]: r for r in conn.execute(
        """SELECT DISTINCT ON (entity_id) entity_id, seq, op, title, content, body, valid_from
           FROM entity_version WHERE tenant_id = %s AND entity_id = ANY(%s) AND op <> 'delete'
           ORDER BY entity_id, seq DESC""",
        (tenant_id, entity_ids),
    ).fetchall()}
    span = {r["entity_id"]: r for r in conn.execute(
        """SELECT entity_id, min(valid_from) AS valid_from,
                  max(lower(tx)) FILTER (WHERE op = 'delete') AS deleted_at
           FROM entity_version WHERE tenant_id = %s AND entity_id = ANY(%s) GROUP BY entity_id""",
        (tenant_id, entity_ids),
    ).fetchall()}
    aliases: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute(
        """SELECT entity_id, alias, alias_norm, alias_kind, lower(valid) AS valid_from, upper(valid) AS valid_to,
                  (upper_inf(tx) AND upper_inf(valid)) AS current
           FROM alias WHERE tenant_id = %s AND entity_id = ANY(%s) ORDER BY entity_id, tx_seq""",
        (tenant_id, entity_ids),
    ).fetchall():
        aliases[r["entity_id"]].append(r)
    docs = []
    for ent in ents:
        eid = ent["entity_id"]
        sp = span.get(eid) or {}
        docs.append(D.entity_descriptor(tenant_id, ent, ent["kind_label"], live.get(eid), sp or None,
                                        sp.get("deleted_at"), aliases.get(eid, [])))
    vecs = cached_embed(conn, emb, [D.entity_descriptor_embed_text(d) for d in docs])
    actions = []
    for d, v in zip(docs, vecs):
        d["embedding"], d["encoder"] = v, emb.name
        actions.append({"_op_type": "index", "_index": idx.descriptors, "_id": D.doc_id(tenant_id, "entity", d["ref_id"]),
                        "_source": d})
    return actions


# --------------------------------------------------------------------------------------------- predicates
def predicate_stats(conn: psycopg.Connection, tenant_id: str, predicate_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Schema-level usage of predicates over ALL instances (admin-only output): counts, endpoint-kind pairs,
    first/last use, status shares of causal links. Never instance titles."""
    out: dict[str, dict[str, Any]] = {p: {"usage_count": 0,
                                          "statements": {"count": 0, "endpoint_kinds": {}},
                                          "causal_links": {"count": 0, "pending": 0, "endpoint_kinds": {}, "status": {}}}
                                      for p in predicate_ids}
    rows = conn.execute(
        """SELECT s.predicate_id, se.kind_id AS from_kind, COALESCE(oe.kind_id, '(literal)') AS to_kind, count(*) AS n,
                  min(lower(s.tx)) AS first_at, max(lower(s.tx)) AS last_at
           FROM statement s
           JOIN entity se ON se.tenant_id = s.tenant_id AND se.entity_id = s.subject_id
           LEFT JOIN entity oe ON oe.tenant_id = s.tenant_id AND oe.entity_id = s.object_id
           WHERE s.tenant_id = %s AND upper_inf(s.tx) AND s.predicate_id = ANY(%s)
           GROUP BY 1, 2, 3""",
        (tenant_id, predicate_ids),
    ).fetchall()
    for r in rows:
        st = out[r["predicate_id"]]["statements"]
        st["count"] += r["n"]
        st["endpoint_kinds"][f"{r['from_kind']}→{r['to_kind']}"] = r["n"]
        for k, f in (("first_used_at", min), ("last_used_at", max)):
            cur = st.get(k)
            val = r["first_at"] if k == "first_used_at" else r["last_at"]
            st[k] = val if cur is None else f(cur, val)
    rows = conn.execute(
        """SELECT l.predicate_id, ee.kind_id AS from_kind, ce.kind_id AS to_kind, l.review_status, l.status, count(*) AS n
           FROM causal_link_status l
           JOIN entity ee ON ee.tenant_id = l.tenant_id AND ee.entity_id = l.effect_entity_id
           JOIN entity ce ON ce.tenant_id = l.tenant_id AND ce.entity_id = l.cause_entity_id
           WHERE l.tenant_id = %s AND upper_inf(l.tx) AND l.predicate_id = ANY(%s)
           GROUP BY 1, 2, 3, 4, 5""",
        (tenant_id, predicate_ids),
    ).fetchall()
    for r in rows:
        cl = out[r["predicate_id"]]["causal_links"]
        if r["review_status"] == "pending":
            cl["pending"] += r["n"]
            continue
        cl["count"] += r["n"]
        pair = f"{r['from_kind']}→{r['to_kind']}"
        cl["endpoint_kinds"][pair] = cl["endpoint_kinds"].get(pair, 0) + r["n"]
        cl["status"][r["status"]] = cl["status"].get(r["status"], 0) + r["n"]
    for s in out.values():
        cl = s["causal_links"]
        s["usage_count"] = s["statements"]["count"] + cl["count"]
        if cl["count"]:
            cl["stale_share"] = round(cl["status"].get("STALE", 0) / cl["count"], 3)
        for k in ("first_used_at", "last_used_at"):
            if s["statements"].get(k) is not None:
                s["statements"][k] = s["statements"][k].isoformat()
    return out


def build_predicate_descriptors(conn: psycopg.Connection, tenant_id: str, predicate_ids: list[str]) -> list[dict[str, Any]]:
    if not predicate_ids:
        return []
    emb = get_embedder()
    idx = ensure_indices(conn, emb)
    preds = conn.execute(
        """SELECT predicate_id, label, inverse_label, aliases, description, causal_kind, typical_sensitivity,
                  domain_kinds, range_kinds, status, canonical_predicate_id
           FROM predicate WHERE tenant_id = %s AND predicate_id = ANY(%s) ORDER BY predicate_id""",
        (tenant_id, predicate_ids),
    ).fetchall()
    stats = predicate_stats(conn, tenant_id, [p["predicate_id"] for p in preds])
    docs = [D.predicate_descriptor(tenant_id, p, stats[p["predicate_id"]]) for p in preds]
    vecs = cached_embed(conn, emb, [D.predicate_embed_text(d) for d in docs])
    actions = []
    for d, v in zip(docs, vecs):
        d["embedding"], d["encoder"] = v, emb.name
        actions.append({"_op_type": "index", "_index": idx.descriptors,
                        "_id": D.doc_id(tenant_id, "predicate", d["ref_id"]), "_source": d})
    gone = set(predicate_ids) - {p["predicate_id"] for p in preds}
    for pid in gone:
        actions.append({"_op_type": "delete", "_index": idx.descriptors, "_id": D.doc_id(tenant_id, "predicate", pid)})
    return actions


def _indexed_predicates(idx: Indices, tenant_id: str) -> set[str]:
    res = client().search(index=idx.descriptors, body={
        "size": 0,
        "query": {"bool": {"filter": [{"term": {"tenant_id": tenant_id}}, {"term": {"doc_type": "predicate"}}]}},
        "aggs": {"ids": {"terms": {"field": "ref_id", "size": 10000}}},
    })
    return {b["key"] for b in res["aggregations"]["ids"]["buckets"]}


def _affected_predicates(conn: psycopg.Connection, tenant_id: str, events: list[OutboxEvent]) -> set[str]:
    preds: set[str] = set()
    stmt_ids: list[int] = []
    link_ids: list[str] = []
    for e in events:
        if e.topic == TOPIC_PREDICATE and e.payload.get("predicate_id"):
            preds.add(e.payload["predicate_id"])
        elif e.topic == TOPIC_STATEMENTS:
            stmt_ids += list(e.payload.get("opened") or []) + list(e.payload.get("closed") or [])
        elif e.topic == TOPIC_CAUSAL:
            link_ids += list(e.payload.get("opened") or []) + list(e.payload.get("closed") or [])
    if stmt_ids:
        preds |= {r["predicate_id"] for r in conn.execute(
            "SELECT DISTINCT predicate_id FROM statement WHERE tenant_id = %s AND stmt_id = ANY(%s)",
            (tenant_id, stmt_ids)).fetchall()}
    if link_ids:
        preds |= {r["predicate_id"] for r in conn.execute(
            "SELECT DISTINCT predicate_id FROM causal_link WHERE tenant_id = %s AND link_id = ANY(%s)",
            (tenant_id, link_ids)).fetchall()}
    return preds


class DescriptorBuilder:
    name = "descriptors"
    batch = 1000
    topics = frozenset({TOPIC_ENTITY_VERSION, TOPIC_ENTITY_ACL, TOPIC_STATEMENTS, TOPIC_CAUSAL, TOPIC_PREDICATE})

    def handle(self, conn: psycopg.Connection, tenant_id: str, events: list[OutboxEvent]) -> None:
        with writer_lock(conn, self.name, tenant_id):
            self._handle(conn, tenant_id, events)

    def _handle(self, conn: psycopg.Connection, tenant_id: str, events: list[OutboxEvent]) -> None:
        idx = ensure_indices(conn)
        entity_ids = list(dict.fromkeys(
            e.payload["entity_id"] for e in events
            if e.topic in (TOPIC_ENTITY_VERSION, TOPIC_ENTITY_ACL) and e.payload.get("entity_id")))
        preds = _affected_predicates(conn, tenant_id, events)
        known = {r["predicate_id"] for r in conn.execute(
            "SELECT predicate_id FROM predicate WHERE tenant_id = %s", (tenant_id,)).fetchall()}
        preds |= known - _indexed_predicates(idx, tenant_id)
        actions: list[dict] = []
        for i in range(0, len(entity_ids), 200):
            actions += build_entity_descriptors(conn, tenant_id, entity_ids[i : i + 200])
        actions += build_predicate_descriptors(conn, tenant_id, sorted(preds))
        if actions:
            bulk(actions)
            refresh([idx.descriptors])

    def reset(self, conn: psycopg.Connection, tenant_id: str) -> None:
        idx = ensure_indices(conn)
        with writer_lock(conn, self.name, tenant_id):
            n = delete_tenant([idx.pattern("descriptors")], tenant_id)
        log.info("descriptor index of %s reset (%d docs)", tenant_id, n)


register_consumer(DescriptorBuilder())
