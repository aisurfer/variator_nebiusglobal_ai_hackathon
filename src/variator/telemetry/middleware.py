"""FastMCP middleware: one ``stages`` row per tool call (time, sizes, handles, LLM totals).

Optional memory: set ``VARIATOR_STATS_MEMORY=1`` to record the tracemalloc peak per stage.
tracemalloc slows Python down and counts all threads, so it is off by default.
"""

from __future__ import annotations

import json
import os
import time
import tracemalloc
import uuid
from collections.abc import Iterable
from typing import Any

from fastmcp.server.middleware import Middleware

from variator.telemetry.store import CURRENT, StageRef, StatsStore


def _reply_text(result: Any) -> str:
    parts = getattr(result, "content", None) or []
    return "".join(getattr(p, "text", "") or "" for p in parts)


def _handles(args: dict[str, Any], text: str) -> tuple[str | None, str | None]:
    case_id, run_id = args.get("case"), args.get("run")
    if (case_id is None or run_id is None) and text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        if isinstance(data, dict):
            case_id = case_id or (data.get("case") if isinstance(data.get("case"), str) else None)
            run_id = run_id or (data.get("run") if isinstance(data.get("run"), str) else None)
    return case_id, run_id


class StatsMiddleware(Middleware):
    def __init__(self, store: StatsStore, plugins: Iterable[str] = (), *, entry: str = "mcp"):
        self.store = store
        self.plugins = sorted(plugins, key=len, reverse=True)
        self.entry = entry
        self.memory = os.environ.get("VARIATOR_STATS_MEMORY", "").lower() in ("1", "true", "yes")
        self._fallback_session = uuid.uuid4().hex[:12]

    def _plugin(self, tool: str) -> str:
        return next((p for p in self.plugins if tool.startswith(p + "_")), "core")

    def _session(self, context: Any) -> str:
        ctx = getattr(context, "fastmcp_context", None)
        try:
            session = ctx.session_id if ctx is not None else None
        except Exception:  # no request context (for example in-memory tests)
            session = None
        return session or self._fallback_session

    async def on_call_tool(self, context, call_next):
        tool = context.message.name
        args = context.message.arguments or {}
        session = self._session(context)
        parent = CURRENT.get()
        stage = self.store.begin_stage(
            session=parent.session if parent else session,
            entry=self.entry,
            tool=tool,
            plugin=self._plugin(tool),
            args_chars=len(json.dumps(args, default=str)),
        )
        token = CURRENT.set(StageRef(stage, parent.session if parent else session))
        if self.memory:
            tracemalloc.start() if not tracemalloc.is_tracing() else tracemalloc.reset_peak()
        started = time.perf_counter()
        ok, error, text = True, None, ""
        try:
            result = await call_next(context)
            text = _reply_text(result)
            ok = not getattr(result, "is_error", False)
            return result
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            peak = (
                tracemalloc.get_traced_memory()[1] / 2**20
                if self.memory and tracemalloc.is_tracing()
                else None
            )
            case_id, run_id = _handles(args, text)
            self.store.end_stage(
                stage,
                ok=ok,
                duration_s=time.perf_counter() - started,
                reply_chars=len(text),
                error=error,
                case_id=case_id,
                run_id=run_id,
                peak_mem_mb=round(peak, 2) if peak is not None else None,
            )
            CURRENT.reset(token)
