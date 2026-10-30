"""The as-of guarantee (Docs/KB/AS-OF.md): the data bounds of a tenant and the differential check.

State as known at T. Every version recorded by T (tx <= T; "in effect at T" adds valid_from <= T), every statement and
causal link whose knowledge interval contains T, and every derived fact (mentions, inferred candidates) computed the way
a system that had loaded only the data up to T would have computed it. Nothing recorded after T may change it.

The differential check compares two tenants of one feed: A was loaded only up to T (`seed --until T`) and drained, B was
loaded completely and drained. What B knows at T must equal what A knows: the journal rows (versions, statements, links
with their knowledge intervals), the link statuses, and the answers of the read functions (history, timeline, entity,
why, effects, stale, candidates, statistics, search when projected). Ids that only number rows (statement ids, link ids,
transaction numbers, source revision ids) differ between tenants and are replaced by content keys before comparing.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import orjson
import psycopg

from kb.acl import PrincipalCtx, system_principal

_EPS = timedelta(microseconds=1)


class BeyondData(ValueError):
    """The requested date lies after the end of the tenant's data."""

    def __init__(self, at: datetime, bounds: "Bounds"):
        self.at, self.bounds = at, bounds
        super().__init__(
            f"{at.isoformat()} is after the end of the data of tenant {bounds.tenant_id} "
            f"({bounds.end.isoformat() if bounds.end else 'no data'}); the data spans "
            f"{bounds.start.isoformat() if bounds.start else '—'} … {bounds.end.isoformat() if bounds.end else '—'}")

    def detail(self) -> dict[str, Any]:
        return {"message": str(self), "known_at": self.at.isoformat(), **self.bounds.as_dict()}


@dataclass(frozen=True)
class Bounds:
    tenant_id: str
    start: datetime | None  # first transaction of the tenant (bootstrap included)
    end: datetime | None  # last transaction that brought data (ingest, editor, review, admin; not extraction)

    def as_dict(self) -> dict[str, Any]:
        return {"tenant": self.tenant_id, "data_start": self.start.isoformat() if self.start else None,
                "data_end": self.end.isoformat() if self.end else None}


def bounds(conn: psycopg.Connection, tenant_id: str) -> Bounds:
    row = conn.execute(
        """SELECT min(tx_at) AS start, max(tx_at) FILTER (WHERE kind <> 'extract') AS "end"
           FROM tx_log WHERE tenant_id = %s""", (tenant_id,)).fetchone()
    return Bounds(tenant_id, row["start"] if row else None, row["end"] if row else None)


def view_at(conn: psycopg.Connection, tenant_id: str, known_at: datetime | None) -> datetime | None:
    """The date of a read. None (no date given) stays None: the end of the tenant's data, which the readers express as
    "open rows". A date after the end of the data is refused (BeyondData): a simulated future is readable only where
    the journal holds data, and the wall clock never stands in for data."""
    if known_at is None:
        return None
    b = bounds(conn, tenant_id)
    if b.end is None or known_at > b.end:
        raise BeyondData(known_at, b)
    return known_at


# ------------------------------------------------------------------------------------------------ canonical state
def _has_known(conn: psycopg.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'causal_link' AND column_name = 'known'"
    ).fetchone() is not None


def _iso(v: Any) -> Any:
    return v.astimezone(timezone.utc).isoformat() if isinstance(v, datetime) else v


def _clip(upper: datetime | None, at: datetime) -> str | None:
    """The end of an interval as known at T: an end after T was not known yet."""
    return None if upper is None or upper > at else _iso(upper)


_DROP_EVIDENCE = ("source_revision_id", "candidate", "revalidated_from")


def _evidence(ev: dict | None) -> dict:
    return {k: v for k, v in sorted((ev or {}).items()) if k not in _DROP_EVIDENCE}


