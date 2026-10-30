"""K08: hybrid search over chunks and versions — lexical and vector channels, known_at/valid_at slices, ACL, deletes."""
from __future__ import annotations

import pytest

from kb.search import service
from tests.integration.search.corpus import (
    POLICY_V1,
    POLICY_V2,
    T_DRAFT_DELETE,
    T_POLICY_1,
    T_POLICY_2,
    at,
    drain_search,
)

pytestmark = pytest.mark.integration


def ids(hits):
    return [h["entity_id"] for h in hits]


def test_query_finds_the_report_section(db, admin):
    hits = service.search(db, admin, "why activation dropped")
    top = hits[0]
    assert top["entity_id"] == "confluence.page-310001"
    assert top["section"] == "Why activation dropped"
    assert "41%" in top["text"] and "<mark>" in top["snippet"]
    assert top["version_id"] == "confluence.page-310001@1"
    assert set(top["channels"]) == {"lexical", "vector"}
    assert top["recorded_at"] == at("2026-02-27T10:00:00+03:00") and top["effective_from"] is not None
    assert top["url"] == "https://example.test/confluence.page-310001"


def test_english_query(db, admin):
    hits = service.search(db, admin, "time to first value for new workspaces")
    assert hits[0]["entity_id"] == "confluence.page-310002"
    assert hits[0]["kind"] == "Spec"


def test_paraphrase_reaches_the_report_through_vectors(db, admin):
    hits = service.search(db, admin, "why did activation of new teams fall in February", limit=5)
    assert "confluence.page-310001" in ids(hits)[:3]
    report = next(h for h in hits if h["entity_id"] == "confluence.page-310001")
    assert "vector" in report["channels"]
    hits = service.search(db, admin, "how long until new workspaces see value", mode="vector", limit=5)
    assert "confluence.page-310002" in ids(hits)[:3]


def test_modes_and_kinds(db, admin):
    lex = service.search(db, admin, "checklist", mode="lexical", limit=10)
    assert lex and all(set(h["channels"]) == {"lexical"} for h in lex)
    vec = service.search(db, admin, "checklist", mode="vector", limit=10)
    assert vec and all(set(h["channels"]) == {"vector"} for h in vec)
    only = service.search(db, admin, "checklist", kinds=["Experiment"], limit=10)
    assert only and {h["kind"] for h in only} == {"Experiment"}
    with pytest.raises(ValueError):
        service.search(db, admin, "x", mode="fuzzy")  # type: ignore[arg-type]


def test_known_at_excludes_later_versions_and_finds_edited_text(db, admin):
    k = at("2026-04-01T00:00:00+03:00")  # between policy v1 (March) and v2 (June)
    old = service.search(db, admin, "minimum sample 500 teams", known_at=k, limit=5)
    pol = [h for h in old if h["entity_id"] == "xwiki.policies-experiments"]
    assert pol and pol[0]["text"] == POLICY_V1
    assert pol[0]["version_id"] == "xwiki.policies-experiments@1"
    # the June edit is not known in April (Docs/KB/AS-OF.md)
    assert pol[0]["recorded_at"] == at(T_POLICY_1) and pol[0]["superseded_at"] is None and pol[0]["is_current"]
    # as known at K, the June text does not exist yet
    later = service.search(db, admin, "at most 21 days", known_at=k, limit=10)
    assert all(POLICY_V2 != h["text"] for h in later)
    # now: only the current text is searchable; the edited-away "500" text is not returned
    cur = service.search(db, admin, "minimum sample 500 teams", limit=5)
    pol = [h for h in cur if h["entity_id"] == "xwiki.policies-experiments"]
    assert pol and pol[0]["text"] == POLICY_V2 and pol[0]["version_id"] == "xwiki.policies-experiments@2"
    assert all(h["text"] != POLICY_V1 for h in cur)
    # before the policy existed, nothing of it is found
    none = service.search(db, admin, "minimum sample of teams", known_at=at("2026-03-01T00:00:00+03:00"))
    assert "xwiki.policies-experiments" not in ids(none)


