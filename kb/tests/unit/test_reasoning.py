"""resolve_time and the rule router (no infra)."""
from __future__ import annotations

from datetime import datetime

import pytest

from kb.reasoning.router import parse_rules
from kb.reasoning.timeparse import resolve_time

NOW = datetime.fromisoformat("2026-10-24T12:00:00+03:00")


@pytest.mark.parametrize(
    "q,start,end",
    [
        ("What changed in the experiment policy in April?", "2026-04-01", "2026-05-01"),
        ("What did we decide at the meeting on March 2?", "2026-03-02", "2026-03-03"),
        ("Which tasks were closed last week?", "2026-10-12", "2026-10-19"),
        ("what changed in March 2026?", "2026-03-01", "2026-04-01"),
        ("Changes from 1 to 15 March", "2026-03-01", "2026-03-16"),
        ("What happened in May?", "2026-05-01", "2026-06-01"),
    ],
)
def test_intervals(q, start, end):
    ti = resolve_time(q, NOW)
    assert ti.start.date().isoformat() == start and ti.end.date().isoformat() == end
    assert ti.interpretation


def test_known_at_and_anchor():
    ti = resolve_time("What did we know as of March 31, 2026 about EXP-7?", NOW)
    assert ti.known_at.isoformat().startswith("2026-04-01T00:00")
    hit = {"entity_id": "r", "title": "v2.4.0", "version_id": "r@1", "effective_from": "2026-03-24T16:00:00+03:00"}
    ti = resolve_time("What was done before release v2.4.0?", NOW, ground_event=lambda t: hit)
    assert ti.end.isoformat() == "2026-03-24T16:00:00+03:00" and ti.start is None and ti.anchor == hit


def test_no_time_is_no_constraint():
    assert resolve_time("Why did we launch experiment EXP-7?", NOW).empty


@pytest.mark.parametrize(
    "q,intent,kinds,keys",
    [
        ("Why did we launch experiment EXP-7?", "why", ["Experiment"], ["EXP-7"]),
        ("Why did we launch the experiment with the onboarding checklist?", "why", ["Experiment"], []),
        ("What would happen if we cancel the decision to build the onboarding checklist?", "impact", ["Decision"], []),
        ("Which decisions rely on outdated data?", "stale", ["Decision"], []),
        ("What changed in the policy of experiments?", "changes", ["Policy", "Experiment"], []),
        ("When did we ship release v2.4.0?", "timeline", ["Release"], ["v2.4.0"]),
        ("FP-101", "lookup", [], ["FP-101"]),
        ("Why did we launch the onboarding checklist experiment?", "why", ["Experiment"], []),
    ],
)
def test_router(q, intent, kinds, keys):
    p = parse_rules(q)
    assert (p.intent, p.kind_hints, p.keys) == (intent, kinds, keys)
    assert p.subject_text


def test_router_hypothetical_and_fields():
    p = parse_rules("What happens if we cancel decision DEC-1?")
    assert p.hypothetical == "cancel"
    p = parse_rules("What would change if we changed the rules of the experiment policy?")
    assert p.hypothetical == "update" and p.fields == ["rules"] and "policy" in p.subject_text
