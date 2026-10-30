"""Data builders for graph tests: records written through the real journal path into a fresh tenant."""
from __future__ import annotations

import time
from datetime import datetime

from kb.acl import PrincipalCtx
from kb.contracts import AliasSpec, CanonicalRecord, CausalSpec, Ref, StatementSpec
from kb.graph import memgraph as mg
from kb.journal.writer import journal_tx
from kb.worker.framework import drain
from tests.conftest import at

SRC = "jira-vf"
FP = ["grp:flowpilot"]
EXEC = ["grp:exec"]


def jref(ext: str) -> Ref:
    return Ref(source_id=SRC, object_type="issue", external_id=ext)


def eid(ext: str) -> str:
    return f"jira.{ext}"


def rel(pred: str, ext: str) -> StatementSpec:
    return StatementSpec(predicate=pred, object=jref(ext))


def lit(pred: str, value) -> StatementSpec:
    return StatementSpec(predicate=pred, literal=value)


def cause(pred: str, ext: str) -> CausalSpec:
    return CausalSpec(predicate=pred, cause=jref(ext))


def rec(ext: str, kind: str = "Story", title: str | None = None, fields: dict | None = None, *,
        statements: list[StatementSpec] | None = None, causes: list[CausalSpec] | None = None, deleted: bool = False,
        acl: list[str] | None = None, valid_from: datetime | None = None) -> CanonicalRecord:
    return CanonicalRecord(
        key=Ref(source_id=SRC, object_type="issue", external_id=ext), entity_id=eid(ext), kind=kind,
        title=title or ext, fields=fields or {}, statements=statements or [], causes=causes or [], deleted=deleted,
        acl=acl if acl is not None else FP, valid_from=valid_from, aliases=[AliasSpec(alias=ext, kind="key")],
    )


def apply(db, tenant: str, records: list[CanonicalRecord], when: str, origin: str = "declared:jira_import"):
    with journal_tx(db, tenant, "ingest", at=at(when)) as j:
        return j.apply_records(records, origin=origin, default_acl=FP)


WAITS: list[float] = []  # seconds spent waiting for pg_snapshot_xmin (diagnostics)


def _pending(db, tenant: str) -> int:
    return db.execute(
        """SELECT count(*) AS n FROM outbox o
           WHERE o.tenant_id = %(t)s AND (o.xid, o.id) > COALESCE(
             (SELECT (c.last_xid, c.last_id) FROM consumer_checkpoint c WHERE c.consumer = 'graph' AND c.tenant_id = %(t)s),
             ('0'::xid8, 0::bigint))""",
        {"t": tenant},
    ).fetchone()["n"]


def drain_graph(db, tenant: str, timeout: float = 60.0) -> dict[str, int]:
    """drain(consumers=["graph"]) until no outbox row of the tenant is left. The outbox is read only below
    pg_snapshot_xmin, so an open transaction elsewhere in the shared database (parallel test runs) can hold
    back the newest rows for a moment."""
    total = 0
    start = time.monotonic()
    while True:
        total += drain(db, tenant, consumers=["graph"])["graph"]
        if _pending(db, tenant) == 0:
            break
        if time.monotonic() - start > timeout:
            raise TimeoutError("outbox rows stay invisible to the graph consumer (a long transaction holds xmin)")
        time.sleep(0.1)
    if time.monotonic() - start > 0.5:
        WAITS.append(time.monotonic() - start)
    return {"graph": total}


def user(tenant: str, *groups: str, name: str = "user:test") -> PrincipalCtx:
    return PrincipalCtx(tenant, name, frozenset({name, *groups}), False)


def snapshot(tenant: str) -> tuple[list, list]:
    """Every node and edge of the tenant's subgraph, order-independent (for reset/replay comparisons)."""
    nodes = mg.read("MATCH (n {tenant: $t}) RETURN labels(n) AS l, properties(n) AS p", t=tenant)
    edges = mg.read(
        """MATCH (a {tenant: $t})-[r]->(b {tenant: $t})
           RETURN type(r) AS type, properties(r) AS p, coalesce(a.id, a.vid) AS a, coalesce(b.id, b.vid) AS b""",
        t=tenant,
    )

    def key(x: dict) -> str:
        return repr(sorted((k, repr(v)) for k, v in x.items()))

    return (sorted(key({"l": n["l"], **n["p"]}) for n in nodes),
            sorted(key({"type": e["type"], "a": e["a"], "b": e["b"], **e["p"]}) for e in edges))


def build_scenario(db, tenant: str) -> None:
    """A small history: creations, edits, a closed statement, a tombstone, declared + inferred causal links,
    a literal statement, a retro-dated version and an ACL change."""
    apply(db, tenant, [rec("FP-1", kind="Epic", fields={"summary": "Onboarding", "description": "d"},
                           statements=[lit("status", "Open")])], "2026-03-01T10:00:00+03:00")
    apply(db, tenant, [rec("FP-2", fields={"summary": "Checklist"}, causes=[cause("part_of", "FP-1")])],
          "2026-03-01T11:00:00+03:00")
    apply(db, tenant, [rec("FP-3", fields={"summary": "Tooltip"}, statements=[rel("relates_to", "FP-2")],
                           causes=[cause("part_of", "FP-1")])], "2026-03-01T12:00:00+03:00")
    apply(db, tenant, [rec("FP-4", fields={"summary": "Copy"}, statements=[rel("relates_to", "FP-3"),
                                                                           rel("relates_to", "FP-2")])],
          "2026-03-02T10:00:00+03:00")
    # FP-4 stops citing FP-3: the statement is closed (history kept)
    apply(db, tenant, [rec("FP-4", fields={"summary": "Copy v2"}, statements=[rel("relates_to", "FP-2")])],
          "2026-03-05T10:00:00+03:00")
    apply(db, tenant, [rec("FP-1", kind="Epic", fields={"summary": "Onboarding", "description": "changed"},
                           statements=[lit("status", "In progress")])], "2026-03-06T10:00:00+03:00")
    apply(db, tenant, [rec("FP-5", fields={"summary": "Old"}, statements=[rel("blocks", "FP-2")])],
          "2026-03-07T10:00:00+03:00")
    apply(db, tenant, [rec("FP-5", deleted=True)], "2026-03-08T10:00:00+03:00")
    # retro-dated correction recorded later
    apply(db, tenant, [rec("FP-2", fields={"summary": "Checklist (fixed)"}, causes=[cause("part_of", "FP-1")],
                           valid_from=at("2026-03-02T00:00:00+03:00"))], "2026-03-09T10:00:00+03:00")
    with journal_tx(db, tenant, "extract", at=at("2026-03-10T10:00:00+03:00")) as j:
        j.propose_link(effect_version="jira.FP-4@2", cause_version="jira.FP-3@1", predicate="caused_by",
                       origin="inferred:rules@1", confidence=0.4, evidence={"quote": "because of FP-3"})
        j.declare_link(effect_entity_id="jira.FP-4", cause_entity_id="jira.FP-1", predicate="informed_by")
    with journal_tx(db, tenant, "admin", at=at("2026-03-11T10:00:00+03:00")) as j:
        j.set_acl("jira.FP-3", ["grp:flowpilot", "grp:product"])
