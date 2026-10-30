"""PostgreSQL access: a process-wide pool and the migration runner."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from kb.settings import get_settings

MIGRATIONS = Path(__file__).parent / "migrations"
_pool: ConnectionPool | None = None
_lock = threading.Lock()

__all__ = ["pool", "connect", "migrate", "Jsonb", "dict_row"]


def pool() -> ConnectionPool:
    global _pool
    with _lock:
        if _pool is None:
            s = get_settings()
            _pool = ConnectionPool(
                s.pg_dsn,
                min_size=1,
                max_size=s.pg_pool_max,
                kwargs={"row_factory": dict_row, "autocommit": True},
                open=True,
            )
        return _pool


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """A pooled autocommit connection; group writes with `with conn.transaction():`."""
    with pool().connection() as conn:
        yield conn


def migrate(dsn: str | None = None) -> list[str]:
    """Apply pending migrations in file order under an advisory lock. Returns the applied versions."""
    applied: list[str] = []
    with psycopg.connect(dsn or get_settings().pg_dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(727001)")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migration (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migration").fetchall()}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.stem in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migration (version) VALUES (%s)", (path.stem,))
                applied.append(path.stem)
        finally:
            conn.execute("SELECT pg_advisory_unlock(727001)")
    return applied
