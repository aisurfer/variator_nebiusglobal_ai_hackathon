"""Causal REST (01 §7, §8): why / causes / effects / impact / stale / links. Authoritative in PostgreSQL.

Unknown and invisible targets are both 404 (deps.not_found). Times are ISO-8601 with an offset. Every read takes the
date of view `known_at` (alias `as_of`; default the end of the tenant's data; Docs/KB/AS-OF.md).
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from kb import causal
from kb.api.deps import Conn, KnownAt, Principal, not_found, parse_time, view_time

router = APIRouter(prefix="/api/v1", tags=["causal"])


def _call(fn, conn, p, *args, **kwargs):
    """Run a causal function and map its errors to HTTP statuses."""
    try:
        return fn(conn, p, *args, **kwargs)
    except causal.NotFound:
        raise not_found() from None
    except causal.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except causal.Invalid as exc:
        raise HTTPException(422, str(exc)) from None


def _tenant_matches(p, tenant: str | None) -> None:
    if tenant and tenant != p.tenant_id:
        raise HTTPException(422, "body tenant differs from the request tenant (use ?tenant= or X-KB-Tenant)")


@router.get("/causal/why/{target:path}")
def why(conn: Conn, p: Principal, target: str, depth: Annotated[int, Query(ge=1, le=8)] = 4,
        include_inferred: bool = False, known_at: KnownAt = None) -> dict[str, Any]:
    """Backward trace to the first causes. `target` is an entity id or a version id `X@n`."""
    res = _call(causal.why, conn, p, target, depth=depth, include_inferred=include_inferred, known_at=known_at)
    if res is None:
        raise not_found()
    return res


@router.get("/causal/causes/{version_id:path}")
def causes(conn: Conn, p: Principal, version_id: str, include_inferred: bool = False,
           known_at: KnownAt = None) -> dict[str, Any]:
    res = _call(causal.causes, conn, p, version_id, include_inferred=include_inferred, known_at=known_at)
    if res is None:
        raise not_found()
    return res


@router.get("/causal/effects/{target:path}")
def effects(conn: Conn, p: Principal, target: str, depth: Annotated[int, Query(ge=1, le=8)] = 4,
            include_inferred: bool = False, status: Annotated[list[str] | None, Query()] = None,
            known_at: KnownAt = None) -> dict[str, Any]:
    res = _call(causal.effects, conn, p, target, depth=depth, include_inferred=include_inferred,
                status_filter=status, known_at=known_at)
    if res is None:
        raise not_found()
    return res


class ImpactRequest(BaseModel):
    entity_id: str
    fields: list[str] = Field(default_factory=lambda: ["*"], description='changed fields; ["*"] = any change')
    depth: int = Field(4, ge=1, le=8)
    include_inferred: bool = False
    tenant: str | None = None
    known_at: str | None = Field(None, description="date of view (default: the end of the tenant's data)")


@router.post("/causal/impact")
def impact(conn: Conn, p: Principal, req: ImpactRequest) -> dict[str, Any]:
    _tenant_matches(p, req.tenant)
    return _call(causal.impact, conn, p, req.entity_id, req.fields, depth=req.depth,
                 include_inferred=req.include_inferred,
                 known_at=view_time(conn, p.tenant_id, parse_time(req.known_at, "known_at")))


@router.get("/causal/stale")
def stale(conn: Conn, p: Principal, scope: str | None = None, kinds: Annotated[list[str] | None, Query()] = None,
          since: str | None = None, limit: Annotated[int, Query(ge=1, le=1000)] = 100,
          known_at: KnownAt = None) -> dict[str, Any]:
    """`scope` = entity id (its downstream incl. itself) or `kinds` = dependent kinds; as known at `known_at`."""
    if scope and kinds:
        raise HTTPException(422, "use scope or kinds, not both")
    return _call(causal.stale_dependencies, conn, p, scope=scope or kinds, since=parse_time(since, "since"),
                 limit=limit, known_at=known_at)


@router.get("/causal/stale/history")
def stale_history(conn: Conn, p: Principal, known_at: KnownAt = None) -> dict[str, Any]:
    """Every declared link that was STALE or BROKEN at some moment up to `known_at` (default: the end of the data),
    with the intervals of the knowledge axis in which it was: what `causal/stale` answers at any date T is the links
    whose interval contains T. For read models that evaluate many dates (the dashboard)."""
    return _call(causal.stale_history, conn, p, known_at=known_at)


@router.get("/causal/links/{link_id}")
def get_link(conn: Conn, p: Principal, link_id: str, known_at: KnownAt = None) -> dict[str, Any]:
    res = causal.get_link(conn, p, link_id, known_at=known_at)
    if res is None:
        raise not_found()
    return res


class DeclareRequest(BaseModel):
    effect_entity_id: str
    cause_entity_id: str
    predicate: str
    sensitivity: list[str] | None = None
    evidence: dict[str, Any] | str | None = None
    tenant: str | None = None


@router.post("/causal/links")
def declare(conn: Conn, p: Principal, req: DeclareRequest) -> dict[str, Any]:
    _tenant_matches(p, req.tenant)
    return _call(causal.declare, conn, p, req.effect_entity_id, req.cause_entity_id, req.predicate,
                 sensitivity=req.sensitivity, evidence=req.evidence)


class ReasonRequest(BaseModel):
    reason: str | None = None


@router.post("/causal/links/{link_id}/retract")
def retract(conn: Conn, p: Principal, link_id: str, req: ReasonRequest) -> dict[str, Any]:
    return _call(causal.retract, conn, p, link_id, req.reason or "")


@router.post("/causal/links/{link_id}/revalidate")
def revalidate(conn: Conn, p: Principal, link_id: str, req: ReasonRequest | None = None) -> dict[str, Any]:
    return _call(causal.revalidate, conn, p, link_id, (req.reason if req else None))
