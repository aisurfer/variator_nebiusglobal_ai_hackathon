"""Direction words: «why did incidents rise?» is never grounded on «Incidents fell in February 2027»."""

from __future__ import annotations

import pytest

from kb.reasoning import execute
from kb.reasoning.direction import DOWN, UP, direction_of, split_by_direction
from kb.reasoning.router import parse_rules

ROSE = {"id": "sim.SIG-0101", "label": "Incidents rose in March 2027", "kind": "LifeEvent", "score": 0.80}
FELL = {"id": "sim.LE-0088", "label": "Incidents fell in February 2027", "kind": "LifeEvent", "score": 1.00}
PLAIN = {"id": "sim.INC-0042", "label": "api: stalled data loads", "kind": "Incident", "score": 0.70}


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Why did incidents rise?", UP),
        ("Why did incidents increase in May?", UP),
        ("Why has the cash grown?", UP),
        ("Incidents fell in February 2027", DOWN),
        ("Why did the cash drop?", DOWN),
        ("Revenue declined", DOWN),
        ("Why did we set up a webhook?", None),  # a phrasal verb, not a direction
        ("Follow-up on the slow-down", None),
        ("Upland Logistics: platform services agreement", None),  # «up» inside a word
        ("Why did incidents rise and then fall?", None),  # both directions: none
        ("Why did Acme give notice?", None),
        ("", None),
        (None, None),
    ],
)
def test_direction_of(text, want):
    assert direction_of(text) == want


def test_split_keeps_the_order_and_puts_the_opposite_apart():
    keep, against = split_by_direction("Why did incidents rise?", [FELL, ROSE, PLAIN])
    assert [c["id"] for c in keep] == [ROSE["id"], PLAIN["id"]]
    assert [c["id"] for c in against] == [FELL["id"]]
    keep, against = split_by_direction("Why did incidents fall?", [FELL, ROSE, PLAIN])
    assert [c["id"] for c in keep] == [FELL["id"], PLAIN["id"]] and against == [ROSE]
    assert split_by_direction("Why did Acme give notice?", [FELL, ROSE]) == ([FELL, ROSE], [])


def _ground(monkeypatch, cands):
    calls = []

    def fake(ctx, tool, args):
        assert tool == "ground"
        calls.append(args)
        return [dict(c) for c in cands]

    monkeypatch.setattr(execute, "_call", fake)
    return calls


def test_the_subject_has_the_direction_of_the_question(monkeypatch):
    _ground(monkeypatch, [FELL, ROSE, PLAIN])
    subject, _ = execute.ground_subject(None, parse_rules("Why did incidents rise?"))
    assert subject["id"] == ROSE["id"]  # «fell» scored higher, but means the opposite
    subject, _ = execute.ground_subject(None, parse_rules("Why did incidents fall?"))
    assert subject["id"] == FELL["id"]


def test_only_the_opposite_found_gives_no_subject_and_names_it(monkeypatch):
    _ground(monkeypatch, [FELL])
    subject, alternatives = execute.ground_subject(None, parse_rules("Why did incidents rise?"))
    assert subject is None
    assert [a["id"] for a in alternatives] == [FELL["id"]]  # the answer says it may be meant


def test_no_direction_word_keeps_the_ranking(monkeypatch):
    _ground(monkeypatch, [FELL, ROSE])
    subject, _ = execute.ground_subject(None, parse_rules("What happened with incidents in February?"))
    assert subject["id"] == FELL["id"]