def link_keys(conn: psycopg.Connection, tenant_id: str) -> dict[str, str]:
    """link id -> content key (effect version, predicate, cause version, origin, start of knowledge)."""
    col = "known" if _has_known(conn) else "tx"
    return {r["link_id"]: f"{r['effect_version']}|{r['predicate_id']}|{r['cause_version']}|{r['origin']}|{_iso(r['k'])}"
            for r in conn.execute(
                f"""SELECT link_id, effect_version, predicate_id, cause_version, origin, lower({col}) AS k
                    FROM causal_link WHERE tenant_id = %s""", (tenant_id,)).fetchall()}


def stmt_keys(conn: psycopg.Connection, tenant_id: str) -> dict[str, str]:
    """'stmt:<id>' (graph edge ids) -> content key of the statement."""
    col = "known" if _has_known(conn) else "tx"
    return {f"stmt:{r['stmt_id']}": f"stmt:{r['subject_id']}|{r['predicate_id']}|{r['object_id']}|{r['origin']}|{_iso(r['k'])}"
            for r in conn.execute(
                f"""SELECT stmt_id, subject_id, predicate_id, object_id, origin, lower({col}) AS k FROM statement
                    WHERE tenant_id = %s AND object_id IS NOT NULL""", (tenant_id,)).fetchall()}


def journal_state(conn: psycopg.Connection, tenant_id: str, at: datetime) -> dict[str, dict[str, Any]]:
    """Journal rows known at T, keyed by content: versions, statements and causal links with their intervals."""
    col = "known" if _has_known(conn) else "tx"
    out: dict[str, dict[str, Any]] = {"versions": {}, "statements": {}, "links": {}}
    for r in conn.execute(
        """SELECT version_id, op, content_hash, valid_from, lower(tx) AS rec, upper(tx) AS sup, actor_id
           FROM entity_version WHERE tenant_id = %s AND lower(tx) <= %s""", (tenant_id, at)).fetchall():
        out["versions"][r["version_id"]] = [r["op"], r["content_hash"], _iso(r["valid_from"]), _iso(r["rec"]),
                                            _clip(r["sup"], at), r["actor_id"]]
    for r in conn.execute(
        f"""SELECT subject_id, subject_version_id, predicate_id, object_id, object_literal, qualifiers, origin,
                   lower(valid) AS vf, upper(valid) AS vt, lower({col}) AS kf, upper({col}) AS kt, confidence, provenance
            FROM statement WHERE tenant_id = %s AND lower({col}) <= %s AND NOT isempty({col})""",
            (tenant_id, at)).fetchall():
        key = "|".join(str(x) for x in (r["subject_id"], r["predicate_id"], r["object_id"],
                                        orjson.dumps(r["object_literal"], option=orjson.OPT_SORT_KEYS).decode(),
                                        orjson.dumps(r["qualifiers"], option=orjson.OPT_SORT_KEYS).decode(),
                                        r["origin"], _iso(r["kf"])))
        prov = _evidence(r["provenance"])
        out["statements"][key] = [r["subject_version_id"], _iso(r["vf"]), _iso(r["vt"]), _clip(r["kt"], at),
                                  round(float(r["confidence"]), 4), prov]
    for r in conn.execute(
        f"""SELECT effect_version, cause_version, predicate_id, origin, causal_kind, sensitivity, confidence,
                   review_status, retract_reason, evidence, lower(valid) AS vf, upper(valid) AS vt,
                   lower({col}) AS kf, upper({col}) AS kt
            FROM causal_link WHERE tenant_id = %s AND lower({col}) <= %s AND NOT isempty({col})""",
            (tenant_id, at)).fetchall():
        key = f"{r['effect_version']}|{r['predicate_id']}|{r['cause_version']}|{r['origin']}|{_iso(r['kf'])}"
        closed = r["kt"] is not None and r["kt"] <= at
        review = r["review_status"] if closed or not r["origin"].startswith("inferred:") else "pending"
        out["links"][key] = [r["causal_kind"], sorted(r["sensitivity"]) if r["sensitivity"] is not None else None,
                             round(float(r["confidence"]), 4), review, r["retract_reason"] if closed else None,
                             _iso(r["vf"]), _iso(r["vt"]), _clip(r["kt"], at), _evidence(r["evidence"])]
    from kb import causal

    p = system_principal(tenant_id)
    keys = link_keys(conn, tenant_id)
    ids = [lid for lid, k in keys.items() if k in out["links"]]
    out["status"] = {keys[lid]: s for lid, s in causal.link_status(conn, p, ids, known_at=at).items()}
    return out


