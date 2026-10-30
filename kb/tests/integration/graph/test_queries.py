"""Graph queries (K10): bitemporal expand, ACL-safe traversals, paths, hub-penalised PPR, consistency."""
from __future__ import annotations

import pytest

from kb.acl import system_principal
from kb.graph import service
from kb.journal.writer import journal_tx
from tests.conftest import at
from tests.integration.graph.helpers import EXEC, FP, apply, build_scenario, cause, drain_graph, rec, rel, user

pytestmark = pytest.mark.integration


def ids(res):
    return sorted(n["entity_id"] for n in res["nodes"])


def edge_set(res):
    return sorted((e["source"], e["type"], e["target"]) for e in res["edges"])


def test_closed_statement_and_known_at(db, gt):
    build_scenario(db, gt)
    drain_graph(db, gt)
    p = system_principal(gt)
    now = service.expand(p, ["jira.FP-4"])
    assert ids(now) == ["jira.FP-1", "jira.FP-2", "jira.FP-4"]
    assert ("jira.FP-4", "relates_to", "jira.FP-3") not in edge_set(now)
    # known on 3 March: the statement FP-4 relates_to FP-3 was still open (closed on 5 March)
    then = service.expand(p, ["jira.FP-4"], known_at=at("2026-03-03T00:00:00+03:00"))
    assert ("jira.FP-4", "relates_to", "jira.FP-3") in edge_set(then)
    assert "jira.FP-1" not in ids(then)  # the informed_by link was declared on 10 March
    e = next(x for x in then["edges"] if x["target"] == "jira.FP-3")
    assert e["edge"] == "relation" and e["recorded_at"] == at("2026-03-02T10:00:00+03:00")
    # the closure on 5 March is not known on 3 March: nothing recorded after K is in the answer (Docs/KB/AS-OF.md)
    assert e["retracted_at"] is None and e["open"] is True
    # node state as known at K: FP-4 was at version 1
    assert next(n for n in then["nodes"] if n["entity_id"] == "jira.FP-4")["version_id"] == "jira.FP-4@1"
    # before FP-4 existed there is nothing to expand
    assert service.expand(p, ["jira.FP-4"], known_at=at("2026-03-01T12:30:00+03:00"))["nodes"] == []
    # paths follow the same time slice
    assert service.path(p, "jira.FP-4", "jira.FP-3")["length"] == 2
    assert service.path(p, "jira.FP-4", "jira.FP-3", known_at=at("2026-03-03T00:00:00+03:00"))["length"] == 1
    # a deleted entity: its own statements are closed, so it is gone from the current graph but present before
    assert ids(service.expand(p, ["jira.FP-2"]))[:] == ["jira.FP-1", "jira.FP-2", "jira.FP-3", "jira.FP-4"]
    before_delete = service.expand(p, ["jira.FP-2"], known_at=at("2026-03-07T12:00:00+03:00"))
    assert "jira.FP-5" in ids(before_delete)


def test_inferred_links_and_predicate_filter(db, gt):
    build_scenario(db, gt)
    drain_graph(db, gt)
    p = system_principal(gt)
    assert "jira.FP-3" not in ids(service.expand(p, ["jira.FP-4"]))
    inf = service.expand(p, ["jira.FP-4"], include_inferred=True)
    e = next(x for x in inf["edges"] if x["target"] == "jira.FP-3")
    assert e["edge"] == "causal" and e["review_status"] == "pending" and e["origin"] == "inferred:rules@1"
    assert e["effect_version"] == "jira.FP-4@2" and e["cause_version"] == "jira.FP-3@1"
    only = service.expand(p, ["jira.FP-4"], predicates=["informed_by"])
    assert edge_set(only) == [("jira.FP-4", "informed_by", "jira.FP-1")]
    two = service.expand(p, ["jira.FP-4"], depth=2)
    assert {n["entity_id"]: n["depth"] for n in two["nodes"]}["jira.FP-3"] == 2


