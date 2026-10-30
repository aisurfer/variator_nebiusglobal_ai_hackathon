"""Projection invariants (reindex reproduces the indices), REST endpoints and measured latency."""
from __future__ import annotations

import statistics
import time

import pytest

from kb.search import service
from kb.search.opensearch import client, ensure_indices
from kb.worker import framework
from tests.integration.search.corpus import SEARCH_CONSUMERS, apply, at, drain_search, mk

pytestmark = pytest.mark.integration


def os_counts(db, tenant: str) -> dict[str, int]:
    idx = ensure_indices(db)
    os = client()

    def n(index: str, *flt: dict) -> int:
        return os.count(index=index, body={"query": {"bool": {"filter": [{"term": {"tenant_id": tenant}}, *flt]}}})["count"]

    return {
        "chunks": n(idx.chunks), "chunks_current": n(idx.chunks, {"term": {"is_current": True}}),
        "versions": n(idx.versions), "versions_current": n(idx.versions, {"term": {"is_current": True}}),
        "entities": n(idx.entities),
        "descriptors_entity": n(idx.descriptors, {"term": {"doc_type": "entity"}}),
        "descriptors_predicate": n(idx.descriptors, {"term": {"doc_type": "predicate"}}),
    }


def pg_counts(db, tenant: str) -> dict[str, int]:
    q = lambda sql: db.execute(sql, (tenant,)).fetchone()["n"]  # noqa: E731
    return {
        "chunks": q("SELECT count(*) AS n FROM chunk WHERE tenant_id = %s"),
        "chunks_current": q("""SELECT count(*) AS n FROM chunk c JOIN entity e USING (tenant_id, entity_id)
                               WHERE c.tenant_id = %s AND c.version_to_seq IS NULL AND e.tombstone_tx IS NULL"""),
        "versions": q("SELECT count(*) AS n FROM entity_version WHERE tenant_id = %s"),
        "versions_current": q("""SELECT count(*) AS n FROM entity_version v JOIN entity e USING (tenant_id, entity_id)
                                 WHERE v.tenant_id = %s AND upper_inf(v.tx) AND e.tombstone_tx IS NULL"""),
        "entities": q("SELECT count(*) AS n FROM entity WHERE tenant_id = %s AND tombstone_tx IS NULL"),
        "descriptors_entity": q("SELECT count(*) AS n FROM entity WHERE tenant_id = %s"),
        "descriptors_predicate": q("SELECT count(*) AS n FROM predicate WHERE tenant_id = %s"),
    }


def snapshot(db, p) -> list:
    out = []
    for q in ("why activation dropped", "time to first value", "minimum sample"):
        out.append([(h["chunk_id"], h["version_id"]) for h in service.search(db, p, q, limit=10)])
    out.append([c["id"] for c in service.ground(db, p, "checklist", limit=10)])
    return out


def test_projection_matches_journal_and_reindex_reproduces(db, corpus, admin):
    before = os_counts(db, corpus)
    assert before == pg_counts(db, corpus)
    snap = snapshot(db, admin)
    framework.reset(db, corpus, consumers=SEARCH_CONSUMERS)
    assert all(v == 0 for v in os_counts(db, corpus).values())
    assert service.search(db, admin, "why activation dropped") == []
    drain_search(db, corpus)
    assert os_counts(db, corpus) == before
    assert snapshot(db, admin) == snap
    # a second drain is a no-op and re-projecting the same state changes nothing
    assert framework.drain(db, corpus, consumers=SEARCH_CONSUMERS) == {"search": 0, "descriptors": 0}
    from kb.search.projector import project_entities

    project_entities(db, corpus, ["xwiki.policies-experiments", "confluence.page-310009"])
    assert os_counts(db, corpus) == before


@pytest.fixture
def api(corpus):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kb.api import routes_search

    app = FastAPI()
    app.include_router(routes_search.router)
    return TestClient(app)


