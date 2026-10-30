"""Memgraph access (KB-4): driver singleton, schema, small query helpers, time conversion.

Memgraph is one shared database for all tenants: every node and edge carries `tenant` and every query filters
by it. The projection is disposable (KB-2) — it is rebuilt from the journal with `reset` + `drain`.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from neo4j import Driver, GraphDatabase, ManagedTransaction

from kb.settings import get_settings

log = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = timedelta(microseconds=1)

_driver: Driver | None = None
_lock = threading.Lock()
_schema_ready = False

# Label-property indexes (composite ones serve the MERGE keys), edge-property indexes for MERGE by journal id,
# and uniqueness of the node keys so concurrent projectors cannot duplicate a node.
SCHEMA = (
    "CREATE INDEX ON :Entity",
    "CREATE INDEX ON :Version",
    "CREATE INDEX ON :Entity(tenant)",
    "CREATE INDEX ON :Entity(id)",
    "CREATE INDEX ON :Entity(tenant, id)",
    "CREATE INDEX ON :Version(tenant)",
    "CREATE INDEX ON :Version(vid)",
    "CREATE INDEX ON :Version(tenant, vid)",
    "CREATE INDEX ON :Version(tenant, entity_id)",
    "CREATE EDGE INDEX ON :REL(stmt_id)",
    "CREATE EDGE INDEX ON :REL(type)",
    "CREATE EDGE INDEX ON :CAUSED_BY(link_id)",
    "CREATE EDGE INDEX ON :CAUSAL(link_id)",
    "CREATE CONSTRAINT ON (n:Entity) ASSERT n.tenant, n.id IS UNIQUE",
    "CREATE CONSTRAINT ON (n:Version) ASSERT n.tenant, n.vid IS UNIQUE",
)


class GraphDisabled(RuntimeError):
    """KB_GRAPH_ENABLED=false: the projection is switched off (authoritative answers stay in PostgreSQL)."""


def enabled() -> bool:
    return get_settings().graph_enabled


def driver() -> Driver:
    global _driver
    if not enabled():
        raise GraphDisabled("graph projection disabled (KB_GRAPH_ENABLED=false)")
    with _lock:
        if _driver is None:
            _driver = GraphDatabase.driver(get_settings().memgraph_uri, auth=None, connection_timeout=5.0,
                                           max_connection_pool_size=20)
        return _driver


def close() -> None:
    global _driver, _schema_ready
    with _lock:
        if _driver is not None:
            _driver.close()
        _driver = None
        _schema_ready = False


def ensure_schema(force: bool = False) -> bool:
    """Create indexes and constraints (idempotent in Memgraph). Returns False when the graph is disabled."""
    global _schema_ready
    if not enabled():
        return False
    if _schema_ready and not force:
        return True
    d = driver()
    with d.session() as s:  # index DDL must run in auto-commit transactions
        for stmt in SCHEMA:
            s.run(stmt).consume()
    _schema_ready = True
    return True


def available() -> bool:
    if not enabled():
        return False
    try:
        driver().verify_connectivity()
        return True
    except Exception:  # noqa: BLE001
        return False


def _collect(tx: ManagedTransaction, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [r.data() for r in tx.run(cypher, **params)]


_snapshot_tx: ContextVar[Any] = ContextVar("kb_graph_snapshot_tx", default=None)


@contextmanager
def snapshot() -> Iterator[None]:
    """Run every `read` inside the block in one read transaction: the queries of a service call see one
    consistent snapshot of the projection (Memgraph snapshot isolation) and share the round trips. Nests."""
    if _snapshot_tx.get() is not None:
        yield
        return
    ensure_schema()
    with driver().session() as s:
        tx = s.begin_transaction()
        token = _snapshot_tx.set(tx)
        try:
            yield
        finally:
            _snapshot_tx.reset(token)
            tx.close()  # read-only: nothing to commit


def read(cypher: str, **params: Any) -> list[dict[str, Any]]:
    tx = _snapshot_tx.get()
    if tx is not None:
        return [r.data() for r in tx.run(cypher, **params)]
    ensure_schema()
    with driver().session() as s:
        return [r.data() for r in s.run(cypher, **params)]  # auto-commit: one round trip


def write(cypher: str, **params: Any) -> list[dict[str, Any]]:
    ensure_schema()
    with driver().session() as s:
        return s.execute_write(_collect, cypher, params)


def write_tx(fn: Callable[[ManagedTransaction], Any]) -> Any:
    """Run several statements in one managed write transaction (retried on transient conflicts)."""
    ensure_schema()
    with driver().session() as s:
        return s.execute_write(fn)


# ---------------------------------------------------------------- time: integer epoch microseconds
def to_us(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.year >= 9999:  # 'infinity' upper bounds
        return None
    return (dt - _EPOCH) // _US


def from_us(us: int | None) -> datetime | None:
    if us is None:
        return None
    return _EPOCH + timedelta(microseconds=int(us))


def now_us() -> int:
    return to_us(datetime.now(timezone.utc))  # type: ignore[return-value]