def test_budget_truncation(db, gt):
    apply(db, gt, [rec("HUB-1", kind="Epic")], "2026-03-01T09:00:00+03:00")
    for i in range(12):
        apply(db, gt, [rec(f"L-{i}", statements=[rel("relates_to", "HUB-1")])], f"2026-03-01T10:{i:02d}:00+03:00")
    drain_graph(db, gt)
    p = system_principal(gt)
    full = service.expand(p, ["jira.HUB-1"])
    assert len(full["nodes"]) == 13 and len(full["edges"]) == 12 and full["truncated_by_budget"] is False
    cut = service.expand(p, ["jira.HUB-1"], max_nodes=5)
    assert len(cut["nodes"]) == 5 and cut["truncated_by_budget"] is True
    assert all(e["source"] in ids(cut) and e["target"] in ids(cut) for e in cut["edges"])
    cut_e = service.expand(p, ["jira.HUB-1"], max_edges=3)
    assert len(cut_e["edges"]) == 3 and cut_e["truncated_by_budget"] is True
    assert service.expand(p, ["jira.HUB-1"], depth=99)["query"]["depth"] == service.MAX_DEPTH


def test_hidden_middle_node_blocks_traversal(db, gt):
    apply(db, gt, [rec("A-1", acl=FP)], "2026-03-01T10:00:00+03:00")
    apply(db, gt, [rec("M-1", kind="Decision", acl=EXEC, statements=[rel("relates_to", "A-1")])],
          "2026-03-01T11:00:00+03:00")
    apply(db, gt, [rec("B-1", acl=FP, statements=[rel("relates_to", "M-1")])], "2026-03-01T12:00:00+03:00")
    with journal_tx(db, gt, "editor", at=at("2026-03-01T13:00:00+03:00")) as j:
        j.declare_link(effect_entity_id="jira.B-1", cause_entity_id="jira.M-1", predicate="decided_in")
    drain_graph(db, gt)
    admin = system_principal(gt)
    sofia = user(gt, "grp:all-staff", "grp:product", "grp:flowpilot", name="user:sofia.lind")

    found = service.path(admin, "jira.A-1", "jira.B-1")
    assert found["found"] and found["length"] == 2 and found["paths"][0]["nodes"] == ["jira.A-1", "jira.M-1", "jira.B-1"]

    hidden = service.path(sofia, "jira.A-1", "jira.B-1")
    assert hidden["found"] is False and hidden["paths"] == [] and hidden["nodes"] == {}
    for depth in (1, 2, 3):
        res = service.expand(sofia, ["jira.A-1"], depth=depth)
        assert ids(res) == ["jira.A-1"] and res["edges"] == [] and res["truncated_by_budget"] is False
    both = service.expand(sofia, ["jira.A-1", "jira.B-1", "jira.M-1"], depth=2)
    assert both["seeds"] == ["jira.A-1", "jira.B-1"] and ids(both) == ["jira.A-1", "jira.B-1"] and both["edges"] == []
    # the budget flag does not betray hidden neighbours
    assert service.expand(sofia, ["jira.A-1"], max_nodes=1)["truncated_by_budget"] is False
    assert service.expand(admin, ["jira.A-1"], max_nodes=1)["truncated_by_budget"] is True
    assert service.neighborhood(sofia, "jira.M-1") is None
    assert service.path(sofia, "jira.A-1", "jira.M-1")["found"] is False
    assert service.ppr(sofia, ["jira.A-1", "jira.B-1"]) == []
    assert service.version_links(sofia, "jira.B-1@1") == []
    assert [x["entity_id"] for x in service.version_links(admin, "jira.B-1@1")] == ["jira.M-1"]
    assert "jira.M-1" in [r["entity_id"] for r in service.ppr(admin, ["jira.A-1"])]


