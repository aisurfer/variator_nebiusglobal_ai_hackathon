"""Editor operations (declare / retract / revalidate) and the REST surface of the causal engine."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import causal
from kb.api import routes_causal
from tests.integration.causal import story as S

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes_causal.router)  # only this router: other subsystems are optional here
    with TestClient(app) as c:
        yield c


def test_declare_retract(db, story, admin, jordan):
    res = causal.declare(db, admin, S.EXP, S.REPORT, "motivated_by", evidence="Motivation: the activation drop")
    link = res["link"]
    assert res["created"] is True and link["origin"] == "declared:editor" and link["status"] == "CURRENT"
    assert link["effect_version"] == f"{S.EXP}@3" and link["cause_version"] == f"{S.REPORT}@1"
    assert link["sensitivity"] == ["findings", "metrics"]  # typical_sensitivity of motivated_by
    assert link["asserted_by"] == "user:admin" and link["evidence"]["quote"].startswith("Motivation")
    again = causal.declare(db, admin, S.EXP, S.REPORT, "motivated_by")
    assert again["created"] is False and again["link"]["link_id"] == link["link_id"]
    assert link["link_id"] in {x["link_id"] for x in causal.why(db, admin, S.EXP, depth=1)["links"]}
    out = causal.retract(db, admin, link["link_id"], "duplicate of the meeting link")
    assert out["link"]["status"] == "RETRACTED" and out["reviews"][0]["decision"] == "retract"
    assert link["link_id"] not in {x["link_id"] for x in causal.why(db, admin, S.EXP, depth=1)["links"]}
    with pytest.raises(causal.Conflict):
        causal.retract(db, admin, link["link_id"], "again")
    # version-level cycles, non-causal predicates and invisible entities are refused:
    # REL@1 includes MR-42@2, so MR-42@2 <- REL@1 would close a cycle of versions
    with pytest.raises(causal.Conflict):
        causal.declare(db, admin, S.MR1, S.REL, "depends_on")
    with pytest.raises(causal.Conflict):
        causal.declare(db, admin, S.EXP, S.EXP, "depends_on")
    with pytest.raises(causal.Invalid):
        causal.declare(db, admin, S.EXP, S.REPORT, "author")
    with pytest.raises(causal.Conflict):
        causal.declare(db, admin, S.EXP, S.DRAFT, "informed_by")  # the draft is deleted
    with pytest.raises(causal.NotFound):
        causal.declare(db, jordan, S.EXP, S.DECC, "informed_by")
    with pytest.raises(causal.NotFound):
        causal.retract(db, jordan, story.link("exp->decc"), "hidden")
    with pytest.raises(causal.NotFound):
        causal.revalidate(db, jordan, story.link("decc->council"))


def _h(principal: str = "user:admin") -> dict:
    return {"X-KB-Principal": principal}


def test_api(client, story, tenant):
    q = f"?tenant={tenant}"
    r = client.get(f"/api/v1/causal/why/{S.EXP}{q}&depth=8", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"]["version_id"] == f"{S.EXP}@3" and f"{S.REPORT}@1" in body["roots"]
    assert f"{S.MEETING}@1" in body["nodes"] and body["summary"]
    # hidden and unknown are both 404; known_at before the entity existed is 404
    assert client.get(f"/api/v1/causal/why/{S.DECC}{q}", headers=_h("user:jordan.baird")).status_code == 404
    assert client.get(f"/api/v1/causal/why/jira.NOPE-1{q}", headers=_h()).status_code == 404
    assert client.get(f"/api/v1/causal/why/{S.EXP}{q}&known_at=2026-04-01T00:00:00%2B03:00", headers=_h()).status_code == 404
    assert client.get(f"/api/v1/causal/why/{S.EXP}{q}&known_at=yesterday", headers=_h()).status_code == 422
    r = client.get(f"/api/v1/causal/why/{S.EXP}{q}&known_at=2026-05-01T00:00:00%2B03:00", headers=_h())
    assert r.status_code == 200 and r.json()["target"]["version_id"] == f"{S.EXP}@2"
    jw = client.get(f"/api/v1/causal/why/{S.EXP}{q}&depth=8", headers=_h("user:jordan.baird"))
    assert jw.status_code == 200 and "330001" not in jw.text and "ouncil" not in jw.text

    r = client.get(f"/api/v1/causal/causes/{S.EXP}@1{q}", headers=_h())
    assert r.status_code == 200 and len(r.json()["links"]) == 4
    r = client.get(f"/api/v1/causal/effects/{S.POLICY}{q}&status=STALE", headers=_h())
    assert [x["link_id"] for x in r.json()["links"]] == [story.link("exp->policy")]
    r = client.post(f"/api/v1/causal/impact{q}", headers=_h(),
                    json={"entity_id": S.DEC1, "fields": ["statement"], "depth": 8, "tenant": tenant})
    assert r.status_code == 200 and r.json()["totals"]["affected"] == 7
    assert client.post(f"/api/v1/causal/impact{q}", headers=_h(),
                       json={"entity_id": S.DEC1, "tenant": "other"}).status_code == 422
    assert client.post(f"/api/v1/causal/impact{q}", headers=_h("user:jordan.baird"),
                       json={"entity_id": S.DECC}).status_code == 404
    r = client.get(f"/api/v1/causal/stale{q}", headers=_h())
    assert r.status_code == 200 and r.json()["totals"]["items"] == 4
    r = client.get(f"/api/v1/causal/stale{q}&kinds=Story&kinds=MergeRequest", headers=_h())
    assert [i["link_id"] for i in r.json()["items"]] == [story.link("mr2->st2")]
    r = client.get(f"/api/v1/causal/stale{q}&scope={S.EXP}&since=2026-05-15T00:00:00%2B03:00", headers=_h())
    assert [i["link_id"] for i in r.json()["items"]] == [story.link("exp->draft")]

    lid = story.link("epic->dec1")
    r = client.get(f"/api/v1/causal/links/{lid}{q}", headers=_h())
    assert r.status_code == 200 and r.json()["link"]["status"] == "STALE" and r.json()["explanation"]
    assert client.get(f"/api/v1/causal/links/{story.link('exp->decc')}{q}", headers=_h("user:jordan.baird")).status_code == 404
    r = client.post(f"/api/v1/causal/links/{lid}/revalidate{q}", headers=_h(), json={"reason": "still valid"})
    assert r.status_code == 200 and r.json()["new"]["link"]["status"] == "CURRENT"
    assert client.post(f"/api/v1/causal/links/{lid}/revalidate{q}", headers=_h()).status_code == 409
    r = client.post(f"/api/v1/causal/links{q}", headers=_h("user:jordan.baird"),
                    json={"effect_entity_id": S.EXP, "cause_entity_id": S.REPORT, "predicate": "motivated_by",
                          "evidence": {"quote": "because of the activation drop"}})
    assert r.status_code == 200 and r.json()["created"] is True
    new = r.json()["link"]["link_id"]
    assert r.json()["link"]["asserted_by"] == "user:jordan.baird"
    assert client.post(f"/api/v1/causal/links/{new}/retract{q}", headers=_h(), json={}).status_code == 422
    r = client.post(f"/api/v1/causal/links/{new}/retract{q}", headers=_h("user:jordan.baird"), json={"reason": "dup"})
    assert r.status_code == 200 and r.json()["link"]["status"] == "RETRACTED"
    assert client.post(f"/api/v1/causal/links{q}", headers=_h(),
                       json={"effect_entity_id": S.MR1, "cause_entity_id": S.REL,
                             "predicate": "depends_on"}).status_code == 409


def test_entity_level_cycle_is_legal_and_traversals_stay_bounded(db, story, admin):
    """REPORT@1 <- EXP@3 closes an entity-level loop EXP -> DEC-2 -> MEETING -> REPORT -> EXP; versions stay acyclic."""
    res = causal.declare(db, admin, S.REPORT, S.EXP, "informed_by")
    assert res["created"] is True
    for target in (S.EXP, S.REPORT, S.MEETING):
        w = causal.why(db, admin, target, depth=8)
        for c in w["chains"]:
            ents = [w["nodes"][v]["entity_id"] for v in c["versions"]]
            assert len(ents) == len(set(ents)), ents  # no chain revisits an entity
        assert w["target"]["entity_id"] not in {w["nodes"][r]["entity_id"] for r in w["roots"]}
    w = causal.why(db, admin, S.REPORT, depth=8)
    assert f"{S.EXP}@3" in w["nodes"] and w["nodes"][f"{S.EXP}@3"]["role"] == "cause"
    e = causal.effects(db, admin, S.EXP, depth=8)
    assert S.EXP not in {a["entity_id"] for a in e["affected"]}
    assert {S.REPORT, S.MEETING, S.DEC1, S.DEC2} <= {a["entity_id"] for a in e["affected"]}
    im = causal.impact(db, admin, S.EXP, ["*"], depth=8)
    assert S.EXP not in {a["entity_id"] for a in im["affected"]}
