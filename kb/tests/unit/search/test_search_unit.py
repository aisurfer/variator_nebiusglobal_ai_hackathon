"""Unit tests of the search building blocks with the offline hash embedder: no OpenSearch, no PostgreSQL."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from kb.acl import PrincipalCtx
from kb.search import docs as D
from kb.search import query as Q
from kb.search.embeddings import HashEmbedder, cached_embed
from kb.search.opensearch import MAPPING_VERSION, index_body, mappings

T0 = datetime(2026, 3, 1, 10, tzinfo=timezone.utc)
USER = PrincipalCtx("t1", "user:jordan.baird", frozenset({"user:jordan.baird", "grp:all-staff", "grp:flowpilot"}))
ADMIN = PrincipalCtx("t1", "user:admin", frozenset({"user:admin"}), is_admin=True)


# --------------------------------------------------------------------------------------------- embedder
def test_hash_embedder_is_deterministic_unit_and_lexically_similar():
    e = HashEmbedder()
    a, b, c = e.embed_documents(["why did activation drop", "activation dropped in february", "budget review"])
    assert e.embed_documents(["why did activation drop"])[0] == a
    assert e.embed_query("why did activation drop") == a
    assert abs(np.linalg.norm(a) - 1) < 1e-5 and len(a) == e.dim == 256
    assert np.dot(a, b) > np.dot(a, c)
    assert e.name == "hash:v1-256" and e.cacheable is False


def test_cached_embed_skips_the_cache_for_non_cacheable_encoders():
    e = HashEmbedder(64)
    assert cached_embed(None, e, ["a b", "c d"]) == e.embed_documents(["a b", "c d"])  # type: ignore[arg-type]
    assert cached_embed(None, e, []) == []  # type: ignore[arg-type]


def test_get_embedder_follows_settings(monkeypatch):
    from kb import settings as S
    from kb.search import embeddings as E

    monkeypatch.setenv("KB_EMBED_PROVIDER", "hash")
    S.get_settings.cache_clear()
    try:
        e = E.get_embedder()
        assert isinstance(e, HashEmbedder) and E.get_embedder() is e
    finally:
        monkeypatch.delenv("KB_EMBED_PROVIDER")
        S.get_settings.cache_clear()


@pytest.mark.parametrize("env", ["dev", "test", "prod"])
def test_auto_provider_resolves_to_the_local_model(monkeypatch, env):
    from kb import settings as S
    from kb.search import embeddings as E

    monkeypatch.setenv("KB_EMBED_PROVIDER", "auto")
    monkeypatch.setenv("KB_ENV", env)
    monkeypatch.setenv("KB_LLM_API_KEY", "sk-test")  # a key never selects a remote encoder
    S.get_settings.cache_clear()
    S.llm_api_key.cache_clear()
    try:
        assert E.resolved_provider() == "fastembed"
        assert Q.min_cos() == Q.VECTOR_MIN_COS and Q.channel_weights()["lexical"] == 1.0
    finally:
        monkeypatch.undo()
        S.get_settings.cache_clear()
        S.llm_api_key.cache_clear()


# --------------------------------------------------------------------------------------------- fusion
def test_rrf_fuses_ranks():
    fused = Q.rrf({"lexical": ["a", "b", "c"], "vector": ["b", "d", "a"]})
    order = [d for d, _, _ in fused]
    assert order[:2] == ["b", "a"]  # b: 1/62 + 1/61 > a: 1/61 + 1/63
    s = dict((d, sc) for d, sc, _ in fused)
    assert s["b"] == pytest.approx(1 / 62 + 1 / 61) and s["d"] == pytest.approx(1 / 62)
    ranks = {d: r for d, _, r in fused}
    assert ranks["a"] == {"lexical": 1, "vector": 3} and ranks["c"] == {"lexical": 3}


def test_rrf_counts_a_document_once_per_channel_and_breaks_ties():
    fused = Q.rrf({"lexical": ["x", "x", "y"]})
    assert [d for d, _, _ in fused] == ["x", "y"]
    assert fused[0][1] == pytest.approx(1 / 61)
    tie = Q.rrf({"lexical": ["p", "q"], "vector": ["q", "p"]})
    assert tie[0][1] == pytest.approx(tie[1][1]) and [d for d, _, _ in tie] == ["p", "q"]


def test_ground_score_is_calibrated():
    assert Q.ground_score(None, None) == 0.0
    assert Q.ground_score(None, Q.VECTOR_MIN_COS) == 0.0  # at the noise floor the vector adds nothing
    assert Q.ground_score(10.0, None) == pytest.approx(0.25)
    assert Q.ground_score(3.6, 0.63) > Q.ground_score(2.2, 0.58)
    assert Q.ground_score(1e9, 1.0) <= 1.0
    assert Q.cosine_from_score(1.0) == 1.0 and Q.cosine_from_score(0.5) == 0.0


# --------------------------------------------------------------------------------------------- filters
def test_base_filters_apply_tenant_and_acl():
    assert Q.base_filters(USER) == [{"term": {"tenant_id": "t1"}},
                                    {"terms": {"acl": ["grp:all-staff", "grp:flowpilot", "user:jordan.baird"]}}]
    assert Q.base_filters(ADMIN) == [{"term": {"tenant_id": "t1"}}]
    acl = Q.descriptor_acl(USER)[0]["bool"]
    assert {"term": {"doc_type": "predicate"}} in acl["should"] and acl["minimum_should_match"] == 1


def test_chunk_time_filters():
    now = T0 + timedelta(days=200)
    assert Q.chunk_time_filters(None, None, now) == [{"term": {"is_current": True}}]
    k = Q.chunk_time_filters(None, T0, now)
    assert k[0] == {"range": {"recorded_at": {"lte": T0.isoformat()}}}
    should = k[1]["bool"]["should"]
    assert {"range": {"tx_to": {"gt": T0.isoformat()}}} in should
    assert {"bool": {"must_not": {"exists": {"field": "tx_to"}}}} in should
    v = Q.chunk_time_filters(T0, None, now)  # as known at the end of the data, never the wall clock
    assert v == [{"range": {"effective_from": {"lte": T0.isoformat()}}}]
    assert Q.chunk_time_filters(T0, now, now)[0] == {"range": {"recorded_at": {"lte": now.isoformat()}}}


def test_ground_filters():
    f = Q.ground_filters(USER, "entity", ["Experiment"])
    assert {"term": {"doc_type": "entity"}} in f and {"terms": {"kind": ["Experiment"]}} in f
    mixed = Q.ground_filters(ADMIN, None, ["Story"])
    assert mixed[-1]["bool"]["should"] == [{"term": {"doc_type": "predicate"}}, {"terms": {"kind": ["Story"]}}]
    assert Q.ground_filters(ADMIN, "predicate", ["Story"]) == [{"term": {"tenant_id": "t1"}},
                                                               {"term": {"doc_type": "predicate"}}]


def test_knn_body_filters_inside_the_engine():
    body = Q.knn_body([0.1, 0.2], [{"term": {"tenant_id": "t1"}}], 30)
    knn = body["query"]["knn"]["embedding"]
    assert knn["k"] == 30 and knn["filter"] == {"bool": {"filter": [{"term": {"tenant_id": "t1"}}]}}
    assert body["_source"] == {"excludes": ["embedding"]}


# --------------------------------------------------------------------------------------------- keys
@pytest.mark.parametrize("text,want", [
    ("FP-101", "fp-101"), ("MR !42", "!42"), ("merge request 42", "!42"), ("issue 7", "#7"),
    ("what went into v2.4.0?", "v2.4.0"), ("«ADR-014»", "adr-014"), ("flowpilot/app!42", "flowpilot/app!42"),
])
def test_key_tokens(text, want):
    assert want in Q.key_candidates(text)


def test_key_norm_keeps_mr_and_issue_markers():
    assert D.key_norm("!42") == "!42" and D.key_norm("#7.") == "#7" and D.key_norm(" FP-101, ") == "fp-101"
    assert D.key_norm("Café") == "café"
    tokens = Q.key_tokens("FP-101 and FP-102")
    assert tokens["fp-101"] == "FP-101" and "fp-101 and fp-102" in tokens


def test_lexical_ground_body():
    body = Q.lexical_ground("FP-101", [{"term": {"tenant_id": "t1"}}], 20, None)
    should = body["query"]["bool"]["should"]
    names = [c.get("constant_score", {}).get("_name") for c in should]
    assert "key:FP-101" in names and "exact" in names
    nested = [c["nested"]["inner_hits"]["name"] for c in should if "nested" in c]
    assert nested == ["alias_current", "alias_hist"]
    asof = Q.lexical_ground("Project Phoenix", [], 20, T0)
    nested = [c["nested"] for c in asof["query"]["bool"]["should"] if "nested" in c]
    assert [n["inner_hits"]["name"] for n in nested] == ["alias_at", "alias_other"]
    assert nested[0]["boost"] > nested[1]["boost"] and nested[1]["query"]["bool"]["must_not"]


def test_explain_ground():
    hit = {"_source": {"doc_type": "entity"}, "matched_queries": ["key:FP-101", "label"],
           "inner_hits": {"alias_current": {"hits": {"hits": [
               {"_source": {"text": "FP-101", "alias_kind": "key", "current": True}, "matched_queries": ["alias_exact"]}]}}}}
    out, bonus = Q.explain_ground(hit, None)
    assert out[0] == "key FP-101" and "title" in out and bonus == Q.BONUS["key"]
    hist = {"_source": {"doc_type": "entity"}, "matched_queries": [],
            "inner_hits": {"alias_hist": {"hits": {"hits": [
                {"_source": {"text": "Project Phoenix", "alias_kind": "title", "current": False,
                             "valid_to": "2026-07-01T07:00:00+00:00"}, "matched_queries": ["alias_text"]}]}}}}
    out, bonus = Q.explain_ground(hist, None)
    assert out == ["historical alias 'Project Phoenix' until 2026-07-01"] and bonus == 0.0
    at = {"_source": {"doc_type": "entity"}, "matched_queries": [],
          "inner_hits": {"alias_at": {"hits": {"hits": [
              {"_source": {"text": "Project Phoenix", "alias_kind": "title", "current": False,
                           "valid_to": "2026-07-01T07:00:00+00:00"}, "matched_queries": ["alias_exact"]}]}}}}
    out, bonus = Q.explain_ground(at, T0)
    assert "valid at as_of" in out[0] and bonus == Q.BONUS["alias_exact"]
    pred = {"_source": {"doc_type": "predicate"}, "matched_queries": ["exact", "label", "inverse"]}
    out, _ = Q.explain_ground(pred, None)
    assert out == ["exact label", "inverse label"]
    assert Q.explain_ground(None, None) == ([], 0.0)


# --------------------------------------------------------------------------------------------- mappings
def test_mappings():
    m = mappings("chunks", 768)
    p = m["properties"]
    assert m["dynamic"] == "strict"
    assert p["embedding"]["method"]["engine"] == "faiss" and p["embedding"]["space_type"] == "cosinesimil"
    assert p["embedding"]["dimension"] == 768
    assert p["text"]["fields"] == {"en": {"type": "text", "analyzer": "kb_english"}}
    assert p["text"]["analyzer"] == "kb_standard" and p["text"]["search_analyzer"] == "kb_standard_search"
    for f in ("tenant_id", "acl", "entity_id", "kind", "source_id", "section", "chunk_id"):
        assert p[f]["type"] == "keyword"
    for f in ("recorded_at", "tx_to", "effective_from"):
        assert p[f]["type"] == "date"
    assert p["version_from_seq"]["type"] == "integer" and p["is_current"]["type"] == "boolean"
    v = mappings("versions", 768)["properties"]
    assert v["superseded_at"] == {"type": "alias", "path": "tx_to"} and v["version_id"]["type"] == "keyword"
    assert "embedding" not in mappings("entities", None)["properties"]
    d = mappings("descriptors", 64)["properties"]
    assert d["aliases"]["type"] == "nested" and d["stats"] == {"type": "object", "enabled": False}
    with pytest.raises(ValueError):
        mappings("chunks", None)
    body = index_body("chunks", 8)
    analyzers = body["settings"]["analysis"]["analyzer"]
    assert body["settings"]["index"]["knn"] is True and set(analyzers) == {"kb_standard", "kb_standard_search", "kb_english"}
    assert "knn" not in index_body("entities", None)["settings"]["index"]
    assert set(MAPPING_VERSION) == {"chunks", "versions", "entities", "descriptors"}


# --------------------------------------------------------------------------------------------- documents
def _v(seq, op="update", title="T", valid=T0, content=None, body=None, rec=None, sup=None):
    return {"version_id": f"e1@{seq}", "entity_id": "e1", "seq": seq, "op": op, "kind_id": "Policy", "title": title,
            "content": content or {"a": 1, "@relations_hash": "x"}, "body": body, "changed_fields": ["body", "@relations"],
            "valid_from": valid, "recorded_at": rec or valid, "superseded_at": sup, "tx_seq": seq, "actor_id": None}


ENT = {"entity_id": "e1", "kind_id": "Policy", "title": "T2", "source_id": "xwiki-vf", "url": "u", "acl": ["grp:all-staff"],
       "head_seq": 3, "head_version_id": "e1@3", "tombstone_tx": None, "updated_tx": 9}


def test_chunk_doc_interval_and_currency():
    vs = {1: _v(1, "create", "T1", T0), 2: _v(2, title="T1b", valid=T0 - timedelta(days=3)), 3: _v(3, title="T2")}
    closed = {"chunk_id": "e1#1.0", "version_from_seq": 1, "version_to_seq": 2, "tx_from": T0, "tx_to": T0 + timedelta(days=9),
              "ordinal": 0, "section": "S", "text": "x", "char_start": 0, "char_end": 1}
    d = D.chunk_doc("t1", ENT, closed, vs, [0.1], "enc")
    assert d["is_current"] is False and d["title"] == "T1b" and d["version_to_seq"] == 2
    assert d["effective_from"] == (T0 - timedelta(days=3)).isoformat()  # min valid_from over the span (retro edit)
    assert d["tx_to"] == (T0 + timedelta(days=9)).isoformat() and d["embedding"] == [0.1] and d["acl"] == ["grp:all-staff"]
    open_ = dict(closed, chunk_id="e1#3.0", version_from_seq=3, version_to_seq=None, tx_to=None)
    d = D.chunk_doc("t1", ENT, open_, vs, None, None)
    assert d["is_current"] is True and d["tx_to"] is None and "embedding" not in d and d["title"] == "T2"
    gone = dict(ENT, tombstone_tx=10)
    assert D.chunk_doc("t1", gone, open_, vs, None, None)["is_current"] is False
    assert D.chunk_embed_text(closed, "T1") == "T1\nS\nx"


def test_version_doc_and_text():
    v = _v(2, body="Version body", content={"budget": 80, "tags": ["a", "b"], "@relations_hash": "h"})
    d = D.version_doc("t1", ENT, v, [0.2], "enc")
    assert d["is_current"] is True and d["changed_fields"] == ["body"] and d["tx_to"] is None
    assert "budget: 80" in d["text"] and "tags: a, b" in d["text"] and "@relations" not in d["text"]
    assert "changed: body" in d["text"] and "Version body" in d["text"]
    closed = D.version_doc("t1", ENT, dict(v, superseded_at=T0), None, None)
    assert closed["is_current"] is False and closed["tx_to"] == T0.isoformat()
    tomb = D.version_doc("t1", ENT, dict(v, op="delete", content=None, body=None), None, None)
    assert "deleted" in tomb["text"] and tomb["op"] == "delete"


def test_flatten_fields_bounds_and_skips_internal():
    assert D.flatten_fields({"@x": 1, "a": None, "b": True, "c": {"d": [1, 2]}}) == "b: yes\nc: d: 1, 2"
    assert len(D.flatten_fields({f"f{i}": "x" * 100 for i in range(100)}, 500)) <= 500


def test_alias_entries_and_entity_descriptor():
    rows = [
        {"alias": "FP-120", "alias_norm": "fp-120", "alias_kind": "key", "valid_from": T0, "valid_to": None, "current": True},
        {"alias": "Project Phoenix", "alias_norm": "project phoenix", "alias_kind": "title", "valid_from": T0,
         "valid_to": T0 + timedelta(days=120), "current": False},
        {"alias": "Guided setup", "alias_norm": "guided setup", "alias_kind": "title", "valid_from": T0 + timedelta(days=120),
         "valid_to": None, "current": True},
        {"alias": "Guided setup", "alias_norm": "guided setup", "alias_kind": "title", "valid_from": T0 + timedelta(days=120),
         "valid_to": None, "current": True},  # same alias from a second origin
    ]
    entries = D.alias_entries(rows)
    assert len(entries) == 3 and entries[0]["current"] and not entries[-1]["current"]
    ent = dict(ENT, entity_id="jira.FP-120", kind_id="Story", title="Guided setup", tombstone_tx=None)
    doc = D.entity_descriptor("t1", ent, "User story", {"content": {"status": "Open"}, "body": "# H\nText"},
                              {"valid_from": T0}, None, rows)
    assert doc["doc_type"] == "entity" and doc["labels"] == ["Guided setup"]
    assert set(doc["keys_norm"]) == {"fp-120", "jira.fp-120"} and doc["labels_norm"] == ["guided setup"]
    assert doc["description"].startswith("User story (Story). status: Open") and doc["deleted"] is False
    assert "Project Phoenix" in [a["text"] for a in doc["aliases"] if not a["current"]]
    assert "Guided setup" in D.entity_descriptor_embed_text(doc)
    gone = D.entity_descriptor("t1", dict(ent, tombstone_tx=5), None, None, {"valid_from": T0}, T0, [])
    assert gone["deleted"] is True and gone["deleted_at"] == T0.isoformat()


def test_predicate_descriptor_is_schema_level():
    pred = {"predicate_id": "based_on", "label": "built on", "inverse_label": "foundation for",
            "aliases": ["based on", "builds on"], "description": "The object is built on the result of another object.",
            "causal_kind": "based_on", "typical_sensitivity": None, "domain_kinds": ["Experiment"],
            "range_kinds": ["Release"], "status": "active", "canonical_predicate_id": None}
    stats = {"usage_count": 3, "causal_links": {"endpoint_kinds": {"Experiment→Release": 3}}}
    d = D.predicate_descriptor("t1", pred, stats)
    assert d["acl"] == [] and d["stats"] is stats and d["kind"] == "predicate"
    assert d["keys_norm"] == ["based_on"] and {"built on", "based on", "foundation for"} <= set(d["labels_norm"])
    assert "Experiment → Release" in d["description"] and d["causal_kind"] == "based_on"
    text = D.predicate_embed_text(d)
    assert "built on" in text and "3" not in text  # counts never reach the embedded/scored text


def test_bulk_waits_out_a_rejection_of_the_cluster(monkeypatch):
    """429 rejected_execution under indexing pressure (eight parts writing at once): the chunk is sent again."""
    from opensearchpy.exceptions import TransportError

    from kb.search import opensearch as O

    calls = []

    from types import SimpleNamespace

    from opensearchpy.serializer import JSONSerializer

    class Fake:
        transport = SimpleNamespace(serializer=JSONSerializer())

        def bulk(self, body, **kw):
            calls.append(body)
            if len(calls) == 1:
                raise TransportError(429, "rejected_execution_exception", {})
            n = body.count("\n") // 2
            return {"errors": False, "items": [{"index": {"status": 201}} for _ in range(n)]}

    monkeypatch.setattr(O, "client", lambda: Fake())
    monkeypatch.setattr(O, "BULK_BACKOFF_S", 0.01)
    acts = [{"_op_type": "index", "_index": "i", "_id": str(k), "_source": {"k": k}} for k in range(3)]
    assert O.bulk(acts) == 3 and len(calls) == 2