def test_ppr_prefers_co_connected_node_over_hub(db, gt):
    seeds = ["S-1", "S-2", "S-3"]
    for s in seeds:
        apply(db, gt, [rec(s)], "2026-03-01T09:00:00+03:00")
    apply(db, gt, [rec("X-1", kind="Report", statements=[rel("relates_to", s) for s in seeds])],
          "2026-03-01T10:00:00+03:00")
    apply(db, gt, [rec("HUB-1", kind="Project", statements=[rel("in_project", s) for s in seeds])],
          "2026-03-01T10:30:00+03:00")
    for i in range(30):
        apply(db, gt, [rec(f"L-{i}", statements=[rel("in_project", "HUB-1")])], f"2026-03-01T11:{i:02d}:00+03:00")
    apply(db, gt, [rec("P-1", statements=[rel("relates_to", "S-1")])], "2026-03-01T12:00:00+03:00")
    drain_graph(db, gt)
    p = system_principal(gt)
    res = service.ppr(p, [f"jira.{s}" for s in seeds], top_k=10)
    order = [r["entity_id"] for r in res]
    assert order[0] == "jira.X-1", res
    assert order.index("jira.X-1") < order.index("jira.HUB-1")
    assert order.index("jira.X-1") < order.index("jira.P-1")
    top = res[0]
    assert top["relation"] == service.PPR_LABEL and top["via"] == ["relates_to"] and top["path"][-1] == "jira.X-1"
    assert top["score"] > 0 and top["kind"] == "Report"
    assert not any(r["entity_id"].startswith("jira.S-") for r in res)  # seeds are not answers


def test_ppr_window_and_declared_cause_exclusion(db, gt):
    apply(db, gt, [rec("OLD-1", statements=[])], "2026-01-10T10:00:00+03:00")
    apply(db, gt, [rec("C-1", kind="Report")], "2026-03-02T10:00:00+03:00")
    apply(db, gt, [rec("D-1", kind="Decision", statements=[rel("relates_to", "OLD-1")],
                       causes=[cause("motivated_by", "C-1")])], "2026-03-03T10:00:00+03:00")
    apply(db, gt, [rec("Y-1", statements=[rel("relates_to", "D-1")])], "2026-03-04T10:00:00+03:00")
    apply(db, gt, [rec("Z-1", statements=[rel("relates_to", "D-1")])], "2026-05-01T10:00:00+03:00")
    drain_graph(db, gt)
    p = system_principal(gt)
    ws, we = at("2026-02-15T00:00:00+03:00"), at("2026-03-31T00:00:00+03:00")
    got = {r["entity_id"]: r for r in service.ppr(p, ["jira.D-1"], window_start=ws, window_end=we)}
    assert "jira.Y-1" in got and "jira.C-1" in got
    assert "jira.Z-1" not in got  # related only after the window
    assert "jira.OLD-1" not in got  # no version in the window (active_only defaults to True with a window)
    assert got["jira.Y-1"]["versions_in_window"] == ["jira.Y-1@1"]
    passive = {r["entity_id"] for r in service.ppr(p, ["jira.D-1"], window_start=ws, window_end=we, active_only=False)}
    assert "jira.OLD-1" in passive and "jira.Z-1" not in passive
    excl = {r["entity_id"] for r in service.ppr(p, ["jira.D-1"], window_start=ws, window_end=we,
                                                exclude_declared_causes_of="jira.D-1")}
    assert "jira.C-1" not in excl and "jira.Y-1" in excl
    current = {r["entity_id"] for r in service.ppr(p, ["jira.D-1"])}
    assert {"jira.Z-1", "jira.OLD-1", "jira.C-1", "jira.Y-1"} <= current


