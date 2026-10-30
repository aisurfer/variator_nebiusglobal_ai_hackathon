"""Ingest pipeline (K04, flow F1): inbox -> dedupe -> raw blob -> adapter -> one journal transaction.

The same path serves a real webhook/delta connector and the simulated replay. A delivery is acknowledged only
after the inbox row is durable; a repeated delivery_id is a no-op; unchanged content creates no version.
"""
from __future__ import annotations

import logging
from datetime import datetime
from functools import lru_cache

import orjson
import psycopg

from kb.connectors.base import AdapterContext, ContainerConfig, SourceConfig, TenantSources, get_adapter
from kb.contracts import IngestResult, SourceEnvelope
from kb.ingest.blobstore import BlobStore
from kb.journal.writer import journal_tx
from kb.settings import get_settings
from kb.text import canonical_json, sha256_hex

log = logging.getLogger(__name__)


class UnknownSource(Exception):
    pass


def tenant_sources(conn: psycopg.Connection, tenant_id: str) -> TenantSources:
    rows = conn.execute(
        "SELECT source_id, provider, display_name, base_url, config FROM source_instance WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchall()
    trow = conn.execute("SELECT name, settings FROM tenant WHERE tenant_id = %s", (tenant_id,)).fetchone()
    sources = {}
    for r in rows:
        cfg = r["config"] or {}
        containers = {
            k: ContainerConfig(k, (v or {}).get("name", k), tuple((v or {}).get("acl") or []))
            for k, v in (cfg.get("containers") or {}).items()
        }
        raw = {"id": r["source_id"], "provider": r["provider"], "name": r["display_name"], "base_url": r["base_url"], **cfg}
        sources[r["source_id"]] = SourceConfig(r["source_id"], r["provider"], r["display_name"], r["base_url"], containers, raw)
    tz = ((trow or {}).get("settings") or {}).get("timezone", "UTC")
    return TenantSources(tenant_id, (trow or {}).get("name", tenant_id), tz, sources)


@lru_cache(maxsize=1)
def _blobs() -> BlobStore:
    return BlobStore()


def ingest(conn: psycopg.Connection, env: SourceEnvelope, *, sources: TenantSources | None = None) -> IngestResult:
    settings = get_settings()
    sources = sources or tenant_sources(conn, env.tenant_id)
    src = sources.sources.get(env.source_id)
    if src is None:
        raise UnknownSource(f"{env.tenant_id}/{env.source_id}")
    observed_at = env.observed_at if (env.observed_at and settings.allow_simulated_time) else None

    with conn.transaction():
        row = conn.execute(
            """INSERT INTO ingest_event (tenant_id, source_id, delivery_id, operation, object_type, external_id, observed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, source_id, delivery_id) DO UPDATE SET status = ingest_event.status
               RETURNING id, status, (xmax = 0) AS inserted""",
            (env.tenant_id, env.source_id, env.delivery_id, env.operation, env.object_type, env.external_id, observed_at),
        ).fetchone()
    event_id = row["id"]
    if not row["inserted"] and row["status"] != "failed":
        return IngestResult(delivery_id=env.delivery_id, status="duplicate")

    def fail(msg: str) -> IngestResult:
        conn.execute("UPDATE ingest_event SET status = 'failed', error = %s WHERE id = %s", (msg[:2000], event_id))
        return IngestResult(delivery_id=env.delivery_id, status="failed", error=msg)

    raw = canonical_json(env.payload) if env.payload is not None else b""
    blob_key = _blobs().put(raw) if env.payload is not None else None
    ctx = AdapterContext(env.tenant_id, src, sources)
    default_acl = env.acl if env.acl is not None else ctx.container_acl(env.container)
    try:
        if env.operation == "acl":
            records = []
        else:
            records = get_adapter(src.provider).normalize(env, ctx)
    except Exception as exc:  # adapter errors quarantine the delivery, other deliveries continue
        log.exception("adapter failed for %s", env.delivery_id)
        return fail(f"adapter: {exc!r}")

    try:
        with journal_tx(conn, env.tenant_id, "ingest", actor_id=f"connector:{src.id}", at=observed_at,
                        ref={"ingest_event_id": event_id, "delivery_id": env.delivery_id}) as j:
            rev = conn.execute(
                """INSERT INTO source_revision (tenant_id, source_id, object_type, external_id, provider_version,
                     content_hash, blob_key, source_modified_at, observed_at, acl)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (env.tenant_id, env.source_id, env.object_type, env.external_id, env.provider_version,
                 sha256_hex(raw), blob_key, env.source_modified_at, j.txc.tx_at, default_acl),
            ).fetchone()
            if env.operation == "acl":
                entity_id = j.resolve_ref_external(env.source_id, env.object_type, env.external_id)
                result_entities: list[str] = []
                empty: dict[str, int] = {}
                if entity_id:
                    for eid in [entity_id, *j.children_of(entity_id)]:
                        j.set_acl(eid, default_acl)
                        result_entities.append(eid)
                status = "processed" if result_entities else "unchanged"
                versions: list[str] = []
            else:
                res = j.apply_records(records, origin=f"declared:{src.provider}_import", source_revision_id=rev["id"],
                                      default_acl=default_acl,
                                      source_modified_at=env.source_modified_at or observed_at)
                for w in res.warnings:
                    log.warning("%s: %s", env.delivery_id, w)
                result_entities, versions = res.entities or res.unchanged, res.versions
                status = "processed" if res.versions else "unchanged"
                empty = res.empty
            conn.execute(
                "UPDATE ingest_event SET status = %s, source_revision_id = %s, tx_seq = %s, error = NULL WHERE id = %s",
                (status, rev["id"], j.txc.tx_seq, event_id),
            )
            tx_seq = j.txc.tx_seq
    except Exception as exc:
        log.exception("journal write failed for %s", env.delivery_id)
        return fail(f"journal: {exc!r}")
    return IngestResult(delivery_id=env.delivery_id, status=status, tx_seq=tx_seq, entities=result_entities,
                        versions=versions, empty=empty)


def envelope_from_json(data: bytes | str) -> SourceEnvelope:
    return SourceEnvelope.model_validate(orjson.loads(data))


def parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
