"""Bounded traversal of version-bound causal links in PostgreSQL (one recursive CTE per call).

Backward ("why"): from a node X@n follow the links whose effect is a version of X with seq <= n (X@n inherits
the links that earlier versions asserted and that are still open), to their cause version C@k; continue from
C@k the same way. Forward ("effects"): from E@m follow the links whose cause is a version of E with seq >= m.
Impact: forward over the links open now (or known at K), the first hop filtered by sensitivity, dependents deleted
(by K) excluded.

Limits: depth (<= MAX_DEPTH), fan-out per node (LATERAL ... LIMIT), and UNION de-duplication of
(link, node, depth) rows, which also guards cycles. The next entity of every hop must be visible to the principal
(ACL filter inside the CTE), so a hidden entity is never entered and never counted.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from kb.acl import PrincipalCtx
from kb.causal._base import MAX_DEPTH, acl_clause, exists_clause, origin_clause, params

_HOP = """
    SELECT l.link_id, {next_entity} AS next_entity, {next_seq} AS next_seq, l.predicate_id AS pred, false AS twin
    FROM causal_link l
    JOIN entity_version ev ON ev.tenant_id = l.tenant_id AND ev.version_id = l.effect_version
    JOIN entity_version cv ON cv.tenant_id = l.tenant_id AND cv.version_id = l.cause_version
    JOIN entity ne ON ne.tenant_id = l.tenant_id AND ne.entity_id = {next_entity}
    WHERE l.tenant_id = %(t)s AND {cond} AND {exists} AND {origin} AND {acl} {extra}
    ORDER BY (l.origin LIKE 'declared:%%') DESC, l.confidence DESC, lower(l.known), l.link_key, l.link_id
    LIMIT %(fan)s"""

# Backward only: a decision that another decision implements is the same decision recorded twice (the meeting
# page and the HR record). The walk steps from it to that twin, so that the causes of the twin count.
_TWIN = """
    SELECT l.link_id, l.effect_entity_id AS next_entity, ev.seq AS next_seq, l.predicate_id AS pred, true AS twin
    FROM causal_link l
    JOIN entity_version ev ON ev.tenant_id = l.tenant_id AND ev.version_id = l.effect_version
    JOIN entity ce ON ce.tenant_id = l.tenant_id AND ce.entity_id = l.cause_entity_id AND ce.kind_id = 'Decision'
    JOIN entity ne ON ne.tenant_id = l.tenant_id AND ne.entity_id = l.effect_entity_id AND ne.kind_id = 'Decision'
    WHERE l.tenant_id = %(t)s AND l.cause_entity_id = {current} AND l.predicate_id = 'implements'
      AND {exists} AND {origin} AND {acl} {extra}
    LIMIT %(fan)s"""

_WALK = """
WITH RECURSIVE walk(link_id, next_entity, next_seq, pred, twin, depth) AS (
  SELECT s0.link_id, s0.next_entity, s0.next_seq, s0.pred, s0.twin, 1 FROM ({seed}) s0
  UNION
  SELECT x.link_id, x.next_entity, x.next_seq, x.pred, x.twin, w.depth + 1
  FROM walk w CROSS JOIN LATERAL ({step}) x
  WHERE w.depth < %(depth)s
)
SELECT link_id, min(depth) AS depth, bool_or(twin) AS twin FROM walk GROUP BY link_id
"""

# A meeting informs every decision made at it, but it is not a cause of each one: a walk does not take the
# meeting's bases (informed_by) after a step to the meeting (decided_in), and the reverse forwards.
_STOP = {"back": ("decided_in", "informed_by"), "fwd": ("informed_by", "decided_in")}


def walk_sql(direction: str, p: PrincipalCtx, *, known_at: datetime | None, include_inferred: bool,
             impact_fields: list[str] | None = None) -> str:
    if direction == "back":
        nxt, nseq = "l.cause_entity_id", "cv.seq"
        seed_cond = "l.effect_entity_id = %(e)s AND ev.seq <= %(n)s"
        step_cond = "l.effect_entity_id = w.next_entity AND ev.seq <= w.next_seq"
        extra = ""
    elif direction == "fwd":
        nxt, nseq = "l.effect_entity_id", "ev.seq"
        seed_cond = "l.cause_entity_id = %(e)s AND cv.seq >= %(n)s"
        step_cond = "l.cause_entity_id = w.next_entity AND cv.seq >= w.next_seq"
        extra = ""
    elif direction == "impact":
        nxt, nseq = "l.effect_entity_id", "0"
        seed_cond = "l.cause_entity_id = %(e)s"
        if impact_fields is not None and "*" not in impact_fields:
            seed_cond += " AND (l.sensitivity IS NULL OR l.sensitivity && %(fields)s::text[])"
        step_cond = "l.cause_entity_id = w.next_entity"
        extra = "AND ne.tombstone_tx IS NULL" if known_at is None else (
            "AND NOT EXISTS (SELECT 1 FROM entity_version d WHERE d.tenant_id = l.tenant_id AND d.entity_id = ne.entity_id "
            "AND d.op = 'delete' AND d.tx @> %(k)s::timestamptz)")
    else:  # pragma: no cover - programming error
        raise ValueError(direction)
    if direction in _STOP:
        prev, nxt_pred = _STOP[direction]
        step_cond += f" AND NOT (w.pred = '{prev}' AND l.predicate_id = '{nxt_pred}')"
    common: dict[str, Any] = dict(next_entity=nxt, next_seq=nseq, exists=exists_clause(known_at),
                                  origin=origin_clause(include_inferred), acl=acl_clause(p, "ne.acl"), extra=extra)
    seed, step = _HOP.format(cond=seed_cond, **common), _HOP.format(cond=step_cond, **common)
    if direction == "back":
        twin = dict(exists=common["exists"], origin=common["origin"], acl=common["acl"])
        seed = f"({seed}) UNION ALL ({_TWIN.format(current='%(e)s', extra='', **twin)})"
        step = f"({step}) UNION ALL ({_TWIN.format(current='w.next_entity', extra='AND w.twin IS NOT TRUE', **twin)})"
    return _WALK.format(seed=seed, step=step)


def walk(conn: psycopg.Connection, p: PrincipalCtx, direction: str, entity_id: str, seq: int, *, depth: int,
         fanout: int, known_at: datetime | None = None, include_inferred: bool = False,
         impact_fields: list[str] | None = None, twins: set[str] | None = None) -> dict[str, int]:
    """{link_id: minimal depth} of the links reachable from (entity_id, seq) within the limits; `twins` gets the
    ids of the links taken from a decision to its twin (walked from the cause to the effect)."""
    depth = max(1, min(int(depth), MAX_DEPTH))
    sql = walk_sql(direction, p, known_at=known_at, include_inferred=include_inferred, impact_fields=impact_fields)
    rows = conn.execute(sql, params(p, e=entity_id, n=seq, k=known_at, depth=depth, fan=fanout,
                                    fields=impact_fields or [])).fetchall()
    if twins is not None:
        twins.update(r["link_id"] for r in rows if r["twin"])
    return {r["link_id"]: r["depth"] for r in rows}


def candidate_counts(conn: psycopg.Connection, p: PrincipalCtx, direction: str, nodes: list[tuple[str, int]], *,
                     known_at: datetime | None, include_inferred: bool) -> dict[tuple[str, int], int]:
    """Number of visible traversable links from each node (to tell first causes from depth/fan-out cut-offs)."""
    if not nodes:
        return {}
    if direction == "back":
        on, seq_cond, other = "l.effect_entity_id = n.entity_id", "ev.seq <= n.seq", "l.cause_entity_id"
    else:
        on, seq_cond, other = "l.cause_entity_id = n.entity_id", "cv.seq >= n.seq", "l.effect_entity_id"
    sql = f"""
      SELECT n.entity_id, n.seq, count(l.link_id) AS n_links
      FROM unnest(%(ents)s::text[], %(seqs)s::int[]) AS n(entity_id, seq)
      JOIN causal_link l ON l.tenant_id = %(t)s AND {on}
      JOIN entity_version ev ON ev.tenant_id = l.tenant_id AND ev.version_id = l.effect_version
      JOIN entity_version cv ON cv.tenant_id = l.tenant_id AND cv.version_id = l.cause_version
      JOIN entity ne ON ne.tenant_id = l.tenant_id AND ne.entity_id = {other}
      WHERE {seq_cond} AND {exists_clause(known_at)} AND {origin_clause(include_inferred)} AND {acl_clause(p, 'ne.acl')}
      GROUP BY n.entity_id, n.seq"""
    rows = conn.execute(sql, params(p, ents=[n[0] for n in nodes], seqs=[n[1] for n in nodes], k=known_at)).fetchall()
    return {(r["entity_id"], r["seq"]): r["n_links"] for r in rows}
