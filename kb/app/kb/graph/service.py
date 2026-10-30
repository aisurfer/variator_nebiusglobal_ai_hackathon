"""Graph queries over the Memgraph projection (K10): expand, neighborhood, path, PPR (regime 2), consistency.

Access control is evaluated inside Cypher (01 §4.5): a node is usable only if its `acl` intersects the
principal's groups (admins see everything); variable-length traversals filter every expanded (edge, node) pair
with a Memgraph filter lambda, so a walk never passes through a hidden node, and an edge is returned only when
both endpoints are visible. Budgets and degrees are computed over visible elements only, so nothing reveals that
something was hidden.

Time: `known_at` K selects edges with tx_from <= K < tx_to (closed history included); without it only open edges
are used. Entity-level traversals use :REL (statements) and :CAUSAL (causal links projected to entities; the
effect and cause version ids stay on the edge). Authoritative causal answers (why/impact/stale with sensitivity)
are computed in PostgreSQL by kb.causal; the graph is for exploration, paths and PPR.

Returned edges are directed: relation subject -> object; causal effect -> cause.
"""
from __future__ import annotations

import functools
import random
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import psycopg

from kb.acl import PrincipalCtx
from kb.graph import memgraph as mg
from kb.graph.memgraph import from_us, now_us, to_us
from kb.graph.ppr import hub_penalized_scores
from kb.journal import reader
from kb.worker.framework import watermarks

MAX_DEPTH = 3
MAX_PATH_DEPTH = 6
HARD_MAX_NODES = 2000
HARD_MAX_EDGES = 5000
PPR_MAX_NODES = 3000
PPR_MAX_EDGES = 20000
PPR_DEFAULT_WINDOW = timedelta(days=30)
PPR_LABEL = "likely related (not declared)"
_VID = re.compile(r"^(.+)@(\d+)$")


