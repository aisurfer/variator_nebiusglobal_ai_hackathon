"""Graph projection (K10): the Memgraph subgraph mirrors the journal, idempotently and reproducibly."""
from __future__ import annotations

import pytest

from kb.graph import memgraph as mg
from kb.graph import projector
from kb.graph.service import projection_counts
from kb.worker import framework
from tests.integration.graph.helpers import apply, build_scenario, drain_graph, rec, snapshot

pytestmark = pytest.mark.integration

COUNT_KEYS = ("entities", "versions", "current_versions", "next", "relations", "relations_open", "causal_links",
              "causal_links_open")


def assert_counts_match(db, tenant):
    c = projection_counts(db, tenant)
    j, g = c["journal"], c["graph"]
    assert {k: j[k] for k in COUNT_KEYS} == {k: g[k] for k in COUNT_KEYS}, c
    assert g["has_version"] == g["versions"]
    assert g["caused_by"] == g["causal_links"] and g["caused_by_open"] == g["causal_links_open"]
    return c


def test_projection_matches_journal(db, gt):
    build_scenario(db, gt)
    done = drain_graph(db, gt)
    assert done["graph"] > 0
    c = assert_counts_match(db, gt)
    assert c["graph"]["relations"] > c["graph"]["relations_open"]  # closed statements keep their edges

    # versions: chain, single current head, tombstone, retro-dated valid time
    rows = mg.read("""MATCH (e:Entity {tenant: $t, id: 'jira.FP-2'})-[:HAS_VERSION]->(v:Version)
                      RETURN v.vid AS vid, v.is_current AS cur, v.tx_to AS tx_to, v.valid_from AS vf ORDER BY v.seq""",
                   t=gt)
    assert [r["vid"] for r in rows] == ["jira.FP-2@1", "jira.FP-2@2"]
    assert [r["cur"] for r in rows] == [False, True] and rows[0]["tx_to"] is not None and rows[1]["tx_to"] is None
    assert rows[1]["vf"] < rows[0]["tx_to"]  # valid_from of the correction lies before it was recorded
    nxt = mg.read("""MATCH (a:Version {tenant: $t, vid: 'jira.FP-2@1'})-[:NEXT]->(b:Version) RETURN b.vid AS b""", t=gt)
    assert nxt == [{"b": "jira.FP-2@2"}]
    fp5 = mg.read("""MATCH (e:Entity {tenant: $t, id: 'jira.FP-5'})-[:HAS_VERSION]->(v:Version {is_current: true})
                     RETURN e.deleted AS deleted, e.deleted_at AS da, v.op AS op, v.tx_from AS tf""", t=gt)
    assert fp5[0]["deleted"] is True and fp5[0]["op"] == "delete" and fp5[0]["da"] == fp5[0]["tf"]

    # statements -> :REL with the predicate as a property; literal statements are not projected
    rel = mg.read("""MATCH (:Entity {tenant: $t, id: 'jira.FP-4'})-[r:REL]->(o:Entity)
                     RETURN o.id AS o, r.type AS type, r.tx_to IS NULL AS open, r.origin AS origin ORDER BY o""", t=gt)
    assert rel == [{"o": "jira.FP-2", "type": "relates_to", "open": True, "origin": "declared:jira_import"},
                   {"o": "jira.FP-3", "type": "relates_to", "open": False, "origin": "declared:jira_import"}]
    assert mg.read("MATCH (:Entity {tenant: $t})-[r:REL {type: 'status'}]->() RETURN count(r) AS n", t=gt)[0]["n"] == 0

    # causal links: version level and entity level, version ids kept on the entity edge
    cl = db.execute("SELECT * FROM causal_link WHERE tenant_id = %s AND effect_entity_id = 'jira.FP-2'", (gt,)).fetchone()
    cb = mg.read("""MATCH (ev:Version {tenant: $t})-[c:CAUSED_BY {link_id: $l}]->(cv:Version)
                    RETURN ev.vid AS ev, cv.vid AS cv, c.kind AS kind, c.review_status AS rs""", t=gt, l=cl["link_id"])
    assert cb == [{"ev": cl["effect_version"], "cv": cl["cause_version"], "kind": cl["causal_kind"], "rs": "accepted"}]
    ce = mg.read("""MATCH (a:Entity {tenant: $t})-[c:CAUSAL {link_id: $l}]->(b:Entity)
                    RETURN a.id AS a, b.id AS b, c.type AS type, c.effect_version AS ev, c.cause_version AS cv""",
                 t=gt, l=cl["link_id"])
    assert ce == [{"a": "jira.FP-2", "b": "jira.FP-1", "type": "part_of", "ev": cl["effect_version"],
                   "cv": cl["cause_version"]}]
    pend = mg.read("MATCH (:Entity {tenant: $t})-[c:CAUSAL {review_status: 'pending'}]->() RETURN c.origin AS o", t=gt)
    assert pend == [{"o": "inferred:rules@1"}]

    # ACL change reaches the entity and all its versions
    acl = mg.read("""MATCH (e:Entity {tenant: $t, id: 'jira.FP-3'})-[:HAS_VERSION]->(v:Version)
                     RETURN e.acl AS e, collect(v.acl) AS v""", t=gt)
    assert acl[0]["e"] == ["grp:flowpilot", "grp:product"] and all(a == acl[0]["e"] for a in acl[0]["v"])


