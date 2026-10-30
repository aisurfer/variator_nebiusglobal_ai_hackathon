"""The date of view on the REST surface (Docs/KB/AS-OF.md): one parameter for every read, the end of the data by default,
a date after the end of the data refused with the bounds; stale links of any date from their history."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import asof, causal
from kb.api import routes_causal, routes_core
from tests.conftest import at
from tests.integration.causal import story as S

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    app = FastAPI()
    for r in (routes_core.router, routes_causal.router):
        app.include_router(r)
    with TestClient(app) as c:
        yield c


def test_the_date_of_view_is_one_parameter_bounded_by_the_data(db, story, client):
    t = story.tenant
    b = client.get("/api/v1/bounds", params={"tenant": t}).json()
    end = asof.bounds(db, t).end
    assert b["data_end"] == end.isoformat() and b["data_start"] <= b["data_end"]
    k = "2026-05-01T00:00:00+03:00"
    by_known = client.get(f"/api/v1/causal/why/{S.EXP}", params={"tenant": t, "known_at": k, "depth": 8}).json()
    by_alias = client.get(f"/api/v1/causal/why/{S.EXP}", params={"tenant": t, "as_of": k, "depth": 8}).json()
    assert by_known == by_alias and by_known["target"]["version_id"] == f"{S.EXP}@2"
    assert client.get(f"/api/v1/causal/why/{S.EXP}", params={"tenant": t, "known_at": k,
                                                              "as_of": "2026-05-02T00:00:00+03:00"}).status_code == 422
    # after the end of the data: an honest refusal with the bounds, not an answer from the wall clock
    later = (end + timedelta(days=1)).isoformat()
    for path in (f"/api/v1/causal/why/{S.EXP}", "/api/v1/entities", f"/api/v1/entities/{S.EXP}/history", "/api/v1/timeline",
                 "/api/v1/causal/stale", "/api/v1/admin/stats"):
        r = client.get(path, params={"tenant": t, "known_at": later})
        assert r.status_code == 422, path
        assert r.json()["detail"]["data_end"] == end.isoformat(), path
    # at the end of the data: the same as without a date
    a = client.get(f"/api/v1/causal/why/{S.EXP}", params={"tenant": t, "depth": 8}).json()
    e = client.get(f"/api/v1/causal/why/{S.EXP}", params={"tenant": t, "depth": 8, "known_at": end.isoformat()}).json()
    assert {(x["link_id"], x["status"]) for x in a["links"]} == {(x["link_id"], x["status"]) for x in e["links"]}
    assert a["target"] == e["target"]


def test_reads_as_of_k_hold_nothing_recorded_later(db, story, client):
    t, k = story.tenant, "2026-04-13T00:00:00+03:00"  # after the draft, before the experiment
    ents = {x["entity_id"] for x in client.get("/api/v1/entities", params={"tenant": t, "known_at": k, "limit": 500}).json()["items"]}
    assert S.DRAFT in ents and S.EXP not in ents and S.COUNCIL not in ents
    h = client.get(f"/api/v1/entities/{S.POLICY}/history", params={"tenant": t, "known_at": k}).json()["versions"]
    assert [v["version_id"] for v in h] == [f"{S.POLICY}@1"] and h[0]["superseded_at"] is None and h[0]["is_current"]
    assert client.get(f"/api/v1/entities/{S.EXP}", params={"tenant": t, "known_at": k}).status_code == 404
    card = client.get(f"/api/v1/entities/{S.POLICY}", params={"tenant": t, "known_at": k}).json()
    assert card["head"]["version_id"] == f"{S.POLICY}@1" and card["versions"] == 1
    tl = client.get("/api/v1/timeline", params={"tenant": t, "known_at": k, "limit": 2000}).json()["events"]
    assert tl and all(at(e["recorded_at"]) <= at(k) for e in tl)
    stats = client.get("/api/v1/admin/stats", params={"tenant": t, "known_at": k}).json()
    now = client.get("/api/v1/admin/stats", params={"tenant": t}).json()
    assert stats["versions"] == len(client.get("/api/v1/timeline", params={"tenant": t, "known_at": k, "limit": 2000})
                                    .json()["events"]) < now["versions"]
    assert "Experiment" not in stats["entities_by_kind"] and "Experiment" in now["entities_by_kind"]
    # the draft was deleted on 20 May: its links are BROKEN now, not on 13 April
    assert "BROKEN" not in stats["causal_links_by_status"] and "BROKEN" in now["causal_links_by_status"]


def test_stale_at_any_date_is_the_history_interval_containing_it(db, story, admin):
    hist = causal.stale_history(db, admin)["items"]
    assert hist
    moments = sorted({at(x) for x in S.T.values()} | {at(x) + timedelta(hours=1) for x in S.T.values()})
    seen = set()
    for k in moments:
        st = causal.stale_dependencies(db, admin, known_at=k, limit=1000)["items"]
        got = {(x["link_id"], x["status"], x["changed_at"], x["effect_title"], x["cause_title"]) for x in st}
        want = {(x["link_id"], g["status"], g["changed_at"], g["effect_title"], g["cause_title"])
                for x in hist for g in x["segments"] if g["from"] <= k and (g["to"] is None or g["to"] > k)}
        assert got == want, k
        seen |= {s for _, s, *_ in got}
    assert seen == {"STALE", "BROKEN"}
    # the history as known at K ends there: what is known later is not in it
    k = at(S.T["policy_edit"]) + timedelta(hours=1)
    early = causal.stale_history(db, admin, known_at=k)["items"]
    assert all(g["from"] <= k and (g["to"] is None or g["to"] <= k) for x in early for g in x["segments"])
    assert {x["link_id"] for x in early} < {x["link_id"] for x in hist}
