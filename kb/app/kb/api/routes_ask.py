"""POST /api/v1/ask — a manager's question answered with versions, both times and link origins."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from kb.api.deps import Conn, get_principal, get_tenant, not_found, parse_time, view_time
from kb.reasoning.ask import ask

router = APIRouter(prefix="/api/v1", tags=["ask"])


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    tenant: str | None = None
    known_at: str | None = Field(None, description="answer as known at this time (the date of view; default: the end "
                                                   "of the tenant's data)")
    now: str | None = Field(None, description="reference time for relative expressions (default: known_at, else the "
                                              "end of the tenant's data — never the wall clock)")
    mode: Literal["auto", "deterministic", "agent"] = "auto"


@router.post("/ask")
def post_ask(conn: Conn, body: AskRequest, tenant: str | None = None,
             x_kb_tenant: Annotated[str | None, Header()] = None,
             x_kb_principal: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    tid = body.tenant or get_tenant(tenant, x_kb_tenant)
    p = get_principal(conn, tid, x_kb_principal)
    return ask(conn, p, body.question, now=parse_time(body.now, "now"),
               known_at=view_time(conn, tid, parse_time(body.known_at, "known_at")), mode=body.mode)


@router.get("/ask/runs/{run_id}")
def get_run(conn: Conn, run_id: str, tenant: str | None = None, x_kb_tenant: Annotated[str | None, Header()] = None,
            x_kb_principal: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    tid = get_tenant(tenant, x_kb_tenant)
    p = get_principal(conn, tid, x_kb_principal)
    row = conn.execute("SELECT * FROM investigation_run WHERE tenant_id = %s AND run_id = %s", (tid, run_id)).fetchone()
    if row is None or (row["principal"] != p.principal_id and not p.is_admin):
        raise not_found()
    return row