def test_neighborhood_and_version_links(db, gt):
    build_scenario(db, gt)
    drain_graph(db, gt)
    p = system_principal(gt)
    nb = service.neighborhood(p, "jira.FP-2")
    assert nb["center"] == "jira.FP-2" and [v["version_id"] for v in nb["versions"]] == ["jira.FP-2@1", "jira.FP-2@2"]
    assert nb["versions"][1]["is_current"] and not nb["versions"][0]["is_current"]
    old = service.neighborhood(p, "jira.FP-2@1", known_at=at("2026-03-03T00:00:00+03:00"))
    assert [v["version_id"] for v in old["versions"]] == ["jira.FP-2@1"] and old["versions"][0]["is_current"]
    assert service.neighborhood(p, "jira.NOPE") is None
    causes = service.version_links(p, "jira.FP-4@2", include_inferred=True)
    assert sorted(c["version_id"] for c in causes) == ["jira.FP-1@2", "jira.FP-3@1"]
    effects = service.version_links(p, "jira.FP-1@1", direction="effects")
    assert sorted(c["version_id"] for c in effects) == ["jira.FP-2@1", "jira.FP-3@1"]  # links stay bound to @1


def test_consistency_sample_has_no_mismatches(db, gt):
    build_scenario(db, gt)
    drain_graph(db, gt)
    for p in (system_principal(gt), user(gt, "grp:flowpilot")):
        res = service.consistency_sample(db, p, samples=40, seed=7)
        assert res["checked"] > 0 and res["mismatches"] == [] and res["ok"] is True, res
    admin = service.consistency_sample(db, system_principal(gt), samples=40, seed=11)
    assert admin["counts_match"] is True
    # lag is reported, and samples stay below the graph watermark
    apply(db, gt, [rec("FP-1", kind="Epic", fields={"summary": "late"})], "2026-04-01T10:00:00+03:00")
    lag = service.consistency_sample(db, system_principal(gt), samples=40, seed=3)
    assert lag["watermark"]["lag"] == 1 and lag["mismatches"] == [] and lag["counts_match"] is None
    wm = service.watermark(db, gt, at("2026-05-01T00:00:00+03:00"))
    assert wm["known_at_beyond_watermark"] is True
    assert service.watermark(db, gt, at("2026-03-02T00:00:00+03:00"))["known_at_beyond_watermark"] is False


def test_query_plans_start_from_an_index(db, gt, monkeypatch):
    """Every Cypher statement the service runs is anchored on a label-property index: a plan that scans all
    nodes of a label would read every tenant of the shared database."""
    from kb.graph import memgraph as mg

    build_scenario(db, gt)
    drain_graph(db, gt)
    recorded: list[tuple[str, dict]] = []
    real = mg.read

    def spy(cypher, **params):
        recorded.append((cypher, params))
        return real(cypher, **params)

    monkeypatch.setattr(mg, "read", spy)
    for p in (system_principal(gt), user(gt, "grp:flowpilot")):
        k = at("2026-03-03T00:00:00+03:00")
        service.expand(p, ["jira.FP-4", "jira.FP-2@1"], depth=3, include_inferred=True, predicates=["relates_to"])
        service.expand(p, ["jira.FP-4"], depth=2, known_at=k)
        service.neighborhood(p, "jira.FP-2", known_at=k)
        service.path(p, "jira.FP-4", "jira.FP-1")
        service.version_links(p, "jira.FP-4@2", direction="effects")
        service.ppr(p, ["jira.FP-2"], window_start=at("2026-02-01T00:00:00+03:00"),
                    window_end=at("2026-03-31T00:00:00+03:00"), exclude_declared_causes_of="jira.FP-2")
        service.consistency_sample(db, p, samples=5, seed=1)
    monkeypatch.setattr(mg, "read", real)
    assert len(recorded) > 20
    for cypher, params in recorded:
        plan =[r["QUERY PLAN"].strip() for r in real("EXPLAIN " + cypher, **params)]
        scans = [op for op in plan if op.startswith("* ScanAll") and "ByLabelProperties" not in op]
        assert not scans, (cypher, plan)