def _in_snapshot(fn):
    """All graph reads of one service call run in one read transaction (consistent snapshot, fewer round trips)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with mg.snapshot():
            return fn(*args, **kwargs)
    return wrapper


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(int(v), hi))


def _base(p: PrincipalCtx) -> dict[str, Any]:
    return {"t": p.tenant_id, "admin": bool(p.is_admin), "groups": p.group_list}


# ---------------------------------------------------------------- Cypher fragments (values always as parameters)
def _vis(n: str) -> str:
    return f"({n}.tenant = $t AND ($admin OR any(g IN {n}.acl WHERE g IN $groups)))"


def _node_ok(n: str, mode: str) -> str:
    parts = [_vis(n)]
    if mode == "known_at":
        parts.append(f"{n}.created_at <= $k")
    elif mode == "window":  # existed in the window on either time axis
        parts.append(f"({n}.created_at <= $we OR {n}.first_valid <= $we) "
                     f"AND ({n}.deleted_at IS NULL OR {n}.deleted_at > $ws)")
    return "(" + " AND ".join(parts) + ")"


def _edge_ok(e: str, mode: str, *, include_inferred: bool, predicates: bool) -> str:
    parts = [f"{e}.tenant = $t"]
    if mode == "current":
        parts.append(f"{e}.tx_to IS NULL")
    elif mode == "known_at":
        parts.append(f"{e}.tx_from <= $k AND ({e}.tx_to IS NULL OR {e}.tx_to > $k)")
    elif mode == "window":  # recorded or effective by the window end, not retracted before its start
        parts.append(f"({e}.tx_from <= $we OR {e}.valid_from <= $we) AND ({e}.tx_to IS NULL OR {e}.tx_to > $ws)")
    if not include_inferred:
        parts.append(f"{e}.review_status = 'accepted' AND NOT {e}.origin STARTS WITH 'inferred'")
    if predicates:
        parts.append(f"{e}.type IN $preds")
    return "(" + " AND ".join(parts) + ")"


def _candidates(ids: Iterable[str]) -> list[str]:
    """Entity ids; a version id '<entity>@<seq>' also names its entity."""
    out: list[str] = []
    for i in ids:
        if not i:
            continue
        out.append(i)
        m = _VID.match(i)
        if m:
            out.append(m.group(1))
    return list(dict.fromkeys(out))


def _to_entity(i: str, found: set[str]) -> str | None:
    if i in found:
        return i
    m = _VID.match(i)
    return m.group(1) if m and m.group(1) in found else None


def _seeds(params: dict[str, Any], ids: Iterable[str], mode: str) -> list[str]:
    ids = list(ids)
    cands = _candidates(ids)
    if not cands:
        return []
    rows = mg.read(f"""UNWIND $cands AS c MATCH (s:Entity {{tenant: $t, id: c}}) WHERE {_node_ok('s', mode)}
                       RETURN s.id AS id""", **params, cands=cands)
    found = {r["id"] for r in rows}
    return list(dict.fromkeys(e for e in (_to_entity(i, found) for i in ids) if e))


# Results come back as ONE record holding a list of positional rows: per-record transfer and decoding in the
# Bolt path costs ~70 µs, so collect() is several times faster than one record per row.
EDGE_FIELDS = ("type", "stmt_id", "link_id", "origin", "review_status", "confidence", "tx_from", "tx_to", "valid_from",
               "valid_to", "subject_version", "kind", "effect_version", "cause_version", "sensitivity")
_EDGE_LIST = "[" + ", ".join(f"e.{f}" for f in EDGE_FIELDS) + "]"


def _rows(records: list[dict[str, Any]], key: str = "rows") -> list[list[Any]]:
    return (records[0].get(key) or []) if records else []


def _reach(params: dict[str, Any], seeds: list[str], depth: int, mode: str, *, include_inferred: bool,
           predicates: bool, limit: int) -> list[tuple[str, int]]:
    """Visible entities within `depth` hops of the seeds, nearest first: a level-by-level BFS that stops as soon
    as `limit` nodes are found (hubs make deep levels huge). Every expansion filters the edge and the reached
    node in Cypher, so the walk never continues from a hidden node; `seen` makes cycles harmless. Within a
    level, nodes linked to more frontier nodes come first."""
    seen: dict[str, bool] = {s: True for s in seeds}
    frontier = list(seeds)
    out: list[tuple[str, int]] = []
    for hop in range(1, int(depth) + 1):
        if not frontier or len(out) >= limit:
            break
        rows = _rows(mg.read(
            f"""WITH $seen AS seen UNWIND $frontier AS fid
                MATCH (s:Entity {{tenant: $t, id: fid}})-[e:REL|CAUSAL]-(m)
                WHERE seen[m.id] IS NULL AND {_edge_ok('e', mode, include_inferred=include_inferred, predicates=predicates)}
                  AND {_node_ok('m', mode)}
                WITH m.id AS id, count(DISTINCT s) AS links ORDER BY links DESC, id LIMIT $lim
                RETURN collect(id) AS rows""",
            **params, frontier=frontier, seen=seen, lim=limit - len(out),
        ))
        frontier = list(rows)
        for i in frontier:
            seen[i] = True
            out.append((i, hop))
    return out


def _edges_among(params: dict[str, Any], ids: list[str], mode: str, *, include_inferred: bool, predicates: bool,
                 limit: int, slim: bool = False) -> list[dict[str, Any]]:
    """Edges whose both endpoints are in `ids` (all visible by construction; checked again in Cypher).
    Rows: {source, target, rel: REL|CAUSAL, e: edge properties} (slim: only the type)."""
    if not ids:
        return []
    fields = "[e.type]" if slim else _EDGE_LIST
    rows = _rows(mg.read(
        f"""WITH $idset AS ix UNWIND $ids AS i
            MATCH (a:Entity {{tenant: $t, id: i}})-[e:REL|CAUSAL]->(b)
            WHERE ix[b.id] = true AND {_vis('a')} AND {_vis('b')}
              AND {_edge_ok('e', mode, include_inferred=include_inferred, predicates=predicates)}
            WITH a, b, e LIMIT $lim
            RETURN collect([a.id, b.id, type(e), {fields}]) AS rows""",
        **params, ids=ids, idset={i: True for i in ids}, lim=limit,
    ))
    names = ("type",) if slim else EDGE_FIELDS
    return [{"source": a, "target": b, "rel": rel, "e": dict(zip(names, vals))} for a, b, rel, vals in rows]


def _nodes(params: dict[str, Any], ids: list[str], k: int | None) -> dict[str, dict[str, Any]]:
    """Node cards (state as known at k, else current) for visible ids."""
    if not ids:
        return {}
    vfilter = "v.tx_from <= $k AND (v.tx_to IS NULL OR v.tx_to > $k)" if k is not None else "v.is_current = true"
    rows = _rows(mg.read(
        f"""UNWIND $ids AS i MATCH (n:Entity {{tenant: $t, id: i}}) WHERE {_vis('n')}
            OPTIONAL MATCH (n)-[:HAS_VERSION]->(v) WHERE {vfilter}
            RETURN collect([n.id, n.kind, n.title, n.url, n.head_vid, n.deleted, v.vid, v.kind, v.title, v.op]) AS rows""",
        **(params | {"ids": ids, "k": k}),
    ))
    out: dict[str, dict[str, Any]] = {}
    for nid, kind, title, url, head_vid, deleted, vid, vkind, vtitle, vop in rows:
        out[nid] = {
            "entity_id": nid,
            "kind": vkind or kind,
            "title": vtitle or title,
            "url": url,
            "version_id": vid or (head_vid if k is None else None),
            "deleted": (vop == "delete") if vid else bool(deleted),
        }
    return out


def _edge_out(rel: str, source: str, target: str, e: dict[str, Any], k: int | None = None) -> dict[str, Any]:
    if k is not None and e.get("tx_to") is not None and e["tx_to"] > k:
        # closed after K: open as known at K, and a candidate reviewed after K was pending then (Docs/KB/AS-OF.md)
        e = {**e, "tx_to": None}
        if str(e.get("origin") or "").startswith("inferred:"):
            e["review_status"] = "pending"
    out = {
        "source": source,
        "target": target,
        "type": e.get("type"),
        "origin": e.get("origin"),
        "confidence": e.get("confidence"),
        "review_status": e.get("review_status"),
        "recorded_at": from_us(e.get("tx_from")),
        "retracted_at": from_us(e.get("tx_to")),
        "valid_from": from_us(e.get("valid_from")),
        "valid_to": from_us(e.get("valid_to")),
        "open": e.get("tx_to") is None,
    }
    if rel == "REL":
        out.update({"id": f"stmt:{e.get('stmt_id')}", "edge": "relation", "stmt_id": e.get("stmt_id"),
                    "subject_version": e.get("subject_version")})
    else:
        out.update({"id": f"link:{e.get('link_id')}", "edge": "causal", "link_id": e.get("link_id"),
                    "causal_kind": e.get("kind"), "effect_version": e.get("effect_version"),
                    "cause_version": e.get("cause_version"), "sensitivity": e.get("sensitivity")})
    return out


def _mode(known_at: datetime | None) -> tuple[str, int | None]:
    k = to_us(known_at)
    return ("known_at" if k is not None else "current"), k


# ---------------------------------------------------------------- expand / neighborhood
@_in_snapshot
def expand(
    p: PrincipalCtx,
    ids: list[str],
    *,
    predicates: list[str] | None = None,
    depth: int = 1,
    known_at: datetime | None = None,
    include_inferred: bool = False,
    max_nodes: int = 200,
    max_edges: int = 500,
) -> dict[str, Any]:
    """Visible neighbourhood of the seed entities (or version ids) over relations and causal links.

    Returns {seeds, nodes: [{entity_id, kind, title, url, version_id, deleted, depth, seed}],
    edges: [{id, edge: relation|causal, type, source, target, origin, recorded_at, retracted_at, valid_from,
    valid_to, open, review_status, confidence, (stmt_id, subject_version) | (link_id, causal_kind,
    effect_version, cause_version, sensitivity)}], truncated_by_budget, query}.
    depth: 1..3 (default 1); include_inferred=False keeps only accepted, non-inferred edges.
    """
    depth = _clamp(depth, 1, MAX_DEPTH)
    max_nodes = _clamp(max_nodes, 1, HARD_MAX_NODES)
    max_edges = _clamp(max_edges, 0, HARD_MAX_EDGES)
    mode, k = _mode(known_at)
    preds = sorted(set(predicates)) if predicates else None
    params = _base(p) | {"k": k, "preds": preds}
    query = {"ids": list(ids), "predicates": preds, "depth": depth, "known_at": known_at,
             "include_inferred": include_inferred, "max_nodes": max_nodes, "max_edges": max_edges}
    seeds = _seeds(params, ids, mode)[:max_nodes]
    if not seeds:
        return {"seeds": [], "nodes": [], "edges": [], "truncated_by_budget": False, "query": query}
    budget = max_nodes - len(seeds)
    reach = _reach(params, seeds, depth, mode, include_inferred=include_inferred, predicates=bool(preds),
                   limit=budget + 1) if budget > 0 else []
    truncated = len(reach) > budget
    if budget <= 0:  # seeds alone fill the budget: report truncation only if a visible neighbour exists
        truncated = bool(_reach(params, seeds, 1, mode, include_inferred=include_inferred, predicates=bool(preds),
                                limit=1))
    reach = reach[:max(budget, 0)]
    depth_of = {s: 0 for s in seeds}
    depth_of.update({i: h for i, h in reach})
    node_ids = list(depth_of)
    cards = _nodes(params, node_ids, k)
    nodes = []
    for i in node_ids:
        if i in cards:
            nodes.append(cards[i] | {"depth": depth_of[i], "seed": depth_of[i] == 0})
    rows = _edges_among(params, node_ids, mode, include_inferred=include_inferred, predicates=bool(preds),
                        limit=max(max_edges * 2, max_edges + 1))
    rows.sort(key=lambda r: (min(depth_of[r["source"]], depth_of[r["target"]]),
                             max(depth_of[r["source"]], depth_of[r["target"]]), r["source"], r["target"], r["rel"],
                             str(r["e"].get("type")), str(r["e"].get("origin")), r["e"].get("tx_from") or 0,
                             str(r["e"].get("effect_version")), str(r["e"].get("cause_version"))))
    if len(rows) > max_edges:
        truncated = True
    edges = [_edge_out(r["rel"], r["source"], r["target"], r["e"], k) for r in rows[:max_edges]]
    return {"seeds": seeds, "nodes": nodes, "edges": edges, "truncated_by_budget": truncated, "query": query}


@_in_snapshot
def neighborhood(
    p: PrincipalCtx,
    entity_id: str,
    *,
    depth: int = 1,
    known_at: datetime | None = None,
    include_inferred: bool = False,
    predicates: list[str] | None = None,
    max_nodes: int = 200,
    max_edges: int = 500,
) -> dict[str, Any] | None:
    """expand() around one entity plus its version chain (visual drill-down). None: unknown or not visible."""
    res = expand(p, [entity_id], predicates=predicates, depth=depth, known_at=known_at,
                 include_inferred=include_inferred, max_nodes=max_nodes, max_edges=max_edges)
    if not res["seeds"]:
        return None
    center = res["seeds"][0]
    k = to_us(known_at)
    rows = mg.read(
        f"""MATCH (n:Entity {{tenant: $t, id: $id}})-[:HAS_VERSION]->(v)
            WHERE {_vis('n')} AND ($k IS NULL OR v.tx_from <= $k)
            RETURN properties(v) AS v ORDER BY v.seq""",
        **_base(p), id=center, k=k,
    )
    res["center"] = center
    res["versions"] = [{
        "version_id": r["v"]["vid"], "seq": r["v"]["seq"], "op": r["v"]["op"], "title": r["v"].get("title"),
        "recorded_at": from_us(r["v"]["tx_from"]),
        "superseded_at": from_us(r["v"].get("tx_to")) if k is None or (r["v"].get("tx_to") or 0) <= k else None,
        "effective_from": from_us(r["v"].get("valid_from")),
        "is_current": (r["v"].get("tx_to") is None) if k is None
        else (r["v"].get("tx_to") is None or r["v"]["tx_to"] > k),
    } for r in rows]
    return res


# ---------------------------------------------------------------- paths
@_in_snapshot
def path(
    p: PrincipalCtx,
    source_id: str,
    target_id: str,
    *,
    max_depth: int = 4,
    known_at: datetime | None = None,
    include_inferred: bool = False,
    predicates: list[str] | None = None,
    max_paths: int = 10,
) -> dict[str, Any]:
    """All shortest visible paths (undirected) between two entities, at most max_depth hops (1..6).
    Returns {source, target, found, length, paths: [{nodes: [ids], edges: [edge]}], nodes: {id: card}}."""
    max_depth = _clamp(max_depth, 1, MAX_PATH_DEPTH)
    max_paths = _clamp(max_paths, 1, 50)
    mode, k = _mode(known_at)
    preds = sorted(set(predicates)) if predicates else None
    params = _base(p) | {"k": k, "preds": preds}
    out: dict[str, Any] = {"source": source_id, "target": target_id, "found": False, "length": None, "paths": [],
                           "nodes": {}, "query": {"max_depth": max_depth, "known_at": known_at,
                                                  "include_inferred": include_inferred, "predicates": preds}}
    ends = _seeds(params, [source_id, target_id], mode)
    src, dst = _to_entity(source_id, set(ends)), _to_entity(target_id, set(ends))
    if not src or not dst:
        return out
    out["source"], out["target"] = src, dst
    if src == dst:
        out.update({"found": True, "length": 0, "paths": [{"nodes": [src], "edges": []}],
                    "nodes": _nodes(params, [src], k)})
        return out
    lam = f"(e, n | {_edge_ok('e', mode, include_inferred=include_inferred, predicates=bool(preds))} AND {_node_ok('n', mode)})"
    rows = mg.read(
        f"""MATCH (a:Entity {{tenant: $t, id: $src}}) MATCH (b:Entity {{tenant: $t, id: $dst}})
            MATCH p = (a)-[:REL|CAUSAL *ALLSHORTEST {max_depth} (e, n | 1) w {lam}]-(b)
            RETURN [x IN nodes(p) | x.id] AS ids,
                   [r IN relationships(p) | {{rel: type(r), s: startNode(r).id, o: endNode(r).id, e: properties(r)}}] AS rels
            LIMIT $lim""",
        **params, src=src, dst=dst, lim=max_paths,
    )
    if not rows:
        return out
    seen: set[str] = set()
    for r in rows:
        out["paths"].append({"nodes": r["ids"], "edges": [_edge_out(x["rel"], x["s"], x["o"], x["e"], k) for x in r["rels"]]})
        seen.update(r["ids"])
    out["found"] = True
    out["length"] = len(rows[0]["rels"])
    out["nodes"] = _nodes(params, sorted(seen), k)
    return out


# ---------------------------------------------------------------- version-level causal links
@_in_snapshot
def version_links(
    p: PrincipalCtx, version_id: str, *, direction: str = "causes", known_at: datetime | None = None,
    include_inferred: bool = False,
) -> list[dict[str, Any]]:
    """Direct CAUSED_BY links of one version: its causes (effect -> cause) or its effects, both ends visible."""
    mode, k = _mode(known_at)
    params = _base(p) | {"k": k, "preds": None}
    arrow = "-[c:CAUSED_BY]->" if direction == "causes" else "<-[c:CAUSED_BY]-"
    rows = mg.read(
        f"""MATCH (v:Version {{tenant: $t, vid: $vid}}){arrow}(o)
            WHERE {_vis('v')} AND {_vis('o')} AND {_edge_ok('c', mode, include_inferred=include_inferred, predicates=False)}
            RETURN properties(c) AS c, o.vid AS other, o.entity_id AS other_entity, o.title AS other_title""",
        **params, vid=version_id,
    )
    out = []
    for r in rows:
        c = r["c"]
        out.append({"link_id": c.get("link_id"), "predicate": c.get("predicate"), "causal_kind": c.get("kind"),
                    "origin": c.get("origin"), "review_status": c.get("review_status"),
                    "version_id": r["other"], "entity_id": r["other_entity"], "title": r["other_title"],
                    "recorded_at": from_us(c.get("tx_from")), "retracted_at": from_us(c.get("tx_to"))})
    return out


# ---------------------------------------------------------------- Personalized PageRank (regime 2)
def _declared_causes(params: dict[str, Any], entity_id: str, mode: str, depth: int = 6) -> set[str]:
    lam = f"(e, n | {_edge_ok('e', mode, include_inferred=False, predicates=False)} AND {_node_ok('n', mode)})"
    rows = mg.read(
        f"""MATCH (d:Entity {{tenant: $t, id: $id}}) WHERE {_vis('d')}
            MATCH p = (d)-[:CAUSAL *BFS ..{int(depth)} {lam}]->(c)
            RETURN collect(DISTINCT c.id) AS ids""",
        **params, id=entity_id,
    )
    return set(rows[0]["ids"]) if rows else set()


@_in_snapshot
def ppr(
    p: PrincipalCtx,
    seeds: list[str],
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    top_k: int = 10,
    hops: int = 2,
    exclude_declared_causes_of: str | None = None,
    include_inferred: bool = False,
    active_only: bool | None = None,
    max_nodes: int = PPR_MAX_NODES,
) -> list[dict[str, Any]]:
    """Entities likely related to the seeds — regime 2 (research §5.6), NOT declared causality.

    Personalized PageRank over the visible `hops`-hop subgraph of the seeds, restricted to the window
    [window_start, window_end] on either time axis: an edge counts if it was recorded or became effective by
    window_end and was not retracted before window_start; an entity if it existed then (window_end defaults to
    now, window_start to window_end - 30 days; no window = the current graph). Visible seeds always count.
    Edge weights penalise hubs by inverse log-degree and scores are degree-normalised (kb.graph.ppr).
    active_only (default: True when a window is given) keeps only entities with a version recorded or effective
    in the window. exclude_declared_causes_of: drop that entity and everything it already declares as a cause.

    Returns [{entity_id, title, kind, version_id, score, ppr, via: [edge types along the path from the nearest
    seed], path: [entity ids], versions_in_window, relation: "likely related (not declared)"}].
    """
    hops = _clamp(hops, 1, MAX_DEPTH)
    top_k = _clamp(top_k, 1, 100)
    max_nodes = _clamp(max_nodes, 1, PPR_MAX_NODES)
    windowed = window_start is not None or window_end is not None
    params = _base(p) | {"k": None, "preds": None}
    if windowed:
        we = to_us(window_end) if window_end is not None else now_us()
        ws = to_us(window_start) if window_start is not None else we - int(PPR_DEFAULT_WINDOW / timedelta(microseconds=1))
        if ws > we:
            ws, we = we, ws
        params |= {"ws": ws, "we": we}
        mode = "window"
    else:
        mode = "current"
        ws = we = None
    if active_only is None:
        active_only = windowed
    seed_ids = _seeds(params, seeds, "visible")[:max_nodes]  # the question's focus counts whatever its time
    if not seed_ids:
        return []
    reach = _reach(params, seed_ids, hops, mode, include_inferred=include_inferred, predicates=False,
                   limit=max(max_nodes - len(seed_ids), 1))
    node_ids = seed_ids + [i for i, _ in reach]
    if len(node_ids) <= len(seed_ids):
        return []
    rows = _edges_among(params, node_ids, mode, include_inferred=include_inferred, predicates=False,
                        limit=PPR_MAX_EDGES, slim=True)
    deg_rows = _rows(mg.read(
        f"""UNWIND $ids AS id
            MATCH (n:Entity {{tenant: $t, id: id}})
            OPTIONAL MATCH (n)-[e:REL|CAUSAL]-(m)
            WHERE {_edge_ok('e', mode, include_inferred=include_inferred, predicates=False)} AND {_node_ok('m', mode)}
            WITH id, count(DISTINCT m) AS deg
            RETURN collect([id, deg]) AS rows""",
        **params, ids=node_ids,
    ))
    degree = {i: max(int(d), 1) for i, d in deg_rows}  # visible degree in the (windowed) graph
    scores = hub_penalized_scores(node_ids, [(r["source"], r["target"]) for r in rows], seed_ids, degree)

    excluded = set(seed_ids)
    if exclude_declared_causes_of:
        target = _to_entity(exclude_declared_causes_of, set(_seeds(params, [exclude_declared_causes_of], "current")))
        if target:
            excluded.add(target)
            excluded |= _declared_causes(params, target, mode)
    active: dict[str, list[str]] = {}
    if windowed:
        for i, vids in _rows(mg.read(
            f"""UNWIND $ids AS i MATCH (n:Entity {{tenant: $t, id: i}})-[:HAS_VERSION]->(v)
                WHERE {_vis('n')}
                  AND ((v.tx_from >= $ws AND v.tx_from <= $we) OR (v.valid_from >= $ws AND v.valid_from <= $we))
                WITH n.id AS id, collect(v.vid) AS vids
                RETURN collect([id, vids]) AS rows""",
            **params, ids=node_ids,
        )):
            active[i] = sorted(vids, key=lambda x: int(x.rsplit("@", 1)[-1]))
    cands = [n for n in node_ids if n not in excluded and scores.get(n, (0, 0))[0] > 0
             and (not active_only or n in active)]
    cands.sort(key=lambda n: (-scores[n][0], n))
    cands = cands[:top_k]
    if not cands:
        return []
    adj: dict[str, dict[str, set[str]]] = {}
    for r in rows:
        t = r["e"].get("type") or r["rel"]
        adj.setdefault(r["source"], {}).setdefault(r["target"], set()).add(t)
        adj.setdefault(r["target"], {}).setdefault(r["source"], set()).add(t)
    parent = _bfs_parents(adj, seed_ids)
    cards = _nodes(params, cands, we if windowed else None)
    out = []
    for n in cands:
        chain = _chain(parent, n)
        via = ["|".join(sorted(adj[a][b])) for a, b in zip(chain, chain[1:])]
        card = cards.get(n, {"entity_id": n})
        out.append({
            "entity_id": n, "title": card.get("title"), "kind": card.get("kind"), "version_id": card.get("version_id"),
            "deleted": card.get("deleted", False),
            "score": round(scores[n][0], 6), "ppr": round(scores[n][1], 6),
            "via": via, "path": chain, "versions_in_window": active.get(n, []), "relation": PPR_LABEL,
        })
    return out


def _bfs_parents(adj: dict[str, dict[str, set[str]]], sources: list[str]) -> dict[str, str | None]:
    parent: dict[str, str | None] = {s: None for s in sources}
    q = deque(sources)
    while q:
        u = q.popleft()
        for v in sorted(adj.get(u, {})):
            if v not in parent:
                parent[v] = u
                q.append(v)
    return parent


def _chain(parent: dict[str, str | None], n: str) -> list[str]:
    if n not in parent:
        return [n]
    chain = [n]
    while parent[chain[-1]] is not None:
        chain.append(parent[chain[-1]])  # type: ignore[arg-type]
    return list(reversed(chain))


# ---------------------------------------------------------------- watermark, counts, consistency
def watermark(conn: psycopg.Connection, tenant: str, known_at: datetime | None = None) -> dict[str, Any]:
    wm = watermarks(conn, tenant)
    j, g = wm.get("journal", 0), wm.get("graph", 0)
    out: dict[str, Any] = {"journal": j, "graph": g, "lag": max(j - g, 0)}
    if known_at is not None:
        beyond = False
        if g < j:
            row = conn.execute("SELECT tx_at FROM tx_log WHERE tenant_id = %s AND tx_seq = %s", (tenant, g)).fetchone()
            beyond = row is None or known_at > row["tx_at"]
        out["known_at_beyond_watermark"] = beyond
    return out


@_in_snapshot
def projection_counts(conn: psycopg.Connection, tenant: str) -> dict[str, dict[str, int]]:
    """Journal row counts next to the projected element counts of the tenant."""
    q = lambda sql: conn.execute(sql, (tenant,)).fetchone()  # noqa: E731
    journal = {
        "entities": q("SELECT count(*) AS n FROM entity WHERE tenant_id = %s")["n"],
        "versions": q("SELECT count(*) AS n FROM entity_version WHERE tenant_id = %s")["n"],
        "relations": q("SELECT count(*) AS n FROM statement WHERE tenant_id = %s AND object_id IS NOT NULL")["n"],
        "relations_open": q("SELECT count(*) AS n FROM statement WHERE tenant_id = %s AND object_id IS NOT NULL "
                            "AND upper_inf(tx)")["n"],
        "causal_links": q("SELECT count(*) AS n FROM causal_link WHERE tenant_id = %s")["n"],
        "causal_links_open": q("SELECT count(*) AS n FROM causal_link WHERE tenant_id = %s AND upper_inf(tx)")["n"],
    }
    journal["current_versions"] = journal["entities"]
    journal["next"] = journal["versions"] - journal["entities"]
    t = {"t": tenant}
    one = lambda cy: (mg.read(cy, **t) or [{}])[0]  # noqa: E731
    ent = one("MATCH (n:Entity {tenant: $t}) RETURN count(n) AS n")
    ver = one("MATCH (v:Version {tenant: $t}) RETURN count(v) AS n, count(CASE WHEN v.is_current THEN 1 END) AS cur")
    hv = one("MATCH (:Entity {tenant: $t})-[h:HAS_VERSION]->(:Version) RETURN count(h) AS n")
    nx_ = one("MATCH (:Version {tenant: $t})-[x:NEXT]->(:Version) RETURN count(x) AS n")
    rel = one("MATCH (:Entity {tenant: $t})-[r:REL]->(:Entity) RETURN count(r) AS n, count(CASE WHEN r.tx_to IS NULL THEN 1 END) AS open")
    cau = one("MATCH (:Entity {tenant: $t})-[r:CAUSAL]->(:Entity) RETURN count(r) AS n, count(CASE WHEN r.tx_to IS NULL THEN 1 END) AS open")
    cby = one("MATCH (:Version {tenant: $t})-[r:CAUSED_BY]->(:Version) RETURN count(r) AS n, count(CASE WHEN r.tx_to IS NULL THEN 1 END) AS open")
    graph = {
        "entities": ent.get("n", 0), "versions": ver.get("n", 0), "current_versions": ver.get("cur", 0),
        "has_version": hv.get("n", 0), "next": nx_.get("n", 0), "relations": rel.get("n", 0),
        "relations_open": rel.get("open", 0), "causal_links": cau.get("n", 0), "causal_links_open": cau.get("open", 0),
        "caused_by": cby.get("n", 0), "caused_by_open": cby.get("open", 0),
    }
    return {"journal": journal, "graph": graph}


def consistency_sample(conn: psycopg.Connection, p: PrincipalCtx, samples: int = 20, *,
                       seed: int | None = None) -> dict[str, Any]:
    """Random (entity, K) pairs (research §5.7): the version id the journal returns for known_at=K
    (kb.journal.reader.version_at) vs the graph Version with tx_from <= K < tx_to. K is drawn up to the graph
    watermark, so projection lag does not count as a mismatch. Counts are added for admins."""
    rng = random.Random(seed)
    samples = _clamp(samples, 1, 500)
    t = p.tenant_id
    wm = watermark(conn, t)
    out: dict[str, Any] = {"samples": samples, "checked": 0, "mismatches": [], "ok": True, "watermark": wm}
    if wm["graph"] <= 0:
        out["note"] = "graph projection is empty for this tenant"
        return out
    g_at = conn.execute("SELECT tx_at FROM tx_log WHERE tenant_id = %s AND tx_seq = %s", (t, wm["graph"])).fetchone()
    g_at = g_at["tx_at"] if g_at else datetime.now(timezone.utc)
    acl_sql, acl_params = p.sql_filter("e.acl")
    ents = [r["entity_id"] for r in conn.execute(
        f"""SELECT e.entity_id FROM entity e WHERE e.tenant_id = %s AND {acl_sql} AND e.created_tx <= %s
            ORDER BY random() LIMIT %s""", (t, *acl_params, wm["graph"], samples)).fetchall()]
    vers: dict[str, list[dict]] = {}
    for r in conn.execute(
        """SELECT entity_id, version_id, lower(tx) AS tf, upper(tx) AS tt FROM entity_version
           WHERE tenant_id = %s AND entity_id = ANY(%s) AND tx_seq <= %s ORDER BY entity_id, seq""",
        (t, ents, wm["graph"]),
    ).fetchall():
        vers.setdefault(r["entity_id"], []).append(r)
    probes = []
    for i, eid in enumerate(ents):
        vs = vers.get(eid) or []
        if not vs:
            continue
        v = rng.choice(vs)
        hi = min(v["tt"] or g_at, g_at)
        strategy = rng.choice(("lower", "inside", "inside", "before") + (("upper",) if v["tt"] and v["tt"] <= g_at else ()))
        if strategy == "lower":
            k = v["tf"]
        elif strategy == "upper":
            k = v["tt"]
        elif strategy == "before":
            k = vs[0]["tf"] - timedelta(microseconds=1)
        else:
            span = max((hi - v["tf"]) // timedelta(microseconds=1), 0)
            k = v["tf"] + timedelta(microseconds=rng.randint(0, span))
        probes.append({"i": i, "e": eid, "k": to_us(k), "at": k})
    graph_hits: dict[int, list[str]] = {}
    if probes:
        for r in mg.read(
            f"""UNWIND $probes AS q
                MATCH (v:Version {{tenant: $t, entity_id: q.e}}) WHERE {_vis('v')}
                  AND v.tx_from <= q.k AND (v.tx_to IS NULL OR v.tx_to > q.k)
                RETURN q.i AS i, collect(v.vid) AS vids""",
            **_base(p), probes=[{"i": q["i"], "e": q["e"], "k": q["k"]} for q in probes],
        ):
            graph_hits[r["i"]] = r["vids"]
    for q in probes:
        row = reader.version_at(conn, p, q["e"], known_at=q["at"])
        jv = row["version_id"] if row else None
        gv = graph_hits.get(q["i"], [])
        out["checked"] += 1
        if len(gv) > 1 or (gv[0] if gv else None) != jv:
            out["mismatches"].append({"entity_id": q["e"], "known_at": q["at"], "journal": jv, "graph": gv})
    out["ok"] = not out["mismatches"]
    if p.is_admin:
        c = projection_counts(conn, t)
        out["counts"] = c
        keys = ("entities", "versions", "current_versions", "next", "relations", "relations_open", "causal_links",
                "causal_links_open")
        out["counts_match"] = all(c["journal"][key] == c["graph"][key] for key in keys) \
            and c["graph"]["has_version"] == c["graph"]["versions"] \
            and c["graph"]["caused_by"] == c["graph"]["causal_links"] if wm["lag"] == 0 else None
    return out
