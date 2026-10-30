"""Bitemporal reads of the journal (research §2.1, §4.3).

known_at(K): the version that was the head as recorded at K  ->  tx @> K.
as_of(T, K): what was true at business time T, as known at K  ->  among versions recorded by K, the one with
             the greatest valid_from <= T (ties: greatest seq). A tombstone there means "did not exist".
Statements are known at K when their knowledge interval contains K (`known @> K`, Docs/KB/AS-OF.md). Without K a
read is as of the end of the tenant's data (the open rows), never as of the wall clock.
Every function takes a PrincipalCtx and returns nothing the principal may not see.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg

from kb.acl import PrincipalCtx

_INF = datetime.max.replace(tzinfo=timezone.utc)

VERSION_COLS = """v.version_id, v.entity_id, v.seq, v.op, v.kind_id, v.title, v.content, v.body, v.changed_fields,
  v.valid_from, lower(v.tx) AS recorded_at, upper(v.tx) AS superseded_at, v.tx_seq, v.actor_id,
  v.source_revision_id, v.source_modified_at, v.acl"""


def clean_fields(content: dict | None) -> dict:
    return {k: v for k, v in (content or {}).items() if not k.startswith("@")}


def version_out(row: dict, *, with_body: bool = False) -> dict:
    out = {
        "version_id": row["version_id"],
        "entity_id": row["entity_id"],
        "seq": row["seq"],
        "op": row["op"],
        "kind": row["kind_id"],
        "title": row["title"],
        "fields": clean_fields(row.get("content")),
        "changed_fields": [f for f in row.get("changed_fields") or [] if not f.startswith("@")],
        "effective_from": row["valid_from"],
        "recorded_at": row["recorded_at"],
        "superseded_at": row.get("superseded_at"),
        "is_current": row.get("superseded_at") is None,
        "actor_id": row.get("actor_id"),
        "source_revision_id": row.get("source_revision_id"),
    }
    if with_body:
        out["body"] = row.get("body")
    return out


def heads_at(conn: psycopg.Connection, tenant_id: str, entity_ids: list[str] | None, known_at: datetime | None,
             ) -> dict[str, dict]:
    """Entity rows as known at K: the head version recorded by K gives kind, title, head and deletion (`entity` holds
    the end of the data). Entities with no version recorded by K are absent. Without K the `entity` rows themselves.
    Keys: entity_id, kind_id, title, head_version_id, head_seq, tombstone_tx (not None when deleted), acl, url,
    source_id, versions (count recorded by K)."""
    if known_at is None:
        rows = conn.execute(
            """SELECT entity_id, kind_id, title, head_version_id, head_seq, tombstone_tx, acl, url, source_id,
                      head_seq AS versions FROM entity
               WHERE tenant_id = %s AND (%s::text[] IS NULL OR entity_id = ANY(%s))""",
            (tenant_id, entity_ids, entity_ids)).fetchall()
        return {r["entity_id"]: r for r in rows}
    rows = conn.execute(
        """SELECT e.entity_id, h.kind_id, h.title, h.version_id AS head_version_id, h.seq AS head_seq,
                  CASE WHEN h.op = 'delete' THEN h.tx_seq END AS tombstone_tx, e.acl, e.url, e.source_id,
                  h.seq AS versions
           FROM entity e JOIN LATERAL (
             SELECT v.version_id, v.seq, v.kind_id, coalesce(v.title, e.title) AS title, v.op, v.tx_seq
             FROM entity_version v
             WHERE v.tenant_id = e.tenant_id AND v.entity_id = e.entity_id AND lower(v.tx) <= %s
             ORDER BY v.seq DESC LIMIT 1) h ON true
           WHERE e.tenant_id = %s AND (%s::text[] IS NULL OR e.entity_id = ANY(%s))""",
        (known_at, tenant_id, entity_ids, entity_ids)).fetchall()
    return {r["entity_id"]: r for r in rows}


def get_entity(conn: psycopg.Connection, p: PrincipalCtx, entity_id: str) -> dict | None:
    row = conn.execute(
        """SELECT e.*, so.source_id AS primary_source, so.object_type, so.external_id
           FROM entity e LEFT JOIN LATERAL (
             SELECT source_id, object_type, external_id FROM source_object s
             WHERE s.tenant_id = e.tenant_id AND s.entity_id = e.entity_id AND s.lifecycle <> 'mapped'
             ORDER BY first_seen_tx LIMIT 1) so ON true
           WHERE e.tenant_id = %s AND e.entity_id = %s""",
        (p.tenant_id, entity_id),
    ).fetchone()
    if row is None or not p.can_see(row["acl"]):
        return None
    return row


def get_version(conn: psycopg.Connection, p: PrincipalCtx, version_id: str, *,
                known_at: datetime | None = None) -> dict | None:
    """A version (None if invisible or, as known at K, not recorded yet). As known at K a version superseded after
    K is still current."""
    row = conn.execute(
        f"""SELECT {VERSION_COLS}, e.acl AS entity_acl FROM entity_version v
            JOIN entity e ON e.tenant_id = v.tenant_id AND e.entity_id = v.entity_id
            WHERE v.tenant_id = %s AND v.version_id = %s""",
        (p.tenant_id, version_id),
    ).fetchone()
    if row is None or not p.can_see(row["entity_acl"]):
        return None
    if known_at is not None:
        if row["recorded_at"] > known_at:
            return None
        if row["superseded_at"] is not None and row["superseded_at"] > known_at:
            row["superseded_at"] = None
    return row


def version_at(
    conn: psycopg.Connection, p: PrincipalCtx, entity_id: str, *, valid_at: datetime | None = None,
    known_at: datetime | None = None,
) -> dict | None:
    """The version of the entity at business time valid_at as known at known_at (both default to now)."""
    if get_entity(conn, p, entity_id) is None:
        return None
    k = known_at or _INF
    if valid_at is None:
        if known_at is None:  # the head at the end of the data
            return conn.execute(
                f"SELECT {VERSION_COLS} FROM entity_version v WHERE v.tenant_id = %s AND v.entity_id = %s "
                "ORDER BY seq DESC LIMIT 1",
                (p.tenant_id, entity_id),
            ).fetchone()
        return conn.execute(
            f"""SELECT {VERSION_COLS} FROM entity_version v
                WHERE v.tenant_id = %s AND v.entity_id = %s AND v.tx @> %s::timestamptz""",
            (p.tenant_id, entity_id, k),
        ).fetchone()
    return conn.execute(
        f"""SELECT {VERSION_COLS} FROM entity_version v
            WHERE v.tenant_id = %s AND v.entity_id = %s AND lower(v.tx) <= %s AND v.valid_from <= %s
            ORDER BY v.valid_from DESC, v.seq DESC LIMIT 1""",
        (p.tenant_id, entity_id, k, valid_at),
    ).fetchone()


def as_of(
    conn: psycopg.Connection, p: PrincipalCtx, entity_ids: list[str], *, valid_at: datetime | None = None,
    known_at: datetime | None = None,
) -> list[dict]:
    out = []
    for eid in entity_ids:
        row = version_at(conn, p, eid, valid_at=valid_at, known_at=known_at)
        if row is not None:
            v = version_out(row)
            v["existed"] = row["op"] != "delete"
            out.append(v)
    return out


def _as_known(row: dict, known_at: datetime | None) -> dict:
    """A version row as known at K: a supersession recorded after K is not known yet."""
    if known_at is not None and row.get("superseded_at") is not None and row["superseded_at"] > known_at:
        row = {**row, "superseded_at": None}
    return row


def history(
    conn: psycopg.Connection, p: PrincipalCtx, entity_id: str, *, start: datetime | None = None,
    end: datetime | None = None, fields: list[str] | None = None, known_at: datetime | None = None,
) -> list[dict] | None:
    """Versions recorded in [start, end) on the transaction axis, as known at K (versions recorded by K; None if the
    entity is invisible or did not exist yet at K)."""
    if get_entity(conn, p, entity_id) is None:
        return None
    rows = conn.execute(
        f"""SELECT {VERSION_COLS} FROM entity_version v
            WHERE v.tenant_id = %s AND v.entity_id = %s
              AND (%s::timestamptz IS NULL OR lower(v.tx) >= %s) AND (%s::timestamptz IS NULL OR lower(v.tx) < %s)
              AND (%s::timestamptz IS NULL OR lower(v.tx) <= %s)
              AND (%s::text[] IS NULL OR v.changed_fields && %s::text[] OR v.op <> 'update')
            ORDER BY v.seq""",
        (p.tenant_id, entity_id, start, start, end, end, known_at, known_at, fields, fields),
    ).fetchall()
    if known_at is not None and not rows and conn.execute(
            "SELECT 1 FROM entity_version WHERE tenant_id = %s AND entity_id = %s AND lower(tx) <= %s LIMIT 1",
            (p.tenant_id, entity_id, known_at)).fetchone() is None:
        return None
    return [version_out(_as_known(r, known_at)) for r in rows]


def _statements_open_at(conn: psycopg.Connection, p: PrincipalCtx, subject_id: str, at: datetime) -> list[dict]:
    rows = conn.execute(
        """SELECT s.stmt_id, s.predicate_id, s.object_id, s.object_literal, s.origin, e.acl AS object_acl
           FROM statement s LEFT JOIN entity e ON e.tenant_id = s.tenant_id AND e.entity_id = s.object_id
           WHERE s.tenant_id = %s AND s.subject_id = %s AND s.known @> %s::timestamptz""",
        (p.tenant_id, subject_id, at),
    ).fetchall()
    return [r for r in rows if r["object_id"] is None or p.can_see(r["object_acl"])]


def diff(
    conn: psycopg.Connection, p: PrincipalCtx, entity_id: str, from_seq: int | None = None, to_seq: int | None = None,
    *, known_at: datetime | None = None,
) -> dict | None:
    """Field-level and statement-level difference between two versions (default: previous vs head; as known at K
    only versions recorded by K, the head then)."""
    ent = get_entity(conn, p, entity_id)
    if ent is None:
        return None
    head_seq = ent["head_seq"]
    if known_at is not None:
        h = heads_at(conn, p.tenant_id, [entity_id], known_at).get(entity_id)
        if h is None:
            return None
        head_seq = h["head_seq"]
        if (to_seq or 0) > head_seq or (from_seq or 0) > head_seq:
            return None
    to_seq = to_seq or head_seq
    from_seq = from_seq if from_seq is not None else max(1, to_seq - 1)
    rows = conn.execute(
        f"SELECT {VERSION_COLS} FROM entity_version v WHERE v.tenant_id = %s AND v.entity_id = %s AND v.seq = ANY(%s)",
        (p.tenant_id, entity_id, [from_seq, to_seq]),
    ).fetchall()
    by_seq = {r["seq"]: r for r in rows}
    a, b = by_seq.get(from_seq), by_seq.get(to_seq)
    if a is None or b is None:
        return None
    fa, fb = clean_fields(a["content"]), clean_fields(b["content"])
    changes = [
        {"field": f, "before": fa.get(f), "after": fb.get(f)}
        for f in sorted(set(fa) | set(fb))
        if fa.get(f) != fb.get(f)
    ]
    if a["title"] != b["title"]:
        changes.insert(0, {"field": "title", "before": a["title"], "after": b["title"]})
    if (a["body"] or "") != (b["body"] or ""):
        changes.append({"field": "body", "before_chars": len(a["body"] or ""), "after_chars": len(b["body"] or "")})

    def key(r: dict) -> tuple:
        return (r["predicate_id"], r["object_id"], str(r["object_literal"]))

    sa = {key(r): r for r in _statements_open_at(conn, p, entity_id, a["recorded_at"])}
    sb = {key(r): r for r in _statements_open_at(conn, p, entity_id, b["recorded_at"])}

    def st(r: dict) -> dict:
        return {"predicate": r["predicate_id"], "object_id": r["object_id"], "literal": r["object_literal"],
                "origin": r["origin"]}

    return {
        "entity_id": entity_id,
        "from": version_out(_as_known(a, known_at)),
        "to": version_out(_as_known(b, known_at)),
        "changes": changes,
        "statements_added": [st(sb[k]) for k in sb.keys() - sa.keys()],
        "statements_removed": [st(sa[k]) for k in sa.keys() - sb.keys()],
    }


def statements(
    conn: psycopg.Connection, p: PrincipalCtx, entity_id: str, *, known_at: datetime | None = None,
    direction: str = "both",
) -> list[dict]:
    """Statements about the entity (out: entity is subject; in: entity is object) visible to the principal, known at
    K (default: the open ones, the end of the data)."""
    parts = []
    if direction in ("out", "both"):
        parts.append("s.subject_id = %(e)s")
    if direction in ("in", "both"):
        parts.append("s.object_id = %(e)s")
    rows = conn.execute(
        f"""SELECT s.stmt_id, s.subject_id, s.subject_version_id, s.predicate_id, s.object_id, s.object_literal,
                   s.qualifiers, lower(s.valid) AS valid_from, upper(s.valid) AS valid_to, lower(s.known) AS recorded_at,
                   upper(s.known) AS retracted_at, s.origin, s.confidence, s.provenance,
                   se.acl AS subject_acl, se.title AS subject_title, se.kind_id AS subject_kind,
                   oe.acl AS object_acl, oe.title AS object_title, oe.kind_id AS object_kind
            FROM statement s
            JOIN entity se ON se.tenant_id = s.tenant_id AND se.entity_id = s.subject_id
            LEFT JOIN entity oe ON oe.tenant_id = s.tenant_id AND oe.entity_id = s.object_id
            WHERE s.tenant_id = %(t)s AND ({' OR '.join(parts)})
              AND CASE WHEN %(k)s::timestamptz IS NULL THEN upper_inf(s.tx) ELSE s.known @> %(k)s::timestamptz END
            ORDER BY s.predicate_id, s.subject_id, s.object_id, s.stmt_key, lower(s.known)""",
        {"t": p.tenant_id, "e": entity_id, "k": known_at},
    ).fetchall()
    heads = heads_at(conn, p.tenant_id, sorted({r["subject_id"] for r in rows} | {r["object_id"] for r in rows
                                                                                 if r["object_id"]}), known_at) \
        if known_at is not None else {}
    out = []
    for r in rows:
        if not p.can_see(r["subject_acl"]) or (r["object_id"] and not p.can_see(r["object_acl"])):
            continue
        r = {k: v for k, v in r.items() if not k.endswith("_acl")}
        if known_at is not None:  # names and kinds as known at K; an end recorded after K is not known yet
            for side, eid in (("subject", r["subject_id"]), ("object", r["object_id"])):
                h = heads.get(eid) if eid else None
                if h is not None:
                    r[f"{side}_title"], r[f"{side}_kind"] = h["title"], h["kind_id"]
            if r["retracted_at"] is not None and r["retracted_at"] > known_at:
                r["retracted_at"] = None
        out.append(r)
    return out


def timeline(
    conn: psycopg.Connection, p: PrincipalCtx, *, entity_ids: list[str] | None = None, start: datetime | None = None,
    end: datetime | None = None, kinds: list[str] | None = None, axis: str = "valid", limit: int = 500,
    known_at: datetime | None = None,
) -> list[dict]:
    """Versions (create/update/delete events) in an interval on the valid or the tx axis, merged and ordered; as known
    at K only versions recorded by K."""
    col = "v.valid_from" if axis == "valid" else "lower(v.tx)"
    acl_sql, acl_params = p.sql_filter("e.acl")
    rows = conn.execute(
        f"""SELECT {VERSION_COLS} FROM entity_version v
            JOIN entity e ON e.tenant_id = v.tenant_id AND e.entity_id = v.entity_id
            WHERE v.tenant_id = %s AND {acl_sql}
              AND (%s::text[] IS NULL OR v.entity_id = ANY(%s))
              AND (%s::text[] IS NULL OR v.kind_id = ANY(%s))
              AND (%s::timestamptz IS NULL OR {col} >= %s) AND (%s::timestamptz IS NULL OR {col} < %s)
              AND (%s::timestamptz IS NULL OR lower(v.tx) <= %s)
            ORDER BY {col}, v.tx_seq, v.version_id LIMIT %s""",
        (p.tenant_id, *acl_params, entity_ids, entity_ids, kinds, kinds, start, start, end, end, known_at, known_at,
         limit),
    ).fetchall()
    return [version_out(_as_known(r, known_at)) for r in rows]


def entity_card(conn: psycopg.Connection, p: PrincipalCtx, entity_id: str, *,
                known_at: datetime | None = None) -> dict[str, Any] | None:
    """The entity with its head and statements (as known at K: the head then; None if it did not exist yet)."""
    ent = get_entity(conn, p, entity_id)
    if ent is None:
        return None
    if known_at is not None:
        h = heads_at(conn, p.tenant_id, [entity_id], known_at).get(entity_id)
        if h is None:
            return None
        ent = {**ent, "kind_id": h["kind_id"], "title": h["title"], "head_version_id": h["head_version_id"],
               "head_seq": h["head_seq"], "tombstone_tx": h["tombstone_tx"]}
    head = get_version(conn, p, ent["head_version_id"], known_at=known_at)
    return {
        "entity_id": ent["entity_id"],
        "kind": ent["kind_id"],
        "title": ent["title"],
        "url": ent["url"],
        "source_id": ent["source_id"],
        "external": {"source_id": ent["primary_source"], "object_type": ent["object_type"], "external_id": ent["external_id"]},
        "deleted": ent["tombstone_tx"] is not None,
        "head": version_out(head, with_body=True) if head else None,
        "versions": ent["head_seq"],
        "statements": statements(conn, p, entity_id, known_at=known_at),
    }