_LINK_ID = re.compile(r"\bcl-\d+-\d+\b|\bstmt:\d+\b")
_DROP_KEYS = frozenset({"stmt_id", "tx_seq", "took_ms", "watermarks", "watermark", "run_id", "source_revision_id",
                        "stale_projection", "total", "tenant"})


def normalize(obj: Any, keys: dict[str, str]) -> Any:
    """JSON of a read with row numbers replaced: link ids by content keys, statement ids and counters dropped."""
    if isinstance(obj, dict):
        return {(keys.get(k, k) if isinstance(k, str) else k): normalize(v, keys)
                for k, v in obj.items() if k not in _DROP_KEYS}
    if isinstance(obj, (list, tuple)):
        return [normalize(v, keys) for v in obj]
    if isinstance(obj, str):
        if obj in keys:
            return keys[obj]
        return _LINK_ID.sub(lambda m: keys.get(m.group(0), m.group(0)), obj) if ("cl-" in obj or "stmt:" in obj) else obj
    if isinstance(obj, datetime):
        return _iso(obj)
    if isinstance(obj, float):
        return round(obj, 6)
    return obj


def _call(fn: Callable, *args: Any, at: datetime, **kwargs: Any) -> Any:
    """Call a read function as of T: `known_at` when it takes one, else the closest window it offers (the answer a
    user of that function gets), else without a date."""
    params = inspect.signature(fn).parameters
    if "known_at" in params:
        kwargs["known_at"] = at
    elif "end" in params:
        kwargs["end"] = at + _EPS
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a failing read is a difference, not a crash of the check
        return {"error": type(exc).__name__}


def sample_entities(conn: psycopg.Connection, tenant_id: str, at: datetime, n: int | None) -> list[str]:
    """Entities known at T, every k-th in id order (deterministic), or all of them."""
    rows = [r["entity_id"] for r in conn.execute(
        """SELECT DISTINCT entity_id FROM entity_version WHERE tenant_id = %s AND lower(tx) <= %s ORDER BY entity_id""",
        (tenant_id, at)).fetchall()]
    if n is None or len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def api_state(conn: psycopg.Connection, tenant_id: str, at: datetime, entities: Iterable[str], *,
              queries: Iterable[str] = (), graph: bool = False) -> dict[str, Any]:
    """Answers of the read functions as of T, normalised."""
    from kb import causal
    from kb.api import routes_core
    from kb.journal import reader

    p: PrincipalCtx = system_principal(tenant_id)
    keys = link_keys(conn, tenant_id) | stmt_keys(conn, tenant_id)
    out: dict[str, Any] = {}
    for eid in entities:
        out[f"history:{eid}"] = _call(reader.history, conn, p, eid, at=at)
        out[f"entity:{eid}"] = _call(reader.entity_card, conn, p, eid, at=at)
        out[f"why:{eid}"] = _call(causal.why, conn, p, eid, include_inferred=True, at=at)
        out[f"effects:{eid}"] = _call(causal.effects, conn, p, eid, depth=2, include_inferred=True, at=at)
        if graph:
            from kb.graph import service as gs

            out[f"neighborhood:{eid}"] = _call(gs.neighborhood, p, eid, include_inferred=True, at=at)
    out["timeline"] = _call(reader.timeline, conn, p, limit=100000, at=at)
    out["stale"] = _call(causal.stale_dependencies, conn, p, limit=1000, at=at)
    out["entities"] = _call(routes_core.list_entities, conn, p, limit=100000, offset=0, at=at)
    out["stats"] = _call(routes_core.admin_stats, conn, p, at=at)
    if queries:
        from kb.search import service as ss

        for q in queries:
            for mode in ("lexical", "vector"):  # each channel's match set; the hybrid order is BM25-dependent
                out[f"search:{mode}:{q}"] = _without_bm25(_call(ss.search, conn, p, q, mode=mode, limit=100, at=at))
    return {k: normalize(v, keys) for k, v in out.items()}


