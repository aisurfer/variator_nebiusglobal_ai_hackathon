"""why / causes / effects over the FlowPilot story: versions, both times, statuses, roots, chains, ACL, known_at."""
from __future__ import annotations

import json

import pytest
from fastapi.encoders import jsonable_encoder

from kb import causal
from kb.journal.writer import journal_tx
from tests.integration.causal import story as S
from tests.integration.causal.story import at

pytestmark = pytest.mark.integration


def _chain_entities(chain: dict, nodes: dict) -> list[str]:
    return [nodes[v]["entity_id"] for v in chain["versions"]]


def test_why_experiment_reaches_meeting_and_report(db, story, admin):
    w = causal.why(db, admin, S.EXP, depth=8)
    assert w["target"]["entity_id"] == S.EXP and w["target"]["version_id"] == f"{S.EXP}@3"
    nodes = w["nodes"]
    meeting = nodes[f"{S.MEETING}@1"]
    assert meeting["kind"] == "Meeting" and meeting["key_fields"]["held_at"] == "2026-03-02T11:00:00+03:00"
    assert meeting["key_fields"]["attendees"] == S.ATTENDEES
    assert meeting["effective_from"] == at("2026-03-02T11:00:00+03:00")  # business time of the meeting
    assert meeting["recorded_at"] == at(S.T["meeting"])  # when the protocol was recorded
    assert meeting["current_version"] == f"{S.MEETING}@2" and not meeting["is_current"]
    assert meeting["actor"] == "Jordan Baird"
    dec2 = nodes[f"{S.DEC2}@1"]
    assert dec2["key_fields"]["statement"] == S.DEC2_TEXT and dec2["key_fields"]["owner"] == "Jordan Baird"
    exp1 = nodes[f"{S.EXP}@1"]
    assert exp1["role"] == "effect_version" and exp1["key_fields"]["hypothesis"].startswith("The checklist")
    # roots = first causes: the report, the policy, the deleted draft, the council meeting (admin sees it)
    assert w["roots"] == [f"{S.REPORT}@1", f"{S.POLICY}@1", f"{S.DRAFT}@1", f"{S.COUNCIL}@1"]
    assert nodes[f"{S.DRAFT}@1"]["deleted"] is True and w["frontier"] == []
    complete = [_chain_entities(c, nodes) for c in w["chains"] if c["complete"]]
    assert [S.EXP, S.DEC2, S.MEETING, S.REPORT] in complete
    assert [S.EXP, S.REL, S.MR1, S.ST1, S.EPIC, S.DEC1, S.MEETING, S.REPORT] in complete
    assert [S.EXP, S.REL, S.MR2, S.ST2, S.EPIC, S.DEC1, S.MEETING, S.REPORT] in complete
    # the decision branch is the best chain to the report
    to_report = [c for c in w["chains"] if c["root"] == f"{S.REPORT}@1"]
    assert _chain_entities(to_report[0], nodes) == [S.EXP, S.DEC2, S.MEETING, S.REPORT]
    by_id = {x["link_id"]: x for x in w["links"]}
    gov = by_id[story.link("exp->policy")]
    assert gov["status"] == "STALE" and gov["changed_fields"] == ["rules"]
    assert gov["cause_version"] == f"{S.POLICY}@1" and gov["cause_current_version"] == f"{S.POLICY}@2"
    assert gov["changed_by_version"]["version_id"] == f"{S.POLICY}@2"
    assert gov["predicate_label"] == "governed by" and gov["causal_kind"] == "depends_on"
    assert gov["origin"] == "declared:jira_import" and gov["review_status"] == "accepted"
    assert gov["depth"] == 1 and gov["asserted_at"] == at(S.T["exp"])
    assert by_id[story.link("exp->draft")]["status"] == "BROKEN"
    assert by_id[story.link("mr2->st2")]["status"] == "STALE"
    assert by_id[story.link("mr2->st2")]["changed_fields"] == ["description"]
    assert by_id[story.link("epic->dec1")]["status"] == "STALE"
    assert by_id[story.link("st1->epic")]["status"] == "CURRENT_COMPATIBLE"
    assert by_id[story.link("exp->rel")]["evidence"]["quote"] == "Experiment on release v2.4.0"
    assert by_id[story.link("epic->dec1")]["depth"] == 5
    s = w["summary"]
    assert s.startswith("experiment “EXP-7: Onboarding checklist A/B” (jira.EXP-7@3;")
    assert "Growth sync — 2 March" in s and "held 2026-03-02 11:00 MSK" in s and "Sofia Lind" in s
    assert "the dependency is stale (STALE)" in s and "declared:jira_import" in s and "cl-" in s
    assert "the link is broken (BROKEN)" in s and "Root causes:" in s
    assert "transaction axis" in w["interpretation"] and w["summary_lines"][-1] == w["interpretation"]


