"""Plan validation and the rule planner without infra (Docs/KB/QUERY-PLANNER.md)."""
from __future__ import annotations

from datetime import datetime

from kb.reasoning.plan import Vocabulary, plan_from_dict, plan_rules, plan_schema
from kb.reasoning.timeparse import resolve_time

NOW = datetime.fromisoformat("2026-09-22T15:00:00+07:00")
TZ = "Asia/Novosibirsk"
V = Vocabulary(
    "t", kinds={k: {"label": l, "count": 10} for k, l in [("Meeting", "Meeting"), ("Email", "Email"), ("Decision", "Decision"),
                                                        ("Task", "Task"), ("Person", "Person"), ("Organization", "Organization")]},
    predicates={"participant": {"label": "participant", "domain": [], "range": [], "causal": None, "count": 5},
                "author": {"label": "author", "domain": [], "range": [], "causal": None, "count": 5},
                "decided_in": {"label": "decided at", "domain": ["Decision"], "range": ["Meeting"], "causal": "caused_by", "count": 9},
                "works_at": {"label": "works at", "domain": ["Person"], "range": ["Organization"], "causal": None, "count": 2}},
    states={"Task": ["Done", "To Do"]}, state_fields={"Task": "status"})


def _raw(**over):
    raw = {"operation": "list", "kinds": ["Meeting"], "nodes": [], "edges": [],
           "time": {"start": "2026-09-16", "end": "2026-09-23", "applies_to": "x", "event": "occurred", "state_field": None,
                    "state_values": [], "known_at": None, "expression": None, "interpretation": None},
           "text": "", "text_filter": False, "subject": None, "hypothetical": None, "fields": [], "sort": "time_desc", "limit": 50}
    raw.update(over)
    return raw


def test_schema_enumerates_the_ontology():
    s = plan_schema(V)
    assert s["properties"]["kinds"]["items"]["enum"] == sorted(V.kinds)
    assert "*" in s["properties"]["edges"]["items"]["properties"]["predicate"]["enum"]


def test_valid_plan_dates_are_local_and_absolute():
    plan, errors = plan_from_dict(_raw(), "q", V, TZ)
    assert not errors and plan.time.start.isoformat() == "2026-09-16T00:00:00+07:00"


def test_invalid_plans_are_reported():
    _, errors = plan_from_dict(_raw(kinds=["Spaceship"], edges=[{"src": "x", "predicate": "loves", "dst": "n1", "negate": False}],
                                    time={**_raw()["time"], "start": "next tuesday"}), "q", V, TZ)
    text = "; ".join(errors)
    assert "unknown kinds" in text and "unknown nodes" in text and "ISO 8601" in text and "1 to 4 kinds" in text


def test_a_name_in_a_node_id_is_the_name():
    plan, errors = plan_from_dict(_raw(nodes=[{"id": "Ksenia Lavrova", "kinds": ["Person"], "text": None, "key": None, "group": None}],
                                       edges=[{"src": "x", "predicate": "participant", "dst": "Ksenia Lavrova", "negate": False}]), "q", V, TZ)
    assert not errors and plan.nodes[0].text == "Ksenia Lavrova"


def _rules(q):
    return plan_rules(q, now=NOW, tz=TZ, vocab=V, ti=resolve_time(q, NOW, tz=TZ))


def test_rules_internal_meetings_of_the_last_week():
    p = _rules("which meetings, syncs and calls did we hold within the team over the past week?")
    assert p.operation == "list" and p.kinds == ["Meeting"]
    assert [(e.predicate, e.negate) for e in p.edges] == [("participant", True)] and p.nodes[0].group == "external"


def test_rules_nested_pattern_with_a_person():
    p = _rules("decisions made at meetings attended by Belov in July")
    assert [(e.src, e.predicate, e.dst) for e in p.edges] == [("x", "decided_in", "m1"), ("m1", "participant", "n2")]
    assert p.node("n2").text == "Belov"


def test_rules_closed_tasks_and_counts():
    p = _rules("Which tasks were closed in the last 10 days?")
    assert (p.time.event, p.time.state_field, p.time.state_values) == ("state", "status", ["Done"])
    assert _rules("How many emails were there in June?").operation == "count"
    assert _rules("How much money do the logistics people owe us?").operation == "search"  # a fact, not a count


def test_rules_keep_router_operations():
    assert _rules("Why was the release moved?").operation == "why"
    assert _rules("Which of our emails rely on outdated data?").operation == "stale"


def test_an_undeclared_variable_node_takes_kinds_from_the_predicate():
    plan, errors = plan_from_dict(_raw(kinds=["Decision"], nodes=[{"id": "n1", "kinds": ["Person"], "text": "Belov", "key": None, "group": None}],
                                       edges=[{"src": "x", "predicate": "decided_in", "dst": "m1", "negate": False},
                                              {"src": "m1", "predicate": "participant", "dst": "n1", "negate": False}]), "q", V, TZ)
    assert not errors and plan.node("m1").kinds == ["Meeting"] and plan.node("m1").variable
