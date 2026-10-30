"""Run statistics in SQLite: one row per stage (tool call, agent run) and per LLM call.

A *stage* is one MCP tool call or one agent run. LLM calls made inside a stage are
linked to it through the ``CURRENT`` context variable, which also reaches the worker
threads of sync tools. ``session`` groups the stages of one client session (one run).

Tables (query them with SQL for spending analysis, or use ``summary``):

* ``stages``: ts, session, entry, tool, plugin, kind (local|llm), ok, error, duration_s,
  args_chars, reply_chars, case_id, run_id, peak_mem_mb, llm_calls, prompt_tokens,
  cached_tokens, completion_tokens, cost_usd
* ``llm_calls``: ts, stage_id, session, model, prompt_tokens, cached_tokens,
  completion_tokens, cost_usd, duration_s
"""

from __future__ import annotations

import contextvars
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from variator.llm.usage import Usage

ENV_STATS = "VARIATOR_STATS"
DEFAULT_STATS_PATH = Path(".variator") / "stats.sqlite"
GROUPS = {"tool", "plugin", "kind", "session", "case_id", "entry", "day"}


@dataclass(frozen=True)
class StageRef:
    stage_id: int
    session: str


CURRENT: contextvars.ContextVar[StageRef | None] = contextvars.ContextVar("variator_stage", default=None)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, session TEXT, entry TEXT, tool TEXT, plugin TEXT,
  kind TEXT DEFAULT 'local', ok INTEGER, error TEXT, duration_s REAL, args_chars INTEGER, reply_chars INTEGER,
  case_id TEXT, run_id TEXT, peak_mem_mb REAL, llm_calls INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0,
  cached_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, stage_id INTEGER, session TEXT, model TEXT,
  prompt_tokens INTEGER, cached_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL, duration_s REAL
);
CREATE INDEX IF NOT EXISTS stages_session ON stages(session);
CREATE INDEX IF NOT EXISTS llm_stage ON llm_calls(stage_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class StatsStore:
    """Thread-safe writer and reader. ``path=None`` keeps the database in memory (tests)."""

    def __init__(self, path: str | Path | None = None):
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(":memory:" if path is None else str(path), check_same_thread=False)
        self.db.executescript(_SCHEMA)
        self.lock = threading.Lock()

    def _exec(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.lock:
            cursor = self.db.execute(sql, args)
            self.db.commit()
            return cursor

    def query(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.lock:
            cursor = self.db.execute(sql, args)
            names = [c[0] for c in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    # --- writes ------------------------------------------------------------------------
    def begin_stage(
        self, *, session: str, entry: str, tool: str, plugin: str | None, args_chars: int = 0
    ) -> int:
        cursor = self._exec(
            "INSERT INTO stages (ts, session, entry, tool, plugin, args_chars) VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), session, entry, tool, plugin, args_chars),
        )
        return int(cursor.lastrowid)

    def end_stage(
        self,
        stage_id: int,
        *,
        ok: bool,
        duration_s: float,
        reply_chars: int = 0,
        error: str | None = None,
        case_id: str | None = None,
        run_id: str | None = None,
        peak_mem_mb: float | None = None,
    ) -> None:
        self._exec(
            "UPDATE stages SET ok = ?, error = ?, duration_s = ?, reply_chars = ?, case_id = ?, run_id = ?, "
            "peak_mem_mb = ?, kind = CASE WHEN llm_calls > 0 THEN 'llm' ELSE 'local' END WHERE id = ?",
            (int(ok), error, duration_s, reply_chars, case_id, run_id, peak_mem_mb, stage_id),
        )

    def record_llm(self, model: str, usage: Usage, cost: float | None, duration_s: float | None) -> None:
        """Ledger listener: one row per LLM call, plus totals on the current stage."""
        ref = CURRENT.get()
        stage_id, session = (ref.stage_id, ref.session) if ref else (None, None)
        self._exec(
            "INSERT INTO llm_calls (ts, stage_id, session, model, prompt_tokens, cached_tokens,"
            " completion_tokens, cost_usd, duration_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(),
                stage_id,
                session,
                model,
                usage.prompt_tokens,
                usage.cached_tokens,
                usage.completion_tokens,
                cost,
                duration_s,
            ),
        )
        if stage_id is not None:
            self._exec(
                "UPDATE stages SET llm_calls = llm_calls + 1, prompt_tokens = prompt_tokens + ?, "
                "cached_tokens = cached_tokens + ?, completion_tokens = completion_tokens + ?, "
                "cost_usd = cost_usd + ? WHERE id = ?",
                (usage.prompt_tokens, usage.cached_tokens, usage.completion_tokens, cost or 0.0, stage_id),
            )

    def attach(self, ledger: Any) -> None:
        """Add ``record_llm`` to a ``UsageLedger`` (one time per ledger)."""
        if self.record_llm not in ledger.listeners:
            ledger.listeners.append(self.record_llm)

    # --- reads -------------------------------------------------------------------------
    def summary(
        self, group_by: str = "tool", *, since: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Totals per group: stages, errors, time, reply size, tokens, cost (descending cost, then time)."""
        if group_by not in GROUPS:
            raise ValueError(f"group_by must be one of {sorted(GROUPS)}")
        key = "substr(ts, 1, 10)" if group_by == "day" else group_by
        where, args = ("WHERE ts >= ?", (since,)) if since else ("", ())
        sql = (
            f"SELECT {key} AS {group_by}, COUNT(*) AS stages, "
            "SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors, "  # open stages (ok NULL) do not count
            "ROUND(SUM(duration_s), 3) AS seconds, ROUND(AVG(duration_s), 3) AS avg_seconds, "
            "SUM(reply_chars) AS reply_chars, SUM(llm_calls) AS llm_calls, "
            "SUM(prompt_tokens) AS prompt_tokens, SUM(cached_tokens) AS cached_tokens, "
            "SUM(completion_tokens) AS completion_tokens, "
            f"ROUND(SUM(cost_usd), 6) AS cost_usd FROM stages {where} GROUP BY {key} "
            "ORDER BY cost_usd DESC, seconds DESC LIMIT ?"
        )
        return self.query(sql, (*args, limit))

    def by_model(self, *, since: str | None = None) -> list[dict[str, Any]]:
        where, args = ("WHERE ts >= ?", (since,)) if since else ("", ())
        return self.query(
            "SELECT model, COUNT(*) AS calls, SUM(prompt_tokens) AS prompt_tokens,"
            " SUM(cached_tokens) AS cached_tokens, SUM(completion_tokens) AS completion_tokens,"
            " ROUND(SUM(cost_usd), 6) AS cost_usd, ROUND(AVG(duration_s), 3) AS avg_seconds"
            f" FROM llm_calls {where} GROUP BY model ORDER BY cost_usd DESC",
            args,
        )
