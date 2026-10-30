"""Causal engine (K11): computed link statuses, why / causes / effects / impact / stale, editor ops.

Authoritative answers are computed in PostgreSQL (KB-4); the Memgraph projection is not consulted.
Every function takes a PrincipalCtx and never returns, counts or hints at entities the principal cannot see.
"""
from kb.causal._base import CausalError, Conflict, Invalid, NotFound
from kb.causal.editor import declare, retract, revalidate
from kb.causal.impact import impact, stale_dependencies, stale_history
from kb.causal.status import link_status
from kb.causal.trace import causes, effects, get_link, why

__all__ = [
    "CausalError", "Conflict", "Invalid", "NotFound",
    "link_status", "why", "causes", "effects", "get_link", "impact", "stale_dependencies", "stale_history",
    "declare", "retract", "revalidate",
]
