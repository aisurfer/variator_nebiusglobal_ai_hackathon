"""Typed read tools (research §5.3) used by /ask (the plan executor).

A tool gets a ToolContext (connection, principal, now, known_at), validated arguments, and returns JSON-ready
data. Every call leaves a receipt (tool, args, version/entity ids it returned, duration, ok) — the audit trail
of an investigation; hidden reasoning is never stored. Limits are enforced here, independent of the caller.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import psycopg

from kb.acl import PrincipalCtx
from kb.journal import reader
from kb.text import norm

MAX_DEPTH = 8
MAX_LIMIT = 50


@dataclass
class ToolContext:
    conn: psycopg.Connection
    principal: PrincipalCtx
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    known_at: datetime | None = None
    tz: str = "UTC"  # tenant timezone for presenting times
    receipts: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: set[str] = field(default_factory=set)  # version/entity ids the tools actually returned


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema of the arguments
    fn: Callable[..., Any]

    def tool_schema(self) -> dict[str, Any]:
        """The tool as an entry of the `tools` list of a chat completions request."""
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def _dt(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def _upto(ctx: "ToolContext", v: Any) -> datetime | None:
    """A time a tool was asked for, never after the date of view (Docs/KB/AS-OF.md); without one, the date of view."""
    t = _dt(v)
    if ctx.known_at is None:
        return t
    return ctx.known_at if t is None or t > ctx.known_at else t


def _collect_ids(obj: Any, out: set[str], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("version_id", "effect_version", "cause_version", "cause_current_version", "entity_id") and isinstance(v, str):
                out.add(v)
            else:
                _collect_ids(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:500]:
            _collect_ids(v, out, depth + 1)


def call(ctx: ToolContext, name: str, args: dict[str, Any]) -> Any:
    tool = TOOLS[name]
    t0 = time.monotonic()
    ok, err, result = True, None, None
    try:
        result = tool.fn(ctx, **args)
        return result
    except Exception as exc:  # the receipt records the failure; the caller decides
        ok, err = False, f"{type(exc).__name__}: {exc}"
        raise
    finally:
        ids: set[str] = set()
        if ok:
            _collect_ids(result, ids)
            ctx.evidence_ids |= ids
        ctx.receipts.append({"tool": name, "args": args, "ok": ok, "error": err, "ms": round((time.monotonic() - t0) * 1000),
                             "returned_ids": sorted(ids)[:100]})


# ------------------------------------------------------------------ grounding and search
def _ground_pg(ctx: ToolContext, text: str, entity_kinds: list[str] | None, limit: int) -> list[dict]:
    """Fallback grounding over the journal's alias table when the search projection is unavailable."""
    p = ctx.principal
    acl_sql, acl_params = p.sql_filter("e.acl")
    t = norm(text)
    rows = ctx.conn.execute(
        f"""SELECT e.entity_id, e.kind_id, e.title, max(CASE WHEN a.alias_norm = %s THEN 3
                                                       WHEN a.alias_norm LIKE %s THEN 1 ELSE 0 END) AS score
            FROM alias a JOIN entity e ON e.tenant_id = a.tenant_id AND e.entity_id = a.entity_id
            WHERE a.tenant_id = %s AND {acl_sql} AND e.tombstone_tx IS NULL AND upper_inf(a.tx)
              AND (a.alias_norm = %s OR a.alias_norm LIKE %s)
              AND (%s::text[] IS NULL OR e.kind_id = ANY(%s))
            GROUP BY 1, 2, 3 ORDER BY score DESC, e.entity_id LIMIT %s""",
        (t, f"%{t}%", p.tenant_id, *acl_params, t, f"%{t}%", entity_kinds, entity_kinds, limit),
    ).fetchall()
    return [{"id": r["entity_id"], "doc_type": "entity", "kind": r["kind_id"], "label": r["title"],
             "score": float(r["score"]), "matched": "alias (journal)"} for r in rows]


def t_ground(ctx: ToolContext, text: str, kind: str | None = "entity", entity_kinds: list[str] | None = None,
             limit: int = 5) -> list[dict]:
    limit = min(limit, 20)
    try:
        from kb.search.service import ground

        hits = ground(ctx.conn, ctx.principal, text, kind=kind, entity_kinds=entity_kinds, as_of=ctx.known_at,
                      limit=limit)
        if hits or kind == "predicate":
            return hits
    except ModuleNotFoundError:
        pass
    return _ground_pg(ctx, text, entity_kinds, limit) if kind != "predicate" else []


