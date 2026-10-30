"""Weekly scenario tree: level sizes, genealogy shared by forks, common random numbers, weights."""

import numpy as np
import pytest

from variator.orgsim.streams import Streams
from variator.orgsim.tree import TreeNode, grow_tree, plan_genealogy
from variator.synthetic.company import derived_rng


def walk(state: float, rng: np.random.Generator) -> float:
    return state + rng.normal()


@pytest.mark.parametrize(("k", "m", "levels"), [(3, 200, 8), (3, 10, 6), (2, 5, 7), (4, 1, 3), (1, 50, 5)])
def test_level_sizes_never_exceed_the_cap(k, m, levels):
    g = plan_genealogy(levels, k=k, max_nodes=m, seed=1)
    sizes = g.sizes()
    assert sizes[0] == 1 and len(sizes) == levels + 1
    for prev, cur in zip(sizes, sizes[1:], strict=False):
        assert cur == min(prev * k, m) <= m


def test_keys_and_parents_agree():
    g = plan_genealogy(4, k=3, max_nodes=7, seed=2)
    assert g.keys[0] == ("tree",) and g.keys[1] == ("tree/0", "tree/1", "tree/2")
    for level in range(1, 5):
        for i, key in enumerate(g.keys[level]):
            parent, j = g.parents[level][i], g.child_index[level][i]
            assert key == f"{g.keys[level - 1][parent]}/{j}"
        assert len(set(g.keys[level])) == len(g.keys[level])


@pytest.mark.parametrize("seed", range(5))
def test_genealogy_is_the_same_in_every_fork(seed):
    base = grow_tree(0.0, walk, levels=6, k=3, max_nodes=20, seed=seed)
    fork = grow_tree(100.0, lambda s, r: s * 1.1 + r.normal() + 3, levels=6, k=3, max_nodes=20, seed=seed)
    assert base.genealogy.same_as(fork.genealogy)
    other_seed = grow_tree(0.0, walk, levels=6, k=3, max_nodes=20, seed=seed + 100)
    assert not base.genealogy.same_as(other_seed.genealogy)


def test_forks_share_the_random_numbers_of_each_node():
    base = grow_tree(0.0, walk, levels=5, k=3, max_nodes=15, seed=4)
    shifted = grow_tree(0.0, lambda s, r: s + r.normal() + 1.0, levels=5, k=3, max_nodes=15, seed=4)
    for level in range(6):
        diff = shifted.stack(level) - base.stack(level)
        assert np.allclose(diff, level)  # only the decision differs, never the noise


def test_node_stream_is_derived_from_the_key():
    tree = grow_tree(0.0, walk, levels=2, k=2, max_nodes=10, seed=9)
    key = tree.genealogy.keys[2][3]
    parent = tree.states[1][tree.parents[2][3]]
    assert tree.states[2][3] == parent + derived_rng(9, key).normal()


def test_only_kept_children_are_stepped():
    calls = []
    tree = grow_tree(0, lambda s, r: calls.append(1) or s + 1, levels=5, k=3, max_nodes=8, seed=0)
    assert len(calls) == sum(tree.genealogy.sizes()[1:])


def test_leaves_have_equal_weights_and_paths():
    tree = grow_tree(0.0, walk, levels=6, k=3, max_nodes=12, seed=3)
    w = tree.leaf_weights()
    assert np.allclose(w, 1 / 12) and np.isclose(w.sum(), 1)
    paths = tree.leaf_paths()
    assert paths.shape == (12, 7) and (paths[:, 0] == 0).all()
    for leaf in range(12):
        p = tree.path(leaf)
        assert all(tree.parents[lv][p[lv]] == p[lv - 1] for lv in range(1, 7))
        assert tree.path_states(leaf)[-1] == tree.leaves[leaf]


def test_selection_is_uniform_over_children():
    # level 2 has 9 candidates and keeps 4: each candidate stays with probability 4/9
    hits = np.zeros(9)
    for seed in range(3000):
        g = plan_genealogy(2, k=3, max_nodes=4, seed=seed)
        hits[g.parents[2] * 3 + g.child_index[2]] += 1
    assert np.allclose(hits / 3000, 4 / 9, atol=0.04)


def test_the_tree_mean_has_no_bias():
    # a random walk has mean 0 at every level; the leaves of a capped tree must agree
    means = [np.mean(grow_tree(0.0, walk, levels=8, k=3, max_nodes=30, seed=s).leaves) for s in range(60)]
    # shared prefixes make the leaves correlated: the standard error of the mean is about 0.17 here
    assert abs(np.mean(means)) < 0.5


def test_node_context_and_scoped_streams():
    seen = []

    def step(state, streams, node):
        assert isinstance(streams, Streams) and isinstance(node, TreeNode)
        seen.append((node.level, node.key))
        return state + streams.rng("billing", "acme", "pay").random()

    tree = grow_tree(0.0, step, levels=2, k=2, max_nodes=3, seed=5, rng_kind="streams", with_node=True)
    assert seen[0] == (1, "tree/0") and {lv for lv, _ in seen} == {1, 2}
    first = derived_rng(5, "tree/0:billing:acme:pay").random()
    assert tree.states[1][0] == first
    with pytest.raises(ValueError):
        grow_tree(0.0, walk, levels=1, seed=1, rng_kind="bad")
    with pytest.raises(ValueError):
        plan_genealogy(2, seed=1, root_key="a/b")
