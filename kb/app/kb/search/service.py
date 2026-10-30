"""Public search API used by the REST routes and the reasoning layer (K08/K09).

- search(): hybrid (BM25 + kNN, RRF k=60) over chunks in a (valid_at, known_at) slice; every hit names the exact
  version that contained the chunk in that slice (kb.journal.reader.version_at);
- search_versions(): hybrid over version documents on the transaction axis (degradation mode 3, "what changed");
- ground(): one hybrid query over the descriptor index for entities and predicates (exact keys strongly boosted,
  historical aliases honoured with as_of); ambiguity stays visible: several scored candidates;
- consistency_sample(): journal vs kb-versions agreement on random (entity, K) pairs (research §5.7).

ACL: both retrievers run inside the principal's filter (tenant + acl terms) before scoring, so hidden documents
never take result slots, never produce snippets and are never counted; results are re-checked against the
current entity ACL in PostgreSQL (defence against a lagging projection).
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import psycopg

from kb.acl import PrincipalCtx
from kb.journal import reader
from kb.search import query as Q
from kb.search.embeddings import get_embedder
from kb.search.opensearch import client, ensure_indices

log = logging.getLogger(__name__)
Mode = Literal["hybrid", "lexical", "vector"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _visible(conn: psycopg.Connection, p: PrincipalCtx, entity_ids: list[str]) -> dict[str, dict]:
    """Current entity rows the principal may see (PG is the authority for ACL)."""
    if not entity_ids:
        return {}
    rows = conn.execute(
        "SELECT entity_id, kind_id, title, url, acl, tombstone_tx, head_version_id FROM entity "
        "WHERE tenant_id = %s AND entity_id = ANY(%s)",
        (p.tenant_id, list(set(entity_ids))),
    ).fetchall()
    return {r["entity_id"]: r for r in rows if p.can_see(r["acl"])}


def _run(index: str, q: str, filters: list[dict], size: int, mode: Mode, lexical_body) -> tuple[dict, dict, dict]:
    """Run the requested retrievers. Returns (fused order input per channel, lexical hits by id, vector hits by id)."""
    os = client()
    lex: dict[str, dict] = {}
    vec: dict[str, dict] = {}
    channels: dict[str, list[str]] = {}
    if mode in ("hybrid", "lexical"):
        res = os.search(index=index, body=lexical_body)
        hits = res["hits"]["hits"]
        lex = {h["_id"]: h for h in hits}
        channels["lexical"] = [h["_id"] for h in hits]
    if mode in ("hybrid", "vector"):
        v = get_embedder().embed_query(q)
        res = os.search(index=index, body=Q.knn_body(v, filters, size))
        floor = Q.min_cos()
        hits = [h for h in res["hits"]["hits"] if Q.cosine_from_score(h["_score"]) >= floor]
        vec = {h["_id"]: h for h in hits}
        channels["vector"] = [h["_id"] for h in hits]
    return channels, lex, vec


def _channels_out(ranks: dict[str, int], lex: dict | None, vec: dict | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "lexical" in ranks and lex is not None:
        out["lexical"] = {"rank": ranks["lexical"], "score": round(lex["_score"], 4)}
    if "vector" in ranks and vec is not None:
        out["vector"] = {"rank": ranks["vector"], "similarity": Q.cosine_from_score(vec["_score"])}
    return out


def _known_end(tx_to: str | None, known_at: datetime | None) -> str | None:
    """The end of a version's interval as known at K (an end recorded after K is not known)."""
    if tx_to is None or known_at is None:
        return tx_to
    return None if datetime.fromisoformat(tx_to) > known_at else tx_to


def _snippet(hit: dict | None, text: str, n: int = 240) -> str:
    if hit is not None:
        for frags in (hit.get("highlight") or {}).values():
            if frags:
                return frags[0]
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: t.rfind(" ", 0, n) if t.rfind(" ", 0, n) > n // 2 else n] + "…"


