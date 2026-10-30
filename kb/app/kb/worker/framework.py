"""Outbox consumers (K07, KB-6).

Each consumer has its own checkpoint per tenant and reads outbox rows in (xid, id) order, only from
transactions below pg_snapshot_xmin: those are all finished, and every later transaction gets a larger xid,
so no row can appear behind the checkpoint. Projectors must be idempotent and state-based (re-read the
journal for the ids in the event), so redelivery after a crash is harmless.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import psycopg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboxEvent:
    id: int
    xid: str
    tenant_id: str
    tx_seq: int
    topic: str
    payload: dict[str, Any]


class Consumer(Protocol):
    name: str
    topics: frozenset[str]

    def handle(self, conn: psycopg.Connection, tenant_id: str, events: list[OutboxEvent]) -> None:
        """Process a batch (only events of the consumer's topics). Raise to retry the whole batch later."""
        ...

    def reset(self, conn: psycopg.Connection, tenant_id: str) -> None:
        """Drop the consumer's projection of the tenant (reindex)."""
        ...


CONSUMERS: dict[str, Consumer] = {}


def register_consumer(c: Consumer) -> Consumer:
    CONSUMERS[c.name] = c
    return c


PARTITIONED = ("search",)  # the consumers a parallel drain splits by entity (the slow ones)


def part_of(entity_id: str, parts: int) -> int:
    """The part (0..parts-1) of an entity: a hash stable across processes."""
    import zlib

    return zlib.crc32(entity_id.encode()) % parts


class Part:
    """Consumer `base` restricted to the entities of part k of n, with a checkpoint of its own (`base#k/n`).
    It reads every event of the stream and hands the base the part it owns; the base decides what of the rest
    it still needs (extraction searches late mentions of every new entity, re-processes only its own texts)."""

    def __init__(self, base: Consumer, k: int, n: int):
        self.base, self.k, self.n = base, k, n
        self.name = part_name(base.name, k, n)
        self.topics = base.topics
        self.batch = getattr(base, "batch", 200) * n  # as many own events per batch as the whole consumer has

    def owns(self, e: OutboxEvent) -> bool:
        return part_of(str((e.payload or {}).get("entity_id") or ""), self.n) == self.k

    def handle(self, conn: psycopg.Connection, tenant_id: str, events: list[OutboxEvent]) -> None:
        self.base.handle(conn, tenant_id, events, part=(self.k, self.n))  # type: ignore[call-arg]

    def reset(self, conn: psycopg.Connection, tenant_id: str) -> None:
        self.base.reset(conn, tenant_id)


def part_name(name: str, k: int, n: int) -> str:
    return f"{name}#{k}/{n}"


def _consumer(registry: dict[str, Consumer], name: str) -> Consumer | None:
    if "#" not in name:
        return registry.get(name)
    base, _, kn = name.partition("#")
    k, _, n = kn.partition("/")
    return Part(registry[base], int(k), int(n)) if base in registry and k.isdigit() and n.isdigit() else None


def split_checkpoints(conn: psycopg.Connection, tenant_id: str, name: str, parts: int) -> None:
    """Start the parts of a consumer where the consumer stands (nothing: from the start)."""
    fold_checkpoints(conn, tenant_id, name)
    with conn.transaction():
        for k in range(parts):
            conn.execute(
                """INSERT INTO consumer_checkpoint (consumer, tenant_id, last_xid, last_id, updated_at)
                   SELECT %s, tenant_id, last_xid, last_id, now() FROM consumer_checkpoint
                   WHERE consumer = %s AND tenant_id = %s""", (part_name(name, k, parts), name, tenant_id))


def fold_checkpoints(conn: psycopg.Connection, tenant_id: str, name: str) -> bool:
    """Fold the parts of a consumer back into it: the consumer stands where its slowest part stands (every part
    reads the whole stream, so all of them are at the same place after a complete drain). Leftovers of a drain
    that failed are folded the same way: the consumer then re-reads what its fastest parts already did, which a
    consumer may do (projections are idempotent). Returns whether there were parts."""
    with conn.transaction():
        rows = conn.execute(
            """SELECT last_xid, last_id FROM consumer_checkpoint WHERE tenant_id = %s AND consumer LIKE %s
               ORDER BY last_xid, last_id LIMIT 1""", (tenant_id, f"{name}#%")).fetchone()
        if rows is None:
            return False
        conn.execute(
            """INSERT INTO consumer_checkpoint (consumer, tenant_id, last_xid, last_id, updated_at)
               VALUES (%s, %s, %s, %s, now())
               ON CONFLICT (consumer, tenant_id) DO UPDATE SET last_xid = EXCLUDED.last_xid,
                 last_id = EXCLUDED.last_id, updated_at = now()""", (name, tenant_id, rows["last_xid"], rows["last_id"]))
        wm = conn.execute("SELECT min(tx_seq) AS s FROM projection_watermark WHERE tenant_id = %s AND projector LIKE %s",
                          (tenant_id, f"{name}#%")).fetchone()["s"]
        if wm is not None:
                conn.execute(
                """INSERT INTO projection_watermark (tenant_id, projector, tx_seq, updated_at) VALUES (%s, %s, %s, now())
                   ON CONFLICT (tenant_id, projector) DO UPDATE
                   SET tx_seq = GREATEST(projection_watermark.tx_seq, EXCLUDED.tx_seq), updated_at = now()""",
                (tenant_id, name, wm))
        conn.execute("DELETE FROM consumer_checkpoint WHERE tenant_id = %s AND consumer LIKE %s", (tenant_id, f"{name}#%"))
        conn.execute("DELETE FROM projection_watermark WHERE tenant_id = %s AND projector LIKE %s",
                     (tenant_id, f"{name}#%"))
    return True


def load_consumers() -> dict[str, Consumer]:
    """Import the modules that register consumers; a module not implemented yet is skipped."""
    from importlib import import_module

    for mod in ("kb.search.projector", "kb.search.descriptors", "kb.graph.projector"):
        try:
            import_module(mod)
        except ModuleNotFoundError as exc:
            if exc.name != mod:
                raise
    return CONSUMERS


def _fetch(conn: psycopg.Connection, consumer: str, tenant_id: str, batch: int) -> tuple[list[OutboxEvent], tuple]:
    cp = conn.execute(
        "SELECT last_xid::text AS last_xid, last_id FROM consumer_checkpoint WHERE consumer = %s AND tenant_id = %s",
        (consumer, tenant_id),
    ).fetchone()
    last_xid, last_id = (cp["last_xid"], cp["last_id"]) if cp else ("0", 0)
    # ORDER BY the table's xid8, not the text alias of the select list (an output name wins in ORDER BY): as text
    # '1000001' < '999999', and a consumer whose rows crossed a digit boundary skipped the shorter xids
    rows = conn.execute(
        """SELECT o.id, o.xid::text AS xid, o.tenant_id, o.tx_seq, o.topic, o.payload FROM outbox o
           WHERE o.tenant_id = %s AND (o.xid, o.id) > (%s::xid8, %s)
             AND o.xid < pg_snapshot_xmin(pg_current_snapshot())
           ORDER BY o.xid, o.id LIMIT %s""",
        (tenant_id, last_xid, last_id, batch),
    ).fetchall()
    if rows and len(rows) == batch:
        # a batch ends with a whole transaction: the events of one journal transaction share its moment, and a
        # consumer that derives state as of that moment (extract) must see all of them in one batch
        rows += conn.execute(
            """SELECT o.id, o.xid::text AS xid, o.tenant_id, o.tx_seq, o.topic, o.payload FROM outbox o
               WHERE o.tenant_id = %s AND o.xid = %s::xid8 AND o.id > %s ORDER BY o.id""",
            (tenant_id, rows[-1]["xid"], rows[-1]["id"]),
        ).fetchall()
    return [OutboxEvent(**r) for r in rows], (last_xid, last_id)


def drain(
    conn: psycopg.Connection,
    tenant_id: str,
    *,
    consumers: list[str] | None = None,
    batch: int | None = None,
    max_batches: int | None = None,
    raise_errors: bool = True,
) -> dict[str, int]:
    """Process everything pending for the tenant. Returns processed event counts per consumer (-1: failed;
    with raise_errors=False a failing consumer is skipped and retried on the next drain). `batch` overrides the
    consumer's own batch size (`Consumer.batch`, 200 when it has none)."""
    registry = load_consumers()
    names = consumers or sorted(registry)
    done: dict[str, int] = {}
    for name in names:
        c = _consumer(registry, name)
        if c is None:
            continue
        lock_key = f"{name}:{tenant_id}"
        # one drainer per (consumer, tenant) across processes: the worker and a CLI drain may run concurrently
        got = conn.execute("SELECT pg_try_advisory_lock(727100, hashtext(%s)) AS ok", (lock_key,)).fetchone()["ok"]
        if not got:
            done[name] = 0
            continue
        try:
            done[name] = _drain_one(conn, c, name, tenant_id, batch or getattr(c, "batch", 200), max_batches,
                                    raise_errors)
        finally:
            conn.execute("SELECT pg_advisory_unlock(727100, hashtext(%s))", (lock_key,))
    return done


def _drain_one(conn: psycopg.Connection, c: Consumer, name: str, tenant_id: str, batch: int,
               max_batches: int | None, raise_errors: bool) -> int:
    n = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        events, _ = _fetch(conn, name, tenant_id, batch)
        if not events:
            break
        relevant = [e for e in events if e.topic in c.topics]
        if relevant:
            try:
                c.handle(conn, tenant_id, relevant)
            except Exception:
                if raise_errors:
                    raise
                log.exception("consumer %s failed on tenant %s; will retry", name, tenant_id)
                n = -1
                break
        last = events[-1]
        with conn.transaction():
            conn.execute(
                """INSERT INTO consumer_checkpoint (consumer, tenant_id, last_xid, last_id, updated_at)
                   VALUES (%s, %s, %s::xid8, %s, now())
                   ON CONFLICT (consumer, tenant_id) DO UPDATE SET last_xid = EXCLUDED.last_xid,
                     last_id = EXCLUDED.last_id, updated_at = now()""",
                (name, tenant_id, last.xid, last.id),
            )
            conn.execute(
                """INSERT INTO projection_watermark (tenant_id, projector, tx_seq, updated_at) VALUES (%s, %s, %s, now())
                   ON CONFLICT (tenant_id, projector) DO UPDATE
                   SET tx_seq = GREATEST(projection_watermark.tx_seq, EXCLUDED.tx_seq), updated_at = now()""",
                (tenant_id, name, max(e.tx_seq for e in events)),
            )
        n += sum(1 for e in relevant if c.owns(e)) if isinstance(c, Part) else len(relevant)
        batches += 1
    return n


def reset(conn: psycopg.Connection, tenant_id: str, consumers: list[str] | None = None) -> None:
    registry = load_consumers()
    for name in consumers or sorted(registry):
        c = registry.get(name)
        if c is None:
            continue
        c.reset(conn, tenant_id)
        with conn.transaction():  # the consumer and the parts a stopped parallel drain left behind
            conn.execute("DELETE FROM consumer_checkpoint WHERE tenant_id = %s AND (consumer = %s OR consumer LIKE %s)",
                         (tenant_id, name, f"{name}#%"))
            conn.execute("DELETE FROM projection_watermark WHERE tenant_id = %s AND (projector = %s OR projector LIKE %s)",
                         (tenant_id, name, f"{name}#%"))


def watermarks(conn: psycopg.Connection, tenant_id: str) -> dict[str, int]:
    head = conn.execute("SELECT last_tx_seq FROM tenant_clock WHERE tenant_id = %s", (tenant_id,)).fetchone()
    rows = conn.execute(
        "SELECT projector, tx_seq FROM projection_watermark WHERE tenant_id = %s", (tenant_id,)
    ).fetchall()
    out = {"journal": head["last_tx_seq"] if head else 0}
    out.update({r["projector"]: r["tx_seq"] for r in rows})
    return out


def pending(conn: psycopg.Connection, tenant_id: str, consumers: list[str] | None = None) -> dict[str, int]:
    """Outbox rows behind each consumer's checkpoint (including rows of still-open transactions)."""
    names = consumers or sorted(load_consumers())
    out = {}
    for name in names:
        cp = conn.execute(
            "SELECT last_xid::text AS last_xid, last_id FROM consumer_checkpoint WHERE consumer = %s AND tenant_id = %s",
            (name, tenant_id),
        ).fetchone()
        last_xid, last_id = (cp["last_xid"], cp["last_id"]) if cp else ("0", 0)
        out[name] = conn.execute(
            "SELECT count(*) AS n FROM outbox WHERE tenant_id = %s AND (xid, id) > (%s::xid8, %s)",
            (tenant_id, last_xid, last_id),
        ).fetchone()["n"]
    return out


RETRIES = 5  # consecutive failures of one consumer a long drain survives (a network error of the embedding API)


def prepare_session(conn: psycopg.Connection) -> None:
    """A connection of a parallel drain (a bulk load):
    - no parallel query workers: the processes are the parallelism, and parallel workers of a dozen sessions at
      once exhaust the server's dynamic shared memory (/dev/shm: «could not resize shared memory»);
    - asynchronous commit: a journal transaction holds the tenant clock until its commit, so with a synchronous
      commit every part's write waited for the WAL flush of the one before (11 writes a second on a busy disk).
      Safe for the journal: the WAL is written in order, so a crash loses a tail of transactions together with
      the consumer checkpoints written after them, and that work is done again."""
    conn.execute("SET max_parallel_workers_per_gather = 0")
    conn.execute("SET synchronous_commit = off")


def _drain_retrying(c: psycopg.Connection, tenant_id: str, name: str, raise_errors: bool, failures: list[int]) -> int:
    """drain() of one consumer; a failure is retried after a pause (the checkpoint did not move), up to RETRIES."""
    import time

    try:
        n = drain(c, tenant_id, consumers=[name], raise_errors=True).get(name, 0)
        failures[0] = 0
        return n
    except Exception:
        failures[0] += 1
        if failures[0] > RETRIES or not raise_errors:
            raise
        log.warning("drain %s of %s failed (%d of %d), retrying", name, tenant_id, failures[0], RETRIES, exc_info=True)
        time.sleep(min(60, 2 ** failures[0]))
        return 0


def _drain_process(name: str, tenant_id: str, names: list[str], idx: int, idle: Any, done: Any, stop: Any,
                   errors: Any, raise_errors: bool) -> None:
    """One consumer of a parallel drain in its own process (spawned: a fresh interpreter and connection pool)."""
    import time

    from kb.db import connect

    failures = [0]
    try:
        with connect() as c:
            prepare_session(c)
            while not stop.is_set():
                n = _drain_retrying(c, tenant_id, name, raise_errors, failures)
                if n > 0:
                    with done.get_lock():
                        done[idx] += n
                    idle[idx] = 0
                    continue
                idle[idx] = 1
                if all(idle[:]) and not any(pending(c, tenant_id, names).values()):
                    stop.set()
                    return
                time.sleep(0.5)
    except BaseException as exc:  # a failing consumer stops the whole drain
        errors.put(f"{name}: {type(exc).__name__}: {exc}")
        stop.set()


def drain_processes(
    tenant_id: str,
    *,
    consumers: list[str] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    progress_every_s: float = 30.0,
    raise_errors: bool = True,
    parts: int = 1,
) -> dict[str, int]:
    """drain_parallel with a process per consumer: extraction and search are CPU-bound in Python, and threads of one
    process share one interpreter lock. Each process sets nothing up before it starts (spawn).

    parts > 1 splits search (PARTITIONED) into that many processes by entity: an entity's
    and projection read only the journal and the links of that entity (decisions on a pair, version binding and
    cycles look at the pair and at accepted, declared links), so the parts give the same result as one consumer.
    The parts start where the consumer stands and fold back into it at the end; meanwhile this process holds the
    consumer's drain lock, so that the shared worker keeps away. Counts are per consumer, each event once."""
    from kb.db import connect

    bases = consumers or sorted(load_consumers())
    split = [b for b in bases if parts > 1 and b in PARTITIONED]
    names = [x for b in bases for x in ([part_name(b, k, parts) for k in range(parts)] if b in split else [b])]
    base_of = {x: x.partition("#")[0] for x in names}
    with connect() as c:
        held = []
        for b in split:
            if not c.execute("SELECT pg_try_advisory_lock(727100, hashtext(%s)) AS ok", (f"{b}:{tenant_id}",)
                             ).fetchone()["ok"]:
                raise RuntimeError(f"{b} of {tenant_id} is being drained elsewhere")
            held.append(b)
            split_checkpoints(c, tenant_id, b, parts)
        try:
            out = _run_processes(tenant_id, names, progress, progress_every_s, raise_errors, base_of)
        finally:
            for b in held:
                fold_checkpoints(c, tenant_id, b)
                c.execute("SELECT pg_advisory_unlock(727100, hashtext(%s))", (f"{b}:{tenant_id}",))
    return out


def _sum_by_base(values: dict[str, int], base_of: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for k, v in values.items():
        out[base_of.get(k, k)] = out.get(base_of.get(k, k), 0) + v
    return out


def _run_processes(tenant_id: str, names: list[str], progress: Callable[[dict[str, Any]], None] | None,
                   progress_every_s: float, raise_errors: bool, base_of: dict[str, str]) -> dict[str, int]:
    import multiprocessing as mp
    import time

    from kb.db import connect

    ctx = mp.get_context("spawn")
    idle = ctx.Array("i", [0] * len(names))
    done = ctx.Array("q", [0] * len(names))
    stop, errors = ctx.Event(), ctx.Queue()
    procs = [ctx.Process(target=_drain_process, args=(n, tenant_id, names, i, idle, done, stop, errors, raise_errors),
                         name=f"drain-{n}", daemon=True) for i, n in enumerate(names)]
    t0 = time.monotonic()
    for p in procs:
        p.start()
    with connect() as c:
        while any(p.is_alive() for p in procs):
            for p in procs:
                p.join(timeout=progress_every_s / max(1, len(procs)))
            if progress and any(p.is_alive() for p in procs):
                pend = pending(c, tenant_id, names)  # a split consumer is as far as its slowest part
                agg: dict[str, int] = {}
                for k, v in pend.items():
                    agg[base_of[k]] = max(agg.get(base_of[k], 0), v)
                progress({"seconds": round(time.monotonic() - t0),
                          "done": _sum_by_base(dict(zip(names, done[:])), base_of), "pending": agg,
                          "watermarks": watermarks(c, tenant_id)})
    if not errors.empty():
        raise RuntimeError(errors.get())
    return _sum_by_base(dict(zip(names, done[:])), base_of)


def drain_parallel(
    tenant_id: str,
    *,
    consumers: list[str] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    progress_every_s: float = 30.0,
    raise_errors: bool = True,
) -> dict[str, int]:
    """Drain every consumer in its own thread and connection until nothing is pending for any of them (T07 A7).
    Extraction writes journal rows that graph, search and descriptors pick up while it still runs, so the wall
    time is that of the slowest consumer instead of the sum. No time limit; progress every `progress_every_s`.
    drain_processes does the same with a process per consumer (no shared interpreter lock)."""
    import threading
    import time

    from kb.db import connect

    names = consumers or sorted(load_consumers())
    done = {n: 0 for n in names}
    idle = {n: False for n in names}
    errors: list[BaseException] = []
    stop = threading.Event()
    t0 = time.monotonic()

    def loop(name: str) -> None:
        failures = [0]
        try:
            with connect() as c:
                while not stop.is_set():
                    n = _drain_retrying(c, tenant_id, name, raise_errors, failures)
                    if n > 0:
                        done[name] += n
                        idle[name] = False
                        continue
                    idle[name] = True
                    if all(idle.values()) and not any(pending(c, tenant_id, names).values()):
                        stop.set()
                        return
                    time.sleep(0.5)
        except BaseException as exc:  # a failing consumer stops the whole drain (raise_errors)
            errors.append(exc)
            stop.set()

    threads = [threading.Thread(target=loop, args=(n,), name=f"drain-{n}", daemon=True) for n in names]
    for th in threads:
        th.start()
    with connect() as c:
        while any(th.is_alive() for th in threads):
            for th in threads:
                th.join(timeout=progress_every_s / max(1, len(threads)))
            if progress and any(th.is_alive() for th in threads):
                progress({"seconds": round(time.monotonic() - t0), "done": dict(done),
                          "pending": pending(c, tenant_id, names), "watermarks": watermarks(c, tenant_id)})
    if errors:
        raise errors[0]
    return done


def drain_until_idle(
    conn: psycopg.Connection,
    tenant_id: str,
    *,
    consumers: list[str] | None = None,
    timeout_s: float | None = 600.0,
    raise_errors: bool = True,
) -> dict[str, int]:
    """Drain repeatedly until nothing is pending: rows of transactions that were open during a pass (they block
    pg_snapshot_xmin) and rows written by consumers themselves (extraction) are picked up by later passes."""
    import time

    deadline = time.monotonic() + timeout_s if timeout_s is not None else float("inf")
    total: dict[str, int] = {}
    while True:
        done = drain(conn, tenant_id, consumers=consumers, raise_errors=raise_errors)
        for k, v in done.items():
            total[k] = total.get(k, 0) + max(v, 0)
        left = pending(conn, tenant_id, consumers)
        if not any(left.values()) or time.monotonic() > deadline:
            if any(left.values()):
                log.warning("drain_until_idle timed out for %s: %s", tenant_id, left)
            return total
        if not any(v > 0 for v in done.values()):
            time.sleep(0.2)
