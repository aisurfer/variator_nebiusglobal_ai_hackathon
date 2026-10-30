"""Confluence adapter: ADF pages -> Meeting/Report/Spec/Document + derived Decision and ActionItem records."""
from __future__ import annotations

import json

from kb.connectors.base import get_adapter
from tests.unit.connectors.conftest import causes, ext, make_env, normalize, sources, statements

CF = "confluence-vf"


def test_delete():
    (gone,) = normalize(make_env(CF, "page", "310004", None, container="PROD", op="delete"))
    assert gone.deleted and gone.entity_id == "confluence.page-310004" and gone.key.external_id == "310004"


def test_ref_from_url():
    a, src = get_adapter("confluence"), sources().sources[CF]
    base = "https://vectorforge.atlassian.net/wiki"
    assert a.ref_from_url(f"{base}/spaces/PROD/pages/310002", src).external_key() == ext(CF, "page", "310002")
    assert a.ref_from_url(f"{base}/spaces/PROD/pages/310002/Growth+sync", src).external_id == "310002"
    assert a.ref_from_url(f"{base}/spaces/PROD/pages/310002#dec-2", src).external_key() == ext(CF, "decision", "310002#dec-2")
    assert a.ref_from_url(f"{base}/spaces/PROD/pages/310002/Growth+sync#task-1", src).external_key() == \
        ext(CF, "task", "310002#task-1")
    assert a.ref_from_url(f"{base}/pages/viewpage.action?pageId=310006", src).external_id == "310006"
    assert a.ref_from_url(f"{base}/spaces/PROD/pages/310002#comments", src).object_type == "page"
    assert a.ref_from_url("https://vectorforge.atlassian.net/browse/FP-101", src) is None
    assert a.ref_from_url(f"{base}/spaces/PROD/overview", src) is None


def _page(content: list[dict], labels: tuple[str, ...] = ("meeting-notes",)) -> dict:
    return {"id": "1", "title": "Sync", "authorId": "712020:sofia.lind", "version": {"number": 1},
            "labels": {"results": [{"name": x} for x in labels]},
            "body": {"atlas_doc_format": {"value": json.dumps({"type": "doc", "content": content})}}}


def _decision(local_id: str, text: str) -> dict:
    return {"type": "decisionItem", "attrs": {"localId": local_id, "state": "DECIDED"},
            "content": [{"type": "text", "text": text}]}


def test_decision_numbering_without_trivial_local_ids():
    content = [{"type": "decisionList", "attrs": {"localId": "x"},
                "content": [_decision("9f2c", "first"), _decision("dec-1", "pinned"), _decision("77aa", "third")]}]
    recs = normalize(make_env(CF, "page", "1", _page(content), container="PROD"))
    numbered = {r.fields["statement"]: r.entity_id for r in recs[1:]}
    assert numbered == {"pinned": "confluence.page-1.dec-1", "first": "confluence.page-1.dec-2",
                        "third": "confluence.page-1.dec-3"}




# ------------------------------------------------------------------ an English meeting page built inline
def _t(text: str) -> dict:
    return {"type": "text", "text": text}


def _p(*inline: dict) -> dict:
    return {"type": "paragraph", "content": list(inline)}


def _mention(account_id: str, name: str) -> dict:
    return {"type": "mention", "attrs": {"id": account_id, "text": f"@{name}"}}


def _row(label: str, *inline: dict) -> dict:
    return {"type": "tableRow", "content": [{"type": "tableHeader", "content": [_p(_t(label))]},
                                            {"type": "tableCell", "content": [_p(*inline)]}]}


def _meeting_page() -> dict:
    base = "https://vectorforge.atlassian.net/wiki/spaces/PROD/pages"
    props = {"type": "bodiedExtension", "attrs": {"extensionKey": "details"}, "content": [{"type": "table", "content": [
        _row("Date", {"type": "date", "attrs": {"timestamp": "1772409600000"}}, _t(" 11:00–12:00 (MSK)")),
        _row("Attendees", _mention("712020:priya.nand", "Priya Nand"), _t(", "), _mention("712020:sofia.lind", "Sofia Lind")),
        _row("Note taker", _mention("712020:jordan.baird", "Jordan Baird")),
        _row("Based on", {"type": "inlineCard", "attrs": {"url": f"{base}/310001"}}),
    ]}]}
    content = [
        props,
        {"type": "heading", "attrs": {"level": 2}, "content": [_t("Agenda")]},
        {"type": "orderedList", "content": [{"type": "listItem", "content": [_p(_t("Activation drop in January"))]}]},
        {"type": "heading", "attrs": {"level": 2}, "content": [_t("Discussion")]},
        _p(_t("We chose the checklist because activation fell from 41% to 33%.")),
        {"type": "decisionList", "attrs": {"localId": "d"}, "content": [
            {"type": "decisionItem", "attrs": {"localId": "dec-1", "state": "DECIDED"},
             "content": [_t("Build a step-by-step onboarding checklist "), _mention("712020:sofia.lind", "Sofia Lind")]}]},
        {"type": "taskList", "attrs": {"localId": "t"}, "content": [
            {"type": "taskItem", "attrs": {"localId": "task-1", "state": "TODO"},
             "content": [_mention("712020:jordan.baird", "Jordan Baird"), _t(" create the epic in Jira")]}]},
    ]
    return {"id": "310002", "title": "Growth sync — March 2, 2026", "status": "current",
            "space": {"id": "98305", "key": "PROD", "name": "Product"}, "authorId": "712020:jordan.baird",
            "createdAt": "2026-03-02T09:40:00.000Z", "version": {"number": 1, "createdAt": "2026-03-02T09:40:00.000Z"},
            "labels": {"results": [{"name": "meeting-notes"}]},
            "body": {"atlas_doc_format": {"value": json.dumps({"type": "doc", "content": content})}}}


def test_english_meeting_page_properties_decisions_and_action_items():
    recs = normalize(make_env(CF, "page", "310002", _meeting_page(), container="PROD"))
    assert [r.entity_id for r in recs] == ["confluence.page-310002", "confluence.page-310002.dec-1",
                                           "confluence.page-310002.task-1"]
    m, dec, task = recs
    assert m.kind == "Meeting" and m.fields["held_at"] == "2026-03-02T11:00:00+03:00"
    assert m.fields["attendees"] == ["Priya Nand", "Sofia Lind"] and m.fields["note_taker"] == "Jordan Baird"
    assert "Activation drop in January" in m.fields["agenda"]
    assert "activation fell from 41% to 33%" in m.fields["discussion"]
    assert causes(m) == {("informed_by", ext(CF, "page", "310001"))}
    assert m.causes[0].evidence.locator["property"] == "Based on"
    assert len(statements(m, "participant")) == 2 and statements(m, "in_space") == ["PROD"]
    assert dec.kind == "Decision" and dec.fields["statement"] == "Build a step-by-step onboarding checklist"
    assert dec.fields["owner"] == "Sofia Lind" and causes(dec) == {("decided_in", ext(CF, "page", "310002"))}
    assert dec.body.startswith("# Build a step-by-step onboarding checklist\n\nDecision · DECIDED · owner: Sofia Lind")
    assert task.kind == "ActionItem" and task.fields["assignee"] == "Jordan Baird"
    assert task.title == "Jordan Baird: create the epic in Jira"
    assert causes(task) == {("decided_in", ext(CF, "page", "310002"))} and task.causes[0].sensitivity == ["held_at"]