# --------------------------------------------------------------------------------------------- chunks
def search(
    conn: psycopg.Connection, p: PrincipalCtx, q: str, *, kinds: list[str] | None = None, mode: Mode = "hybrid",
    valid_at: datetime | None = None, known_at: datetime | None = None, limit: int = 20, per_entity: int = 1,
    entity_ids: list[str] | None = None,
) -> list[dict]:
    """Hybrid search over chunks (within `entity_ids` when given: ranking inside a set chosen by a query plan). Without known_at/valid_at only current chunks; with known_at K the chunks whose
    [recorded_at, tx_to) contains K (text edited away later is still found as of K); with valid_at T the chunks of
    the version effective at T as known at K. Each hit carries the exact version_id that contained it."""
    if not q or not q.strip():
        return []
    if mode not in ("hybrid", "lexical", "vector"):
        raise ValueError("mode must be hybrid, lexical or vector")
    limit = max(1, min(limit, 100))
    per_entity = max(1, per_entity)
    idx = ensure_indices(conn)
    filters = Q.base_filters(p) + Q.chunk_time_filters(valid_at, known_at, _now())
    if kinds:
        filters.append({"terms": {"kind": list(kinds)}})
    if entity_ids is not None:
        if not entity_ids:
            return []
        filters.append({"terms": {"entity_id": list(entity_ids)}})
    size = min(max(limit * per_entity * 4, 40), 400)
    channels, lex, vec = _run(idx.chunks, q, filters, size, mode, Q.lexical_chunks(q, filters, size))
    fused = Q.rrf(channels, weights=Q.channel_weights())
    docs = {**{k: h["_source"] for k, h in vec.items()}, **{k: h["_source"] for k, h in lex.items()}}
    visible = _visible(conn, p, [d["entity_id"] for d in docs.values()])

    hits: list[dict] = []
    per: dict[str, int] = {}
    resolved: dict[str, dict | None] = {}
    for _id, score, ranks in fused:
        d = docs[_id]
        eid = d["entity_id"]
        if eid not in visible or per.get(eid, 0) >= per_entity:
            continue
        if eid not in resolved:
            resolved[eid] = reader.version_at(conn, p, eid, valid_at=valid_at, known_at=known_at)
        v = resolved[eid]
        if v is None or v["op"] == "delete":
            continue
        to = d.get("version_to_seq")
        if not (d["version_from_seq"] <= v["seq"] and (to is None or v["seq"] <= to)):
            continue  # the chunk was not part of the state in this slice (retroactive edits)
        per[eid] = per.get(eid, 0) + 1
        sup = v.get("superseded_at")
        if known_at is not None and sup is not None and sup > known_at:
            sup = None  # superseded after K: current as known at K
        hits.append({
            "entity_id": eid,
            "kind": d["kind"],
            "title": v["title"] or d.get("title"),
            "chunk_id": d["chunk_id"],
            "section": d.get("section"),
            "snippet": _snippet(lex.get(_id), d["text"]),
            "text": d["text"],
            "locator": {"char_start": d.get("char_start"), "char_end": d.get("char_end")},
            "score": round(score, 6),
            "channels": _channels_out(ranks, lex.get(_id), vec.get(_id)),
            "version_id": v["version_id"],
            "recorded_at": v["recorded_at"],
            "superseded_at": sup,
            "effective_from": v["valid_from"],
            "is_current": bool(d.get("is_current")) if known_at is None else sup is None,
            "url": visible[eid]["url"] or d.get("url"),
        })
        if len(hits) >= limit:
            break
    return hits


