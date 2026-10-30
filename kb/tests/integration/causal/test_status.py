"""Computed link statuses as known now (view) and as known at K (parameterised query)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kb import causal
from tests.integration.causal import story as S
from tests.integration.causal.story import at

pytestmark = pytest.mark.integration

EXPECTED_NOW = {
    "meeting->report": "CURRENT",
    "dec1->meeting": "CURRENT_COMPATIBLE",  # the meeting page body changed, not its decisions/held_at
    "dec2->meeting": "CURRENT_COMPATIBLE",
    "epic->dec1": "STALE",  # decision statement changed
    "st1->epic": "CURRENT_COMPATIBLE",  # epic status-only change
    "st2->epic": "CURRENT_COMPATIBLE",
    "mr1->st1": "CURRENT",
    "mr2->st2": "STALE",  # story description changed after the MR was opened
    "rel->mr1": "CURRENT",
    "rel->mr2": "CURRENT",
    "exp->rel": "CURRENT",
    "exp->dec2": "CURRENT",
    "exp->policy": "STALE",  # policy rules changed
    "exp->draft": "BROKEN",  # draft deleted
    "decc->council": "CURRENT",
    "exp->decc": "CURRENT",
}


def test_statuses_now(db, story, admin):
    assert set(story.links) == set(EXPECTED_NOW)
    st = causal.link_status(db, admin, story.links.values())
    assert {name: st[lid] for name, lid in story.links.items()} == EXPECTED_NOW


def test_view_and_parameterised_query_agree(db, story, admin):
    now = datetime.now(timezone.utc)
    assert causal.link_status(db, admin, story.links.values()) == \
        causal.link_status(db, admin, story.links.values(), known_at=now)


def test_status_known_at(db, story, admin):
    links = story.links

    def st(name: str, k: str) -> str | None:
        return causal.link_status(db, admin, [links[name]], known_at=at(k)).get(links[name])

    assert st("exp->policy", "2026-05-01T00:00:00+03:00") == "CURRENT"
    assert st("exp->policy", "2026-05-10T10:00:00+03:00") == "STALE"  # recorded exactly at K
    assert st("exp->draft", "2026-05-15T00:00:00+03:00") == "CURRENT"
    assert st("exp->draft", "2026-05-21T00:00:00+03:00") == "BROKEN"
    assert st("mr2->st2", "2026-03-21T00:00:00+03:00") == "CURRENT"
    assert st("epic->dec1", "2026-05-01T00:00:00+03:00") == "CURRENT"
    assert st("epic->dec1", "2026-05-15T00:00:00+03:00") == "CURRENT_COMPATIBLE"  # status-only change of DEC-1
    assert st("epic->dec1", "2026-06-11T00:00:00+03:00") == "STALE"
    # links that did not exist yet at K are absent
    assert st("exp->rel", "2026-04-01T00:00:00+03:00") is None
    assert st("exp->decc", "2026-05-30T00:00:00+03:00") is None


def test_status_respects_acl(db, story, jordan, priya):
    lid = story.link("exp->decc")
    assert causal.link_status(db, jordan, [lid]) == {}
    assert causal.link_status(db, priya, [lid]) == {lid: "CURRENT"}


def test_revalidate_creates_new_current_link_and_keeps_old(db, story, admin):
    old = story.link("epic->dec1")
    res = causal.revalidate(db, admin, old, reason="checklist with progress bar is still what the epic builds")
    new = res["link_id"]
    assert res["new"]["link"]["status"] == "CURRENT"
    assert res["new"]["link"]["cause_version"] == f"{S.DEC1}@3"
    assert res["new"]["link"]["revalidates"] == old
    assert res["new"]["link"]["origin"] == "declared:jira_import"  # the declaration keeps its origin
    assert res["old"]["link"]["status"] == "RETRACTED" and res["old"]["link"]["retract_reason"] == "revalidated"
    assert res["old"]["revalidated_by"] == [new]
    assert [r["decision"] for r in res["old"]["reviews"]] == ["revalidate"]
    assert causal.link_status(db, admin, [old, new]) == {old: "RETRACTED", new: "CURRENT"}
    # history: as known before the revalidation the old link is still there and STALE
    before = at("2026-07-01T00:00:00+03:00")  # the revalidation is recorded now (editor tx time)
    assert causal.link_status(db, admin, [old, new], known_at=before) == {old: "STALE"}
    # revalidating twice is a conflict; the stale report no longer lists it
    with pytest.raises(causal.Conflict):
        causal.revalidate(db, admin, old)
    assert old not in {i["link_id"] for i in causal.stale_dependencies(db, admin)["items"]}


def test_revalidate_broken_is_conflict(db, story, admin):
    with pytest.raises(causal.Conflict):
        causal.revalidate(db, admin, story.link("exp->draft"))
