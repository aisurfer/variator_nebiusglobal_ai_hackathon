"""Editor operations on causal links (01 §7): new journal records, history is never rewritten.

The principal must see both endpoint entities; otherwise the link is reported as not found.
"""
from __future__ import annotations

from typing import Any

import psycopg

from kb.acl import PrincipalCtx
from kb.causal._base import Conflict, Invalid, NotFound
from kb.causal.trace import get_link
from kb.journal import reader
from kb.journal.writer import JournalError, journal_tx
from kb.ontology import get_predicate


def _evidence(evidence: Any) -> dict:
    if evidence is None:
        return {}
    if isinstance(evidence, str):
        return {"quote": evidence}
    if isinstance(evidence, dict):
        return dict(evidence)
    raise Invalid("evidence: a string or an object expected")


def _visible_link(conn: psycopg.Connection, p: PrincipalCtx, link_id: str) -> dict:
    row = conn.execute("SELECT * FROM causal_link WHERE tenant_id = %s AND link_id = %s", (p.tenant_id, link_id)).fetchone()
    if row is None or reader.get_entity(conn, p, row["effect_entity_id"]) is None \
            or reader.get_entity(conn, p, row["cause_entity_id"]) is None:
        raise NotFound(link_id)
    return row


def declare(conn: psycopg.Connection, p: PrincipalCtx, effect_entity_id: str, cause_entity_id: str, predicate: str, *,
            sensitivity: list[str] | None = None, evidence: Any = None) -> dict[str, Any]:
    """Declare effect <- cause (origin declared:editor), bound to the head versions of both at save time.
    Re-declaring an open link returns it unchanged (`created: false`)."""
    if not predicate or not isinstance(predicate, str):
        raise Invalid("predicate required")
    effect = reader.get_entity(conn, p, effect_entity_id)
    if effect is None:
        raise NotFound(effect_entity_id)
    cause = reader.get_entity(conn, p, cause_entity_id)
    if cause is None:
        raise NotFound(cause_entity_id)
    if effect_entity_id == cause_entity_id:
        raise Conflict("an entity cannot be its own cause")
    if effect["tombstone_tx"] is not None or cause["tombstone_tx"] is not None:
        raise Conflict("cannot declare a link on a deleted entity")
    info = get_predicate(conn, p.tenant_id, predicate)
    if info is not None and info.causal_kind is None:
        raise Invalid(f"{predicate} is not a causal predicate")
    if sensitivity is not None and (not isinstance(sensitivity, list) or not all(isinstance(s, str) for s in sensitivity)):
        raise Invalid("sensitivity: a list of field names or null")
    try:
        with journal_tx(conn, p.tenant_id, "editor", actor_id=p.principal_id,
                        ref={"op": "declare", "effect": effect_entity_id, "cause": cause_entity_id,
                             "predicate": predicate}) as j:
            link_id = j.declare_link(effect_entity_id=effect_entity_id, cause_entity_id=cause_entity_id,
                                     predicate=predicate, origin="declared:editor", sensitivity=sensitivity,
                                     evidence=_evidence(evidence))
            created = conn.execute("SELECT tx_seq FROM causal_link WHERE tenant_id = %s AND link_id = %s",
                                   (p.tenant_id, link_id)).fetchone()["tx_seq"] == j.txc.tx_seq
    except JournalError as exc:
        raise Conflict(str(exc)) from None
    out = get_link(conn, p, link_id) or {}
    out["created"] = created
    return out


def retract(conn: psycopg.Connection, p: PrincipalCtx, link_id: str, reason: str) -> dict[str, Any]:
    """Close the link ("we were wrong: D was not because of X"); the record stays as RETRACTED."""
    if not reason or not str(reason).strip():
        raise Invalid("reason required")
    _visible_link(conn, p, link_id)
    try:
        with journal_tx(conn, p.tenant_id, "editor", actor_id=p.principal_id,
                        ref={"op": "retract", "link_id": link_id}) as j:
            j.retract_link(link_id, reviewer=p.principal_id, reason=str(reason))
    except JournalError as exc:
        raise Conflict(str(exc)) from None
    return get_link(conn, p, link_id) or {}


def revalidate(conn: psycopg.Connection, p: PrincipalCtx, link_id: str, reason: str | None = None) -> dict[str, Any]:
    """The editor confirms the effect still holds for the cause's current version: a NEW link to the cause head
    (`revalidates` -> old); the old link is closed and kept. Returns {old, new}."""
    _visible_link(conn, p, link_id)
    try:
        with journal_tx(conn, p.tenant_id, "editor", actor_id=p.principal_id,
                        ref={"op": "revalidate", "link_id": link_id}) as j:
            new_id = j.revalidate_link(link_id, reviewer=p.principal_id, reason=reason)
    except JournalError as exc:
        raise Conflict(str(exc)) from None
    return {"old": get_link(conn, p, link_id), "new": get_link(conn, p, new_id), "link_id": new_id}
