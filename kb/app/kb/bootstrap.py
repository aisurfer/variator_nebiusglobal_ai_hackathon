"""Tenant bootstrap from config: tenant + sequencer, ontology, principals/groups, source instances, people.

Config location: `config/tenants/<tenant>/{sources,people}.yaml` when that directory holds a sources.yaml (a
generated tenant such as `company`), else `config/{sources,people}.yaml`. The ontology is shared
(`config/ontology.yaml`) unless the tenant directory has its own.

People, organizations and external contacts from people.yaml are ingested through the normal journal path
(directory source); the confirmed accounts of people and contacts are mapped to their person entities
(deterministic identity mapping, 02 §8.1). Accounts of an `email` source are e-mail addresses, lower-cased.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml

from kb.connectors.base import AdapterContext, TenantSources, get_adapter, load_sources
from kb.connectors.directory import contact_entity_id, person_entity_id
from kb.contracts import SourceEnvelope
from kb.db import Jsonb
from kb.journal.writer import journal_tx
from kb.ontology import load_ontology, seed_ontology
from kb.settings import get_settings


def load_people(path: Path | None = None) -> dict[str, Any]:
    path = path or get_settings().config_dir / "people.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def tenant_config_dir(tenant_id: str | None = None) -> Path:
    """config/tenants/<tenant>/ when it holds a sources.yaml, else the default config directory."""
    base = get_settings().config_dir
    if tenant_id:
        d = base / "tenants" / tenant_id
        if (d / "sources.yaml").is_file():
            return d
    return base


def tenant_people_at(tenant_id: str | None = None, config_dir: Path | None = None) -> datetime | None:
    """Optional `tenant.people_at` of sources.yaml: when the roster was recorded (must predate the history)."""
    cfg_dir = config_dir or tenant_config_dir(tenant_id)
    data = yaml.safe_load((cfg_dir / "sources.yaml").read_text(encoding="utf-8")) or {}
    value = (data.get("tenant") or {}).get("people_at")
    if value is None:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _ontology_path(cfg_dir: Path) -> Path:
    own = cfg_dir / "ontology.yaml"
    return own if own.is_file() else get_settings().config_dir / "ontology.yaml"


def _account_id(ts: TenantSources, source_id: str, account: Any) -> str:
    acc = str(account).strip()
    return acc.lower() if ts.sources[source_id].provider == "email" else acc


def _map_accounts(conn: psycopg.Connection, tid: str, ts: TenantSources, entity_id: str, accounts: dict | None,
                  tx_seq: int) -> int:
    n = 0
    for source_id, account in (accounts or {}).items():
        if source_id not in ts.sources or account is None or str(account).strip() == "":
            continue
        conn.execute(
            """INSERT INTO source_object (tenant_id, source_id, object_type, external_id, entity_id, lifecycle,
                 first_seen_tx) VALUES (%s, %s, 'account', %s, %s, 'mapped', %s)
               ON CONFLICT (tenant_id, source_id, object_type, external_id)
               DO UPDATE SET entity_id = EXCLUDED.entity_id""",
            (tid, source_id, _account_id(ts, source_id, account), entity_id, tx_seq),
        )
        n += 1
    return n


def ensure_tenant(
    conn: psycopg.Connection,
    tenant_id: str | None = None,
    *,
    people_at: datetime | None = None,
    config_dir: Path | None = None,
    background: bool = True,
) -> dict[str, int]:
    """Idempotent. `tenant_id` overrides the id in sources.yaml (tests use a fresh tenant per run).
    `config_dir` defaults to tenant_config_dir(tenant_id).
    background=False keeps the worker process away from the tenant (tests drain it themselves)."""
    cfg_dir = config_dir or tenant_config_dir(tenant_id)
    ts = load_sources(cfg_dir / "sources.yaml")
    people_cfg = load_people(cfg_dir / "people.yaml")
    tid = tenant_id or ts.tenant_id
    stats = {"people": 0, "organizations": 0, "contacts": 0, "accounts": 0, "sources": 0}
    with conn.transaction():
        conn.execute(
            """INSERT INTO tenant (tenant_id, name, settings) VALUES (%s, %s, %s)
               ON CONFLICT (tenant_id) DO UPDATE SET name = EXCLUDED.name, settings = EXCLUDED.settings""",
            (tid, ts.tenant_name, Jsonb({"timezone": ts.timezone, "background": background,
                                         **({"language": ts.language} if ts.language else {})})),
        )
        conn.execute("INSERT INTO tenant_clock (tenant_id) VALUES (%s) ON CONFLICT DO NOTHING", (tid,))
        seed_ontology(conn, tid, load_ontology(_ontology_path(cfg_dir)))
        for src in ts.sources.values():
            cfg = {k: v for k, v in src.raw.items() if k not in ("id", "provider", "name", "base_url")}
            conn.execute(
                """INSERT INTO source_instance (tenant_id, source_id, provider, display_name, base_url, config)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (tenant_id, source_id) DO UPDATE SET provider = EXCLUDED.provider,
                     display_name = EXCLUDED.display_name, base_url = EXCLUDED.base_url, config = EXCLUDED.config""",
                (tid, src.id, src.provider, src.name, src.base_url, Jsonb(cfg)),
            )
            stats["sources"] += 1
        for gid, g in (people_cfg.get("groups") or {}).items():
            conn.execute(
                """INSERT INTO principal (tenant_id, principal_id, kind, display_name) VALUES (%s, %s, 'group', %s)
                   ON CONFLICT (tenant_id, principal_id) DO UPDATE SET display_name = EXCLUDED.display_name""",
                (tid, gid, (g or {}).get("name", gid)),
            )
        for admin in people_cfg.get("admins") or []:
            conn.execute(
                """INSERT INTO principal (tenant_id, principal_id, kind, display_name, attrs)
                   VALUES (%s, %s, 'user', 'Administrator', '{"admin": true}')
                   ON CONFLICT (tenant_id, principal_id) DO UPDATE SET attrs = EXCLUDED.attrs""",
                (tid, admin),
            )
        for person in people_cfg.get("people") or []:
            pid = person["principal"]
            conn.execute(
                """INSERT INTO principal (tenant_id, principal_id, kind, display_name, person_entity_id)
                   VALUES (%s, %s, 'user', %s, %s)
                   ON CONFLICT (tenant_id, principal_id) DO UPDATE SET display_name = EXCLUDED.display_name,
                     person_entity_id = EXCLUDED.person_entity_id""",
                (tid, pid, person["name"], person_entity_id(person["id"])),
            )
            conn.execute("DELETE FROM membership WHERE tenant_id = %s AND member_id = %s", (tid, pid))
            for g in person.get("groups") or []:
                conn.execute(
                    "INSERT INTO membership (tenant_id, group_id, member_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (tid, g, pid),
                )

    directory = next((s for s in ts.sources.values() if s.provider == "directory"), None)
    if directory is None:
        return stats
    adapter = get_adapter("directory")
    ctx = AdapterContext(tid, directory, ts)
    organizations = [o for o in people_cfg.get("organizations") or [] if o.get("id")]
    org_names = {str(o["id"]): o.get("name") for o in organizations}

    def apply(j: Any, object_type: str, external_id: str, container: str, payload: dict, delivery: str) -> None:
        env = SourceEnvelope(tenant_id=tid, source_id=directory.id, delivery_id=f"bootstrap:{delivery}",
                             object_type=object_type, external_id=external_id, container=container, payload=payload,
                             source_modified_at=people_at)
        j.apply_records(adapter.normalize(env, ctx), origin="declared:directory_import",
                        default_acl=ctx.container_acl("people"), source_modified_at=people_at)

    with journal_tx(conn, tid, "ingest", actor_id="service:bootstrap", at=people_at, ref={"bootstrap": "people"}) as j:
        for org in organizations:  # first: contacts' works_at resolves in the same transaction
            apply(j, "organization", str(org["id"]), "organizations", org, f"org:{org['id']}")
            stats["organizations"] += 1
        for person in people_cfg.get("people") or []:
            apply(j, "person", person["id"], "people", person, person["id"])
            stats["people"] += 1
            stats["accounts"] += _map_accounts(conn, tid, ts, person_entity_id(person["id"]), person.get("accounts"),
                                               j.txc.tx_seq)
        for contact in people_cfg.get("contacts") or []:
            payload = dict(contact)
            if payload.get("org") and not payload.get("org_name") and org_names.get(str(payload["org"])):
                payload["org_name"] = org_names[str(payload["org"])]
            apply(j, "contact", str(contact["id"]), "contacts", payload, f"contact:{contact['id']}")
            stats["contacts"] += 1
            stats["accounts"] += _map_accounts(conn, tid, ts, contact_entity_id(str(contact["id"])),
                                               contact.get("accounts"), j.txc.tx_seq)
    return stats


def drop_tenant(conn: psycopg.Connection, tenant_id: str) -> None:
    """Remove every journal row of a tenant (tests and `seed --reset`). Projections are cleaned by their owners."""
    with conn.transaction():
        for table in ("outbox", "consumer_checkpoint", "projection_watermark", "causal_link", "statement", "unresolved_ref",
                      "review_decision", "alias", "chunk", "entity_version", "entity", "source_object", "source_revision",
                      "ingest_event", "scenario_run", "investigation_run"):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,))
        conn.execute("DELETE FROM tenant WHERE tenant_id = %s", (tenant_id,))
