"""OpenSearch plumbing for the search projection (KB-3): client, mappings, physical indices behind aliases.

Logical indices (01 §6): chunks, versions, entities, descriptors. Each lives in a physical index
`<prefix>-<logical>-v<n>` behind the alias `<prefix>-<logical>`. The physical index is chosen by
(encoder, dim, mapping_version) through `index_registry`: vectors of different encoders or dimensions never share
an index. A new encoder or mapping version gets a new physical index (n+1) and the alias is swapped to it
atomically; the old index stays registered (inactive) until an operator drops it. Readers and writers of a process
always use the physical index of the process's own encoder, so two processes with different encoders never write
mixed vectors. After a switch every tenant must be re-projected (`framework.reset` + `drain`).

Indices are shared by all tenants: every document carries `tenant_id` and every query filters on it, plus `acl`.
"""
from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

import psycopg
from opensearchpy import OpenSearch, helpers
from opensearchpy.exceptions import NotFoundError, RequestError

from kb.settings import get_settings

log = logging.getLogger(__name__)

LOGICAL = ("chunks", "versions", "entities", "descriptors")
VECTOR_INDICES = frozenset({"chunks", "versions", "descriptors"})
# v2: query-time stop words on the standard sub-field (index side keeps every token for phrase/marker queries)
# descriptors v3: kind_label (a query word naming a kind is a type hint for grounding)
# chunks/versions/entities v3, descriptors v4: English only (the Russian analyzer and `.ru` sub-fields were removed)
MAPPING_VERSION = {"chunks": 3, "versions": 3, "entities": 3, "descriptors": 4}
_LOCK_KEY = 727083  # pg advisory lock serialising index creation across processes

_client: OpenSearch | None = None
_client_lock = threading.Lock()
_ensured: dict[tuple[str, str, int], "Indices"] = {}
_ensure_lock = threading.Lock()


def client() -> OpenSearch:
    global _client
    with _client_lock:
        if _client is None:
            # one client per process, shared by the consumers of a parallel drain (T07 A7): a connection each
            _client = OpenSearch(hosts=[get_settings().os_url], timeout=30, max_retries=2, retry_on_timeout=True,
                                 http_compress=get_settings().os_http_compress, pool_maxsize=8)
        return _client


# --------------------------------------------------------------------------------------------- mappings
ANALYSIS: dict[str, Any] = {
    "filter": {
        "en_stop": {"type": "stop", "stopwords": "_english_"},
        "en_stem": {"type": "stemmer", "language": "english"},
        "en_possessive": {"type": "stemmer", "language": "possessive_english"},
    },
    "analyzer": {
        # `standard`: language-neutral tokens (keys, names); `english`: stemmed, stop words
        "kb_standard": {"type": "custom", "tokenizer": "standard", "filter": ["lowercase"]},
        # free-text queries drop English function words ("of", "the"); phrase queries may pass analyzer=kb_standard
        "kb_standard_search": {"type": "custom", "tokenizer": "standard", "filter": ["lowercase", "en_stop"]},
        "kb_english": {"type": "custom", "tokenizer": "standard",
                       "filter": ["en_possessive", "lowercase", "en_stop", "en_stem"]},
    },
}


def _text() -> dict[str, Any]:
    return {"type": "text", "analyzer": "kb_standard", "search_analyzer": "kb_standard_search",
            "fields": {"en": {"type": "text", "analyzer": "kb_english"}}}


def _kw() -> dict[str, Any]:
    return {"type": "keyword"}


def _knn(dim: int) -> dict[str, Any]:
    # faiss HNSW supports efficient (pre-)filtering: the ACL/tenant/time filter is applied during the graph search,
    # so k results come from the visible set and hidden documents never take result slots.
    return {"type": "knn_vector", "dimension": dim, "space_type": "cosinesimil",
            "method": {"name": "hnsw", "engine": "faiss", "parameters": {"m": 16, "ef_construction": 128}}}


def _base() -> dict[str, Any]:
    return {"tenant_id": _kw(), "acl": _kw(), "entity_id": _kw(), "kind": _kw(), "source_id": _kw(),
            "url": {"type": "keyword", "index": False}, "updated_tx": {"type": "long"}}