def t_search(ctx: ToolContext, query: str, kinds: list[str] | None = None, mode: str = "hybrid",
             valid_at: str | None = None, limit: int = 10, entity_ids: list[str] | None = None) -> list[dict]:
    from kb.search.service import search

    return search(ctx.conn, ctx.principal, query, kinds=kinds, mode=mode, valid_at=_dt(valid_at),
                  known_at=ctx.known_at, limit=min(limit, 100 if entity_ids is not None else MAX_LIMIT),
                  entity_ids=entity_ids)


def t_search_versions(ctx: ToolContext, query: str, start: str | None = None, end: str | None = None,
                      kinds: list[str] | None = None, limit: int = 10) -> list[dict]:
    from kb.search.service import search_versions

    end_t = _dt(end)
    if ctx.known_at is not None and (end_t is None or end_t > ctx.known_at):
        end_t = ctx.known_at + timedelta(microseconds=1)  # versions recorded by the date of view
    return search_versions(ctx.conn, ctx.principal, query, start=_dt(start), end=end_t, kinds=kinds,
                           limit=min(limit, MAX_LIMIT), known_at=ctx.known_at)


# ------------------------------------------------------------------ journal
def t_entity(ctx: ToolContext, entity_id: str) -> dict | None:
    card = reader.entity_card(ctx.conn, ctx.principal, entity_id, known_at=ctx.known_at)
    if card and ctx.known_at is not None:
        card["known_at"] = ctx.known_at  # «what did we know at K»: entity_card gave the head and statements at K
    if card and card.get("head"):
        body = card["head"].get("body") or ""
        card["head"]["body"] = body[:1500]
    return card


def t_history(ctx: ToolContext, entity_id: str, start: str | None = None, end: str | None = None,
              fields: list[str] | None = None) -> list[dict] | None:
    return reader.history(ctx.conn, ctx.principal, entity_id, start=_dt(start), end=_dt(end), fields=fields,
                          known_at=ctx.known_at)


def t_diff(ctx: ToolContext, entity_id: str, from_seq: int | None = None, to_seq: int | None = None) -> dict | None:
    return reader.diff(ctx.conn, ctx.principal, entity_id, from_seq, to_seq, known_at=ctx.known_at)


def t_as_of(ctx: ToolContext, entity_ids: list[str], valid_at: str | None = None,
            known_at: str | None = None) -> list[dict]:
    return reader.as_of(ctx.conn, ctx.principal, entity_ids[:MAX_LIMIT], valid_at=_dt(valid_at),
                        known_at=_upto(ctx, known_at))


def t_timeline(ctx: ToolContext, entity_ids: list[str] | None = None, start: str | None = None,
               end: str | None = None, kinds: list[str] | None = None, limit: int = 50) -> list[dict]:
    return reader.timeline(ctx.conn, ctx.principal, entity_ids=entity_ids, start=_dt(start), end=_dt(end),
                           kinds=kinds, limit=min(limit, 200), known_at=ctx.known_at)


# ------------------------------------------------------------------ causal (PostgreSQL, authoritative)
def t_why(ctx: ToolContext, target: str, depth: int = 4, include_inferred: bool = False) -> dict:
    from kb.causal.service import why

    return why(ctx.conn, ctx.principal, target, depth=min(depth, MAX_DEPTH), include_inferred=include_inferred,
               known_at=ctx.known_at)


def t_causes(ctx: ToolContext, version_id: str, include_inferred: bool = False) -> Any:
    from kb.causal.service import causes

    return causes(ctx.conn, ctx.principal, version_id, include_inferred=include_inferred, known_at=ctx.known_at)


def t_effects(ctx: ToolContext, target: str, depth: int = 3, include_inferred: bool = False) -> Any:
    from kb.causal.service import effects

    return effects(ctx.conn, ctx.principal, target, depth=min(depth, MAX_DEPTH), include_inferred=include_inferred,
                   known_at=ctx.known_at)


def t_impact(ctx: ToolContext, entity_id: str, fields: list[str] | None = None, depth: int = 4) -> Any:
    from kb.causal.service import impact

    return impact(ctx.conn, ctx.principal, entity_id, fields or ["*"], depth=min(depth, MAX_DEPTH),
                  known_at=ctx.known_at)


def t_stale(ctx: ToolContext, scope: str | None = None, since: str | None = None, limit: int = 30) -> Any:
    from kb.causal.service import stale_dependencies

    return stale_dependencies(ctx.conn, ctx.principal, scope=scope, since=_dt(since), limit=min(limit, 100),
                              known_at=ctx.known_at)


