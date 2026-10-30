"""Open-schema registry: kinds and predicates are rows (KB element 1). Unknown ones are auto-proposed."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import yaml

from kb.settings import get_settings


@dataclass(frozen=True)
class PredicateInfo:
    predicate_id: str
    label: str
    causal_kind: str | None
    typical_sensitivity: tuple[str, ...] | None
    status: str
    canonical_predicate_id: str | None


def load_ontology(path: Path | None = None) -> dict[str, Any]:
    path = path or get_settings().config_dir / "ontology.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def seed_ontology(conn: psycopg.Connection, tenant_id: str, onto: dict[str, Any] | None = None) -> None:
    onto = onto or load_ontology()
    for kind_id, spec in (onto.get("kinds") or {}).items():
        conn.execute(
            """INSERT INTO kind (tenant_id, kind_id, label, description) VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, kind_id) DO UPDATE SET label = EXCLUDED.label, description = EXCLUDED.description,
                 status = 'active'""",
            (tenant_id, kind_id, spec.get("label", kind_id), spec.get("description")),
        )
    for pid, spec in (onto.get("predicates") or {}).items():
        sens = spec.get("typical_sensitivity")
        conn.execute(
            """INSERT INTO predicate (tenant_id, predicate_id, label, inverse_label, aliases, description, causal_kind,
                                      typical_sensitivity, domain_kinds, range_kinds, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
               ON CONFLICT (tenant_id, predicate_id) DO UPDATE SET label = EXCLUDED.label,
                 inverse_label = EXCLUDED.inverse_label, aliases = EXCLUDED.aliases, description = EXCLUDED.description,
                 causal_kind = EXCLUDED.causal_kind, typical_sensitivity = EXCLUDED.typical_sensitivity,
                 domain_kinds = EXCLUDED.domain_kinds, range_kinds = EXCLUDED.range_kinds, status = 'active'""",
            (
                tenant_id,
                pid,
                spec.get("label", pid),
                spec.get("inverse_label"),
                list(spec.get("aliases") or []),
                spec.get("description"),
                spec.get("causal_kind"),
                list(sens) if sens else None,
                list(spec.get("domain") or []),
                list(spec.get("range") or []),
            ),
        )


def get_predicate(conn: psycopg.Connection, tenant_id: str, predicate_id: str) -> PredicateInfo | None:
    row = conn.execute(
        """SELECT predicate_id, label, causal_kind, typical_sensitivity, status, canonical_predicate_id
           FROM predicate WHERE tenant_id = %s AND predicate_id = %s""",
        (tenant_id, predicate_id),
    ).fetchone()
    if not row:
        return None
    sens = row["typical_sensitivity"]
    return PredicateInfo(
        row["predicate_id"],
        row["label"],
        row["causal_kind"],
        tuple(sens) if sens is not None else None,
        row["status"],
        row["canonical_predicate_id"],
    )


def ensure_predicate(
    conn: psycopg.Connection, tenant_id: str, predicate_id: str, causal_kind: str | None = None
) -> tuple[PredicateInfo, bool]:
    """Return the predicate, registering an unknown one as 'proposed' (schema review). Second value: created."""
    info = get_predicate(conn, tenant_id, predicate_id)
    if info:
        return info, False
    conn.execute(
        """INSERT INTO predicate (tenant_id, predicate_id, label, causal_kind, status)
           VALUES (%s, %s, %s, %s, 'proposed') ON CONFLICT DO NOTHING""",
        (tenant_id, predicate_id, predicate_id.replace("_", " "), causal_kind),
    )
    return get_predicate(conn, tenant_id, predicate_id), True  # type: ignore[return-value]


def ensure_kind(conn: psycopg.Connection, tenant_id: str, kind_id: str) -> None:
    conn.execute(
        "INSERT INTO kind (tenant_id, kind_id, label, status) VALUES (%s, %s, %s, 'proposed') ON CONFLICT DO NOTHING",
        (tenant_id, kind_id, kind_id),
    )