def mappings(logical: str, dim: int | None) -> dict[str, Any]:
    p = _base()
    if logical == "chunks":
        p.update({
            "chunk_id": _kw(), "section": _kw(), "heading": _text(), "title": _text(), "text": _text(),
            "ordinal": {"type": "integer"}, "char_start": {"type": "integer", "index": False},
            "char_end": {"type": "integer", "index": False},
            "version_from_seq": {"type": "integer"}, "version_to_seq": {"type": "integer"},
            "recorded_at": {"type": "date"}, "tx_to": {"type": "date"}, "effective_from": {"type": "date"},
            "is_current": {"type": "boolean"}, "encoder": _kw(),
        })
    elif logical == "versions":
        p.update({
            "version_id": _kw(), "seq": {"type": "integer"}, "op": _kw(), "title": _text(), "text": _text(),
            "changed_fields": _kw(), "actor_id": _kw(),
            "recorded_at": {"type": "date"}, "tx_to": {"type": "date"},
            "superseded_at": {"type": "alias", "path": "tx_to"}, "effective_from": {"type": "date"},
            "is_current": {"type": "boolean"}, "encoder": _kw(),
        })
    elif logical == "entities":
        p.update({
            "title": _text(), "text": _text(), "keys": _kw(), "head_version_id": _kw(), "head_seq": {"type": "integer"},
            "recorded_at": {"type": "date"}, "effective_from": {"type": "date"}, "created_at": {"type": "date"},
        })
    elif logical == "descriptors":
        p.update({
            "doc_type": _kw(), "ref_id": _kw(), "label": _text(), "labels": _text(), "labels_norm": _kw(),
            "kind_label": _text(),
            "keys_norm": _kw(), "inverse_label": _text(), "description": _text(),
            "aliases": {"type": "nested", "properties": {
                "text": _text(), "norm": _kw(), "alias_kind": _kw(), "valid_from": {"type": "date"},
                "valid_to": {"type": "date"}, "current": {"type": "boolean"}}},
            "causal_kind": _kw(), "status": _kw(), "canonical_predicate_id": _kw(), "typical_sensitivity": _kw(),
            "domain_kinds": _kw(), "range_kinds": _kw(), "deleted": {"type": "boolean"},
            "exists_from": {"type": "date"}, "deleted_at": {"type": "date"}, "encoder": _kw(),
            "stats": {"type": "object", "enabled": False},
        })
    else:  # pragma: no cover
        raise ValueError(logical)
    if logical in VECTOR_INDICES:
        if not dim:
            raise ValueError(f"{logical} needs a vector dimension")
        p["embedding"] = _knn(dim)
    return {"dynamic": "strict", "properties": p}


def index_body(logical: str, dim: int | None) -> dict[str, Any]:
    settings: dict[str, Any] = {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "1s"}
    if logical in VECTOR_INDICES:
        settings["knn"] = True
    return {"settings": {"index": settings, "analysis": ANALYSIS}, "mappings": mappings(logical, dim)}


# --------------------------------------------------------------------------------------------- registry
@dataclass(frozen=True)
class Indices:
    """Physical index names (the process's encoder) and aliases of the four logical indices."""

    chunks: str
    versions: str
    entities: str
    descriptors: str
    encoder: str
    dim: int

    def alias(self, logical: str) -> str:
        return f"{get_settings().os_index_prefix}-{logical}"

    def pattern(self, logical: str) -> str:
        """All physical generations of a logical index (for tenant-wide deletes)."""
        return f"{self.alias(logical)}-v*"

    def all(self) -> list[str]:
        return [self.chunks, self.versions, self.entities, self.descriptors]


_VER = re.compile(r"-v(\d+)$")


@contextmanager
def _advisory(conn: psycopg.Connection) -> Iterator[None]:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        yield


def _create_index(name: str, logical: str, dim: int | None) -> bool:
    c = client()
    if c.indices.exists(index=name):
        return False
    try:
        c.indices.create(index=name, body=index_body(logical, dim))
        return True
    except RequestError as exc:  # another process won the race
        if "resource_already_exists_exception" in str(exc):
            return False
        raise


def _alias_targets(alias: str) -> list[str]:
    try:
        return sorted(client().indices.get_alias(name=alias).keys())
    except NotFoundError:
        return []


def _point_alias(alias: str, name: str) -> None:
    targets = _alias_targets(alias)
    if targets == [name]:
        return
    actions: list[dict] = [{"remove": {"index": t, "alias": alias}} for t in targets if t != name]
    actions.append({"add": {"index": name, "alias": alias, "is_write_index": True}})
    client().indices.update_aliases(body={"actions": actions})
    if targets:
        log.warning("alias %s moved %s -> %s: re-project every tenant (framework.reset + drain)", alias, targets, name)


