"""Adapter contract (K05): a provider adapter turns one SourceEnvelope into CanonicalRecords.

Adapters are pure: no database, no network. Everything they need about the tenant's sources comes from
AdapterContext (sources.yaml). Cross-source references (a Jira remote link to a Confluence page, "Closes FP-105"
in a GitLab MR) are expressed as external Refs; the journal resolves them to entities, or parks them in
unresolved_ref until the target arrives.

Entity id conventions (stable, readable; the first mapping is kept forever):
  jira.<KEY>                                  issue            e.g. jira.FP-101
  confluence.page-<id>                        page
  confluence.page-<id>.dec-<n>                decision         (derived from a page's decision items)
  confluence.page-<id>.task-<n>               task             (derived action items)
  gitlab.<project-slug>.mr-<iid>              merge_request    project-slug = path with "/" -> "-"
  gitlab.<project-slug>.commit-<short_sha>    commit
  gitlab.<project-slug>.release-<tag>         release
  gitlab.<project-slug>.issue-<iid>           issue
  person.<employee-id-lowercase>              person (directory)
  sim.<entity_id>                             simulated business record (contract, invoice, ...; kb.connectors.sim)
Account references use object_type "account" with the provider account id as external_id; bootstrap maps
confirmed accounts (people.yaml) to person entities.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from kb.contracts import CanonicalRecord, Ref, SourceEnvelope
from kb.settings import get_settings


@dataclass(frozen=True)
class ContainerConfig:
    key: str
    name: str
    acl: tuple[str, ...]


@dataclass(frozen=True)
class SourceConfig:
    id: str
    provider: str
    name: str
    base_url: str | None
    containers: dict[str, ContainerConfig]
    raw: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def container_acl(self, container: str | None) -> list[str]:
        if container and container in self.containers:
            return list(self.containers[container].acl)
        return []


@dataclass(frozen=True)
class TenantSources:
    tenant_id: str
    tenant_name: str
    timezone: str
    sources: dict[str, SourceConfig]
    language: str | None = None  # en (the only language of this copy); None when undeclared

    def by_provider(self, provider: str) -> list[SourceConfig]:
        return [s for s in self.sources.values() if s.provider == provider]


def load_sources(path: Path | None = None) -> TenantSources:
    path = path or get_settings().config_dir / "sources.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    sources: dict[str, SourceConfig] = {}
    for s in data.get("sources") or []:
        containers = {
            k: ContainerConfig(k, (v or {}).get("name", k), tuple((v or {}).get("acl") or []))
            for k, v in (s.get("containers") or {}).items()
        }
        sources[s["id"]] = SourceConfig(s["id"], s["provider"], s.get("name", s["id"]), s.get("base_url"), containers, s)
    t = data.get("tenant") or {}
    lang = t.get("language")
    if lang is not None and lang != "en":
        raise ValueError(f"tenant.language must be en (this copy is English-only), got {lang!r}")
    return TenantSources(t.get("id", "default"), t.get("name", t.get("id", "default")), t.get("timezone", "UTC"), sources,
                         lang)


@dataclass
class AdapterContext:
    tenant_id: str
    source: SourceConfig
    tenant_sources: TenantSources

    def container_acl(self, container: str | None) -> list[str]:
        return self.source.container_acl(container)

    def account_ref(self, account_id: str | None, label: str | None = None) -> Ref | None:
        if not account_id:
            return None
        return Ref(source_id=self.source.id, object_type="account", external_id=str(account_id), label=label)

    def ref_from_url(self, url: str | None) -> Ref | None:
        """Resolve a URL that points into any source of the tenant (remote links, inline cards)."""
        if not url:
            return None
        for src in self.tenant_sources.sources.values():
            adapter = REGISTRY.get(src.provider)
            if adapter is None or not src.base_url or not url.startswith(src.base_url.rstrip("/")):
                continue
            ref = adapter.ref_from_url(url, src)
            if ref is not None:
                return ref
        return None


class Adapter(Protocol):
    provider: str

    def normalize(self, env: SourceEnvelope, ctx: AdapterContext) -> list[CanonicalRecord]:
        """First record = the primary object of the envelope; the rest are derived children of it.

        For operation="delete" return the primary record with deleted=True (payload may be None); the
        journal tombstones its derived children too.
        """
        ...

    def ref_from_url(self, url: str, source: SourceConfig) -> Ref | None:
        """Map a URL of this provider (under source.base_url) to an external Ref, or None."""
        ...


REGISTRY: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    REGISTRY[adapter.provider] = adapter
    return adapter


def get_adapter(provider: str) -> Adapter:
    if not REGISTRY:
        import kb.connectors  # noqa: F401  (registers the built-in adapters)
    try:
        return REGISTRY[provider]
    except KeyError as exc:
        raise LookupError(f"no adapter for provider {provider!r}") from exc
