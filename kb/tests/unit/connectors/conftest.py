"""Helpers for the adapter unit tests: no database; payloads are built inline by the tests.

The sources and people come from the sample tenant config in config/ (an English demo organisation). Plancy's
fixtures/vectorforge payload feed was not copied (it mixes Russian and English), so the tests that replayed it are
gone; the tests here build their payloads inline."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from kb.connectors.base import AdapterContext, TenantSources, get_adapter, load_sources
from kb.contracts import CanonicalRecord, Ref, SourceEnvelope

CONFIG = Path(__file__).resolve().parents[3] / "config"


@lru_cache
def sources() -> TenantSources:
    return load_sources(CONFIG / "sources.yaml")


@lru_cache
def people() -> list[dict]:
    return yaml.safe_load((CONFIG / "people.yaml").read_text(encoding="utf-8"))["people"]


def account(source_id: str, first_name: str) -> str:
    """Account id of a person in a source: account("jira-vf", "Sofia") -> "712020:sofia.lind"."""
    for p in people():
        if p["name"].split()[0] == first_name:
            return str(p["accounts"][source_id])
    raise KeyError(first_name)


def ctx(source_id: str) -> AdapterContext:
    ts = sources()
    return AdapterContext(ts.tenant_id, ts.sources[source_id], ts)


def normalize(env: SourceEnvelope) -> list[CanonicalRecord]:
    src = sources().sources[env.source_id]
    return get_adapter(src.provider).normalize(env, ctx(env.source_id))


def causes(rec: CanonicalRecord) -> set[tuple[str, str]]:
    return {(c.predicate, c.cause.external_key()) for c in rec.causes}


def statements(rec: CanonicalRecord, predicate: str) -> list[Any]:
    return [s.object.external_key() if s.object else s.literal for s in rec.statements if s.predicate == predicate]


def ext(source_id: str, object_type: str, external_id: str) -> str:
    return Ref(source_id=source_id, object_type=object_type, external_id=external_id).external_key() or ""


def make_env(source_id: str, object_type: str, external_id: str, payload: dict | None, *, container: str | None = None,
             op: str = "upsert") -> SourceEnvelope:
    return SourceEnvelope(tenant_id="company", source_id=source_id, delivery_id=f"t:{external_id}", operation=op,
                          object_type=object_type, external_id=external_id, container=container, payload=payload)
