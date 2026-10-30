"""K09: descriptor index and ground() over entities and predicates."""
from __future__ import annotations

import pytest

from kb.search import service
from tests.integration.search.corpus import at

pytestmark = pytest.mark.integration


def cids(cands):
    return [c["id"] for c in cands]


def test_ground_experiment_with_checklist(db, admin, jordan):
    for p in (admin, jordan):
        c = service.ground(db, p, "experiment with the checklist", kind="entity", entity_kinds=["Experiment"])
        assert c[0]["id"] == "jira.EXP-7" and c[0]["kind"] == "Experiment"
        assert all(x["kind"] == "Experiment" for x in c)
        assert "semantic" in c[0]["matched"]


def test_ambiguity_is_visible(db, admin, jordan):
    c = service.ground(db, admin, "checklist", kind="entity", limit=10)
    got = cids(c)
    assert {"jira.EXP-7", "jira.ATL-12", "confluence.page-310002"} <= set(got)
    assert len([x for x in c if x["score"] > 0]) >= 3
    # the Atlas noise is invisible to a principal without grp:atlas
    assert "jira.ATL-12" not in cids(service.ground(db, jordan, "checklist", kind="entity", limit=20))
    assert "jira.ATL-12" not in cids(service.ground(db, jordan, "ATL-12", limit=20))


def test_exact_keys(db, admin):
    for text, want in (("FP-101", "jira.FP-101"), ("fp-101", "jira.FP-101"), ("EXP-7", "jira.EXP-7"),
                       ("v2.4.0", "gitlab.release-v2.4.0"), ("MR !42", "gitlab.mr-42"), ("!42", "gitlab.mr-42"),
                       ("POL-3", "xwiki.policies-experiments")):
        c = service.ground(db, admin, text)
        assert c[0]["id"] == want, (text, c[:3])
        assert any(m.startswith("key ") for m in c[0]["matched"]), c[0]
        if len(c) > 1:
            assert c[0]["score"] > 2 * c[1]["score"]
    c = service.ground(db, admin, "FP-101")
    assert c[0]["matched"][0] == "key FP-101"
    # FP-110 mentions FP-101 in its body but is only a runner-up
    assert "jira.FP-110" not in cids(c)[:1]


def test_ground_predicates(db, admin, jordan):
    c = service.ground(db, jordan, "based on", kind="predicate")
    assert c[0]["id"] == "based_on" and c[0]["causal_kind"] == "based_on" and c[0]["doc_type"] == "predicate"
    assert "exact label" in c[0]["matched"]
    c = service.ground(db, jordan, "is caused by", kind="predicate")
    assert c[0]["id"] == "caused_by" and c[0]["causal_kind"] == "caused_by"
    c = service.ground(db, jordan, "decision made at the meeting", kind="predicate")
    assert "decided_in" in cids(c)[:2]
    c = service.ground(db, jordan, "blocks", kind="predicate")
    assert c[0]["id"] == "blocked_by"


def test_predicate_stats_admin_only(db, admin, jordan):
    a = service.ground(db, admin, "based_on", kind="predicate")[0]
    assert a["id"] == "based_on" and a["matched"][0] == "key based_on"
    st = a["stats"]
    assert st["causal_links"]["count"] == 1 and st["causal_links"]["endpoint_kinds"] == {"Experiment→Release": 1}
    assert st["usage_count"] == 1
    rel = service.ground(db, admin, "relates_to", kind="predicate")[0]
    assert rel["stats"]["statements"]["endpoint_kinds"] == {"Story→Story": 1}
    j = service.ground(db, jordan, "based_on", kind="predicate")[0]
    assert j["id"] == "based_on" and "stats" not in j


def test_historical_alias_and_as_of(db, admin):
    c = service.ground(db, admin, "Project Phoenix onboarding", kind="entity")
    assert c[0]["id"] == "jira.FP-120" and c[0]["label"] == "Guided setup"
    assert any(m.startswith("historical alias 'Project Phoenix onboarding' until 2026-07-01") for m in c[0]["matched"])
    old = service.ground(db, admin, "Project Phoenix onboarding", kind="entity", as_of=at("2026-05-01T00:00:00+03:00"))
    assert old[0]["id"] == "jira.FP-120"
    assert any("valid at as_of" in m for m in old[0]["matched"])
    assert old[0]["score"] > c[0]["score"]  # the name valid at as_of counts fully
    now = service.ground(db, admin, "Guided setup", kind="entity")
    assert now[0]["id"] == "jira.FP-120" and "exact title" in now[0]["matched"]


def test_deleted_entity_is_grounded_but_flagged(db, admin):
    c = service.ground(db, admin, "Reactivation plan draft", kind="entity")
    assert c[0]["id"] == "confluence.page-310009" and c[0]["deleted"] is True and "deleted" in c[0]["matched"]
    assert c[0]["deleted_at"] is not None


def test_ground_restricted_entity(db, admin, jordan):
    assert service.ground(db, admin, "Product council", kind="entity")[0]["id"] == "confluence.page-310050"
    assert "confluence.page-310050" not in cids(service.ground(db, jordan, "Product council", limit=20))
    assert "confluence.page-310050" not in cids(service.ground(db, jordan, "advertising budget", limit=20))


def test_mixed_kinds(db, admin):
    c = service.ground(db, admin, "based on", limit=10)
    assert c[0]["id"] == "based_on"
    c = service.ground(db, admin, "checklist", entity_kinds=["Story"], limit=10)
    assert all(x["doc_type"] == "predicate" or x["kind"] == "Story" for x in c)
    assert "jira.FP-101" in cids(c)[:3]
