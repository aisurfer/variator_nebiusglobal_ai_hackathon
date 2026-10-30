"""Computed link status (research §2.3, §4.3): never stored, derived from the journal at query time.

As known now (known_at=None) the `causal_link_status` view of migration 001 is used. As known at K (the knowledge
interval `known` of the link, Docs/KB/AS-OF.md):
- the link must be known at K (lower(known) <= K); a link retracted by K is RETRACTED, a link known only after K (or
  withdrawn: an empty interval) does not exist yet and is left out of the result;
- the cause head at K is the cause version with tx @> K;
- BROKEN   — the cause head at K is a tombstone (the deletion was recorded by K);
- CURRENT  — the cause head at K is the bound cause version;
- STALE    — a later cause version recorded by K changed fields intersecting `sensitivity` (NULL = any change);
- CURRENT_COMPATIBLE otherwise (later versions only touched fields the link does not depend on).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

import psycopg

from kb.acl import PrincipalCtx
from kb.causal._base import acl_clause, params, sensitivity_hit, visible_fields

_COLS = """{a}.link_id, {a}.link_key, {a}.effect_entity_id, {a}.effect_version, {a}.cause_entity_id, {a}.cause_version,
  {a}.causal_kind, {a}.predicate_id, {a}.sensitivity, lower({a}.valid) AS valid_from, upper({a}.valid) AS valid_to,
  lower({a}.known) AS asserted_at, upper({a}.known) AS retracted_at, {a}.retract_reason, {a}.origin, {a}.confidence,
  {a}.review_status, {a}.evidence, {a}.revalidates, {a}.tx_seq, ev.seq AS effect_seq, tl.actor_id AS asserted_by"""

_NOW_SQL = f"""
SELECT {_COLS.format(a='s')}, s.cause_seq, s.cause_current_version, s.status
FROM causal_link_status s
JOIN entity_version ev ON ev.tenant_id = s.tenant_id AND ev.version_id = s.effect_version
JOIN entity ee ON ee.tenant_id = s.tenant_id AND ee.entity_id = s.effect_entity_id
JOIN entity ce ON ce.tenant_id = s.tenant_id AND ce.entity_id = s.cause_entity_id
LEFT JOIN tx_log tl ON tl.tenant_id = s.tenant_id AND tl.tx_seq = s.tx_seq
WHERE s.tenant_id = %(t)s AND s.link_id = ANY(%(ids)s) AND {{acl_e}} AND {{acl_c}}
"""

_AT_SQL = f"""
SELECT {_COLS.format(a='l')}, cv.seq AS cause_seq, h.version_id AS cause_current_version,
  CASE
    WHEN isempty(l.known) OR lower(l.known) > %(k)s::timestamptz THEN NULL
    WHEN upper(l.known) <= %(k)s::timestamptz THEN 'RETRACTED'
    WHEN h.op = 'delete' THEN 'BROKEN'
    WHEN h.version_id = l.cause_version THEN 'CURRENT'
    WHEN EXISTS (SELECT 1 FROM entity_version v
                 WHERE v.tenant_id = l.tenant_id AND v.entity_id = l.cause_entity_id AND v.seq > cv.seq
                   AND lower(v.tx) <= %(k)s::timestamptz
                   AND (l.sensitivity IS NULL OR v.changed_fields && l.sensitivity)) THEN 'STALE'
    ELSE 'CURRENT_COMPATIBLE'
  END AS status
FROM causal_link l
JOIN entity_version cv ON cv.tenant_id = l.tenant_id AND cv.version_id = l.cause_version
JOIN entity_version ev ON ev.tenant_id = l.tenant_id AND ev.version_id = l.effect_version
JOIN entity ee ON ee.tenant_id = l.tenant_id AND ee.entity_id = l.effect_entity_id
JOIN entity ce ON ce.tenant_id = l.tenant_id AND ce.entity_id = l.cause_entity_id
LEFT JOIN tx_log tl ON tl.tenant_id = l.tenant_id AND tl.tx_seq = l.tx_seq
LEFT JOIN LATERAL (
  SELECT x.version_id, x.op FROM entity_version x
  WHERE x.tenant_id = l.tenant_id AND x.entity_id = l.cause_entity_id AND x.tx @> %(k)s::timestamptz
  ORDER BY x.seq DESC LIMIT 1) h ON true
