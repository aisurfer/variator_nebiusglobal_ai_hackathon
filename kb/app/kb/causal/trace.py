"""why / causes / effects / get_link: traversals of version-bound causal links with statuses (01 §7).

"Why" is answered on the transaction axis: every link names the cause version that was current when the link
was recorded; the status says whether that basis still holds as known at K (default now).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Callable

import psycopg

from kb.acl import PrincipalCtx
from kb.causal import explain
from kb.causal._base import (
    CAUSAL_KIND_ORDER,
    EFFECTS_FANOUT,
    SALIENT_KINDS,
    MAX_CHAINS,
    MAX_DEPTH,
    STATUSES,
    WHY_FANOUT,
    Invalid,
    Labels,
    is_inferred,
    load_nodes,
    resolve_target,
    split_version,
    tenant_tz,
)
from kb.causal.status import cause_versions, explain_change, status_rows
from kb.causal.walk import candidate_counts, walk

_EVIDENCE_KEYS = ("quote", "locator", "chunk_id", "candidate", "candidate_origin", "accepted_by", "revalidated_by",
                  "revalidated_from", "cue_span")


def _depth(depth: int) -> int:
    try:
        d = int(depth)
    except (TypeError, ValueError):
        raise Invalid("depth must be an integer") from None
    return max(1, min(d, MAX_DEPTH))


def link_out(row: dict, labels: Labels, change: dict | None = None, depth: int | None = None) -> dict[str, Any]:
    ev = row.get("evidence") or {}
    change = change or {}
    out = {
        "link_id": row["link_id"],
        "effect_entity_id": row["effect_entity_id"],
        "effect_version": row["effect_version"],
        "cause_entity_id": row["cause_entity_id"],
        "cause_version": row["cause_version"],
        "predicate": row["predicate_id"],
        "predicate_label": labels.predicate(row["predicate_id"]),
        "causal_kind": row["causal_kind"],
        "origin": row["origin"],
        "inferred": is_inferred(row["origin"]),
        "confidence": row["confidence"],
        "review_status": row["review_status"],
        "status": row["status"],
        "cause_current_version": row["cause_current_version"],
        "sensitivity": row["sensitivity"],
        "changed_fields": change.get("changed_fields", []),
        "changed_by_version": change.get("changed_by_version"),
        "asserted_at": row["asserted_at"],
        "asserted_by": row.get("asserted_by"),
        "retracted_at": row["retracted_at"],
        "retract_reason": row["retract_reason"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "evidence": {k: ev[k] for k in _EVIDENCE_KEYS if ev.get(k) not in (None, "", {}, [])},
        "revalidates": row["revalidates"],
        "effect_seq": row["effect_seq"],
        "cause_seq": row["cause_seq"],
    }
    if depth is not None:
        out["depth"] = depth
    return out


def _as_twin(x: dict) -> dict:
    """A decision implemented by another decision, read backwards: the step goes to its twin record."""
    for a, b in (("effect_entity_id", "cause_entity_id"), ("effect_version", "cause_version"), ("effect_seq", "cause_seq")):
        x[a], x[b] = x[b], x[a]
    return {**x, "predicate": "same_decision", "predicate_label": "same decision as"}


def _link_order(x: dict) -> tuple:
    """Declared before inferred; what caused/decided before bases and dependencies (CAUSAL_KIND_ORDER)."""
    return (x.get("depth", 0), x.get("inferred", False), CAUSAL_KIND_ORDER.get(x.get("causal_kind"), 9),
            x["effect_version"], x["predicate"], x["cause_version"], x.get("origin") or "",
            str(x.get("asserted_at") or ""), x["link_id"])


class _Graph:
    """Links of a traversal with the node rule of walk.py (X@n owns the links of X versions <= n backwards,
    >= n forwards)."""

    def __init__(self, links: list[dict], direction: str):
        self.direction = direction
        self.by_entity: dict[str, list[dict]] = defaultdict(list)
        key = "effect_entity_id" if direction == "back" else "cause_entity_id"
        for link in links:
            self.by_entity[link[key]].append(link)
        for lst in self.by_entity.values():
            lst.sort(key=_link_order)

    def children(self, entity_id: str, seq: int) -> list[dict]:
        if self.direction == "back":
            return [x for x in self.by_entity.get(entity_id, []) if x["effect_seq"] <= seq]
        return [x for x in self.by_entity.get(entity_id, []) if x["cause_seq"] >= seq]

    def next_version(self, link: dict) -> str:
        return link["cause_version"] if self.direction == "back" else link["effect_version"]


def _chains(graph: _Graph, nodes: dict[str, dict], start: str, is_root: Callable[[str], bool]
            ) -> tuple[list[dict], bool]:
    out: list[dict] = []
    truncated = False

    def dfs(vid: str, links: list[dict], versions: list[str], seen: frozenset[str]) -> None:
        nonlocal truncated
        if len(out) >= MAX_CHAINS:
            truncated = True
            return
        node = nodes[vid]
        extended = False
        for link in graph.children(node["entity_id"], node["seq"]):
            nxt = graph.next_version(link)
            if nxt not in nodes or nodes[nxt]["entity_id"] in seen:
                continue
            extended = True
            dfs(nxt, links + [link], versions + [nxt], seen | {nodes[nxt]["entity_id"]})
        if not extended and links:
            out.append({
                "root": vid,
                "complete": is_root(vid),
                "length": len(links),
                "versions": versions,
                "links": [x["link_id"] for x in links],
                "steps": [{"link_id": x["link_id"], "effect_version": x["effect_version"], "predicate": x["predicate"],
                           "predicate_label": x.get("predicate_label"), "cause_version": x["cause_version"],
                           "status": x["status"], "inferred": x["inferred"]} for x in links],
                "predicates": [x["predicate"] for x in links],
                "statuses": [x["status"] for x in links],
                "inferred": any(x["inferred"] for x in links),
            })

    dfs(start, [], [start], frozenset({nodes[start]["entity_id"]}))
    for c in out:  # chains through meetings and decisions answer "why" best (research §5.5)
        c["through"] = sorted({nodes[v]["kind"] for v in c["versions"][1:] if nodes[v]["kind"] in SALIENT_KINDS},
                              key=SALIENT_KINDS.get)
    out.sort(key=lambda c: (not c["complete"], c["inferred"],
                            min((SALIENT_KINDS[k] for k in c["through"]), default=len(SALIENT_KINDS)),
                            c["length"], c["versions"]))
    return out, truncated


def _trace(conn: psycopg.Connection, p: PrincipalCtx, target: str, *, direction: str, depth: int,
           include_inferred: bool, known_at: datetime | None, fanout: int) -> dict[str, Any] | None:
    resolved = resolve_target(conn, p, target, known_at)
    if resolved is None:
        return None
    entity_id, seq, version_id = resolved
    is_version = split_version(target) is not None and version_id == target
    depth = _depth(depth)
    start_seq = seq if (direction == "back" or is_version) else 1
    twins: set[str] = set()
    walked = walk(conn, p, direction, entity_id, start_seq, depth=depth, fanout=fanout, known_at=known_at,
                  include_inferred=include_inferred, twins=twins)
    rows = status_rows(conn, p, walked, known_at=known_at)
    labels = Labels(conn, p.tenant_id)
    node_ids = {version_id} | {r["effect_version"] for r in rows.values()} | {r["cause_version"] for r in rows.values()}
    nodes = load_nodes(conn, p, node_ids, known_at=known_at, labels=labels)
    versions = cause_versions(conn, p.tenant_id, {r["cause_entity_id"] for r in rows.values()})
    links: list[dict] = []
    for lid, r in rows.items():
        if r["effect_version"] not in nodes or r["cause_version"] not in nodes:
            continue  # defensive: both ends must be visible
        # as of K the answer holds only what was known at K: no "status now" (Docs/KB/AS-OF.md)
        out = link_out(r, labels, explain_change(r, versions.get(r["cause_entity_id"], []), known_at), walked[lid])
        links.append(_as_twin(out) if lid in twins else out)
    links.sort(key=_link_order)
    return {"entity_id": entity_id, "seq": start_seq, "version_id": version_id, "depth": depth, "nodes": nodes,
            "links": links, "labels": labels, "is_version": is_version}


def _target_out(t: dict) -> dict[str, Any]:
    node = t["nodes"][t["version_id"]]
    return {"entity_id": t["entity_id"], "version_id": t["version_id"], "seq": node["seq"], "kind": node["kind"],
            "kind_label": node["kind_label"], "title": node["title"], "is_current": node["is_current"],
            "current_version": node["current_version"], "deleted": node["deleted"],
            "effective_from": node["effective_from"], "recorded_at": node["recorded_at"]}


def why(conn: psycopg.Connection, p: PrincipalCtx, target: str, *, depth: int = 4, include_inferred: bool = False,
        known_at: datetime | None = None) -> dict[str, Any] | None:
    """Backward trace from an entity (all its links that exist at K) or a version `X@n` (links of versions <= n)
    to the first causes. None if the target is unknown, invisible, or did not exist yet at K."""
    t = _trace(conn, p, target, direction="back", depth=depth, include_inferred=include_inferred, known_at=known_at,
               fanout=WHY_FANOUT)
    if t is None:
        return None
    nodes, links = t["nodes"], t["links"]
    graph = _Graph(links, "back")
    start = t["version_id"]
    # node depth = shortest number of hops from the target
    node_depth: dict[str, int] = {start: 0}
    for link in links:
        cv = link["cause_version"]
        node_depth[cv] = min(node_depth.get(cv, 99), link["depth"])
    counts = candidate_counts(conn, p, "back", [(n["entity_id"], n["seq"]) for n in nodes.values()],
                              known_at=known_at, include_inferred=include_inferred)
    into: dict[str, list[dict]] = defaultdict(list)
    for link in links:
        into[link["cause_version"]].append(link)
    for vid, n in nodes.items():
        n["depth"] = node_depth.get(vid)
        n["role"] = "target" if vid == start else ("cause" if vid in node_depth else "effect_version")
        n["n_causes"] = len(graph.children(n["entity_id"], n["seq"]))
        n["more_causes"] = counts.get((n["entity_id"], n["seq"]), 0) > n["n_causes"]
        n["via_inferred"] = bool(into[vid]) and all(x["inferred"] for x in into[vid])

    def venue(vid: str) -> bool:
        """A meeting reached only as the place of a decision: the end of its chain, not a cause."""
        return bool(into[vid]) and all(x["predicate"] == "decided_in" for x in into[vid])

    def is_root(vid: str) -> bool:
        n = nodes[vid]
        return vid != start and (counts.get((n["entity_id"], n["seq"]), 0) == 0 or venue(vid))

    reached = [vid for vid in nodes if vid in node_depth and vid != start]
    roots = sorted((v for v in reached if is_root(v) and not venue(v)), key=lambda v: (nodes[v]["recorded_at"], v))
    frontier = sorted((v for v in reached if not is_root(v) and nodes[v]["n_causes"] == 0),
                      key=lambda v: (nodes[v]["recorded_at"], v))
    chains, chains_truncated = _chains(graph, nodes, start, is_root)
    tz = tenant_tz(conn, p.tenant_id)
    for c in chains:
        c["text"] = explain.chain_text(c, nodes, {x["link_id"]: x for x in links}, t["labels"])
    direct = graph.children(t["entity_id"], t["seq"])
    lines = explain.why_lines(nodes[start], direct, nodes, links, roots, chains, known_at, tz, t["labels"])
    interpretation = explain.why_interpretation(known_at, tz)
    return {
        "target": _target_out(t),
        "query": {"target": target, "depth": t["depth"], "include_inferred": include_inferred, "known_at": known_at},
        "known_at": known_at,
        "nodes": nodes,
        "links": links,
        "roots": roots,
        "frontier": frontier,
        "chains": chains,
        "chains_truncated": chains_truncated,
        "summary": "\n".join(lines),
        "summary_lines": lines,
        "interpretation": interpretation,
    }


def causes(conn: psycopg.Connection, p: PrincipalCtx, version_id: str, *, include_inferred: bool = False,
           known_at: datetime | None = None) -> dict[str, Any] | None:
    """Direct causes of a version (or of an entity's head) with statuses."""
    t = _trace(conn, p, version_id, direction="back", depth=1, include_inferred=include_inferred, known_at=known_at,
               fanout=EFFECTS_FANOUT)
    if t is None:
        return None
    return {"target": _target_out(t), "known_at": known_at, "links": t["links"], "nodes": t["nodes"]}


def effects(conn: psycopg.Connection, p: PrincipalCtx, target: str, *, depth: int = 4, include_inferred: bool = False,
            status_filter: list[str] | None = None, known_at: datetime | None = None) -> dict[str, Any] | None:
    """Forward trace: what was built on the entity (any version) or on `X@n` (links to versions >= n).
    `status_filter` keeps only links in those statuses, and only what is reachable through them."""
    if status_filter:
        bad = [s for s in status_filter if s not in STATUSES]
        if bad:
            raise Invalid(f"unknown status: {', '.join(bad)}")
    t = _trace(conn, p, target, direction="fwd", depth=depth, include_inferred=include_inferred, known_at=known_at,
               fanout=EFFECTS_FANOUT)
    if t is None:
        return None
    nodes, links = t["nodes"], t["links"]
    if status_filter:
        allowed = set(status_filter)
        graph = _Graph([x for x in links if x["status"] in allowed], "fwd")
        keep: dict[str, dict] = {}
        frontier = [(t["entity_id"], t["seq"])]
        seen = set(frontier)
        while frontier:
            nxt = []
            for ent, seq in frontier:
                for link in graph.children(ent, seq):
                    keep[link["link_id"]] = link
                    key = (link["effect_entity_id"], link["effect_seq"])
                    if key not in seen:
                        seen.add(key)
                        nxt.append(key)
            frontier = nxt
        links = [x for x in links if x["link_id"] in keep]
        used = {t["version_id"]} | {x["effect_version"] for x in links} | {x["cause_version"] for x in links}
        nodes = {k: v for k, v in nodes.items() if k in used}
    affected: dict[str, dict] = {}
    for link in links:
        n = nodes[link["effect_version"]]
        if n["entity_id"] == t["entity_id"]:
            continue  # an entity-level cycle leads back to the target: it is not its own dependent
        a = affected.get(n["entity_id"])
        if a is None or link["depth"] < a["depth"]:
            affected[n["entity_id"]] = {"entity_id": n["entity_id"], "version_id": n["version_id"],
                                        "current_version": n["current_version"], "kind": n["kind"],
                                        "title": n["title"], "deleted": n["deleted"], "depth": link["depth"]}
    return {
        "target": _target_out(t),
        "query": {"target": target, "depth": t["depth"], "include_inferred": include_inferred,
                  "status_filter": status_filter, "known_at": known_at},
        "known_at": known_at,
        "nodes": nodes,
        "links": links,
        "affected": sorted(affected.values(), key=lambda a: (a["depth"], a["kind"], a["entity_id"])),
    }


def get_link(conn: psycopg.Connection, p: PrincipalCtx, link_id: str, *, known_at: datetime | None = None
             ) -> dict[str, Any] | None:
    """One link with its status at K, both endpoint versions, revalidation lineage and review decisions."""
    rows = status_rows(conn, p, [link_id], known_at=known_at)
    row = rows.get(link_id)
    if row is None:
        return None
    labels = Labels(conn, p.tenant_id)
    versions = cause_versions(conn, p.tenant_id, [row["cause_entity_id"]])
    nodes = load_nodes(conn, p, [row["effect_version"], row["cause_version"]], known_at=known_at, labels=labels)
    if row["effect_version"] not in nodes or row["cause_version"] not in nodes:
        return None
    out = link_out(row, labels, explain_change(row, versions.get(row["cause_entity_id"], []), known_at))
    lineage = conn.execute(
        "SELECT link_id FROM causal_link WHERE tenant_id = %s AND revalidates = %s ORDER BY tx_seq",
        (p.tenant_id, link_id),
    ).fetchall()
    reviews = conn.execute(
        """SELECT decision, reviewer, reason, created_link_id, created_at FROM review_decision
           WHERE tenant_id = %s AND target_type = 'causal_link' AND target_id = %s ORDER BY id""",
        (p.tenant_id, link_id),
    ).fetchall()
    return {"link": out, "effect": nodes[row["effect_version"]], "cause": nodes[row["cause_version"]],
            "revalidated_by": [r["link_id"] for r in lineage], "reviews": reviews, "known_at": known_at,
            "explanation": explain.link_sentence(out, nodes, labels, known_at, tenant_tz(conn, p.tenant_id))}
