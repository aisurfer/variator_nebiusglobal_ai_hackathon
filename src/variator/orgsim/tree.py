"""Weekly scenario tree with a cap on the nodes per level (09-ACTORS-AND-FORKS §13.3).

Each node gives ``k`` variants of the next week. Without a cap the tree grows as k^n,
so each level keeps at most ``max_nodes`` nodes. The kept nodes are a uniform sample
without replacement of all children of the level. Thus every full path has the same
probability to stay, and the leaves are a fair Monte Carlo sample with equal weights
and shared prefixes.

Common random numbers:

- child ``j`` of node ``p`` steps with ``derived_rng(seed, f"{p}/{j}")``;
- the selection uses its own stream per level (``<root>:select:<level>``). It does not
  depend on the states or on the fork, so the genealogy is the same in every fork.

The selection is known before the states, so only the kept children are stepped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from variator.orgsim.streams import Streams, child_key, stream_label
from variator.synthetic.company import derived_rng

ROOT_KEY = "tree"


@dataclass(frozen=True)
class Genealogy:
    """The shape of a tree: per level, the parent index, the child number and the key of each node."""

    k: int
    max_nodes: int
    seed: int
    root_key: str
    parents: tuple[np.ndarray, ...]  # level 0: [-1]
    child_index: tuple[np.ndarray, ...]  # level 0: [-1]
    keys: tuple[tuple[str, ...], ...]

    @property
    def n_levels(self) -> int:
        """Number of steps (weeks); the tree has n_levels + 1 levels of nodes."""
        return len(self.parents) - 1

    def sizes(self) -> list[int]:
        return [len(p) for p in self.parents]

    def same_as(self, other: Genealogy) -> bool:
        return self.keys == other.keys and all(
            np.array_equal(a, b) for a, b in zip(self.parents, other.parents, strict=True)
        )


def plan_genealogy(
    levels: int, *, k: int = 3, max_nodes: int = 200, seed: int, root_key: str = ROOT_KEY
) -> Genealogy:
    """Parents and keys of all levels, from the seed only (no state, no fork)."""
    if levels < 0 or k < 1 or max_nodes < 1:
        raise ValueError("levels >= 0, k >= 1 and max_nodes >= 1 are required")
    if "/" in root_key or ":" in root_key:
        raise ValueError("root_key must not contain '/' or ':'")
    parents = [np.array([-1])]
    child_index = [np.array([-1])]
    keys = [(root_key,)]
    for level in range(1, levels + 1):
        n_children = len(keys[-1]) * k
        if n_children <= max_nodes:
            chosen = np.arange(n_children)
        else:
            rng = derived_rng(seed, stream_label(root_key, "select", level))
            chosen = np.sort(rng.choice(n_children, size=max_nodes, replace=False))
        parent, j = chosen // k, chosen % k
        parents.append(parent.astype(np.int64))
        child_index.append(j.astype(np.int64))
        keys.append(tuple(child_key(keys[-1][p], int(c)) for p, c in zip(parent, j, strict=True)))
    return Genealogy(k, max_nodes, int(seed), root_key, tuple(parents), tuple(child_index), tuple(keys))


@dataclass(frozen=True)
class TreeNode:
    """Where a step happens: the level of the new node (1 = first week), its index and key."""

    level: int
    index: int
    key: str
    parent: int


@dataclass
class ScenarioTree:
    """States of all kept nodes. ``states[level][i]`` belongs to ``genealogy.keys[level][i]``."""

    genealogy: Genealogy
    states: list[list[Any]] = field(default_factory=list)

    @property
    def parents(self) -> tuple[np.ndarray, ...]:
        return self.genealogy.parents

    @property
    def leaves(self) -> list[Any]:
        return self.states[-1]

    def leaf_weights(self) -> np.ndarray:
        """Equal weights of the leaves (they sum to 1)."""
        n = len(self.leaves)
        return np.full(n, 1.0 / n)

    def path(self, leaf: int) -> list[int]:
        """Node index at each level, from the root to ``leaf``."""
        out = [int(leaf)]
        for level in range(self.genealogy.n_levels, 0, -1):
            out.append(int(self.parents[level][out[-1]]))
        return out[::-1]

    def path_states(self, leaf: int) -> list[Any]:
        return [self.states[level][i] for level, i in enumerate(self.path(leaf))]

    def stack(self, level: int) -> np.ndarray:
        """States of one level as an array (for numeric states)."""
        return np.asarray(self.states[level])

    def leaf_paths(self) -> np.ndarray:
        """Node index per [leaf, level]."""
        return np.array([self.path(i) for i in range(len(self.leaves))], dtype=np.int64)


def grow_tree(
    root: Any,
    step: Callable[..., Any],
    *,
    levels: int,
    k: int = 3,
    max_nodes: int = 200,
    seed: int,
    root_key: str = ROOT_KEY,
    rng_kind: str = "generator",
    with_node: bool = False,
) -> ScenarioTree:
    """Grow a tree from ``root`` with ``step(state, rng) -> state``.

    ``rng_kind="generator"`` gives the step a ``numpy.random.Generator`` of the node
    stream; ``"streams"`` gives a ``Streams`` scoped to the node key, so the step can
    take one named stream per entity. ``with_node=True`` calls ``step(state, rng, node)``
    with a ``TreeNode`` (for dated interventions). Forks use the same ``seed``,
    ``k``, ``max_nodes`` and ``root_key``; then their genealogy and node streams agree.
    """
    if rng_kind not in ("generator", "streams"):
        raise ValueError("rng_kind must be 'generator' or 'streams'")
    g = plan_genealogy(levels, k=k, max_nodes=max_nodes, seed=seed, root_key=root_key)
    states: list[list[Any]] = [[root]]
    for level in range(1, levels + 1):
        row = []
        for i, (parent, key) in enumerate(zip(g.parents[level], g.keys[level], strict=True)):
            rng = derived_rng(seed, key) if rng_kind == "generator" else Streams(seed, (key,))
            prev = states[level - 1][int(parent)]
            if with_node:
                row.append(step(prev, rng, TreeNode(level, i, key, int(parent))))
            else:
                row.append(step(prev, rng))
        states.append(row)
    return ScenarioTree(g, states)