# ------------------------------------------------------------------ graph projection
def t_expand(ctx: ToolContext, ids: list[str], predicates: list[str] | None = None, depth: int = 1) -> Any:
    from kb.graph.service import expand

    return expand(ctx.principal, ids[:20], predicates=predicates, depth=min(depth, 2), known_at=ctx.known_at)


def t_ppr(ctx: ToolContext, seeds: list[str], window_start: str | None = None, window_end: str | None = None,
          top_k: int = 10) -> Any:
    from kb.graph.service import ppr

    return ppr(ctx.principal, seeds[:20], window_start=_dt(window_start), window_end=_upto(ctx, window_end),
               top_k=min(top_k, 20))


def t_resolve_time(ctx: ToolContext, expression: str) -> dict:
    from kb.reasoning.timeparse import resolve_time

    return resolve_time(expression, ctx.now, tz=ctx.tz).as_dict()


_S = {"type": "string"}
_SA = {"type": "array", "items": {"type": "string"}}
_I = {"type": "integer"}
_B = {"type": "boolean"}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("ground", "Resolve a name/phrase/key to entities (or to a relation type with kind='predicate'). Returns "
         "candidates with scores; several candidates mean ambiguity.",
         _obj({"text": _S, "kind": {"type": "string", "enum": ["entity", "predicate"]}, "entity_kinds": _SA,
               "limit": _I}, ["text"]), t_ground),
    Tool("search", "Hybrid full-text + semantic search over document fragments. Returns fragments with the "
         "exact version id, recorded and effective times.",
         _obj({"query": _S, "kinds": _SA, "mode": {"type": "string", "enum": ["hybrid", "lexical", "vector"]},
               "valid_at": _S, "limit": _I}, ["query"]), t_search),
    Tool("search_versions", "Search versions (edits) on the transaction-time axis within an interval.",
         _obj({"query": _S, "start": _S, "end": _S, "kinds": _SA, "limit": _I}, ["query"]), t_search_versions),
    Tool("entity", "Open an entity: current version with fields, statements (relations).",
         _obj({"entity_id": _S}, ["entity_id"]), t_entity),
    Tool("history", "All versions of an entity: who changed what and when, including deletion.",
         _obj({"entity_id": _S, "start": _S, "end": _S, "fields": _SA}, ["entity_id"]), t_history),
    Tool("diff", "Field and relation differences between two versions of an entity (default previous vs head).",
         _obj({"entity_id": _S, "from_seq": _I, "to_seq": _I}, ["entity_id"]), t_diff),
    Tool("as_of", "State of entities at business time valid_at as known at known_at.",
         _obj({"entity_ids": _SA, "valid_at": _S, "known_at": _S}, ["entity_ids"]), t_as_of),
    Tool("timeline", "Events (creations, edits, deletions) of entities in an interval, ordered in time.",
         _obj({"entity_ids": _SA, "start": _S, "end": _S, "kinds": _SA, "limit": _I}, []), t_timeline),
    Tool("why", "Declared causal trace of an entity or version back to its first causes, with versions, both "
         "times, origin and current status (CURRENT/STALE/BROKEN...).",
         _obj({"target": _S, "depth": _I, "include_inferred": _B}, ["target"]), t_why),
    Tool("causes", "Direct causes of a version (or an entity's head) with link statuses.",
         _obj({"version_id": _S, "include_inferred": _B}, ["version_id"]), t_causes),
    Tool("effects", "What depends on an entity/version (forward causal traversal) with link statuses.",
         _obj({"target": _S, "depth": _I, "include_inferred": _B}, ["target"]), t_effects),
    Tool("impact", "What would be affected if the given fields of an entity changed (['*'] = any change).",
         _obj({"entity_id": _S, "fields": _SA, "depth": _I}, ["entity_id"]), t_impact),
    Tool("stale_dependencies", "Current objects that rely on superseded or deleted versions of their causes.",
         _obj({"scope": _S, "since": _S, "limit": _I}, []), t_stale),
    Tool("expand", "Neighbourhood of entities in the knowledge graph (relations and causal links).",
         _obj({"ids": _SA, "predicates": _SA, "depth": _I}, ["ids"]), t_expand),
    Tool("ppr", "Entities likely related to the seeds in a time window (Personalized PageRank, NOT declared "
         "causality — label results as hypotheses).",
         _obj({"seeds": _SA, "window_start": _S, "window_end": _S, "top_k": _I}, ["seeds"]), t_ppr),
    Tool("resolve_time", "Turn a time expression into an explicit interval with an interpretation.",
         _obj({"expression": _S}, ["expression"]), t_resolve_time),
]}
