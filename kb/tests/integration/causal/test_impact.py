"""impact / stale_dependencies over the FlowPilot story."""
from __future__ import annotations

import pytest

from kb import causal
from kb.journal.writer import journal_tx
from tests.integration.causal import story as S
from tests.integration.causal.story import at

pytestmark = pytest.mark.integration

DOWNSTREAM_OF_DEC1 = {S.EPIC: 1, S.ST1: 2, S.ST2: 2, S.MR1: 3, S.MR2: 3, S.REL: 4, S.EXP: 5}


def _depths(res: dict) -> dict[str, int]:
    return {a["entity_id"]: a["depth"] for a in res["affected"]}


def test_impact_decision_statement(db, story, admin):
    im = causal.impact(db, admin, S.DEC1, ["statement"], depth=8)
    assert _depths(im) == DOWNSTREAM_OF_DEC1
    by = {a["entity_id"]: a for a in im["affected"]}
    assert [s["predicate"] for s in by[S.EXP]["paths"][0]] == ["implements", "part_of", "closes", "includes", "based_on"]
    assert by[S.EPIC]["via"] == ["implements"] and by[S.REL]["via"] == ["includes"]
    assert by[S.EPIC]["score"] > by[S.ST1]["score"] > by[S.MR1]["score"] > by[S.REL]["score"] > by[S.EXP]["score"]
    assert [d["link_id"] for d in im["direct_links"]] == [story.link("epic->dec1")]
    assert im["direct_links"][0]["would_status"] == "STALE"
    assert im["totals"]["affected"] == 7 and im["totals"]["by_depth"] == {1: 1, 2: 2, 3: 2, 4: 1, 5: 1}
    assert im["entity"]["version_id"] == f"{S.DEC1}@3"
    assert "affects 7 object(s)" in im["explanation"]
    # default depth 4 stops at the release
    assert _depths(causal.impact(db, admin, S.DEC1, ["statement"])) == {
        k: v for k, v in DOWNSTREAM_OF_DEC1.items() if v <= 4}


def test_impact_respects_sensitivity(db, story, admin):
    assert causal.impact(db, admin, S.DEC1, ["status"], depth=8)["affected"] == []  # implements tracks statement
    assert causal.impact(db, admin, S.EPIC, ["status"], depth=8)["affected"] == []  # part_of tracks summary/description
    assert set(_depths(causal.impact(db, admin, S.EPIC, ["description"], depth=8))) == {S.ST1, S.ST2, S.MR1, S.MR2, S.REL, S.EXP}
    assert _depths(causal.impact(db, admin, S.POLICY, ["rules"])) == {S.EXP: 1}
    assert _depths(causal.impact(db, admin, S.POLICY, ["owner"])) == {}
    assert _depths(causal.impact(db, admin, S.REL, ["*"])) == {S.EXP: 1}  # based_on with explicit sensitivity
    assert _depths(causal.impact(db, admin, S.REL, ["notes"])) == {}


def test_impact_excludes_deleted_dependents(db, story, admin):
    with journal_tx(db, story.tenant, "ingest", at=at("2026-07-01T10:00:00+03:00")) as j:
        j.apply_records([S.issue(S.R_ST1, S.ST1, "Story", "x", "y", "Done").model_copy(update={"deleted": True})],
                        origin="declared:jira_import", default_acl=S.FP_ACL)
    d = _depths(causal.impact(db, admin, S.EPIC, ["*"], depth=8))
    assert S.ST1 not in d and S.MR1 not in d  # nothing propagates through a deleted story
    assert d[S.ST2] == 1 and d[S.MR2] == 2


def test_impact_acl(db, story, jordan, priya):
    with pytest.raises(causal.NotFound):
        causal.impact(db, jordan, S.DECC, ["*"])
    assert _depths(causal.impact(db, priya, S.COUNCIL, ["*"])) == {S.DECC: 1, S.EXP: 2}
    dj = _depths(causal.impact(db, jordan, S.REPORT, ["findings"], depth=8))
    dp = _depths(causal.impact(db, priya, S.REPORT, ["findings"], depth=8))
    assert dj[S.MEETING] == 1 and dj[S.DEC2] == 2 and dj[S.EXP] == 3
    assert dj == dp  # the council is not downstream of the report


def test_stale_dependencies(db, story, admin, jordan):
    sd = causal.stale_dependencies(db, admin)
    got = {i["link_id"]: i for i in sd["items"]}
    expected = {story.link(n) for n in ("exp->policy", "mr2->st2", "epic->dec1", "exp->draft")}
    assert set(got) == expected
    assert got[story.link("exp->policy")]["changed_fields"] == ["rules"]
    assert got[story.link("exp->policy")]["changed_at"] == at(S.T["policy_edit"])
    assert got[story.link("mr2->st2")]["changed_fields"] == ["description"]
    assert got[story.link("epic->dec1")]["changed_fields"] == ["statement"]
    assert got[story.link("epic->dec1")]["changed_by_version"]["version_id"] == f"{S.DEC1}@3"  # not the status edit
    assert got[story.link("epic->dec1")]["cause_version"] == f"{S.DEC1}@1"
    assert got[story.link("epic->dec1")]["cause_current_version"] == f"{S.DEC1}@3"
    assert got[story.link("exp->draft")]["status"] == "BROKEN"
    assert sd["totals"] == {"items": 4, "stale": 3, "broken": 1}
    # ranking: experiment/epic first, the merge request last; each item explained
    assert [i["link_id"] for i in sd["items"]][-1] == story.link("mr2->st2")
    assert sd["items"][0]["effect_kind"] in ("Experiment", "Epic", "Decision") and sd["items"][0]["rank"] == 1
    assert all(i["explanation"] for i in sd["items"])
    assert {g["cause_entity_id"] for g in sd["groups"]} == {S.POLICY, S.ST2, S.DEC1, S.DRAFT}
    # scope: an entity's downstream (incl. itself) or dependent kinds
    assert {i["link_id"] for i in causal.stale_dependencies(db, admin, scope=S.EXP)["items"]} == {
        story.link("exp->policy"), story.link("exp->draft")}
    assert {i["link_id"] for i in causal.stale_dependencies(db, admin, scope=S.DEC1)["items"]} == expected
    assert {i["link_id"] for i in causal.stale_dependencies(db, admin, scope=["Story", "MergeRequest"])["items"]} == {
        story.link("mr2->st2")}
    assert {i["link_id"] for i in causal.stale_dependencies(db, admin, since=at("2026-06-01T00:00:00+03:00"))["items"]} == {
        story.link("epic->dec1")}
    assert len(causal.stale_dependencies(db, admin, limit=2)["items"]) == 2
    assert {i["link_id"] for i in causal.stale_dependencies(db, jordan)["items"]} == expected
    with pytest.raises(causal.NotFound):
        causal.stale_dependencies(db, jordan, scope=S.DECC)


