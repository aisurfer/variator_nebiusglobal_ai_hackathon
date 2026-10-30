"""Handle ownership for a multi-user server (HTTP).

``OwnerMiddleware`` sets ``OWNER`` for each tool call. The ``Store`` writes the owner of
each new handle and refuses a handle of another owner as "unknown" (it does not tell
that the handle exists). Owner, in this order:

1. ``client:<id>`` from the OAuth access token (when the server has auth);
2. ``ws:<name>`` from the HTTP header ``X-Variator-Workspace`` (stable across reconnects);
3. ``session:<id>`` from the MCP session id (a reconnect gives a new owner).

Without the middleware (stdio, tests, the in-process agent) ``OWNER`` is None and the
store does no checks. ``variator-mcp --http`` turns it on; ``VARIATOR_ISOLATE=0`` turns it off.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastmcp.server.middleware import Middleware

OWNER: ContextVar[str | None] = ContextVar("variator_owner", default=None)
HEADER = "x-variator-workspace"
ENV_ISOLATE = "VARIATOR_ISOLATE"


def owner_of(context: Any) -> str | None:
    from fastmcp.server.dependencies import get_access_token, get_http_headers

    try:
        token = get_access_token()
    except Exception:  # no auth configured
        token = None
    if token is not None and getattr(token, "client_id", None):
        return f"client:{token.client_id}"
    try:
        workspace = (get_http_headers(include={HEADER}) or {}).get(HEADER, "").strip()
    except Exception:  # not an HTTP request
        workspace = ""
    if workspace:
        return f"ws:{workspace}"
    ctx = getattr(context, "fastmcp_context", None)
    try:
        session = ctx.session_id if ctx is not None else None
    except Exception:  # no request context
        session = None
    return f"session:{session}" if session else None


class OwnerMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        token = OWNER.set(owner_of(context) or "anonymous")
        try:
            return await call_next(context)
        finally:
            OWNER.reset(token)
