"""The week tree: the shape, common random numbers across forks, the leaf examples and the replay."""

from __future__ import annotations

import numpy as np
import pytest

from variator.future import tree
from variator.future.forks import Fork, company_forks

WEEKS = 12
M = 30


@pytest.fixture(scope="module")
def forks(company):
    return {f.id: f for f in company_forks(company.cfg)}


@pytest.fixture(scope="module")
def grown(company, forks):
    return tree.grow(company, forks["pay_tech_debt"], seed=5, weeks=WEEKS, k=3, max_nodes=M)


def test_levels_are_capped_and_selected_uniformly(grown):
    g = grown["genealogy"]
    sizes = g.sizes()
    assert sizes[0] == 1 and sizes[1] == 3 and max(sizes) <= M
    assert all(len(v["cash"]) == n for v, n in zip(grown["values"], sizes, strict=True))


def test_the_same_fork_twice_gives_the_same_tree(company, forks, grown):
    again = tree.grow(company, forks["pay_tech_debt"], seed=5, weeks=WEEKS, k=3, max_nodes=M)
    for a, b in zip(grown["values"], again["values"], strict=True):
        np.testing.assert_array_equal(a["cash"], b["cash"])


def test_forks_share_the_genealogy_and_the_node_streams(company, forks, grown):
    other = tree.grow(company, forks["status_quo"], seed=5, weeks=WEEKS, k=3, max_nodes=M)
    assert grown["genealogy"].same_as(other["genealogy"])
    noop = tree.grow(company, Fork("copy", "copy"), seed=5, weeks=WEEKS, k=3, max_nodes=M)
    for a, b in zip(other["values"], noop["values"], strict=True):
        np.testing.assert_array_equal(a["cash"], b["cash"])


def test_export_has_the_page_schema_and_three_examples(grown):
    doc = tree.export(grown, 5)
    assert {"option", "label", "k", "maxNodes", "weeks", "levels", "bands", "examples"} <= set(doc)
    for w, level in enumerate(doc["levels"]):
        for node in level:
            assert node["week"] == w and set(node["values"]) == {"cash", "load", "incidents", "arr"}
            assert node["parent"] == -1 if w == 0 else 0 <= node["parent"] < len(doc["levels"][w - 1])
    assert sum(n["share"] for n in doc["levels"][-1]) == pytest.approx(
        1.0, abs=1e-4
    )  # shares have 6 decimals
    kinds = [e["kind"] for e in doc["examples"]]
    assert kinds == ["pessimistic", "median", "optimistic"]
    cash = [e["cashEnd"] for e in doc["examples"]]
    assert cash[0] <= cash[1] <= cash[2]
    for e in doc["examples"]:
        assert len(e["keys"]) == WEEKS + 1 and e["keys"][0] == tree.ROOT and e["seed"] == 5


def test_replay_reproduces_the_leaf_path(company, forks, grown):
    doc = tree.export(grown, 5)
    ex = doc["examples"][0]
    path = tree.replay(company, forks["pay_tech_debt"], 5, ex["keys"])
    np.testing.assert_allclose(
        [p["values"]["cash"] for p in path], [w["cash"] for w in ex["weeks"]], rtol=0, atol=1e-6
    )
    texts = [e["text"] for p in path for e in p["events"]]
    assert texts == [e["text"] for e in ex["events"]]


def test_day_examples_bridge(company, grown):
    doc = tree.export(grown, 5)
    days = tree.day_examples({"pay_tech_debt": doc}, company.world.anchor, company.world.as_of)
    rows = days["pay_tech_debt"]
    assert [r["kind"] for r in rows] == ["pessimistic", "median", "optimistic"]
    assert all(len(r["days"]) == tree.DAY_EXAMPLE_DAYS for r in rows)
