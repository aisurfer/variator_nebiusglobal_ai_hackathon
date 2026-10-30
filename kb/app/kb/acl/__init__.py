"""Read policy (01 §4.5): a principal sees an entity when its ACL intersects the principal's groups.

Every read path (PostgreSQL queries, OpenSearch filters, Memgraph traversals, LLM context) must apply the
same check. An edge is visible only if both endpoints are visible; traversals never pass through a hidden
node and never report that something was hidden.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import psycopg


@dataclass(frozen=True)
class PrincipalCtx:
    tenant_id: str
    principal_id: str
    groups: frozenset[str] = field(default_factory=frozenset)  # includes principal_id itself
    is_admin: bool = False

    def can_see(self, acl: list[str] | tuple[str, ...] | None) -> bool:
        if self.is_admin:
            return True
        if not acl:
            return False  # unknown rights are not public (02 §11.1)
        return not self.groups.isdisjoint(acl)

    @property
    def group_list(self) -> list[str]:
        return sorted(self.groups)

    def sql_filter(self, column: str = "acl") -> tuple[str, list]:
        """SQL fragment + params restricting rows by an ACL text[] column."""
        if self.is_admin:
            return "TRUE", []
        return f"{column} && %s::text[]", [self.group_list]

    def os_filter(self) -> list[dict]:
        """OpenSearch filter clauses for a keyword field `acl`."""
        if self.is_admin:
            return []
        return [{"terms": {"acl": self.group_list}}]


class UnknownPrincipal(Exception):
    pass


def resolve_principal(conn: psycopg.Connection, tenant_id: str, principal_id: str) -> PrincipalCtx:
    row = conn.execute(
        "SELECT principal_id, kind, attrs FROM principal WHERE tenant_id = %s AND principal_id = %s",
        (tenant_id, principal_id),
    ).fetchone()
    if not row:
        raise UnknownPrincipal(principal_id)
    groups = {principal_id}
    frontier = [principal_id]
    while frontier:  # transitive group membership
        rows = conn.execute(
            "SELECT group_id FROM membership WHERE tenant_id = %s AND member_id = ANY(%s)", (tenant_id, frontier)
        ).fetchall()
        frontier = [r["group_id"] for r in rows if r["group_id"] not in groups]
        groups.update(frontier)
    return PrincipalCtx(tenant_id, principal_id, frozenset(groups), bool((row["attrs"] or {}).get("admin")))


def system_principal(tenant_id: str) -> PrincipalCtx:
    """For background jobs that process an assigned scope; never used to serve users."""
    return PrincipalCtx(tenant_id, "service:system", frozenset({"service:system"}), True)
