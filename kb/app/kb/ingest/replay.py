"""Simulated connectors: replay a fixture timeline through the real ingest path (K04).

Two timeline formats in fixtures/<tenant>/ (the compressed one wins when both exist):
  timeline.jsonl.gz  one JSON event per line, payload inline ({"at", "source", "object_type", "external_id",
                     "container", "op", "modified_at", "acl", "delivery_id"?, "version"?, "payload"}); streamed,
                     so a generated history of tens of thousands of events never sits in memory at once;
  timeline.yaml      {"events": [...]} with the payload in a separate file ("file", relative to the directory).

Each event becomes a SourceEnvelope whose observed_at is the event time, so the transaction-time axis of the
journal follows the simulated history (what was known on 30 March differs from what is known now).
"""
from __future__ import annotations

import gzip
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import orjson
import psycopg
import yaml

from kb.contracts import SourceEnvelope
from kb.ingest.pipeline import ingest, parse_dt, tenant_sources
from kb.settings import get_settings
from kb.text import sha256_hex

log = logging.getLogger(__name__)

JSONL_TIMELINE = "timeline.jsonl.gz"
YAML_TIMELINE = "timeline.yaml"
PROGRESS_EVERY = 500


def timeline_path(tenant_dir: Path) -> Path:
    gz = tenant_dir / JSONL_TIMELINE
    return gz if gz.is_file() else tenant_dir / YAML_TIMELINE


def iter_timeline(tenant_dir: Path) -> Iterator[dict[str, Any]]:
    """Events in file order: streamed line by line from timeline.jsonl.gz, else read from timeline.yaml."""
    path = timeline_path(tenant_dir)
    if path.name == JSONL_TIMELINE:
        with gzip.open(path, "rb") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield orjson.loads(line)
                except orjson.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{n}: invalid JSON event: {exc}") from exc
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    yield from data.get("events") or []


def load_timeline(tenant_dir: Path) -> list[dict[str, Any]]:
    return list(iter_timeline(tenant_dir))


def event_envelope(tenant_id: str, tenant_dir: Path, ev: dict[str, Any]) -> SourceEnvelope:
    op = ev.get("op", "upsert")
    payload = None
    raw = b"delete"
    if ev.get("payload") is not None:  # inline (timeline.jsonl.gz)
        payload = ev["payload"]
        raw = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    elif ev.get("file"):
        raw = (tenant_dir / ev["file"]).read_bytes()
        payload = orjson.loads(raw)
    at = parse_dt(ev["at"])
    return SourceEnvelope(
        tenant_id=tenant_id,
        source_id=ev["source"],
        delivery_id=ev.get("delivery_id")
        or f"replay:{ev['source']}:{ev['object_type']}:{ev['external_id']}:{op}:{sha256_hex(raw)[:16]}",
        operation=op,
        object_type=ev["object_type"],
        external_id=str(ev["external_id"]),
        container=ev.get("container"),
        provider_version=str(ev["version"]) if ev.get("version") is not None else None,
        source_modified_at=parse_dt(ev.get("modified_at")) or at,
        observed_at=at,
        payload=payload,
        acl=ev.get("acl"),
    )


def replay(
    conn: psycopg.Connection,
    tenant_id: str,
    *,
    fixtures: str | None = None,
    until: datetime | None = None,
    on_event: Any = None,
    on_progress: Callable[[int, dict[str, int]], None] | None = None,
    progress_every: int = PROGRESS_EVERY,
    fixtures_dir: Path | None = None,
) -> dict[str, Any]:
    """Replay the fixture timeline (optionally only events with at <= until). Idempotent: deliveries dedupe.

    on_progress(n_events, counts_by_status) is called every `progress_every` events and once at the end.
    """
    tenant_dir = (fixtures_dir or get_settings().fixtures_dir) / (fixtures or tenant_id)
    sources = tenant_sources(conn, tenant_id)
    counts: Counter[str] = Counter()
    empty: Counter[str] = Counter()
    failures: list[dict[str, str]] = []
    n = 0
    for ev in iter_timeline(tenant_dir):
        if until and parse_dt(ev["at"]) > until:
            break
        env = event_envelope(tenant_id, tenant_dir, ev)
        res = ingest(conn, env, sources=sources)
        counts[res.status] += 1
        empty.update(res.empty)
        n += 1
        if res.status == "failed":
            failures.append({"delivery_id": res.delivery_id, "error": res.error or ""})
        if on_event:
            on_event(ev, res)
        if on_progress and progress_every and n % progress_every == 0:
            on_progress(n, dict(counts))
        elif progress_every and n % progress_every == 0:
            log.info("replay %s: %d events %s", tenant_id, n, dict(counts))
    if on_progress and (not progress_every or n % progress_every):
        on_progress(n, dict(counts))
    return {"events": sum(counts.values()), **counts, "failures": failures, "empty_values": dict(sorted(empty.items()))}
