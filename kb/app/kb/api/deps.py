"""FastAPI dependencies: pooled connection, tenant and principal resolution.

Tenant comes from the `tenant` query parameter or the X-KB-Tenant header; the principal from X-KB-Principal
(until SSO, a trusted gateway sets it). Without the header the dev default principal is used; in prod a missing
principal is rejected.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Iterator

import psycopg
from fastapi import Depends, Header, HTTPException, Query

from kb.acl import PrincipalCtx, UnknownPrincipal, resolve_principal
from kb.db import connect
from kb.settings import get_settings

DEFAULT_TENANT = "company"


def get_conn() -> Iterator[psycopg.Connection]:
    with connect() as conn:
        yield conn


Conn = Annotated[psycopg.Connection, Depends(get_conn)]


def get_tenant(
    tenant: Annotated[str | None, Query(description="Tenant id")] = None,
    x_kb_tenant: Annotated[str | None, Header()] = None,
) -> str:
    return tenant or x_kb_tenant or DEFAULT_TENANT


Tenant = Annotated[str, Depends(get_tenant)]


def get_principal(
    conn: Conn,
    tenant_id: Tenant,
    x_kb_principal: Annotated[str | None, Header()] = None,
) -> PrincipalCtx:
    s = get_settings()
    principal_id = x_kb_principal or (s.default_principal if s.env != "prod" else None)
    if not principal_id:
        raise HTTPException(401, "X-KB-Principal header required")
    if conn.execute("SELECT 1 FROM tenant WHERE tenant_id = %s", (tenant_id,)).fetchone() is None:
        raise HTTPException(404, "unknown tenant")
    try:
        return resolve_principal(conn, tenant_id, principal_id)
    except UnknownPrincipal:
        raise HTTPException(403, "unknown principal") from None


Principal = Annotated[PrincipalCtx, Depends(get_principal)]


def parse_time(value: str | None, name: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name}: ISO-8601 datetime expected") from None
    if dt.tzinfo is None:
        raise HTTPException(422, f"{name}: timezone offset required")
    return dt


def view_time(conn: psycopg.Connection, tenant_id: str, value: datetime | None) -> datetime | None:
    """Check a date of view against the tenant's data (Docs/KB/AS-OF.md): None stays None (the end of the data); a
    date after the end of the data is a 422 with the bounds of the data."""
    from kb.asof import BeyondData, view_at

    try:
        return view_at(conn, tenant_id, value)
    except BeyondData as exc:
        raise HTTPException(422, exc.detail()) from None


def get_known_at(
    conn: Conn,
    tenant_id: Tenant,
    known_at: Annotated[str | None, Query(description="date of view (ISO-8601 with an offset): everything as known "
                                          "then, nothing recorded later; default the end of the tenant's data")] = None,
    as_of: Annotated[str | None, Query(description="alias of known_at")] = None,
) -> datetime | None:
    """The date of view of a read: `known_at` (or its alias `as_of`), the same semantics on every read endpoint."""
    k, a = parse_time(known_at, "known_at"), parse_time(as_of, "as_of")
    if k is not None and a is not None and k != a:
        raise HTTPException(422, "known_at and as_of differ (they are the same parameter)")
    return view_time(conn, tenant_id, k or a)


KnownAt = Annotated[datetime | None, Depends(get_known_at)]


def not_found(what: str = "not found") -> HTTPException:
    # unknown and inaccessible objects are indistinguishable (01 §9)
    return HTTPException(404, what)
