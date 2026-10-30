"""Platform quality runs in SQLite: one row per measured value, for trends and the quality slide.

Table ``quality_runs`` (in ``.variator/stats.sqlite``, next to the run statistics): run_id, ts, git_sha, test,
case_id, metric, seed, n, value, ci_low, ci_high, target, passed, ms, note. ``scripts/quality_run.py`` writes the
rows; ``scripts/quality_report.py`` aggregates them.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from variator.telemetry.store import DEFAULT_STATS_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS quality_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, ts TEXT NOT NULL, git_sha TEXT, test TEXT NOT NULL, case_id TEXT, metric TEXT NOT NULL,
  seed INTEGER, n INTEGER, value REAL, ci_low REAL, ci_high REAL, target REAL, passed INTEGER, ms REAL, note TEXT
);
CREATE INDEX IF NOT EXISTS quality_runs_test ON quality_runs (test, metric, ts);
"""
COLUMNS = ("run_id", "ts", "git_sha", "test", "case_id", "metric", "seed", "n", "value", "ci_low", "ci_high",
           "target", "passed", "ms", "note")


class QualityStore:
    def __init__(self, path: str | Path = DEFAULT_STATS_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def add(self, row: dict[str, Any]) -> None:
        row = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), **row}
        if isinstance(row.get("passed"), bool):
            row["passed"] = int(row["passed"])
        cols = [c for c in COLUMNS if c in row]
        with self._db() as db:
            db.execute(f"INSERT INTO quality_runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                       [row[c] for c in cols])

    def rows(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._db() as db:
            if run_id is None:
                run_id = (db.execute("SELECT run_id FROM quality_runs ORDER BY id DESC LIMIT 1").fetchone() or [None])[0]
            return [dict(r) for r in db.execute("SELECT * FROM quality_runs WHERE run_id = ? ORDER BY id", (run_id,))]

    def history(self, test: str, metric: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT run_id, ts, git_sha, case_id, value FROM quality_runs WHERE test = ? AND metric = ? "
                "ORDER BY id DESC LIMIT ?", (test, metric, limit))]
