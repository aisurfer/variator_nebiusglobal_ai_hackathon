"""Graph REST (K10): expand, neighbourhood, paths, PPR (regime 2) and the graph consistency check.

Every response carries `watermark` = {journal, graph, lag[, known_at_beyond_watermark]}: the graph is a
projection and may lag the journal (01 §5 F2). With KB_GRAPH_ENABLED=false the endpoints answer 503.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from neo4j.exceptions import ServiceUnavailable
from pydantic import BaseModel, Field

from kb.api.deps import Conn, KnownAt, Principal, not_found, parse_time, view_time
from kb.graph import memgraph as mg
from kb.graph import service

router = APIRouter(prefix="/api/v1", tags=["graph"])


def _guard() -> None:
    if not mg.enabled():
        raise HTTPException(503, "graph projection disabled (KB_GRAPH_ENABLED=false)")


def _call(fn, *args, **kwargs):
    _guard()
    try:
        return fn(*args, **kwargs)
    except ServiceUnavailable:
        raise HTTPException(503, "graph store unavailable") from None


class ExpandIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=50, description="entity ids (or version ids)")
    predicates: list[str] | None = Field(None, description="only these relation / causal predicates")
    depth: int = Field(1, ge=1, le=service.MAX_DEPTH)
    known_at: str | None = Field(None, description="ISO time: the graph as known then (closed edges included)")
    include_inferred: bool = Field(False, description="also pending/inferred causal links")
    max_nodes: int = Field(200, ge=1, le=service.HARD_MAX_NODES)
    max_edges: int = Field(500, ge=0, le=service.HARD_MAX_EDGES)


@router.post("/graph/expand")
def graph_expand(conn: Conn, p: Principal, body: ExpandIn) -> dict[str, Any]:
    """Visible neighbourhood of the seeds over relations and causal links, within node/edge budgets."""
    k = view_time(conn, p.tenant_id, parse_time(body.known_at, "known_at"))
    out = _call(service.expand, p, body.ids, predicates=body.predicates, depth=body.depth, known_at=k,
                include_inferred=body.include_inferred, max_nodes=body.max_nodes, max_edges=body.max_edges)
    out["watermark"] = service.watermark(conn, p.tenant_id, k)
    return out


@router.get("/graph/neighborhood/{entity_id:path}")
def graph_neighborhood(
    conn: Conn, p: Principal, entity_id: str, depth: Annotated[int, Query(ge=1, le=service.MAX_DEPTH)] = 1,
    known_at: KnownAt = None, include_inferred: bool = False,
    predicates: Annotated[list[str] | None, Query()] = None,
    max_nodes: Annotated[int, Query(ge=1, le=service.HARD_MAX_NODES)] = 200,
    max_edges: Annotated[int, Query(ge=0, le=service.HARD_MAX_EDGES)] = 500,
) -> dict[str, Any]:
    """Drill-down around one entity: neighbours, edges and the entity's version chain."""
    k = known_at
    out = _call(service.neighborhood, p, entity_id, depth=depth, known_at=k, include_inferred=include_inferred,
                predicates=predicates, max_nodes=max_nodes, max_edges=max_edges)
    if out is None:
        raise not_found()
    out["watermark"] = service.watermark(conn, p.tenant_id, k)
    return out


@router.get("/graph/path")
def graph_path(
    conn: Conn, p: Principal, source: str, target: str,
    max_depth: Annotated[int, Query(ge=1, le=service.MAX_PATH_DEPTH)] = 4, known_at: KnownAt = None,
    include_inferred: bool = False, predicates: Annotated[list[str] | None, Query()] = None,
    max_paths: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """All shortest visible paths between two entities (no path is reported through hidden nodes)."""
    k = known_at
    out = _call(service.path, p, source, target, max_depth=max_depth, known_at=k, include_inferred=include_inferred,
                predicates=predicates, max_paths=max_paths)
    out["watermark"] = service.watermark(conn, p.tenant_id, k)
    return out


class PprIn(BaseModel):
    seeds: list[str] = Field(min_length=1, max_length=50)
    window_start: str | None = None
    window_end: str | None = None
    top_k: int = Field(10, ge=1, le=100)
    hops: int = Field(2, ge=1, le=service.MAX_DEPTH)
    exclude_declared_causes_of: str | None = None
    include_inferred: bool = False
    active_only: bool | None = None


@router.post("/graph/ppr")
def graph_ppr(conn: Conn, p: Principal, body: PprIn) -> dict[str, Any]:
    """Regime 2: entities likely related to the seeds in a time window (hypotheses, not declared causality)."""
    ws = parse_time(body.window_start, "window_start")
    we = view_time(conn, p.tenant_id, parse_time(body.window_end, "window_end"))
    items = _call(service.ppr, p, body.seeds, window_start=ws, window_end=we, top_k=body.top_k, hops=body.hops,
                  exclude_declared_causes_of=body.exclude_declared_causes_of, include_inferred=body.include_inferred,
                  active_only=body.active_only)
    return {"label": service.PPR_LABEL, "regime": 2, "items": items,
            "query": body.model_dump(), "watermark": service.watermark(conn, p.tenant_id, we)}


@router.get("/admin/consistency/graph", tags=["admin"])
def admin_consistency_graph(
    conn: Conn, p: Principal, samples: Annotated[int, Query(ge=1, le=500)] = 20, seed: int | None = None,
) -> dict[str, Any]:
    """Random (entity, known_at) pairs: journal version vs graph version (research §5.7); counts for admins."""
    return _call(service.consistency_sample, conn, p, samples, seed=seed)