# --------------------------------------------------------------------------------------------- versions
def search_versions(
    conn: psycopg.Connection, p: PrincipalCtx, q: str, *, start: datetime | None = None, end: datetime | None = None,
    kinds: list[str] | None = None, limit: int = 20, mode: Mode = "hybrid", known_at: datetime | None = None,
) -> list[dict]:
    """Hybrid search over version documents recorded (tx axis) in [start, end); as known at K only versions recorded
    by K, and a supersession after K is not known."""
    if not q or not q.strip():
        return []
    limit = max(1, min(limit, 100))
    idx = ensure_indices(conn)
    filters = Q.base_filters(p)
    rng: dict[str, str] = {}
    if start is not None:
        rng["gte"] = Q.iso(start)  # type: ignore[assignment]
    if end is not None:
        rng["lt"] = Q.iso(end)  # type: ignore[assignment]
    if known_at is not None:
        rng["lte"] = Q.iso(known_at)  # type: ignore[assignment]
    if rng:
        filters.append({"range": {"recorded_at": rng}})
    if kinds:
        filters.append({"terms": {"kind": list(kinds)}})
    size = min(max(limit * 3, 30), 300)
    body = Q.lexical_chunks(q, filters, size)
    body["query"]["bool"]["should"] = body["query"]["bool"]["should"][:2]  # versions have no headings
    channels, lex, vec = _run(idx.versions, q, filters, size, mode, body)
    docs = {**{k: h["_source"] for k, h in vec.items()}, **{k: h["_source"] for k, h in lex.items()}}
    visible = _visible(conn, p, [d["entity_id"] for d in docs.values()])
    out = []
    for _id, score, ranks in Q.rrf(channels):
        d = docs[_id]
        if d["entity_id"] not in visible:
            continue
        out.append({
            "version_id": d["version_id"],
            "entity_id": d["entity_id"],
            "seq": d["seq"],
            "op": d["op"],
            "kind": d["kind"],
            "title": d.get("title"),
            "snippet": _snippet(lex.get(_id), d.get("text") or ""),
            "changed_fields": d.get("changed_fields") or [],
            "actor_id": d.get("actor_id"),
            "recorded_at": d.get("recorded_at"),
            "superseded_at": _known_end(d.get("tx_to"), known_at),
            "effective_from": d.get("effective_from"),
            "is_current": bool(d.get("is_current")) if known_at is None else _known_end(d.get("tx_to"), known_at) is None,
            "score": round(score, 6),
            "channels": _channels_out(ranks, lex.get(_id), vec.get(_id)),
            "url": visible[d["entity_id"]]["url"] or d.get("url"),
        })
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------------------------- grounding
def ground(
    conn: psycopg.Connection, p: PrincipalCtx, text: str, *, kind: Literal["entity", "predicate"] | None = None,
    entity_kinds: list[str] | None = None, as_of: datetime | None = None, limit: int = 5,
) -> list[dict]:
    """Resolve a mention to entity/predicate candidates through one hybrid query over kb-descriptors.

    Exact keys (FP-101, !42, v2.4.0, ADR-014) and exact names get a bonus above any fusion score; BM25 runs on
    labels/aliases with the standard and English analysers; kNN on descriptor embeddings. The score is a calibrated fusion
    (query.ground_score), not RRF, so gaps between candidates are meaningful. `as_of` makes aliases valid at that
    time count fully (historical names) and down-weights the others. Candidates keep their scores so a near tie
    (ambiguity) is visible to the caller."""
    if not text or not text.strip():
        return []
    if kind not in (None, "entity", "predicate"):
        raise ValueError("kind must be entity, predicate or None")
    limit = max(1, min(limit, 50))
    idx = ensure_indices(conn)
    filters = Q.ground_filters(p, kind, entity_kinds)
    size = max(limit * 4, 20)
    channels, lex, vec = _run(idx.descriptors, text, filters, size, "hybrid",
                              Q.lexical_ground(text, filters, size, as_of))
    docs = {**{k: h["_source"] for k, h in vec.items()}, **{k: h["_source"] for k, h in lex.items()}}
    ent_ids = [d["ref_id"] for d in docs.values() if d["doc_type"] == "entity"]
    visible = _visible(conn, p, ent_ids)
    # as of a date only the entities that existed then, with their names and deletion then (Docs/KB/AS-OF.md)
    heads = reader.heads_at(conn, p.tenant_id, ent_ids, as_of) if as_of is not None else None
    scored = []
    for _id, _, ranks in Q.rrf(channels):
        d = docs[_id]
        if d["doc_type"] == "entity" and d["ref_id"] not in visible:
            continue
        if heads is not None and d["doc_type"] == "entity":
            h = heads.get(d["ref_id"])
            if h is None:
                continue
            d = {**d, "label": h["title"] or d["label"], "deleted": h["tombstone_tx"] is not None,
                 "deleted_at": None if h["tombstone_tx"] is None else d.get("deleted_at")}
        matched, bonus = Q.explain_ground(lex.get(_id), as_of)
        if "vector" in ranks:
            matched.append("semantic")
        lh, vh = lex.get(_id), vec.get(_id)
        total = Q.ground_score(lh["_score"] if lh else None,
                               Q.cosine_from_score(vh["_score"]) if vh else None) + bonus
        deleted = bool(d.get("deleted"))
        if deleted and (as_of is None or not d.get("deleted_at") or as_of >= datetime.fromisoformat(d["deleted_at"])):
            total *= 0.6
            matched.append("deleted")
        c: dict[str, Any] = {
            "id": d["ref_id"],
            "doc_type": d["doc_type"],
            "kind": d["kind"],
            "label": d["label"],
            "score": round(total, 6),
            "matched": matched,
            "explanation": "; ".join(matched),
            "channels": _channels_out(ranks, lex.get(_id), vec.get(_id)),
        }
        if d["doc_type"] == "predicate":
            c.update({"causal_kind": d.get("causal_kind"), "inverse_label": d.get("inverse_label"),
                      "description": d.get("description"), "status": d.get("status"),
                      "canonical_predicate_id": d.get("canonical_predicate_id"),
                      "typical_sensitivity": d.get("typical_sensitivity") or [],
                      "domain_kinds": d.get("domain_kinds") or [], "range_kinds": d.get("range_kinds") or []})
            if p.is_admin:
                c["stats"] = d.get("stats")
        else:
            c.update({"description": d.get("description"), "url": visible[d["ref_id"]]["url"], "deleted": deleted,
                      "deleted_at": d.get("deleted_at")})
        scored.append(c)
    scored.sort(key=lambda c: -c["score"])
    return scored[:limit]