def ensure_indices(conn: psycopg.Connection | None = None, embedder: Any = None) -> Indices:
    """Create (once per process, race-safe) the physical indices of the current encoder and their aliases."""
    from kb.search.embeddings import get_embedder

    e = embedder or get_embedder()
    prefix = get_settings().os_index_prefix
    key = (prefix, e.name, e.dim)
    with _ensure_lock:
        hit = _ensured.get(key)
        if hit is not None:
            return hit
        if conn is None:
            from kb.db import connect

            with connect() as c2:
                out = _ensure(c2, prefix, e.name, e.dim)
        else:
            out = _ensure(conn, prefix, e.name, e.dim)
        _ensured[key] = out
        return out


def _ensure(conn: psycopg.Connection, prefix: str, encoder: str, dim: int) -> Indices:
    names: dict[str, str] = {}
    with _advisory(conn):
        for logical in LOGICAL:
            alias = f"{prefix}-{logical}"
            enc, d = (encoder, dim) if logical in VECTOR_INDICES else (None, None)
            mv = MAPPING_VERSION[logical]
            rows = conn.execute(
                "SELECT index_name, encoder, dim, mapping_version, active FROM index_registry WHERE alias = %s",
                (alias,),
            ).fetchall()
            match = [r for r in rows if (r["encoder"], r["dim"], r["mapping_version"]) == (enc, d, mv)]
            if match:
                name = match[0]["index_name"]
            else:
                gens = [int(m.group(1)) for r in rows if (m := _VER.search(r["index_name"]))]
                name = f"{alias}-v{max(gens, default=0) + 1}"
                conn.execute(
                    """INSERT INTO index_registry (index_name, alias, encoder, dim, mapping_version, active)
                       VALUES (%s, %s, %s, %s, %s, false) ON CONFLICT (index_name) DO NOTHING""",
                    (name, alias, enc, d, mv),
                )
            created = _create_index(name, logical, d)
            targets = _alias_targets(alias)
            if created or not targets or not match:
                _point_alias(alias, name)
                conn.execute("UPDATE index_registry SET active = (index_name = %s) WHERE alias = %s", (name, alias))
            elif targets != [name]:
                log.info("alias %s points to %s; this process (%s) uses %s", alias, targets, encoder, name)
            names[logical] = name
    return Indices(names["chunks"], names["versions"], names["entities"], names["descriptors"], encoder, dim)


# --------------------------------------------------------------------------------------------- writes
@contextmanager
def writer_lock(conn: psycopg.Connection, consumer: str, tenant_id: str) -> Iterator[None]:
    """Serialise the writers of one (consumer, tenant): a projector re-reads PostgreSQL only after the previous
    writer has finished writing, so a slower concurrent drain can never overwrite a newer state. A session-level
    advisory lock (no open transaction: it must not hold back the outbox xmin of other tenants)."""
    key = f"kb.search:{consumer}:{tenant_id}"
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))


class BulkError(Exception):
    pass


BULK_RETRIES = 8  # a 429 (rejected_execution under indexing pressure: many writers at once) waits and retries
BULK_BACKOFF_S = 1.0  # doubled per retry, up to 30 s
BULK_CHUNK_BYTES = 10 * 1024 * 1024  # a request well under the cluster's in-flight indexing limit (10% of heap)


def bulk(actions: Iterable[dict[str, Any]], *, refresh: bool = False) -> int:
    """Bulk write. Deletes of missing documents are expected and ignored; anything else raises."""
    acts = list(actions)
    if not acts:
        return 0
    ok, errors = helpers.bulk(client(), acts, raise_on_error=False, raise_on_exception=True, refresh=refresh,
                              request_timeout=120, max_retries=BULK_RETRIES, initial_backoff=BULK_BACKOFF_S,
                              max_backoff=30, max_chunk_bytes=BULK_CHUNK_BYTES)
    real = []
    for err in errors or []:
        (op, info), = err.items()
        status = info.get("status")
        if op == "delete" and status == 404:
            continue
        real.append(err)
    if real:
        raise BulkError(f"{len(real)} bulk errors, first: {real[0]}")
    return ok


def refresh(names: Iterable[str]) -> None:
    client().indices.refresh(index=",".join(names), ignore_unavailable=True)


def delete_tenant(index_patterns: Iterable[str], tenant_id: str, extra: list[dict] | None = None) -> int:
    """delete_by_query of the tenant's documents in every physical generation of the given logical indices."""
    q: dict[str, Any] = {"bool": {"filter": [{"term": {"tenant_id": tenant_id}}, *(extra or [])]}}
    res = client().delete_by_query(index=",".join(index_patterns), body={"query": q}, conflicts="proceed",
                                   refresh=True, allow_no_indices=True, ignore_unavailable=True, request_timeout=300)
    return int(res.get("deleted", 0))