def test_valid_at_slice(db, admin):
    hits = service.search(db, admin, "minimum sample", valid_at=at("2026-05-01T00:00:00+03:00"), limit=5)
    pol = [h for h in hits if h["entity_id"] == "xwiki.policies-experiments"]
    assert pol and pol[0]["version_id"] == "xwiki.policies-experiments@1"
    hits = service.search(db, admin, "minimum sample", valid_at=at("2026-07-01T00:00:00+03:00"), limit=5)
    pol = [h for h in hits if h["entity_id"] == "xwiki.policies-experiments"]
    assert pol and pol[0]["version_id"] == "xwiki.policies-experiments@2"


def test_deleted_entity(db, admin):
    q = "emails with a promo code to inactive teams"
    assert "confluence.page-310009" not in ids(service.search(db, admin, q))
    before = service.search(db, admin, q, known_at=at("2026-05-01T00:00:00+03:00"))
    assert before[0]["entity_id"] == "confluence.page-310009"
    # current as known then: the deletion was recorded later (Docs/KB/AS-OF.md)
    assert before[0]["version_id"] == "confluence.page-310009@1" and before[0]["is_current"] is True
    assert before[0]["superseded_at"] is None
    after = service.search(db, admin, q, known_at=at(T_DRAFT_DELETE))
    assert "confluence.page-310009" not in ids(after)
    from kb.search.opensearch import client, ensure_indices

    idx = ensure_indices(db)
    assert not client().exists(index=idx.entities, id=f"{admin.tenant_id}:confluence.page-310009")


def test_acl_hides_restricted_space(db, admin, jordan):
    q = "advertising budget"
    assert ids(service.search(db, admin, q))[0] == "confluence.page-310050"
    for mode in ("hybrid", "lexical", "vector"):
        hits = service.search(db, jordan, q, mode=mode, limit=50)
        assert "confluence.page-310050" not in ids(hits)
        assert all("advertising" not in h["snippet"] for h in hits)
    assert "jira.ATL-12" not in ids(service.search(db, jordan, "Atlas ledger migration checklist", limit=50))
    assert "confluence.page-310050" not in ids(service.search(db, jordan, q, known_at=at("2026-05-01T00:00:00+03:00")))
    assert all(h["entity_id"] != "confluence.page-310050" for h in service.search_versions(db, jordan, q, limit=50))


def test_acl_change_is_projected(db, admin, jordan):
    from kb.journal.writer import journal_tx

    q = "Checklist analytics step completion events"
    assert "jira.FP-110" in ids(service.search(db, jordan, q))
    with journal_tx(db, jordan.tenant_id, "admin", at=at("2026-08-01T10:00:00+03:00")) as j:
        j.set_acl("jira.FP-110", ["grp:exec"])
    drain_search(db, jordan.tenant_id)
    assert "jira.FP-110" not in ids(service.search(db, jordan, q, limit=50))
    assert "jira.FP-110" not in [c["id"] for c in service.ground(db, jordan, "FP-110")]
    assert "jira.FP-110" in ids(service.search(db, admin, q))


def test_search_versions(db, admin):
    hits = service.search_versions(db, admin, "sample of teams", kinds=["Policy"])
    assert [h["version_id"] for h in hits[:2]] == ["xwiki.policies-experiments@1", "xwiki.policies-experiments@2"] or \
           [h["version_id"] for h in hits[:2]] == ["xwiki.policies-experiments@2", "xwiki.policies-experiments@1"]
    v2 = next(h for h in hits if h["seq"] == 2)
    assert v2["changed_fields"] == ["body"] and v2["is_current"] is True
    # transaction-axis window: only what was recorded in June
    june = service.search_versions(db, admin, "sample of teams", start=at("2026-06-01T00:00:00+03:00"),
                                   end=at("2026-07-01T00:00:00+03:00"))
    assert [h["version_id"] for h in june] == ["xwiki.policies-experiments@2"]
    deleted = service.search_versions(db, admin, "Reactivation plan draft")
    assert any(h["op"] == "delete" and h["entity_id"] == "confluence.page-310009" for h in deleted)


def test_per_entity(db, admin):
    one = service.search(db, admin, "onboarding checklist activation", limit=20)
    assert len(ids(one)) == len(set(ids(one)))
    many = service.search(db, admin, "onboarding checklist activation metrics goal", per_entity=3, limit=20)
    spec = [h for h in many if h["entity_id"] == "confluence.page-310002"]
    assert 1 < len(spec) <= 3


def test_consistency(db, admin):
    res = service.consistency_sample(db, admin, samples=40, seed=7)
    assert res["samples"] >= 20 and res["ok"], res["mismatches"]
