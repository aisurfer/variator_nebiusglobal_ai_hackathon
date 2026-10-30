"""Graph module units without infrastructure: PPR math, hub penalty, time conversion, Cypher fragments."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kb.acl import PrincipalCtx
from kb.graph import ppr as P
from kb.graph import service as S
from kb.graph.memgraph import from_us, to_us
from kb.graph.projector import _next_pairs


def hub_graph(leaves: int = 30, hub_seeds: int = 3):
    seeds = ["S1", "S2", "S3"]
    edges = [("X", s) for s in seeds] + [("H", s) for s in seeds[:hub_seeds]]
    edges += [("H", f"L{i}") for i in range(leaves)] + [("P", "S1")]
    nodes = sorted({a for e in edges for a in e})
    return nodes, edges, seeds


def test_ppr_is_a_distribution_and_respects_personalization():
    nodes, edges, seeds = hub_graph()
    pr = P.personalized_pagerank(nodes, [(u, v, 1.0) for u, v in edges], {s: 1.0 for s in seeds})
    assert sum(pr.values()) == pytest.approx(1.0, abs=1e-9)
    assert all(v >= 0 for v in pr.values())
    assert min(pr[s] for s in seeds) > max(pr[f"L{i}"] for i in range(30))
    # a dangling seed keeps its mass; an empty personalization gives zeros
    iso = P.personalized_pagerank(["a", "b"], [], {"a": 1.0})
    assert iso["a"] == pytest.approx(1.0) and iso["b"] == 0
    assert P.personalized_pagerank(["a"], [], {}) == {"a": 0.0}
    assert P.personalized_pagerank([], [], {"a": 1}) == {}


def test_uniform_ppr_favours_the_hub_and_the_penalty_fixes_it():
    nodes, edges, seeds = hub_graph()
    uniform = P.personalized_pagerank(nodes, [(u, v, 1.0) for u, v in edges], {s: 1.0 for s in seeds})
    assert uniform["H"] > uniform["X"]
    scored = P.hub_penalized_scores(nodes, edges, seeds)
    assert scored["X"][0] > scored["H"][0]
    assert scored["X"][0] > scored["P"][0]  # several seeds beat a single seed
    # with an external (larger) degree the hub is penalised further
    deg = {n: 1 for n in nodes} | {"X": 3, "H": 200, "S1": 3, "S2": 2, "S3": 2}
    assert P.hub_penalized_scores(nodes, edges, seeds, deg)["H"][0] < scored["H"][0]


@pytest.mark.parametrize("leaves,hub_seeds", [(5, 2), (30, 1), (30, 2), (30, 3), (40, 3)])
def test_penalty_is_robust(leaves, hub_seeds):
    nodes, edges, seeds = hub_graph(leaves, hub_seeds)
    s = P.hub_penalized_scores(nodes, edges, seeds)
    assert s["X"][0] > s["H"][0] and s["X"][0] > s["P"][0]


def test_hub_weight_decreases_with_degree():
    assert P.hub_weight(1, 1) > P.hub_weight(1, 10) > P.hub_weight(10, 10) > 0


def test_time_microseconds_roundtrip():
    dt = datetime(2026, 3, 2, 11, 0, 0, 123456, tzinfo=timezone(timedelta(hours=3)))
    us = to_us(dt)
    assert isinstance(us, int) and from_us(us) == dt
    assert to_us(None) is None and from_us(None) is None
    assert to_us(datetime(2026, 1, 1)) == to_us(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert to_us(datetime.max.replace(tzinfo=timezone.utc)) is None
    assert to_us(dt + timedelta(microseconds=1)) - us == 1


def test_cypher_fragments():
    cur = S._edge_ok("e", "current", include_inferred=False, predicates=False)
    assert "e.tx_to IS NULL" in cur and "review_status = 'accepted'" in cur and "STARTS WITH 'inferred'" in cur
    k = S._edge_ok("e", "known_at", include_inferred=True, predicates=True)
    assert "e.tx_from <= $k" in k and "e.tx_to > $k" in k and "review_status" not in k and "e.type IN $preds" in k
    w = S._edge_ok("e", "window", include_inferred=False, predicates=False)
    assert "$we" in w and "$ws" in w
    n = S._node_ok("n", "known_at")
    assert "any(g IN n.acl WHERE g IN $groups)" in n and "$admin" in n and "n.created_at <= $k" in n
    for frag in (cur, k, w, n):  # values never inlined
        assert "'grp:" not in frag
    base = S._base(PrincipalCtx("t1", "user:a", frozenset({"user:a", "grp:x"}), False))
    assert base == {"t": "t1", "admin": False, "groups": ["grp:x", "user:a"]}


def test_ids_and_paths_helpers():
    assert S._candidates(["jira.FP-1@3", "jira.FP-2", "", "person.a@b.c"]) == \
        ["jira.FP-1@3", "jira.FP-1", "jira.FP-2", "person.a@b.c"]
    assert S._to_entity("jira.FP-1@3", {"jira.FP-1"}) == "jira.FP-1"
    assert S._to_entity("jira.FP-1@3", {"jira.FP-1@3", "jira.FP-1"}) == "jira.FP-1@3"
    assert S._to_entity("x", set()) is None
    adj = {"s": {"a": {"r"}}, "a": {"s": {"r"}, "b": {"q", "p"}}, "b": {"a": {"q", "p"}}}
    parent = S._bfs_parents(adj, ["s"])
    assert S._chain(parent, "b") == ["s", "a", "b"] and S._chain(parent, "zz") == ["zz"]
    assert _next_pairs([{"vid": "e@2", "entity_id": "e", "seq": 2}, {"vid": "e@1", "entity_id": "e", "seq": 1},
                        {"vid": "f@1", "entity_id": "f", "seq": 1}]) == [{"a": "e@1", "b": "e@2"}]
