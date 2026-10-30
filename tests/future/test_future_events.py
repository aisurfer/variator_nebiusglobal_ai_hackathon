"""The week event tree (WP18c): shares, split labels, the pinned day-level paths, determinism, bands."""

from __future__ import annotations

import json

import numpy as np
import pytest

from variator.future import events as evt
from variator.future.forks import company_forks

N = 150
SEED = 7


@pytest.fixture(scope="module")
def forks(company):
    return {f.id: f for f in company_forks(company.cfg)}


@pytest.fixture(scope="module")
def trees(company, forks):
    return evt.company_trees(
        company, company.cfg, [forks["status_quo"], forks["pay_tech_debt"], forks["hire_5"]], n=N, seed=SEED
    )


def _active(tree, w):
    return [nd for nd in tree["nodes"] if nd["from"] <= w < nd["to"]]


def test_shares_sum_to_one_in_every_week(trees):
    for tree in trees.values():
        for w in range(tree["weeks"]):
            assert sum(nd["share"] for nd in _active(tree, w)) == pytest.approx(1.0, abs=1e-5), (
                tree["option"],
                w,
            )


def test_every_split_has_an_event_label(trees):
    for tree in trees.values():
        kids: dict = {}
        for nd in tree["nodes"]:
            kids.setdefault(nd["parent"], []).append(nd)
        assert len(kids[None]) == 1 and kids[None][0]["split"] is None
        for nd in tree["nodes"]:
            if nd["parent"] is None:
                continue
            sp = nd["split"]
            assert sp["text"] and sp["week"] == nd["from"] and sp["type"]
        for parent, cs in kids.items():
            if parent is None:
                continue
            assert len(cs) == 2 and sum(bool(c["split"].get("rest")) for c in cs) == 1
            ev = next(c for c in cs if not c["split"].get("rest"))
            assert ev["split"].get("other") or "weekP10" in ev["split"]


def test_the_six_pinned_paths_keep_their_seeds_and_leaves(trees):
    spec = evt.fixed_paths()
    assert len(spec["paths"]) == 6
    for p in spec["paths"]:
        got = next(x for x in trees[p["fork"]]["paths"] if x.get("fixed") and x["kind"] == p["kind"])
        assert got["seed"] == p["seed"] and got["treeLeaf"] == p["leaf"] and got["keys"] == p["keys"]
        assert len(got["weeks"]) == spec["weeks"] + 1 and got["nodes"][0] == "n0"


def test_same_seed_same_trees(company, forks, trees):
    again = evt.company_trees(
        company, company.cfg, [forks["status_quo"], forks["pay_tech_debt"], forks["hire_5"]], n=N, seed=SEED
    )
    assert json.dumps(again, sort_keys=True) == json.dumps(trees, sort_keys=True)


def test_bands_contain_the_leaf_paths(trees):
    for tree in trees.values():
        nodes = {nd["id"]: nd for nd in tree["nodes"]}
        for p in tree["paths"]:
            if p.get("fixed") or p.get("kind"):
                continue  # leaf representatives: the paths nearest the median cash of their leaf
            leaf = nodes[p["leaf"]]
            w, lo, _, hi = leaf["bands"]["cash"][-1]
            assert lo - 1 <= p["weeks"][w]["cash"] <= hi + 1


def test_paths_follow_the_tree_and_list_their_events(trees):
    for tree in trees.values():
        nodes = {nd["id"]: nd for nd in tree["nodes"]}
        for p in tree["paths"]:
            for a, b in zip(p["nodes"], p["nodes"][1:], strict=False):
                assert nodes[b]["parent"] == a
            assert all("material" in e and "week" in e for e in p["events"])


def test_hires_appear_in_the_hire_fork_only(trees):
    def keys(tree):
        return {e["text"] for p in tree["paths"] for e in p["events"] if e["material"]}

    assert any("new engineers on board" in t for t in keys(trees["hire_5"]))
    assert not any("new engineers on board" in t for t in keys(trees["status_quo"]))


def test_case_a_event_tree_on_the_month_grid(company):
    from datetime import date

    from variator.future import delivery
    from variator.future.forks import case_config

    params = delivery.delivery_params(company.habits, case_config(delivery.CASE))
    res = delivery.simulate(params, 200, 3, as_of=date(2026, 10, 27), crew_=delivery.crew(company.habits))
    trees = delivery.event_trees(res, 3)
    assert set(trees) == {"in_house", "nearshore_tm", "offshore_fixed"}
    for tree in trees.values():
        assert tree["unit"] == "month" and tree["weeks"] == delivery.H and tree["metrics"][0] == "money"
        for w in range(tree["weeks"]):
            assert sum(nd["share"] for nd in _active(tree, w)) == pytest.approx(1.0, abs=1e-5)
    assert np.isfinite([p["weeks"][-1]["money"] for p in trees["in_house"]["paths"]]).all()


def test_texts_are_people_facing_and_keep_the_raw_text(trees):
    assert evt.shock_text("shock:fx_shock", "Currency move raises offshore cost", 0.0885) == (
        "Currency move raises offshore cost by about 9%"
    )
    assert evt.shock_text("shock:regulation_cost", "New regulation", 250_000).endswith("about $250k a year")
    for tree in trees.values():
        for nd in tree["nodes"]:
            sp = nd["split"]
            if sp and sp.get("rest"):
                assert sp.get("of")
            if sp:
                assert "size 0" not in sp["text"]
        for p in tree["paths"]:
            for e in p["events"]:
                assert "size 0" not in e["text"]
                if e["type"] == "shock":
                    assert e["raw"].split(":")[0] in e["text"]