def test_rest_endpoints(db, api, corpus):
    jb = {"X-KB-Principal": "user:jordan.baird"}
    r = api.get("/api/v1/search", params={"q": "why activation dropped", "tenant": corpus})
    assert r.status_code == 200
    body = r.json()
    assert body["hits"][0]["entity_id"] == "confluence.page-310001"
    assert body["stale_projection"] is False and body["watermarks"]["search"] >= 1 and "journal" in body["watermarks"]
    r = api.get("/api/v1/search", params={"q": "advertising budget", "tenant": corpus, "limit": 50}, headers=jb)
    assert "confluence.page-310050" not in [h["entity_id"] for h in r.json()["hits"]]
    r = api.get("/api/v1/search", params={"q": "sample 500 teams", "tenant": corpus,
                                          "known_at": "2026-04-01T00:00:00+03:00"})
    assert r.json()["hits"][0]["version_id"] == "xwiki.policies-experiments@1"
    assert api.get("/api/v1/search", params={"q": "x", "tenant": corpus, "known_at": "2026-04-01"}).status_code == 422
    assert api.get("/api/v1/search", params={"q": "x", "tenant": corpus, "mode": "fuzzy"}).status_code == 422

    r = api.get("/api/v1/search/versions", params={"q": "sample", "tenant": corpus, "kinds": ["Policy"]})
    assert {h["version_id"] for h in r.json()["hits"]} == {"xwiki.policies-experiments@1", "xwiki.policies-experiments@2"}

    r = api.get("/api/v1/ground", params={"text": "based on", "kind": "predicate", "tenant": corpus}, headers=jb)
    g = r.json()
    assert g["candidates"][0]["id"] == "based_on" and g["ambiguous"] is False and "stats" not in g["candidates"][0]
    r = api.get("/api/v1/ground", params={"text": "checklist", "kind": "entity", "tenant": corpus})
    assert len(r.json()["candidates"]) >= 3

    r = api.get("/api/v1/admin/consistency/search", params={"tenant": corpus, "samples": 15, "seed": 1})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert api.get("/api/v1/admin/consistency/search", params={"tenant": corpus}, headers=jb).status_code == 403


def test_stale_projection_flag(db, api, corpus):
    apply(db, corpus, [mk("confluence-vf", "page", "310077", "confluence.page-310077", "Document", "New note",
                          "Fresh text about retention.", ["grp:all-staff"])], "2026-09-01T10:00:00+03:00")
    r = api.get("/api/v1/search", params={"q": "retention", "tenant": corpus}).json()
    assert r["stale_projection"] is True and r["watermarks"]["journal"] > r["watermarks"]["search"]
    # as known before the unprojected change, the projection is complete
    r = api.get("/api/v1/search", params={"q": "retention", "tenant": corpus,
                                          "known_at": "2026-08-01T00:00:00+03:00"}).json()
    assert r["stale_projection"] is False
    drain_search(db, corpus)
    r = api.get("/api/v1/search", params={"q": "retention", "tenant": corpus}).json()
    assert r["stale_projection"] is False and r["hits"][0]["entity_id"] == "confluence.page-310077"


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def test_latency(db, corpus, admin, jordan, capsys):
    """Measured on the host against the shared dev stack; printed with -s, asserted loosely."""
    queries = ["why activation dropped", "time to first value", "onboarding checklist", "minimum sample of teams",
               "advertising budget", "calendar connection", "control group", "release notes checklist",
               "emails to inactive teams", "guided setup", "activation of new teams", "pricing experiment"]
    cases = {
        "search hybrid": lambda q: service.search(db, jordan, q),
        "search lexical": lambda q: service.search(db, jordan, q, mode="lexical"),
        "search known_at": lambda q: service.search(db, jordan, q, known_at=at("2026-04-01T00:00:00+03:00")),
        "search_versions": lambda q: service.search_versions(db, jordan, q),
        "ground entity": lambda q: service.ground(db, jordan, q, kind="entity"),
        "ground predicate": lambda q: service.ground(db, jordan, q, kind="predicate"),
    }
    from kb.search.embeddings import get_embedder

    e = get_embedder()
    t = time.perf_counter()
    e.embed_documents(["a cold query for the measurement"])
    embed_ms = (time.perf_counter() - t) * 1000
    report = {}
    for name, fn in cases.items():
        fn(queries[0])  # warm-up
        ms = []
        for q in queries * 2:
            t = time.perf_counter()
            fn(q)
            ms.append((time.perf_counter() - t) * 1000)
        report[name] = (statistics.median(ms), _pct(ms, 0.95))
    with capsys.disabled():
        print(f"\n[latency] query embedding (uncached, 1 text): {embed_ms:.1f} ms")
        for name, (p50, p95) in report.items():
            print(f"[latency] {name:18} p50 {p50:6.1f} ms   p95 {p95:6.1f} ms")
    assert all(p95 < 3000 for _, p95 in report.values())
