"""Jira adapter: issue JSON + remote links -> CanonicalRecord (fields, statements, causal references)."""
from __future__ import annotations

from kb.connectors.base import get_adapter
from kb.connectors.jira import split_sections, wiki_to_markdown
from tests.unit.connectors.conftest import causes, ctx, ext, make_env, normalize, sources, statements

JIRA = "jira-vf"


def test_ref_from_url():
    adapter, src = get_adapter("jira"), sources().sources[JIRA]
    assert adapter.ref_from_url("https://vectorforge.atlassian.net/browse/FP-101", src).external_key() == ext(JIRA, "issue", "FP-101")
    assert adapter.ref_from_url("https://vectorforge.atlassian.net/browse/EXP-7?focusedCommentId=1", src).external_id == "EXP-7"
    assert adapter.ref_from_url("https://vectorforge.atlassian.net/jira/software/projects/FP/boards/1?selectedIssue=FP-102",
                                src).external_id == "FP-102"
    assert adapter.ref_from_url("https://vectorforge.atlassian.net/browse/EXP", src) is None
    assert adapter.ref_from_url("https://vectorforge.atlassian.net/wiki/spaces/PROD/pages/310002", src) is None
    assert adapter.ref_from_url("https://vectorforge.atlassian.net.evil.io/browse/FP-1", src) is None
    # through the context: a Confluence URL under the Jira base URL still resolves to Confluence
    assert ctx(JIRA).ref_from_url("https://vectorforge.atlassian.net/wiki/spaces/PROD/pages/310002#dec-2").object_type == "decision"


def test_delete_and_degenerate_payloads():
    (gone,) = normalize(make_env(JIRA, "issue", "FP-999", None, container="FP", op="delete"))
    assert gone.deleted and gone.entity_id == "jira.FP-999"
    for p in ({}, {"fields": {}}, {"fields": {"issuelinks": [{}], "comment": {}, "labels": None}, "remotelinks": [{}]},
              {"fields": {"description": {"type": "doc", "content": [{"type": "paragraph", "content": [
                  {"type": "mention", "attrs": {"id": "712020:sofia.lind", "text": "@Sofia Lind"}}]}]}}}):
        (r,) = normalize(make_env(JIRA, "issue", "FP-998", p, container="FP"))
        assert r.entity_id == "jira.FP-998" and r.kind == "Task"


def test_wiki_markup():
    md = wiki_to_markdown("h3. Acceptance criteria\n* one\n** nested\n# first\nSee [the spec|https://x.io/s]")
    assert md == "### Acceptance criteria\n- one\n  - nested\n1. first\nSee [the spec](https://x.io/s)"
    rest, found = split_sections("Story text.\n\n### Acceptance criteria\n- a\n- b\n\n### Notes\nkeep")
    assert found == {"acceptance_criteria": "- a\n- b"} and "keep" in rest and "- a" not in rest


def test_parent_issue_links_and_remote_links():
    p = {"key": "FP-120", "fields": {
        "summary": "Checklist crashes on Safari", "description": "Steps to reproduce: open the checklist.",
        "issuetype": {"name": "Bug"}, "project": {"key": "FP", "name": "FlowPilot"}, "status": {"name": "To Do"},
        "created": "2026-03-20T10:00:00.000+0300", "updated": "2026-03-20T10:00:00.000+0300", "labels": [],
        "parent": {"key": "FP-101", "fields": {"summary": "Onboarding checklist", "issuetype": {"name": "Epic"}}},
        "issuelinks": [{"type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                        "inwardIssue": {"key": "FP-105", "fields": {"summary": "Progress API"}}},
                       {"type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
                        "outwardIssue": {"key": "EXP-7", "fields": {"summary": "Checklist experiment"}}}]},
         "remotelinks": [{"id": 1, "relationship": "caused by",
                          "object": {"url": "https://gitlab.vectorforge.io/flowpilot/app/-/merge_requests/45",
                                     "title": "feat: checklist widget"}}]}
    (r,) = normalize(make_env(JIRA, "issue", "FP-120", p, container="FP"))
    assert (r.entity_id, r.kind) == ("jira.FP-120", "Bug")
    assert causes(r) == {("part_of", ext(JIRA, "issue", "FP-101")), ("blocked_by", ext(JIRA, "issue", "FP-105")),
                         ("caused_by", ext("gitlab-vf", "merge_request", "flowpilot/app!45"))}
    assert statements(r, "relates_to") == [ext(JIRA, "issue", "EXP-7")]
