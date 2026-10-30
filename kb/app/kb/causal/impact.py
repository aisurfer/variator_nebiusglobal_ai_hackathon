"""impact (research query 5) and stale_dependencies (research query 4) over the links open now or known at K."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import psycopg

from kb.acl import PrincipalCtx
from kb.causal import explain
from kb.causal._base import (
    EFFECTS_FANOUT,
    KIND_WEIGHT,
    MAX_DEPTH,
    MAX_PATHS,
    Invalid,
    Labels,
    NotFound,
    acl_clause,
    fields_match,
    fmt_dt,
    hub_weight,
    is_inferred,
    load_nodes,
    origin_clause,
    params,
    tenant_tz,
)
from kb.causal.status import cause_versions, explain_change, status_rows
from kb.causal.trace import link_out
from kb.causal.walk import walk
from kb.journal import reader

DECAY = 0.85


def _fields(fields: list[str] | str | None) -> list[str]:
    if fields is None or fields == "*":
        return ["*"]
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",")]
    out = [str(f).strip() for f in fields if str(f).strip()]
    return out or ["*"]


def _depth(depth: int) -> int:
    try:
        return max(1, min(int(depth), MAX_DEPTH))
    except (TypeError, ValueError):
        raise Invalid("depth must be an integer") from None


def _entities(conn: psycopg.Connection, p: PrincipalCtx, ids: set[str], known_at: datetime | None = None
              ) -> dict[str, dict]:
    """Visible entity rows (as known at K: head, title and deletion then)."""
    if not ids:
        return {}
    rows = reader.heads_at(conn, p.tenant_id, sorted(ids), known_at)
    return {e: r for e, r in rows.items() if p.can_see(r["acl"])}


def impact_core(conn: psycopg.Connection, p: PrincipalCtx, entity_id: str, fields: list[str], *, depth: int,
                include_inferred: bool, labels: Labels, known_at: datetime | None = None) -> dict[str, Any]:
    """Affected entities downstream of a change of `fields` of `entity_id` (no visibility check of the source), over
    the links open now or known at K."""
    walked = walk(conn, p, "impact", entity_id, 0, depth=depth, fanout=EFFECTS_FANOUT,
                  include_inferred=include_inferred, impact_fields=fields, known_at=known_at)
    rows = status_rows(conn, p, walked, known_at=known_at)
    adj: dict[str, list[dict]] = defaultdict(list)
    for r in rows.values():
        adj[r["cause_entity_id"]].append(r)
    for lst in adj.values():
        lst.sort(key=lambda r: (r["predicate_id"], r["effect_version"], r["link_key"], r["asserted_at"]))
    fanout = {c: len({r["effect_entity_id"] for r in lst}) for c, lst in adj.items()}
    depth_of: dict[str, int] = {entity_id: 0}
    paths: dict[str, list[list[dict]]] = {entity_id: [[]]}
    frontier = [entity_id]
    for d in range(1, depth + 1):
        nxt: list[str] = []
        for c in frontier:
            for r in adj.get(c, []):
                if c == entity_id and not fields_match(r["sensitivity"], fields):
                    continue
                e = r["effect_entity_id"]
                if e == entity_id or (e in depth_of and depth_of[e] < d):
                    continue
                step = {"link_id": r["link_id"], "predicate": r["predicate_id"],
                        "predicate_label": labels.predicate(r["predicate_id"]), "cause_entity_id": c,
                        "cause_version": r["cause_version"], "effect_entity_id": e,
                        "effect_version": r["effect_version"], "status": r["status"],
                        "inferred": is_inferred(r["origin"]), "confidence": r["confidence"]}
                for path in paths[c]:
                    if any(s["effect_entity_id"] == e for s in path):
                        continue
                    if e not in depth_of:
                        depth_of[e] = d
                        paths[e] = []
                        nxt.append(e)
                    if len(paths[e]) < MAX_PATHS:
                        paths[e].append(path + [step])
        frontier = nxt
        if not frontier:
            break
    ents = _entities(conn, p, set(depth_of) - {entity_id}, known_at)
    affected: list[dict] = []
    for e, d in depth_of.items():
        if e == entity_id or e not in ents:
            continue
        info = ents[e]
        scored = []
        for path in paths[e]:
            score = DECAY ** (len(path) - 1)
            for s in path:
                score *= hub_weight(fanout.get(s["cause_entity_id"], 1), s["confidence"])
            scored.append((score, path))
        scored.sort(key=lambda x: (-x[0], len(x[1])))
        affected.append({
            "entity_id": e,
            "kind": info["kind_id"],
            "kind_label": labels.kind(info["kind_id"]),
            "title": info["title"],
            "current_version": info["head_version_id"],
            "version_id": info["head_version_id"],
            "url": info["url"],
            "depth": d,
            "paths": [pth for _, pth in scored],
            "predicates": sorted({s["predicate"] for _, pth in scored for s in pth}),
            "via": sorted({pth[-1]["predicate"] for _, pth in scored if pth}),
            "score": round(scored[0][0], 4) if scored else 0.0,
            "inferred_only": bool(scored) and all(any(s["inferred"] for s in pth) for _, pth in scored),
        })
    affected.sort(key=lambda a: (a["depth"], -a["score"], a["entity_id"]))
    direct = [r for r in rows.values() if r["cause_entity_id"] == entity_id and fields_match(r["sensitivity"], fields)]
    return {"affected": affected, "direct": direct, "rows": rows}


def _totals(affected: list[dict]) -> dict[str, Any]:
    by_depth: dict[int, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    for a in affected:
        by_depth[a["depth"]] += 1
        by_kind[a["kind"]] += 1
    return {"affected": len(affected), "by_depth": dict(sorted(by_depth.items())), "by_kind": dict(sorted(by_kind.items()))}


def impact(conn: psycopg.Connection, p: PrincipalCtx, entity_id: str, fields: list[str] | None, *, depth: int = 4,
           include_inferred: bool = False, known_at: datetime | None = None) -> dict[str, Any]:
    """What would a change of `fields` (["*"] = any) of the entity affect: dependents whose open links (known at K)
    have sensitivity NULL or overlapping the fields, then recursively their dependents (deleted ones excluded)."""
    labels = Labels(conn, p.tenant_id)
    ent = _entities(conn, p, {entity_id}, known_at).get(entity_id)
    if ent is None:
        raise NotFound(entity_id)
    fields = _fields(fields)
    depth = _depth(depth)
    core = impact_core(conn, p, entity_id, fields, depth=depth, include_inferred=include_inferred, labels=labels,
                       known_at=known_at)
    node = load_nodes(conn, p, [ent["head_version_id"]], labels=labels, known_at=known_at).get(ent["head_version_id"])
    versions = cause_versions(conn, p.tenant_id, [entity_id])
    direct = []
    for r in sorted(core["direct"], key=lambda r: (r["effect_entity_id"], r["effect_version"], r["link_key"])):
        out = link_out(r, labels, explain_change(r, versions.get(entity_id, []), known_at))
        out["would_status"] = "STALE" if r["status"] in ("CURRENT", "CURRENT_COMPATIBLE", "STALE") else r["status"]
        direct.append(out)
    affected = core["affected"]
    totals = _totals(affected) | {"direct_links": len(direct)}
    what = ("any fields" if "*" in fields else "fields " + ", ".join(fields))
    lines = [(f"A change of {what} of {explain.node_ref(node)} affects {len(affected)} object(s) through "
                f"{'declared' if not include_inferred else 'declared and inferred'} links "
                f"(depth up to {depth}); direct dependencies that become STALE: {len(direct)}.")]
    lines += explain.affected_lines(affected, labels)
    return {"entity": node, "fields": fields, "depth": depth, "include_inferred": include_inferred,
            "known_at": known_at, "affected": affected, "direct_links": direct, "totals": totals,
            "explanation": "\n".join(lines), "explanation_lines": lines}


# ------------------------------------------------------------------------------------------------ stale
_STALE_SQL = """
SELECT s.link_id, s.link_key, s.effect_entity_id, s.effect_version, s.cause_entity_id, s.cause_version, s.cause_seq,
       s.cause_current_version, s.status, s.predicate_id, s.causal_kind, s.sensitivity, s.origin, s.confidence,
       lower(s.known) AS asserted_at,
       ee.kind_id AS effect_kind, ee.title AS effect_title, ee.head_version_id AS effect_current_version,
       ce.kind_id AS cause_kind, ce.title AS cause_title, ce.tombstone_tx AS cause_tombstone