WHERE l.tenant_id = %(t)s AND l.link_id = ANY(%(ids)s) AND {{acl_e}} AND {{acl_c}}
"""


def status_rows(conn: psycopg.Connection, p: PrincipalCtx, link_ids: Iterable[str], *,
                known_at: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Link rows with `status`, `cause_seq`, `cause_current_version` (at K). Invisible links and links that did
    not exist yet at K are left out."""
    ids = sorted(set(link_ids))
    if not ids:
        return {}
    sql = _NOW_SQL if known_at is None else _AT_SQL
    sql = sql.format(acl_e=acl_clause(p, "ee.acl"), acl_c=acl_clause(p, "ce.acl"))
    rows = conn.execute(sql, params(p, ids=ids, k=known_at)).fetchall()
    out = {r["link_id"]: r for r in rows if r["status"] is not None}
    if known_at is not None:
        for r in out.values():  # what happened to a link after K is not known at K
            if r["retracted_at"] is not None and r["retracted_at"] > known_at:
                r["retracted_at"] = r["retract_reason"] = None
                if r["origin"].startswith("inferred:"):
                    r["review_status"] = "pending"
    return out


def link_status(conn: psycopg.Connection, p: PrincipalCtx, link_ids: Iterable[str], *,
                known_at: datetime | None = None) -> dict[str, str]:
    """{link_id: status} as known at K (default: now, via the causal_link_status view)."""
    return {lid: r["status"] for lid, r in status_rows(conn, p, link_ids, known_at=known_at).items()}


def cause_versions(conn: psycopg.Connection, tenant_id: str, entity_ids: Iterable[str]) -> dict[str, list[dict]]:
    """All versions of the given (cause) entities, ordered by seq — the material for explaining a status."""
    ids = sorted(set(entity_ids))
    if not ids:
        return {}
    rows = conn.execute(
        """SELECT entity_id, version_id, seq, op, changed_fields, valid_from, lower(tx) AS recorded_at
           FROM entity_version WHERE tenant_id = %s AND entity_id = ANY(%s) ORDER BY entity_id, seq""",
        (tenant_id, ids),
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["entity_id"], []).append(r)
    return out


def explain_change(link: dict, versions: list[dict], known_at: datetime | None = None) -> dict[str, Any]:
    """What happened to the cause after the bound version (as recorded by K): the fields relevant for the link,
    the first version that made it stale/broken, and the head at K."""
    later = [v for v in versions if v["seq"] > link["cause_seq"] and (known_at is None or v["recorded_at"] <= known_at)]
    sens = link["sensitivity"]
    relevant: list[str] = []
    first = None
    for v in later:
        if v["op"] == "delete":
            if first is None:
                first = v
            continue
        hit = sensitivity_hit(v["changed_fields"] or [], sens)
        if hit and first is None:
            first = v
        relevant.extend(f for f in hit if f not in relevant)
    all_changed: list[str] = []
    for v in later:
        all_changed.extend(f for f in (v["changed_fields"] or []) if f not in all_changed)
    head = later[-1] if later else None
    status = link.get("status")
    if status == "BROKEN":
        first = next((v for v in reversed(later) if v["op"] == "delete"), first)
    out: dict[str, Any] = {
        "changed_fields": visible_fields(relevant) if status in ("STALE", "BROKEN") else [],
        "other_changed_fields": [f for f in visible_fields(all_changed) if f not in visible_fields(relevant)],
        "cause_current": None,
        "changed_by_version": None,
    }
    if head is not None:
        out["cause_current"] = {"version_id": head["version_id"], "seq": head["seq"], "op": head["op"],
                                "effective_from": head["valid_from"], "recorded_at": head["recorded_at"]}
    if first is not None and status in ("STALE", "BROKEN"):
        out["changed_by_version"] = {"version_id": first["version_id"], "seq": first["seq"], "op": first["op"],
                                     "effective_from": first["valid_from"], "recorded_at": first["recorded_at"]}
    return out