def test_why_default_depth_and_frontier(db, story, admin):
    w = causal.why(db, admin, S.EXP)  # depth 4
    assert w["query"]["depth"] == 4
    assert f"{S.EPIC}@1" in w["frontier"]  # EXP -> REL -> MR -> STORY -> EPIC, cut by depth
    assert w["nodes"][f"{S.EPIC}@1"]["more_causes"] is True
    assert f"{S.REPORT}@1" in w["roots"]  # via the short decision branch
    assert all(n["depth"] is None or n["depth"] <= 4 for n in w["nodes"].values())


def test_why_known_at(db, story, admin):
    w = causal.why(db, admin, S.EXP, depth=8, known_at=at("2026-05-01T00:00:00+03:00"))
    assert w["target"]["version_id"] == f"{S.EXP}@2" and w["known_at"] == at("2026-05-01T00:00:00+03:00")
    by_id = {x["link_id"]: x for x in w["links"]}
    gov = by_id[story.link("exp->policy")]
    # as known at K: nothing of what happened later (the policy edit of June) is in the answer (Docs/KB/AS-OF.md)
    assert gov["status"] == "CURRENT" and "status_now" not in gov and "cause_current_version_now" not in gov
    assert gov["cause_current_version"] == f"{S.POLICY}@1"
    assert causal.why(db, admin, S.EXP, depth=8)["links"] and {x["status"] for x in causal.why(
        db, admin, S.EXP, depth=8)["links"] if x["link_id"] == story.link("exp->policy")} == {"STALE"}
    assert by_id[story.link("exp->draft")]["status"] == "CURRENT"
    assert by_id[story.link("epic->dec1")]["status"] == "CURRENT"
    assert story.link("exp->decc") not in by_id  # declared later (June)
    assert w["nodes"][f"{S.MEETING}@1"]["current_version"] == f"{S.MEETING}@1"  # head as known at K
    assert "as known at 2026-05-01" in w["summary_lines"][0]
    # before the experiment existed: nothing to explain
    assert causal.why(db, admin, S.EXP, known_at=at("2026-04-01T00:00:00+03:00")) is None
    assert causal.why(db, admin, f"{S.EXP}@3", known_at=at("2026-05-01T00:00:00+03:00")) is None
    assert causal.why(db, admin, "jira.NOPE-1") is None


def test_why_version_target(db, story, admin):
    w1 = causal.why(db, admin, f"{S.EXP}@1", depth=1)
    w3 = causal.why(db, admin, S.EXP, depth=1)
    l1 = {x["link_id"] for x in w1["links"]}
    l3 = {x["link_id"] for x in w3["links"]}
    assert story.link("exp->decc") in l3 and story.link("exp->decc") not in l1  # asserted by EXP@3
    assert l3 - l1 == {story.link("exp->decc")}
    assert w1["target"]["version_id"] == f"{S.EXP}@1" and w1["target"]["is_current"] is False


