"""Shared pieces of the causal engine: errors, SQL clause builders, labels, node cards, formatting.

Conventions used by every query of the engine (01-ARCHITECTURE §4.3, research §2.3, §4.3):
- a causal link is `effect_version -> cause_version`; traversal backwards ("why") goes effect -> cause;
- visibility: a link is visible only if both endpoint entities are visible to the principal; a traversal never
  passes through a hidden entity and never reports that something was cut (no counters, no markers);
- default space: declared links (`origin LIKE 'declared:%'`, review_status accepted); `include_inferred` adds
  `inferred:*` candidates, always labelled `inferred: true`;
- time: `known_at=None` means "as known at the end of the data" (open ranges, the `causal_link_status` view); a
  timestamp K means "as known at K": links whose knowledge interval contains K (`known @> K`), versions recorded by K
  (`tx`), and nothing recorded after K in the answer (Docs/KB/AS-OF.md).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import orjson
import psycopg

from kb.lang import kind_label, predicate_label

from kb.acl import PrincipalCtx
from kb.text import canonical_json

MAX_DEPTH = 8
WHY_FANOUT = 30  # causes expanded per node
EFFECTS_FANOUT = 100  # dependents expanded per node
MAX_CHAINS = 64
MAX_PATHS = 5
STATUSES = ("CURRENT", "CURRENT_COMPATIBLE", "STALE", "BROKEN", "RETRACTED")

# Fields that describe what an entity *is* for a manager reading an answer (research §5.5 example answer).
KEY_FIELDS: dict[str, list[str]] = {
    "Meeting": ["held_at", "attendees", "note_taker", "status"],
    "Decision": ["statement", "owner", "decided_at", "status"],
    "ActionItem": ["summary", "owner", "due", "status"],
    "Experiment": ["hypothesis", "start_date", "duration_days", "traffic_split", "primary_metric", "status"],
    "Epic": ["summary", "status", "description"],
    "Story": ["summary", "status", "description", "acceptance_criteria"],
    "Task": ["summary", "status"],
    "Bug": ["summary", "status", "severity"],
    "Issue": ["summary", "title", "state"],
    "MergeRequest": ["title", "state", "merged_at", "merge_commit"],
    "Commit": ["message", "sha"],
    "Release": ["tag", "released_at", "merge_requests"],
    "Policy": ["rules", "version", "status", "owner"],
    "Report": ["findings", "metrics", "period"],
    "Document": ["status", "owner", "kind"],
    "Spec": ["status", "owner"],
    "ADR": ["status", "decision"],
    "KBArticle": ["status"],
    "Person": ["role", "department"],
    # simulated business records (kb.connectors.sim)
    "Contract": ["number", "client_name", "client", "amount", "currency", "status", "start_date", "end_date"],
    "Invoice": ["number", "client_name", "client", "amount", "currency", "status", "due_date"],
    "Payment": ["amount", "currency", "direction", "settles", "status", "paid_at"],
    "BankEntry": ["amount", "currency", "direction", "booked_at", "records"],
    "Deal": ["client_name", "client", "status", "amount", "currency", "owner"],
    "EmployeeEvent": ["type", "employee_name", "employee", "effective_date", "status"],
    "Requisition": ["role", "department", "status", "owner"],
    "Payroll": ["period", "amount", "currency", "headcount", "status"],
    "Incident": ["title", "severity", "status", "component", "started_at", "resolved_at"],
    "Deploy": ["component", "version", "environment", "status", "deployed_at"],
    "SupportTicket": ["title", "client_name", "client", "status", "priority"],
    "CloudBill": ["vendor_name", "vendor", "period", "amount", "currency", "status"],
    "TaxPayment": ["tax", "period", "amount", "currency", "status"],
    "Hypothesis": ["statement", "status"],
    "LifeEvent": ["statement", "effective_date"],
    "Habit": ["statement", "level"],
    "Process": ["statement", "owner"],
}
# Order in which causes are presented: what caused/decided first, then bases, then dependencies.
CAUSAL_KIND_ORDER = {"caused_by": 0, "decided_because_of": 1, "based_on": 2, "informed_by": 3, "supersedes": 4,
                     "precondition": 5, "depends_on": 6}
# Chains through these kinds answer "why" best (research §5.5): a meeting where people decided, then a decision.
SALIENT_KINDS = {"Meeting": 0, "Decision": 1, "ADR": 2}
# Importance of the dependent kind when ranking stale dependencies (research §5.5 "importance of D").
KIND_WEIGHT: dict[str, float] = {
    "Decision": 4.0, "Experiment": 4.0, "Release": 3.5, "Epic": 3.0, "Policy": 3.0, "ADR": 3.0, "Spec": 2.5,
    "Story": 2.0, "MergeRequest": 2.0, "Meeting": 1.5, "Task": 1.5, "Bug": 1.5, "Report": 1.5, "Document": 1.0,
}


class CausalError(Exception):
    """Base error of the causal engine."""


class NotFound(CausalError):
    """Unknown, or invisible to the principal (indistinguishable on purpose)."""


class Conflict(CausalError):
    """The operation contradicts the journal state (cycle, link already closed, cause deleted)."""


class Invalid(CausalError, ValueError):
    """Malformed request."""


# ------------------------------------------------------------------------------------------------ SQL clauses
def params(p: PrincipalCtx, **extra: Any) -> dict[str, Any]:
    return {"t": p.tenant_id, "groups": p.group_list, **extra}


def acl_clause(p: PrincipalCtx, column: str) -> str:
    """Same rule as PrincipalCtx.can_see (an empty ACL is not public), as SQL over named params."""
    return "TRUE" if p.is_admin else f"{column} && %(groups)s::text[]"


def origin_clause(include_inferred: bool, alias: str = "l") -> str:
    declared = f"({alias}.origin LIKE 'declared:%%' AND {alias}.review_status = 'accepted')"
    if include_inferred:
        # an inferred candidate counts while it exists at K (pending then); accepted/rejected ones are closed
        return f"({declared} OR {alias}.origin LIKE 'inferred:%%')"
    return declared


def exists_clause(known_at: datetime | None, alias: str = "l") -> str:
    """The link exists now (open) or at K (its knowledge interval contains K, Docs/KB/AS-OF.md)."""
    return f"upper_inf({alias}.tx)" if known_at is None else f"{alias}.known @> %(k)s::timestamptz"


# ------------------------------------------------------------------------------------------------ helpers
def jsonable(value: Any) -> Any:
    """Plain JSON (datetimes -> ISO strings) for scenario_run rows."""
    return orjson.loads(canonical_json(value))


def split_version(version_id: str) -> tuple[str, int] | None:
    ent, sep, seq = version_id.rpartition("@")
    if not sep or not ent or not seq.isdigit():
        return None
    return ent, int(seq)


def is_inferred(origin: str | None) -> bool:
    return bool(origin) and origin.startswith("inferred:")


def visible_fields(changed: Iterable[str]) -> list[str]:
    out = []
    for f in changed:
        if f == "@relations":
            out.append("relations")
        elif f == "@deleted":
            out.append("deleted")
        elif not f.startswith("@"):
            out.append(f)
    return out


def sensitivity_hit(changed: Iterable[str], sensitivity: list[str] | None) -> list[str]:
    """Changed fields that matter for a link: all of them for sensitivity NULL, else the intersection."""
    changed = list(changed)
    if sensitivity is None:
        return changed
    sens = set(sensitivity)
    return [f for f in changed if f in sens]


def fields_match(sensitivity: list[str] | None, fields: list[str]) -> bool:
    if sensitivity is None or "*" in fields:
        return True
    return bool(set(sensitivity) & set(fields))


def hub_weight(fanout: int, confidence: float = 1.0) -> float:
    """Step weight with a hub penalty (research §8 "Hubs"): 1 for a private dependency, ~0.3 for a hub of 10."""
    return float(confidence) / (1.0 + math.log(max(1, fanout)))


def short(value: Any, limit: int = 280) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1].rstrip() + "…"
    if isinstance(value, list):
        return [short(v, limit) for v in value[:12]]
    if isinstance(value, dict):
        return {k: short(v, limit) for k, v in list(value.items())[:12]}
    return value


def key_fields(kind: str, content: dict | None) -> dict[str, Any]:
    fields = {k: v for k, v in (content or {}).items() if not k.startswith("@")}
    names = KEY_FIELDS.get(kind)
    if names:
        picked = {n: short(fields[n]) for n in names if n in fields and fields[n] not in (None, "", [])}
        if picked:
            return picked
    out: dict[str, Any] = {}
    for k, v in fields.items():
        if isinstance(v, (str, int, float, bool)) and v not in ("",):
            out[k] = short(v)
        if len(out) >= 5:
            break
    return out


# ------------------------------------------------------------------------------------------------ labels, tz
def tenant_tz(conn: psycopg.Connection, tenant_id: str) -> ZoneInfo:
    row = conn.execute("SELECT settings FROM tenant WHERE tenant_id = %s", (tenant_id,)).fetchone()
    name = ((row or {}).get("settings") or {}).get("timezone") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - an unknown zone falls back to UTC
        return ZoneInfo("UTC")


class Labels:
    """Kind and predicate labels of a tenant (open schema: they are rows, read once per request)."""

    def __init__(self, conn: psycopg.Connection, tenant_id: str):
        self.kinds = {r["kind_id"]: r["label"] for r in conn.execute(
            "SELECT kind_id, label FROM kind WHERE tenant_id = %s", (tenant_id,)).fetchall()}
        self.predicates = {r["predicate_id"]: r for r in conn.execute(
            """SELECT predicate_id, label, inverse_label, causal_kind, typical_sensitivity, status
               FROM predicate WHERE tenant_id = %s""", (tenant_id,)).fetchall()}

    def kind(self, kind_id: str | None) -> str:
        return kind_label(kind_id, self.kinds.get(kind_id or ""))

    def predicate(self, predicate_id: str) -> str:
        row = self.predicates.get(predicate_id)
        return predicate_label(predicate_id, row["label"] if row else None)

    def inverse(self, predicate_id: str) -> str:
        row = self.predicates.get(predicate_id)
        return predicate_label(predicate_id, (row or {}).get("inverse_label"), inverse=True)

    def causal_kind(self, predicate_id: str) -> str | None:
        row = self.predicates.get(predicate_id)
        return row["causal_kind"] if row else None


def fmt_dt(value: Any, tz: ZoneInfo) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(tz)
    if (local.hour, local.minute, local.second) == (0, 0, 0):
        return local.strftime("%Y-%m-%d")
    return local.strftime("%Y-%m-%d %H:%M ") + (local.tzname() or "")


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# ------------------------------------------------------------------------------------------------ nodes
NODE_SQL = """
SELECT v.version_id, v.entity_id, v.seq, v.op, v.kind_id, v.title, v.content, v.valid_from,
       lower(v.tx) AS recorded_at, upper(v.tx) AS superseded_at, v.actor_id,
       e.title AS entity_title, e.kind_id AS entity_kind, e.url, e.acl, e.head_version_id, e.tombstone_tx,
       a.title AS actor_title, a.acl AS actor_acl, hk.version_id AS head_at_k, hk.op AS head_at_k_op
