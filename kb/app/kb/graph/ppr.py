"""Personalized PageRank with hub penalty (regime 2, research §5.6; KG stack element 6, HippoRAG-style).

Plain uniform PPR is biased toward high-degree nodes: mass that enters a hub circulates through its many
neighbours and comes back. Two corrections, both driven by the (visible) degree of the nodes:

- transition weights  w(u, v) = 1 / (log(2 + deg u) * log(2 + deg v))  — a walk prefers specific neighbours;
- the final score is degree-normalised: score(v) = ppr(v) / sqrt(deg v)  — the stationary degree bias of an
  undirected random walk is removed half-way, so a node co-connected to several seeds beats both a hub and a
  node attached to a single seed.

Pure numpy power iteration (networkx.pagerank needs scipy, which the service does not ship).
"""
from __future__ import annotations

import math
from typing import Hashable, Iterable

import numpy as np


def hub_weight(deg_u: int, deg_v: int) -> float:
    return 1.0 / (math.log(2 + max(deg_u, 0)) * math.log(2 + max(deg_v, 0)))


def personalized_pagerank(
    nodes: list[Hashable],
    edges: Iterable[tuple[Hashable, Hashable, float]],
    personalization: dict[Hashable, float],
    *,
    alpha: float = 0.85,
    tol: float = 1e-10,
    max_iter: int = 200,
) -> dict[Hashable, float]:
    """PPR on an undirected weighted graph (each edge is used in both directions; parallel edges add up).
    A dangling node (no edges) returns its mass to the personalization vector."""
    n = len(nodes)
    if n == 0:
        return {}
    idx = {v: i for i, v in enumerate(nodes)}
    src_l, dst_l, w_l = [], [], []
    for u, v, w in edges:
        if u == v or w <= 0 or u not in idx or v not in idx:
            continue
        iu, iv = idx[u], idx[v]
        src_l += [iu, iv]
        dst_l += [iv, iu]
        w_l += [w, w]
    r = np.zeros(n)
    for k, val in personalization.items():
        if k in idx and val > 0:
            r[idx[k]] = val
    if r.sum() <= 0:
        return {v: 0.0 for v in nodes}
    r /= r.sum()
    src = np.asarray(src_l, dtype=np.int64)
    dst = np.asarray(dst_l, dtype=np.int64)
    w = np.asarray(w_l, dtype=float)
    out = np.zeros(n)
    if len(w):
        np.add.at(out, src, w)
    p = w / out[src] if len(w) else w
    dangling = out == 0
    x = r.copy()
    for _ in range(max_iter):
        nxt = np.zeros(n)
        if len(w):
            np.add.at(nxt, dst, x[src] * p)
        nxt = alpha * (nxt + x[dangling].sum() * r) + (1 - alpha) * r
        done = np.abs(nxt - x).sum() < tol
        x = nxt
        if done:
            break
    return {v: float(x[i]) for v, i in idx.items()}


def hub_penalized_scores(
    nodes: list[Hashable],
    edges: Iterable[tuple[Hashable, Hashable]],
    seeds: Iterable[Hashable],
    degree: dict[Hashable, int] | None = None,
    *,
    alpha: float = 0.85,
) -> dict[Hashable, tuple[float, float]]:
    """{node: (score, raw_ppr)} — PPR seeded uniformly on `seeds` with hub-penalising weights. `degree` is the
    visible degree of each node in the whole (windowed) graph; it defaults to the degree within `edges`."""
    edge_list = [(u, v) for u, v in edges if u != v]
    if degree is None:
        nb: dict[Hashable, set] = {v: set() for v in nodes}
        for u, v in edge_list:
            nb.setdefault(u, set()).add(v)
            nb.setdefault(v, set()).add(u)
        degree = {v: len(s) for v, s in nb.items()}
    seed_list = [s for s in seeds if s in set(nodes)]
    weighted = [(u, v, hub_weight(degree.get(u, 1), degree.get(v, 1))) for u, v in edge_list]
    pr = personalized_pagerank(nodes, weighted, {s: 1.0 for s in seed_list}, alpha=alpha)
    return {v: (s / math.sqrt(max(degree.get(v, 1), 1)), s) for v, s in pr.items()}