def test_why_acl_cuts_restricted_branch_silently(db, story, jordan, priya):
    wj = causal.why(db, jordan, S.EXP, depth=8)
    wp = causal.why(db, priya, S.EXP, depth=8)
    assert f"{S.DECC}@1" in wp["nodes"] and f"{S.COUNCIL}@1" in wp["roots"]
    dump = json.dumps(jsonable_encoder(wj), ensure_ascii=False)
    for secret in ("330001", "Council", "council", "Product council", "all new workspaces", story.link("exp->decc"),
                   story.link("decc->council")):
        assert secret not in dump
    assert {n["entity_id"] for n in wj["nodes"].values()} == {n["entity_id"] for n in wp["nodes"].values()} - {S.DECC, S.COUNCIL}
    assert len(wj["links"]) == len(wp["links"]) - 2
    # nothing hints at a cut: the experiment reports no more causes than it shows
    assert wj["nodes"][f"{S.EXP}@3"]["more_causes"] is False
    assert wj["nodes"][f"{S.EXP}@3"]["n_causes"] == 4
    assert causal.why(db, jordan, S.DECC) is None and causal.why(db, jordan, f"{S.DECC}@1") is None


def test_why_include_inferred(db, story, admin):
    with journal_tx(db, story.tenant, "extract", at=at("2026-06-03T10:00:00+03:00")) as j:
        cand = j.propose_link(effect_version=f"{S.EXP}@3", cause_version=f"{S.REPORT}@1", predicate="motivated_by",
                              origin="inferred:rules@1", confidence=0.6,
                              evidence={"quote": "because of the activation drop"})
    assert cand
    w = causal.why(db, admin, S.EXP, depth=1)
    assert cand not in {x["link_id"] for x in w["links"]}
    wi = causal.why(db, admin, S.EXP, depth=1, include_inferred=True)
    link = next(x for x in wi["links"] if x["link_id"] == cand)
    assert link["inferred"] is True and link["review_status"] == "pending" and link["confidence"] == pytest.approx(0.6)
    assert wi["nodes"][f"{S.REPORT}@1"]["via_inferred"] is True
    chain = next(c for c in wi["chains"] if cand in c["links"])
    assert chain["inferred"] is True
    assert "candidate (not reviewed)" in wi["summary"]


def test_causes(db, story, admin):
    c = causal.causes(db, admin, f"{S.EXP}@1")
    assert {x["predicate"] for x in c["links"]} == {"based_on", "implements", "governed_by", "informed_by"}
    assert len(c["links"]) == 4 and all(x["depth"] == 1 for x in c["links"])
    assert causal.causes(db, admin, "jira.NOPE-1@1") is None


def test_effects(db, story, admin, jordan):
    e = causal.effects(db, admin, S.DEC1, depth=8)
    depths = {a["entity_id"]: a["depth"] for a in e["affected"]}
    assert depths == {S.EPIC: 1, S.ST1: 2, S.ST2: 2, S.MR1: 3, S.MR2: 3, S.REL: 4, S.EXP: 5}
    stale = causal.effects(db, admin, S.POLICY, status_filter=["STALE"])
    assert [x["link_id"] for x in stale["links"]] == [story.link("exp->policy")]
    assert causal.effects(db, admin, S.POLICY, status_filter=["BROKEN"])["links"] == []
    with pytest.raises(causal.Invalid):
        causal.effects(db, admin, S.POLICY, status_filter=["WEIRD"])
    # forward from the council decision: hidden from jordan
    assert causal.effects(db, jordan, S.DECC) is None
    # forward from a version: DEC-1@3 has no dependents (the epic is still bound to DEC-1@1)
    assert causal.effects(db, admin, f"{S.DEC1}@3")["links"] == []


def test_why_latency(db, story, admin):
    """Smoke guard only (the infra is shared by parallel runs): ~25 ms on an idle machine for this story."""
    import time

    best = float("inf")
    for _ in range(5):
        t0 = time.perf_counter()
        causal.why(db, admin, S.EXP, depth=8)
        best = min(best, (time.perf_counter() - t0) * 1000)
    assert best < 1000, best
