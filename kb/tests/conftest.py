"""Shared fixtures. Integration tests run against the infra from `make infra` (or inside the api container) and
isolate themselves with a fresh tenant id, so parallel runs and the seeded `vectorforge` tenant never collide."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
os.environ.setdefault("KB_ENV", "test")


def _pg_ok() -> bool:
    try:
        import psycopg

        from kb.settings import get_settings

        with psycopg.connect(get_settings().pg_dsn, connect_timeout=2):
            return True
    except Exception:
        return False


_PG = None


def pg_available() -> bool:
    global _PG
    if _PG is None:
        _PG = _pg_ok()
    return _PG


@pytest.fixture(scope="session")
def migrated():
    if not pg_available():
        pytest.skip("PostgreSQL not reachable (run `make infra`)")
    from kb.db import migrate

    migrate()
    return True


@pytest.fixture
def db(migrated):
    from kb.db import connect

    with connect() as conn:
        yield conn


@pytest.fixture
def tenant(db):
    """A fresh tenant bootstrapped from config (ontology, sources, principals, people)."""
    from kb.bootstrap import drop_tenant, ensure_tenant

    tid = f"t_{uuid.uuid4().hex[:10]}"
    ensure_tenant(db, tid, people_at=datetime.fromisoformat("2026-01-12T09:00:00+03:00"), background=False)
    yield tid
    if not os.environ.get("KB_KEEP_TEST_TENANTS"):
        drop_tenant(db, tid)
        try:  # a running worker may have projected this tenant too: projections are dropped as well
            from kb.worker.framework import reset

            reset(db, tid)
        except Exception:
            pass


def at(s: str) -> datetime:
    return datetime.fromisoformat(s)
