"""English answers: time interpretations, the router, template answers and explanations. No infra."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from kb.causal import explain
from kb.lang import kind_label, predicate_label
from kb.reasoning.answer import compose
from kb.reasoning.router import parse_rules
from kb.reasoning.timeparse import TimeInterpretation, resolve_time

NOW = datetime.fromisoformat("2026-10-24T12:00:00-07:00")
TZ = ZoneInfo("America/Los_Angeles")
_NON_LATIN = re.compile(r"[^\x00-\x7F“”—→…·•–’]")


def test_labels_are_english():
    assert kind_label("Decision", "Decision row label") == "decision"
    assert kind_label("CustomThing", "custom thing") == "custom thing"  # outside the seed ontology: the row label
    assert kind_label("CustomThing") == "custom thing" and kind_label(None) == "object"
    assert predicate_label("decided_in", "decided on") == "decided at"
    assert predicate_label("decided_in", inverse=True) == "decision made at"
    assert predicate_label("custom_link") == "custom link"


@pytest.mark.parametrize("q,start,end", [
    ("What changed in the pricing policy last week?", "2026-10-12", "2026-10-19"),
    ("What did we decide in March 2026?", "2026-03-01", "2026-04-01"),
    ("What happened on July 1, 2026?", "2026-07-01", "2026-07-02"),
    ("Changes from March 1 to March 15", "2026-03-01", "2026-03-16"),
])
def test_english_intervals_are_interpreted_in_english(q, start, end):
    ti = resolve_time(q, NOW, tz="America/Los_Angeles")
    assert (ti.start.date().isoformat(), ti.end.date().isoformat()) == (start, end)
    assert "read as" in ti.interpretation and not _NON_LATIN.search(ti.interpretation)


def test_known_at_in_english():
    ti = resolve_time("What did we know as of March 31, 2026 about EXP-7?", NOW, tz="America/Los_Angeles")
    assert ti.known_at.date().isoformat() == "2026-04-01" and "transaction axis" in ti.interpretation


@pytest.mark.parametrize("q,intent,kinds,keys", [
    ("Why did we sign the amendment to the Harbor contract?", "why", ["Amendment", "Contract"], []),
    ("What caused the refund spike in July?", "why", [], []),
    ("What would happen if we cancel the decision to move to Fintrek?", "impact", ["Decision"], []),
    ("Which proposals rely on the outdated price list?", "stale", ["CommercialProposal", "PriceList"], []),
    ("What has changed in invoice INV-204?", "changes", ["Invoice"], ["INV-204"]),
    ("When was release v2.4.0 shipped?", "timeline", ["Release"], ["v2.4.0"]),
])
def test_english_router(q, intent, kinds, keys):
    p = parse_rules(q)
    assert (p.intent, p.kind_hints, p.keys) == (intent, kinds, keys)
    assert p.subject_text and not re.search(r"\b(why|did|we|the|to|what|which|would)\b", p.subject_text, re.I)


def test_english_router_keeps_names_in_the_subject():
    p = parse_rules("Why did we move the Harbor Logistics launch?")
    assert p.subject_text == "move Harbor Logistics"


def _why_results():
    nodes = {
        "jira.EXP-7@3": {"version_id": "jira.EXP-7@3", "entity_id": "jira.EXP-7", "kind": "Experiment",
                         "title": "Onboarding checklist", "effective_from": "2026-03-10T10:00:00-07:00",
                         "recorded_at": "2026-03-10T10:00:00-07:00"},
        "conf.m@1": {"version_id": "conf.m@1", "entity_id": "conf.m", "kind": "Meeting", "title": "Growth sync",
                     "effective_from": "2026-03-02T11:00:00-08:00", "recorded_at": "2026-03-02T15:00:00-08:00",
                     "key_fields": {"held_at": "2026-03-02T11:00:00-08:00", "attendees": ["Jordan Baird", "Priya Nand"]}},
        "conf.r@1": {"version_id": "conf.r@1", "entity_id": "conf.r", "kind": "Report", "title": "Activation drop",
                     "effective_from": "2026-02-20T09:00:00-08:00", "recorded_at": "2026-02-20T09:00:00-08:00"},
    }
    links = [{"link_id": "l1", "effect_version": "jira.EXP-7@3", "cause_version": "conf.m@1", "predicate": "decided_in",
              "predicate_label": "decided at", "origin": "declared:jira_import", "status": "STALE",
              "cause_current_version": "conf.m@2", "changed_fields": ["attendees"]},
             {"link_id": "l2", "effect_version": "conf.m@1", "cause_version": "conf.r@1", "predicate": "informed_by",
              "predicate_label": "informed by", "origin": "declared:confluence_import", "status": "CURRENT"}]
    chains = [{"versions": ["jira.EXP-7@3", "conf.m@1", "conf.r@1"], "complete": True, "root": "conf.r@1",
               "text": "“Onboarding checklist” —decided at→ “Growth sync” —informed by→ “Activation drop”"}]
    return {"why": {"target": nodes["jira.EXP-7@3"], "nodes": nodes, "links": links, "chains": chains,
                    "roots": ["conf.r@1"]}}


class _Ctx:
    tz = "America/Los_Angeles"
    known_at = None


def test_template_answer_in_english():
    parsed = parse_rules("Why did we launch experiment EXP-7?")
    subject = {"id": "jira.EXP-7", "label": "Onboarding checklist", "kind": "Experiment", "matched": "key"}
    out = compose(parsed, subject, [], _why_results(), TimeInterpretation(), _Ctx())
    text = out["text"]
    assert not _NON_LATIN.search(text), [line for line in text.splitlines() if _NON_LATIN.search(line)]
    assert text.startswith("Subject: “Onboarding checklist”")
    assert "In short: “Onboarding checklist” [jira.EXP-7@3] — following the discussion on 2026-03-02 11:00" in text
    assert "attendees: Jordan Baird, Priya Nand" in text and "Root causes:" in text
    assert "STALE: the cause changed in the fields that matter, now [conf.m@2], changed fields: attendees" in text
    assert out["citations"][0] == "jira.EXP-7@3" and out["grounded_chain"]


def test_not_found_in_english():
    parsed = parse_rules("What do we know about the Zephyr merger?")
    out = compose(parsed, None, [], {"search": []}, TimeInterpretation(), _Ctx())
    assert out["unknowns"] == ["Nothing was found in the sources available to you (this does not prove the event "
                               "did not happen)."]


def test_explanations_in_english():
    tz = ZoneInfo("UTC")
    assert explain.origin_phrase("declared:jira_import") == "declared in the source, Jira (declared:jira_import)"
    assert explain.origin_phrase("declared:email_import") == "declared in the source, mail (declared:email_import)"
    assert explain.origin_phrase("inferred:implicit") == "inferred automatically, not reviewed (inferred:implicit)"
    assert explain.why_interpretation(None, tz).startswith("“Why” means the grounds at the time of the decision")
    lines = explain.why_lines({"kind_label": "experiment", "title": "EXP", "version_id": "x@1",
                               "effective_from": None, "recorded_at": None}, [], {}, [], [], [], None, tz, None)
    assert lines[0] == "No causal links are declared for experiment “EXP” (x@1; effective —, recorded —)."