def test_redelivery_is_idempotent(db, gt):
    build_scenario(db, gt)
    drain_graph(db, gt)
    before = snapshot(gt)
    # a crash before the checkpoint: every event is delivered again
    db.execute("DELETE FROM consumer_checkpoint WHERE consumer = 'graph' AND tenant_id = %s", (gt,))
    assert drain_graph(db, gt)["graph"] > 0
    assert snapshot(gt) == before
    assert drain_graph(db, gt)["graph"] == 0  # checkpoint: nothing pending
    # handling the same batch twice directly
    events, _ = framework._fetch(db, "probe-none", gt, 10_000)
    rel = [e for e in events if e.topic in projector.PROJECTOR.topics]
    projector.PROJECTOR.handle(db, gt, rel)
    projector.PROJECTOR.handle(db, gt, rel)
    assert snapshot(gt) == before
    assert_counts_match(db, gt)


def test_incremental_updates_follow_the_journal(db, gt):
    apply(db, gt, [rec("FP-1"), ], "2026-03-01T10:00:00+03:00")
    drain_graph(db, gt)
    apply(db, gt, [rec("FP-1", fields={"x": 1})], "2026-03-02T10:00:00+03:00")
    apply(db, gt, [rec("FP-9", causes=[], statements=[])], "2026-03-02T11:00:00+03:00")
    drain_graph(db, gt)
    assert_counts_match(db, gt)
    cur = mg.read("""MATCH (e:Entity {tenant: $t, id: 'jira.FP-1'})-[:HAS_VERSION]->(v:Version)
                     RETURN v.vid AS vid, v.is_current AS cur ORDER BY v.seq""", t=gt)
    assert cur == [{"vid": "jira.FP-1@1", "cur": False}, {"vid": "jira.FP-1@2", "cur": True}]


def test_reset_and_drain_reproduce_the_projection(db, gt):
    build_scenario(db, gt)
    drain_graph(db, gt)
    before = snapshot(gt)
    others = mg.read("MATCH (n:Entity) WHERE n.tenant <> $t RETURN count(n) AS n", t=gt)[0]["n"]
    framework.reset(db, gt, consumers=["graph"])
    assert mg.read("MATCH (n {tenant: $t}) RETURN count(n) AS n", t=gt)[0]["n"] == 0
    assert mg.read("MATCH (n:Entity) WHERE n.tenant <> $t RETURN count(n) AS n", t=gt)[0]["n"] == others
    drain_graph(db, gt)
    assert snapshot(gt) == before
    # a direct rebuild from the tables gives the same graph as the outbox replay
    projector.rebuild(db, gt)
    assert snapshot(gt) == before