def _without_bm25(hits: Any) -> Any:
    """Search hits without what BM25 statistics decide. OpenSearch scores a lexical hit with the statistics of the whole
    shared index (every tenant, every moment, deleted documents until a merge), so the BM25 value, the lexical rank, the
    fused score, the order of hits, which hits make a top-N and which chunk represents an entity are not a function of
    the data known at T. Compared per channel (lexical, vector): the set of entities found among the first 100 (with
    the version and its times) — complete when fewer match — and nothing that depends on the ranking (Docs/KB/AS-OF.md,
    "Search")."""
    if not isinstance(hits, list):
        return hits
    return sorted({(str(h.get("entity_id")), str(h.get("version_id")), str(h.get("recorded_at")),
                    str(h.get("effective_from"))) for h in hits})


def state(conn: psycopg.Connection, tenant_id: str, at: datetime, *, entities: Iterable[str] | None = None,
          sample: int | None = None, queries: Iterable[str] = (), api: bool = True,
          graph: bool = False) -> dict[str, dict[str, Any]]:
    out = journal_state(conn, tenant_id, at)
    if api:
        ents = list(entities) if entities is not None else sample_entities(conn, tenant_id, at, sample)
        out["api"] = api_state(conn, tenant_id, at, ents, queries=queries, graph=graph)
    return out


def compare(a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]], *, examples: int = 5) -> dict[str, Any]:
    """Per category: how many facts were compared, and which are only in A, only in B, or differ."""
    report: dict[str, Any] = {}
    for cat in sorted(set(a) | set(b)):
        xa, xb = a.get(cat, {}), b.get(cat, {})
        only_a = sorted(set(xa) - set(xb))
        only_b = sorted(set(xb) - set(xa))
        differ = sorted(k for k in set(xa) & set(xb)
                        if orjson.dumps(xa[k], option=orjson.OPT_SORT_KEYS) != orjson.dumps(xb[k], option=orjson.OPT_SORT_KEYS))
        report[cat] = {"compared": len(set(xa) | set(xb)), "only_truncated": len(only_a), "only_full": len(only_b),
                       "differ": len(differ), "examples": {"only_truncated": only_a[:examples],
                                                           "only_full": only_b[:examples], "differ": differ[:examples]}}
    report["total"] = {k: sum(r[k] for c, r in report.items() if c != "total")
                       for k in ("compared", "only_truncated", "only_full", "differ")}
    report["equal"] = report["total"]["only_truncated"] + report["total"]["only_full"] + report["total"]["differ"] == 0
    return report