FROM causal_link_status s
JOIN entity ee ON ee.tenant_id = s.tenant_id AND ee.entity_id = s.effect_entity_id
JOIN entity ce ON ce.tenant_id = s.tenant_id AND ce.entity_id = s.cause_entity_id
WHERE s.tenant_id = %(t)s AND s.status IN ('STALE', 'BROKEN') AND {origin}
  AND ee.tombstone_tx IS NULL AND {acl_e} AND {acl_c}
  AND (%(kinds)s::text[] IS NULL OR ee.kind_id = ANY(%(kinds)s::text[]))
  AND (%(scope)s::text[] IS NULL OR s.effect_entity_id = ANY(%(scope)s::text[]))
"""


def _stale_rows_at(conn: psycopg.Connection, p: PrincipalCtx, known_at: datetime, kinds: list[str] | None,
                   scope: list[str] | None) -> list[dict]:
    """The rows of _STALE_SQL as known at K: declared links known at K whose status at K is STALE or BROKEN, of
    effects alive at K (kinds and titles at K)."""
    ids = [r["link_id"] for r in conn.execute(
        f"""SELECT l.link_id FROM causal_link l WHERE l.tenant_id = %(t)s AND l.known @> %(k)s::timestamptz
              AND {origin_clause(False)} AND (%(scope)s::text[] IS NULL OR l.effect_entity_id = ANY(%(scope)s::text[]))""",
        params(p, k=known_at, scope=scope)).fetchall()]
    rows = [r for r in status_rows(conn, p, ids, known_at=known_at).values() if r["status"] in ("STALE", "BROKEN")]
    heads = reader.heads_at(conn, p.tenant_id, sorted({r["effect_entity_id"] for r in rows} |
                                                      {r["cause_entity_id"] for r in rows}), known_at)
    out = []
    for r in rows:
        ee, ce = heads.get(r["effect_entity_id"]), heads.get(r["cause_entity_id"])
        if ee is None or ce is None or ee["tombstone_tx"] is not None or (kinds and ee["kind_id"] not in kinds):
            continue
        out.append({**r, "effect_kind": ee["kind_id"], "effect_title": ee["title"],
                    "effect_current_version": ee["head_version_id"], "cause_kind": ce["kind_id"],
                    "cause_title": ce["title"], "cause_tombstone": ce["tombstone_tx"]})
    return out


def _data_end(conn: psycopg.Connection, tenant_id: str) -> datetime:
    from kb.asof import bounds

    return bounds(conn, tenant_id).end or datetime.now(timezone.utc)


def _downstream(conn: psycopg.Connection, p: PrincipalCtx, entity_id: str, known_at: datetime | None = None) -> set[str]:
    walked = walk(conn, p, "fwd", entity_id, 1, depth=MAX_DEPTH, fanout=EFFECTS_FANOUT, known_at=known_at)
    if not walked:
        return {entity_id}
    rows = conn.execute("SELECT effect_entity_id FROM causal_link WHERE tenant_id = %s AND link_id = ANY(%s)",
                        (p.tenant_id, list(walked))).fetchall()
    return {entity_id} | {r["effect_entity_id"] for r in rows}


def stale_dependencies(conn: psycopg.Connection, p: PrincipalCtx, *, scope: str | list[str] | None = None,
                       since: datetime | None = None, limit: int = 100,
                       known_at: datetime | None = None) -> dict[str, Any]:
    """Effects alive (at K) whose declared links (known at K) are STALE or BROKEN, ranked by the importance of the
    dependent, recency of the cause change (relative to K, by default the end of the data) and fan-out of the
    dependent; grouped by the cause hub.

    scope: an entity id (its downstream incl. itself) or a list of kinds of the dependent."""
    labels = Labels(conn, p.tenant_id)
    tz = tenant_tz(conn, p.tenant_id)
    kinds: list[str] | None = None
    scope_ids: list[str] | None = None
    if isinstance(scope, (list, tuple)):
        kinds = [str(k) for k in scope] or None
    elif isinstance(scope, str) and scope:
        if not _entities(conn, p, {scope}, known_at):
            raise NotFound(scope)
        scope_ids = sorted(_downstream(conn, p, scope, known_at))
    limit = max(1, min(int(limit), 1000))
    if known_at is None:
        sql = _STALE_SQL.format(origin=origin_clause(False, "s"), acl_e=acl_clause(p, "ee.acl"),
                                acl_c=acl_clause(p, "ce.acl"))
        rows = conn.execute(sql, params(p, kinds=kinds, scope=scope_ids)).fetchall()
    else:
        rows = _stale_rows_at(conn, p, known_at, kinds, scope_ids)
    versions = cause_versions(conn, p.tenant_id, {r["cause_entity_id"] for r in rows})
    # fan-out of the dependent: its own visible declared dependents (what a fix of it would have to revisit)
    dep_ids = sorted({r["effect_entity_id"] for r in rows})
    fan: dict[str, int] = {}
    if dep_ids:
        if known_at is None:
            frows = conn.execute(
                f"""SELECT l.cause_entity_id, count(DISTINCT l.effect_entity_id) AS n FROM causal_link l
                    JOIN entity ne ON ne.tenant_id = l.tenant_id AND ne.entity_id = l.effect_entity_id
                    WHERE l.tenant_id = %(t)s AND l.cause_entity_id = ANY(%(ids)s) AND upper_inf(l.tx)
                      AND {origin_clause(False)} AND ne.tombstone_tx IS NULL AND {acl_clause(p, 'ne.acl')}
                    GROUP BY 1""",
                params(p, ids=dep_ids),
            ).fetchall()
        else:
            frows = conn.execute(
                f"""SELECT l.cause_entity_id, count(DISTINCT l.effect_entity_id) AS n FROM causal_link l
                    JOIN entity ne ON ne.tenant_id = l.tenant_id AND ne.entity_id = l.effect_entity_id
                    WHERE l.tenant_id = %(t)s AND l.cause_entity_id = ANY(%(ids)s) AND l.known @> %(k)s::timestamptz
                      AND {origin_clause(False)} AND {acl_clause(p, 'ne.acl')}
                      AND NOT EXISTS (SELECT 1 FROM entity_version d WHERE d.tenant_id = l.tenant_id
                        AND d.entity_id = l.effect_entity_id AND d.op = 'delete' AND d.tx @> %(k)s::timestamptz)
                    GROUP BY 1""",
                params(p, ids=dep_ids, k=known_at),
            ).fetchall()
        fan = {r["cause_entity_id"]: r["n"] for r in frows}
    # recency is measured from the date of the view (K), else from the end of the data — never the wall clock
    now = known_at or _data_end(conn, p.tenant_id)
    items = []
    for r in rows:
        ch = explain_change(r, versions.get(r["cause_entity_id"], []), known_at)
        changed = ch["changed_by_version"] or {}
        changed_at = changed.get("recorded_at")
        if since is not None and (changed_at is None or changed_at < since):
            continue
        age_days = max(0.0, (now - changed_at).total_seconds() / 86400) if changed_at else 365.0
        n_fan = fan.get(r["effect_entity_id"], 0)
        score = (KIND_WEIGHT.get(r["effect_kind"], 1.0) + 2.0 * math.exp(-age_days / 30.0) + math.log1p(n_fan)
                 + (1.0 if r["status"] == "BROKEN" else 0.0))
        items.append({
            "link_id": r["link_id"],
            "status": r["status"],
            "effect_entity_id": r["effect_entity_id"],
            "effect_kind": r["effect_kind"],
            "effect_title": r["effect_title"],
            "effect_version": r["effect_version"],
            "effect_current_version": r["effect_current_version"],
            "predicate": r["predicate_id"],
            "predicate_label": labels.predicate(r["predicate_id"]),
            "causal_kind": r["causal_kind"],
            "origin": r["origin"],
            "cause_entity_id": r["cause_entity_id"],
            "cause_kind": r["cause_kind"],
            "cause_title": r["cause_title"],
            "cause_version": r["cause_version"],
            "cause_current_version": r["cause_current_version"],
            "sensitivity": r["sensitivity"],
            "changed_fields": ch["changed_fields"],
            "changed_by_version": ch["changed_by_version"],
            "changed_at": changed_at,
            "effective_from": changed.get("effective_from"),
            "dependents": n_fan,
            "score": round(score, 3),
        })
    items.sort(key=lambda i: (-i["score"], i["effect_entity_id"], i["effect_version"], i["predicate"],
                              i["cause_version"]))
    items = items[:limit]
    for n, it in enumerate(items, 1):
        it["rank"] = n
        fields = ", ".join(it["changed_fields"])
        what = ("was deleted" if it["status"] == "BROKEN" else f"changed ({fields or 'any fields'})")
        changed_in = (it["changed_by_version"] or {}).get("version_id", it["cause_current_version"])
        it["explanation"] = (f"{labels.kind(it['effect_kind'])} “{it['effect_title']}” ({it['effect_current_version']}) relies on "
            f"“{it['cause_title']}” in version {it['cause_version']} (“{it['predicate_label']}”), which {what} in "
            f"{changed_in} (recorded "
            f"{fmt_dt(it['changed_at'], tz)}); current version {it['cause_current_version']} — {it['status']}.")
    groups: dict[str, dict] = {}
    for it in items:
        g = groups.setdefault(it["cause_entity_id"], {
            "cause_entity_id": it["cause_entity_id"], "cause_kind": it["cause_kind"], "cause_title": it["cause_title"],
            "cause_current_version": it["cause_current_version"], "statuses": [], "link_ids": [], "dependents": [],
            "count": 0, "top_score": it["score"]})
        g["count"] += 1
        g["link_ids"].append(it["link_id"])
        if it["effect_entity_id"] not in g["dependents"]:
            g["dependents"].append(it["effect_entity_id"])
        if it["status"] not in g["statuses"]:
            g["statuses"].append(it["status"])
    return {
        "scope": scope,
        "since": since,
        "known_at": known_at,
        "items": items,
        "groups": sorted(groups.values(), key=lambda g: (-g["count"], -g["top_score"], g["cause_entity_id"])),
        "totals": {"items": len(items), "stale": sum(i["status"] == "STALE" for i in items),
                   "broken": sum(i["status"] == "BROKEN" for i in items)},
    }


# ------------------------------------------------------------------------------------------------ stale history
def _status_at(link: dict, cause: list[dict], k: datetime) -> str | None:
    """The status of a link at K (status._AT_SQL in Python): None if the link is not known at K."""
    if link["known_from"] > k:
        return None
    if link["known_to"] is not None and link["known_to"] <= k:
        return "RETRACTED"
    known = [v for v in cause if v["recorded_at"] <= k]
    if not known:
        return None
    head = known[-1]
    if head["op"] == "delete":
        return "BROKEN"
    if head["version_id"] == link["cause_version"]:
        return "CURRENT"
    sens = link["sensitivity"]
    if any(v["seq"] > link["cause_seq"] and (sens is None or set(v["changed_fields"] or []) & set(sens)) for v in known):
        return "STALE"
    return "CURRENT_COMPATIBLE"


def stale_history(conn: psycopg.Connection, p: PrincipalCtx, *, known_at: datetime | None = None) -> dict[str, Any]:
    """Every declared link (visible ends) that was STALE or BROKEN, with an alive dependent, at some moment up to K, and
    the intervals [from, to) of the knowledge axis in which it was (to = None: still at K). Within an interval the
    status, the change that caused it and both titles are constant, so `stale_dependencies(known_at=T)` lists exactly
    the links with an interval containing T (with the same status, change and titles)."""
    t = p.tenant_id
    links = conn.execute(
        f"""SELECT l.link_id, l.link_key, l.effect_entity_id, l.effect_version, l.cause_entity_id, l.cause_version,
                   cv.seq AS cause_seq, l.predicate_id, l.causal_kind, l.sensitivity, l.origin,
                   lower(l.known) AS known_from, upper(l.known) AS known_to, ee.acl AS effect_acl, ce.acl AS cause_acl
            FROM causal_link l
            JOIN entity_version cv ON cv.tenant_id = l.tenant_id AND cv.version_id = l.cause_version
            JOIN entity ee ON ee.tenant_id = l.tenant_id AND ee.entity_id = l.effect_entity_id
            JOIN entity ce ON ce.tenant_id = l.tenant_id AND ce.entity_id = l.cause_entity_id
            WHERE l.tenant_id = %(t)s AND NOT isempty(l.known) AND {origin_clause(False)}
              AND (%(k)s::timestamptz IS NULL OR lower(l.known) <= %(k)s::timestamptz)
              AND EXISTS (SELECT 1 FROM entity_version v WHERE v.tenant_id = l.tenant_id
                            AND v.entity_id = l.cause_entity_id AND v.seq > cv.seq)""",
        params(p, k=known_at)).fetchall()
    links = [r for r in links if p.can_see(r["effect_acl"]) and p.can_see(r["cause_acl"])]
    ents = sorted({r["cause_entity_id"] for r in links} | {r["effect_entity_id"] for r in links})
    versions: dict[str, list[dict]] = defaultdict(list)
    if ents:
        for v in conn.execute(
            """SELECT entity_id, version_id, seq, op, kind_id, title, changed_fields, valid_from, lower(tx) AS recorded_at
               FROM entity_version WHERE tenant_id = %s AND entity_id = ANY(%s) AND (%s::timestamptz IS NULL OR lower(tx) <= %s)
               ORDER BY entity_id, seq""", (t, ents, known_at, known_at)).fetchall():
            versions[v["entity_id"]].append(v)
    labels = Labels(conn, t)
    items = []
    for r in links:
        cause, effect = versions[r["cause_entity_id"]], versions[r["effect_entity_id"]]
        points = sorted({r["known_from"], *(v["recorded_at"] for v in cause + effect if v["recorded_at"] > r["known_from"]),
                         *([r["known_to"]] if r["known_to"] is not None else [])})
        segments: list[dict] = []
        for i, k in enumerate(points):
            if known_at is not None and k > known_at:
                break
            status = _status_at(r, cause, k)
            eh = next((v for v in reversed(effect) if v["recorded_at"] <= k), None)
            ch = next((v for v in reversed(cause) if v["recorded_at"] <= k), None)
            seg = None
            if status in ("STALE", "BROKEN") and eh is not None and eh["op"] != "delete":
                change = explain_change({**r, "status": status}, cause, k)
                by = change["changed_by_version"] or {}
                seg = {"status": status, "changed_at": by.get("recorded_at"), "changed_by_version": by.get("version_id"),
                       "effective_from": by.get("effective_from"), "changed_fields": change["changed_fields"],
                       "effect_kind": eh["kind_id"], "effect_title": eh["title"], "effect_current_version": eh["version_id"],
                       "cause_kind": ch["kind_id"] if ch else None, "cause_title": ch["title"] if ch else None,
                       "cause_current_version": ch["version_id"] if ch else None}
            end = points[i + 1] if i + 1 < len(points) else None
            if known_at is not None and end is not None and end > known_at:
                end = None
            if seg is None:
                continue
            if segments and segments[-1]["to"] == k and all(segments[-1][f] == seg[f] for f in seg):
                segments[-1]["to"] = end
            else:
                segments.append({"from": k, "to": end, **seg})
        if segments:
            items.append({"link_id": r["link_id"], "effect_entity_id": r["effect_entity_id"],
                          "effect_version": r["effect_version"], "cause_entity_id": r["cause_entity_id"],
                          "cause_version": r["cause_version"], "predicate": r["predicate_id"],
                          "predicate_label": labels.predicate(r["predicate_id"]), "causal_kind": r["causal_kind"],
                          "origin": r["origin"], "sensitivity": r["sensitivity"], "segments": segments})
    items.sort(key=lambda i: (i["effect_entity_id"], i["effect_version"], i["predicate"], i["cause_version"]))
    return {"known_at": known_at, "items": items}