def is_ambiguous(candidates: list[dict], ratio: float = 0.85) -> bool:
    """Two leading candidates with close scores and no decisive exact match on the first one."""
    if len(candidates) < 2:
        return False
    a, b = candidates[0], candidates[1]
    decisive = any(m.startswith(("key ", "exact ")) for m in a["matched"]) and not any(
        m.startswith(("key ", "exact ")) for m in b["matched"])
    return not decisive and b["score"] >= ratio * a["score"]


# --------------------------------------------------------------------------------------------- consistency
def consistency_sample(conn: psycopg.Connection, p: PrincipalCtx, samples: int = 20, *, seed: int | None = None) -> dict:
    """Pick random (entity, K) pairs; compare the journal's version as known at K with kb-versions' answer."""
    idx = ensure_indices(conn)
    rng = random.Random(seed)
    acl_sql, acl_params = p.sql_filter("e.acl")
    rows = conn.execute(
        f"""SELECT v.entity_id, v.version_id, lower(v.tx) AS t0, upper(v.tx) AS t1 FROM entity_version v
            JOIN entity e ON e.tenant_id = v.tenant_id AND e.entity_id = v.entity_id
            WHERE v.tenant_id = %s AND {acl_sql}
            ORDER BY random() LIMIT %s""",
        (p.tenant_id, *acl_params, max(1, samples)),
    ).fetchall()
    os = client()
    checked, mismatches = [], []
    t_start = time.perf_counter()
    for r in rows:
        hi = r["t1"] or _now()
        span = max((hi - r["t0"]).total_seconds(), 0.0)
        k = r["t0"] + timedelta(seconds=rng.random() * span) if span > 0 else r["t0"]
        j = reader.version_at(conn, p, r["entity_id"], known_at=k)
        res = os.search(index=idx.versions, body={
            "size": 2, "_source": ["version_id"],
            "query": {"bool": {"filter": [*Q.base_filters(p), {"term": {"entity_id": r["entity_id"]}},
                                          *Q.open_at("recorded_at", "tx_to", k)]}},
        })
        found = [h["_source"]["version_id"] for h in res["hits"]["hits"]]
        jv = j["version_id"] if j else None
        item = {"entity_id": r["entity_id"], "known_at": k, "journal": jv, "search": found[0] if len(found) == 1 else found}
        checked.append(item)
        if [jv] != found:
            mismatches.append(item)
    return {"samples": len(checked), "matched": len(checked) - len(mismatches), "mismatches": mismatches,
            "ok": not mismatches, "took_ms": round((time.perf_counter() - t_start) * 1000, 1)}
