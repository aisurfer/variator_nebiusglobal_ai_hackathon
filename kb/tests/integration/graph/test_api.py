"""Graph REST endpoints (K10) on a minimal app with only the graph router (other routers are not needed)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.api import routes_graph
from kb.settings import get_settings
from tests.integration.graph.helpers import EXEC, apply, build_scenario, drain_graph, rec, rel

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes_graph.router)
    with TestClient(app) as c:
        yield c


def hdr(tenant: str, principal: str = "user:admin") -> dict[str, str]:
    return {"X-KB-Tenant": tenant, "X-KB-Principal": principal}


def test_graph_endpoints(db, gt, client):
    build_scenario(db, gt)
    apply(db, gt, [rec("SEC-1", kind="Decision", acl=EXEC, statements=[rel("relates_to", "FP-2")])],
          "2026-03-12T10:00:00+03:00")
    drain_graph(db, gt)
    h = hdr(gt)

    r = client.post("/api/v1/graph/expand", json={"ids": ["jira.FP-4"], "depth": 1}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {n["entity_id"] for n in body["nodes"]} == {"jira.FP-1", "jira.FP-2", "jira.FP-4"}
    assert body["watermark"]["lag"] == 0 and body["watermark"]["graph"] == body["watermark"]["journal"]
    r = client.post("/api/v1/graph/expand", json={"ids": ["jira.FP-4"], "known_at": "2026-03-03T00:00:00+03:00"},
                    headers=h)
    assert "jira.FP-3" in {n["entity_id"] for n in r.json()["nodes"]}
    assert r.json()["watermark"]["known_at_beyond_watermark"] is False
    assert client.post("/api/v1/graph/expand", json={"ids": ["x"], "known_at": "2026-03-03"}, headers=h).status_code == 422
    assert client.post("/api/v1/graph/expand", json={"ids": ["x"], "depth": 9}, headers=h).status_code == 422

    r = client.get("/api/v1/graph/neighborhood/jira.FP-2", params={"depth": 1}, headers=h)
    assert r.status_code == 200 and r.json()["center"] == "jira.FP-2" and len(r.json()["versions"]) == 2

    r = client.get("/api/v1/graph/path", params={"source": "jira.FP-4", "target": "jira.FP-3"}, headers=h)
    assert r.status_code == 200 and r.json()["found"] and r.json()["length"] == 2

    r = client.post("/api/v1/graph/ppr", json={"seeds": ["jira.FP-2"], "top_k": 3}, headers=h)
    assert r.status_code == 200 and r.json()["label"] == "likely related (not declared)" and r.json()["regime"] == 2
    assert 0 < len(r.json()["items"]) <= 3 and "watermark" in r.json()

    r = client.get("/api/v1/admin/consistency/graph", params={"samples": 25, "seed": 1}, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["counts_match"] is True

    # ACL: a FlowPilot member does not see the exec-only decision anywhere in the graph API
    s = hdr(gt, "user:sofia.lind")
    assert client.get("/api/v1/graph/neighborhood/jira.SEC-1", headers=s).status_code == 404
    nodes = {n["entity_id"] for n in client.post("/api/v1/graph/expand", json={"ids": ["jira.FP-2"]}, headers=s).json()["nodes"]}
    assert "jira.SEC-1" not in nodes and "jira.FP-1" in nodes
    admin_nodes = {n["entity_id"] for n in client.post("/api/v1/graph/expand", json={"ids": ["jira.FP-2"]}, headers=h).json()["nodes"]}
    assert "jira.SEC-1" in admin_nodes
    items = client.post("/api/v1/graph/ppr", json={"seeds": ["jira.FP-2"], "top_k": 50}, headers=s).json()["items"]
    assert "jira.SEC-1" not in {i["entity_id"] for i in items}
    assert client.get("/api/v1/graph/path", params={"source": "jira.FP-2", "target": "jira.SEC-1"},
                      headers=s).json()["found"] is False
    cons = client.get("/api/v1/admin/consistency/graph", headers=s).json()
    assert cons["ok"] is True and "counts" not in cons  # tenant-wide counts are for admins only


def test_disabled_graph_answers_503(db, gt, client, monkeypatch):
    monkeypatch.setattr(get_settings(), "graph_enabled", False)
    r = client.post("/api/v1/graph/expand", json={"ids": ["jira.FP-1"]}, headers=hdr(gt))
    assert r.status_code == 503