FROM entity_version v
JOIN entity e ON e.tenant_id = v.tenant_id AND e.entity_id = v.entity_id
LEFT JOIN entity a ON a.tenant_id = v.tenant_id AND a.entity_id = v.actor_id
LEFT JOIN LATERAL (
  SELECT h.version_id, h.op FROM entity_version h
  WHERE h.tenant_id = v.tenant_id AND h.entity_id = v.entity_id AND {head}
  ORDER BY h.seq DESC LIMIT 1) hk ON true
WHERE v.tenant_id = %(t)s AND v.version_id = ANY(%(ids)s)
"""


def load_nodes(conn: psycopg.Connection, p: PrincipalCtx, version_ids: Iterable[str], *,
               known_at: datetime | None = None, labels: Labels | None = None) -> dict[str, dict]:
    """Version cards keyed by version id. Versions of entities the principal cannot see are dropped."""
    ids = sorted(set(version_ids))
    if not ids:
        return {}
    head = "upper_inf(h.tx)" if known_at is None else "h.tx @> %(k)s::timestamptz"
    rows = conn.execute(NODE_SQL.format(head=head), params(p, ids=ids, k=known_at)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        if not p.can_see(r["acl"]):
            continue
        kind = r["kind_id"] or r["entity_kind"]
        cur = r["head_at_k"] or r["head_version_id"]
        node = {
            "version_id": r["version_id"],
            "entity_id": r["entity_id"],
            "kind": kind,
            "kind_label": labels.kind(kind) if labels else kind,
            "title": r["title"] or (r["entity_title"] if known_at is None else None),
            "seq": r["seq"],
            "op": r["op"],
            "effective_from": r["valid_from"],
            "recorded_at": r["recorded_at"],
            # a supersession recorded after K is not known at K
            "superseded_at": r["superseded_at"] if known_at is None or (
                r["superseded_at"] is not None and r["superseded_at"] <= known_at) else None,
            "actor_id": r["actor_id"] if r["actor_id"] and p.can_see(r["actor_acl"]) else None,
            "actor": r["actor_title"] if r["actor_id"] and p.can_see(r["actor_acl"]) else None,
            "key_fields": key_fields(kind, r["content"]),
            "url": r["url"],
            "current_version": cur,
            "is_current": cur == r["version_id"],
            "deleted": (r["head_at_k_op"] == "delete") if r["head_at_k"] else r["tombstone_tx"] is not None,
        }
        out[r["version_id"]] = node
    return out


def resolve_target(conn: psycopg.Connection, p: PrincipalCtx, target: str, known_at: datetime | None
                   ) -> tuple[str, int, str] | None:
    """`X` -> (X, head seq at K, head version id); `X@n` -> (X, n, X@n). None if unknown/invisible/not yet at K."""
    sv = split_version(target)
    if sv is not None:
        row = conn.execute(
            """SELECT v.entity_id, v.seq, v.version_id, lower(v.tx) AS recorded_at, e.acl FROM entity_version v
               JOIN entity e ON e.tenant_id = v.tenant_id AND e.entity_id = v.entity_id
               WHERE v.tenant_id = %s AND v.version_id = %s""",
            (p.tenant_id, target),
        ).fetchone()
        if row is not None:
            if not p.can_see(row["acl"]):
                return None
            if known_at is not None and row["recorded_at"] > known_at:
                return None
            return row["entity_id"], row["seq"], row["version_id"]
    ent = conn.execute("SELECT entity_id, acl, head_seq, head_version_id FROM entity WHERE tenant_id = %s AND entity_id = %s",
                       (p.tenant_id, target)).fetchone()
    if ent is None or not p.can_see(ent["acl"]):
        return None
    if known_at is None:
        return ent["entity_id"], ent["head_seq"], ent["head_version_id"]
    row = conn.execute(
        """SELECT seq, version_id FROM entity_version WHERE tenant_id = %s AND entity_id = %s
             AND tx @> %s::timestamptz""",
        (p.tenant_id, target, known_at),
    ).fetchone()
    if row is None:
        return None
    return ent["entity_id"], row["seq"], row["version_id"]