def first_difference(a: Any, b: Any, path: str = "") -> str | None:
    """Where two normalised answers part (for a readable failure)."""
    if type(a) is not type(b):
        return f"{path}: {type(a).__name__} != {type(b).__name__}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                return f"{path}.{k}: {'missing in truncated' if k not in a else 'missing in full'}"
            d = first_difference(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: {len(a)} items != {len(b)} items"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {str(a)[:160]!r} != {str(b)[:160]!r}"


def content_state(conn: psycopg.Connection, tenant_id: str, at: datetime) -> dict[str, dict[str, Any]]:
    """Derived facts by content only, without their knowledge intervals: the rows whose versions were all recorded by T
    (what a reader that cuts by the versions a fact rests on would take). Separates what the algorithms derive
    differently from what is only stamped differently (diagnostics of the differential check)."""
    out: dict[str, dict[str, Any]] = {"derived_statements": {}, "inferred_links": {}}
    for r in conn.execute(
        """SELECT s.subject_id, s.predicate_id, s.object_id, s.origin FROM statement s
           JOIN entity_version v ON v.tenant_id = s.tenant_id AND v.version_id = s.subject_version_id
           WHERE s.tenant_id = %s AND s.origin LIKE 'derived:%%' AND upper_inf(s.tx) AND lower(v.tx) <= %s
             AND (s.object_id IS NULL OR EXISTS (SELECT 1 FROM entity_version o WHERE o.tenant_id = s.tenant_id
                    AND o.entity_id = s.object_id AND lower(o.tx) <= %s))""", (tenant_id, at, at)).fetchall():
        out["derived_statements"][f"{r['subject_id']}|{r['predicate_id']}|{r['object_id']}|{r['origin']}"] = 1
    for r in conn.execute(
        """SELECT l.effect_version, l.predicate_id, l.cause_version, l.origin FROM causal_link l
           JOIN entity_version ev ON ev.tenant_id = l.tenant_id AND ev.version_id = l.effect_version
           JOIN entity_version cv ON cv.tenant_id = l.tenant_id AND cv.version_id = l.cause_version
           LEFT JOIN entity_version sv ON sv.tenant_id = l.tenant_id AND sv.version_id = l.evidence->>'source_version_id'
           WHERE l.tenant_id = %s AND l.origin LIKE 'inferred:%%' AND upper_inf(l.tx)
             AND lower(ev.tx) <= %s AND lower(cv.tx) <= %s AND (sv.version_id IS NULL OR lower(sv.tx) <= %s)""",
            (tenant_id, at, at, at)).fetchall():
        out["inferred_links"][f"{r['effect_version']}|{r['predicate_id']}|{r['cause_version']}|{r['origin']}"] = 1
    return out


def main(argv: list[str] | None = None) -> int:
    """python -m kb.asof --truncated A --full B --at T [--sample N] [--out report.json]: the differential check of two
    tenants of one feed (A loaded up to T, B completely)."""
    import argparse
    import json
    import sys
    import time

    from kb.db import connect

    ap = argparse.ArgumentParser(prog="python -m kb.asof")
    ap.add_argument("--truncated", required=True)
    ap.add_argument("--full", required=True)
    ap.add_argument("--at", required=True, help="ISO time with an offset")
    ap.add_argument("--sample", type=int, default=200, help="entities whose read answers are compared (0: none)")
    ap.add_argument("--queries", nargs="*", default=[], help="search queries (needs the search projection)")
    ap.add_argument("--graph", action="store_true", help="also the graph neighbourhoods (needs the graph projection)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    at = datetime.fromisoformat(args.at)
    t0 = time.monotonic()
    with connect() as conn:
        ents = sample_entities(conn, args.truncated, at, args.sample) if args.sample else []
        a = state(conn, args.truncated, at, entities=ents, queries=args.queries, api=bool(args.sample), graph=args.graph)
        b = state(conn, args.full, at, entities=ents, queries=args.queries, api=bool(args.sample), graph=args.graph)
        rep = compare(a, b, examples=10)
        rep["content"] = compare(content_state(conn, args.truncated, at), content_state(conn, args.full, at), examples=10)
    rep["query"] = {"truncated": args.truncated, "full": args.full, "at": at.isoformat(), "entities": len(ents),
                    "seconds": round(time.monotonic() - t0, 1)}
    summary = {c: {k: r[k] for k in ("compared", "only_truncated", "only_full", "differ")}
               for c, r in rep.items() if isinstance(r, dict) and "compared" in r}
    summary["content"] = {c: {k: r[k] for k in ("compared", "only_truncated", "only_full", "differ")}
                          for c, r in rep["content"].items() if isinstance(r, dict) and "compared" in r}
    print(json.dumps({"at": at.isoformat(), "equal": rep["equal"], **summary}, ensure_ascii=False, indent=1))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1, default=str)
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
