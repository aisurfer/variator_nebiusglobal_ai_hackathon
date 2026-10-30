"""Journal semantics (K03): versions, bitemporal slices, set-diff sync, version-bound causal links."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kb.acl import system_principal
from kb.contracts import AliasSpec, CanonicalRecord, CausalSpec, Ref, SourceEnvelope, StatementSpec
from kb.journal import reader
from kb.journal.writer import JournalError, journal_tx
from tests.conftest import at

pytestmark = pytest.mark.integration
SRC = "jira-vf"


def rec(ext: str, kind: str = "Story", title: str | None = None, fields: dict | None = None, *, body: str | None = None,
        causes: list[CausalSpec] | None = None, statements: list[StatementSpec] | None = None, deleted: bool = False,
        valid_from: datetime | None = None, acl: list[str] | None = None, object_type: str = "issue") -> CanonicalRecord:
    return CanonicalRecord(
        key=Ref(source_id=SRC, object_type=object_type, external_id=ext), entity_id=f"jira.{ext}", kind=kind,
        title=title or ext, fields=fields or {}, body=body, causes=causes or [], statements=statements or [],
        deleted=deleted, valid_from=valid_from, acl=acl if acl is not None else ["grp:flowpilot"],
        aliases=[AliasSpec(alias=ext, kind="key")],
    )


def jref(ext: str) -> Ref:
    return Ref(source_id=SRC, object_type="issue", external_id=ext)


def apply(db, tenant, records, when: str, origin: str = "declared:jira_import"):
    with journal_tx(db, tenant, "ingest", at=at(when)) as j:
        return j.apply_records(records, origin=origin, default_acl=["grp:flowpilot"])


def status(db, tenant, link_id):
    return db.execute("SELECT status FROM causal_link_status WHERE tenant_id = %s AND link_id = %s",
                      (tenant, link_id)).fetchone()["status"]


def test_versions_and_unchanged(db, tenant):
    r1 = apply(db, tenant, [rec("FP-1", fields={"summary": "a", "status": "To Do"}, body="Hello world.")],
               "2026-03-01T10:00:00+03:00")
    assert r1.versions == ["jira.FP-1@1"]
    r2 = apply(db, tenant, [rec("FP-1", fields={"summary": "a", "status": "To Do"}, body="Hello world.")],
               "2026-03-01T11:00:00+03:00")
    assert r2.versions == [] and r2.unchanged == ["jira.FP-1"]
    r3 = apply(db, tenant, [rec("FP-1", fields={"summary": "a", "status": "Done"}, body="Hello world.")],
               "2026-03-02T10:00:00+03:00")
    assert r3.versions == ["jira.FP-1@2"]
    v1, v2 = (db.execute("SELECT * FROM entity_version WHERE tenant_id=%s AND entity_id='jira.FP-1' ORDER BY seq",
                         (tenant,)).fetchall())
    assert v2["changed_fields"] == ["status"]
    assert v1["tx"].upper == v2["tx"].lower and v2["tx"].upper is None
    # unchanged chunk is not duplicated: its interval is extended
    chunks = db.execute("SELECT * FROM chunk WHERE tenant_id=%s AND entity_id='jira.FP-1'", (tenant,)).fetchall()
    assert len(chunks) == 1 and chunks[0]["version_to_seq"] is None
    topics = [r["topic"] for r in db.execute("SELECT topic FROM outbox WHERE tenant_id=%s ORDER BY id", (tenant,))]
    assert topics.count("entity.version") >= 2 + 18  # 18 people from bootstrap


def test_bitemporal_slices(db, tenant):
    p = system_principal(tenant)
    apply(db, tenant, [rec("FP-2", fields={"budget": 120})], "2026-03-01T10:00:00+03:00")
    apply(db, tenant, [rec("FP-2", fields={"budget": 80})], "2026-05-10T10:00:00+03:00")
    # retroactive correction recorded in June: from April 1 the budget was actually 100
    apply(db, tenant, [rec("FP-2", fields={"budget": 100}, valid_from=at("2026-04-01T00:00:00+03:00"))],
          "2026-06-01T10:00:00+03:00")
    known_mar = reader.version_at(db, p, "jira.FP-2", known_at=at("2026-03-31T00:00:00+03:00"))
    assert known_mar["content"]["budget"] == 120
    # what was true on April 15, as known in May (before the correction) vs as known now
    assert reader.version_at(db, p, "jira.FP-2", valid_at=at("2026-04-15T00:00:00+03:00"),
                             known_at=at("2026-05-20T00:00:00+03:00"))["content"]["budget"] == 120
    assert reader.version_at(db, p, "jira.FP-2", valid_at=at("2026-04-15T00:00:00+03:00"))["content"]["budget"] == 100
    assert reader.version_at(db, p, "jira.FP-2", valid_at=at("2026-05-15T00:00:00+03:00"))["content"]["budget"] == 80
    hist = reader.history(db, p, "jira.FP-2")
    assert [h["seq"] for h in hist] == [1, 2, 3]
    d = reader.diff(db, p, "jira.FP-2", 1, 2)
    assert d["changes"] == [{"field": "budget", "before": 120, "after": 80}]


def test_causal_link_binding_and_status(db, tenant):
    apply(db, tenant, [rec("FP-10", kind="Epic", fields={"summary": "Epic", "description": "d", "status": "Open"})],
          "2026-03-01T10:00:00+03:00")
    r = apply(db, tenant, [rec("FP-11", fields={"summary": "s"}, causes=[CausalSpec(predicate="part_of", cause=jref("FP-10"))])],
              "2026-03-01T11:00:00+03:00")
    (lid,) = r.links_opened
    link = db.execute("SELECT * FROM causal_link WHERE tenant_id=%s AND link_id=%s", (tenant, lid)).fetchone()
    assert (link["effect_version"], link["cause_version"]) == ("jira.FP-11@1", "jira.FP-10@1")
    assert link["sensitivity"] == ["summary", "description"] and link["causal_kind"] == "caused_by"
    assert status(db, tenant, lid) == "CURRENT"
    # status change of the epic is outside the link's sensitivity
    apply(db, tenant, [rec("FP-10", kind="Epic", fields={"summary": "Epic", "description": "d", "status": "Done"})],
          "2026-03-02T10:00:00+03:00")
    assert status(db, tenant, lid) == "CURRENT_COMPATIBLE"
    # the effect is edited but still cites the epic: the link stays bound to FP-11@1
    r = apply(db, tenant, [rec("FP-11", fields={"summary": "s2"}, causes=[CausalSpec(predicate="part_of", cause=jref("FP-10"))])],
              "2026-03-03T10:00:00+03:00")
    assert r.links_opened == [] and r.links_closed == []
    apply(db, tenant, [rec("FP-10", kind="Epic", fields={"summary": "Epic", "description": "changed", "status": "Done"})],
          "2026-03-04T10:00:00+03:00")
    assert status(db, tenant, lid) == "STALE"
    apply(db, tenant, [rec("FP-10", kind="Epic", deleted=True)], "2026-03-05T10:00:00+03:00")
    assert status(db, tenant, lid) == "BROKEN"
    # the source stops citing the cause: the link is retracted, history kept
    r = apply(db, tenant, [rec("FP-11", fields={"summary": "s3"})], "2026-03-06T10:00:00+03:00")
    assert r.links_closed == [lid] and status(db, tenant, lid) == "RETRACTED"


def test_statement_set_diff(db, tenant):
    def st(x):
        return StatementSpec(predicate="relates_to", object=jref(x))

    for x in ("FP-20", "FP-21", "FP-22"):
        apply(db, tenant, [rec(x)], "2026-03-01T09:00:00+03:00")
    apply(db, tenant, [rec("FP-23", statements=[st("FP-20"), st("FP-21")])], "2026-03-01T10:00:00+03:00")
    before = {r["object_id"]: r["stmt_id"] for r in db.execute(
        "SELECT object_id, stmt_id FROM statement WHERE tenant_id=%s AND subject_id='jira.FP-23' AND upper_inf(tx)", (tenant,))}
    apply(db, tenant, [rec("FP-23", statements=[st("FP-21"), st("FP-22")])], "2026-03-02T10:00:00+03:00")
    after = {r["object_id"]: r["stmt_id"] for r in db.execute(
        "SELECT object_id, stmt_id FROM statement WHERE tenant_id=%s AND subject_id='jira.FP-23' AND upper_inf(tx)", (tenant,))}
    assert set(after) == {"jira.FP-21", "jira.FP-22"}
    assert after["jira.FP-21"] == before["jira.FP-21"]  # unchanged statement untouched


def test_late_binding(db, tenant):
    apply(db, tenant, [rec("FP-31", causes=[CausalSpec(predicate="blocked_by", cause=jref("FP-30"))])],
          "2026-03-01T10:00:00+03:00")
    assert db.execute("SELECT count(*) AS n FROM causal_link WHERE tenant_id=%s", (tenant,)).fetchone()["n"] == 0
    r = apply(db, tenant, [rec("FP-30", kind="Task")], "2026-03-01T12:00:00+03:00")
    (lid,) = r.links_opened
    link = db.execute("SELECT * FROM causal_link WHERE tenant_id=%s AND link_id=%s", (tenant, lid)).fetchone()
    assert (link["effect_version"], link["cause_version"], link["causal_kind"]) == ("jira.FP-31@1", "jira.FP-30@1", "precondition")
    # re-delivering FP-31 does not duplicate the resolved link
    r = apply(db, tenant, [rec("FP-31", fields={"x": 1}, causes=[CausalSpec(predicate="blocked_by", cause=jref("FP-30"))])],
              "2026-03-02T12:00:00+03:00")
    assert r.links_opened == []


def test_derived_children_and_cycles(db, tenant):
    page = CanonicalRecord(key=Ref(source_id="confluence-vf", object_type="page", external_id="1"),
                           entity_id="confluence.page-1", kind="Meeting", title="Sync", fields={"decisions": ["a", "b"]})

    def dec(n):
        return CanonicalRecord(key=Ref(source_id="confluence-vf", object_type="decision", external_id=f"1#dec-{n}"),
                               entity_id=f"confluence.page-1.dec-{n}", kind="Decision", title=f"d{n}",
                               fields={"statement": f"d{n}"},
                               causes=[CausalSpec(predicate="decided_in", cause=Ref(source_id="confluence-vf",
                                                                                     object_type="page", external_id="1"))])
    apply(db, tenant, [page, dec(1), dec(2)], "2026-03-02T12:00:00+03:00", origin="declared:confluence_import")
    r = apply(db, tenant, [page.model_copy(update={"fields": {"decisions": ["a"]}}), dec(1)], "2026-03-03T12:00:00+03:00",
              origin="declared:confluence_import")
    assert "confluence.page-1.dec-2@2" in r.versions
    ent = db.execute("SELECT tombstone_tx FROM entity WHERE tenant_id=%s AND entity_id='confluence.page-1.dec-2'", (tenant,)).fetchone()
    assert ent["tombstone_tx"] is not None
    # entity-level cycles are allowed (FP-40@2 -> FP-41@1 -> FP-40@1), version-level ones are rejected
    apply(db, tenant, [rec("FP-40"), rec("FP-41", causes=[CausalSpec(predicate="depends_on", cause=jref("FP-40"))])],
          "2026-03-04T10:00:00+03:00")
    r = apply(db, tenant, [rec("FP-40", fields={"v": 2}, causes=[CausalSpec(predicate="depends_on", cause=jref("FP-41"))])],
              "2026-03-05T10:00:00+03:00")
    assert len(r.links_opened) == 1 and not r.warnings
    with pytest.raises(JournalError):
        with journal_tx(db, tenant, "editor") as j:
            j.declare_link(effect_entity_id="jira.FP-40", cause_entity_id="jira.FP-41", predicate="relates_cause",
                           effect_version="jira.FP-40@1", cause_version="jira.FP-41@1")
    # a tombstone keeps the entity's kind even if the delete record guesses another one
    apply(db, tenant, [rec("FP-41", kind="Whatever", deleted=True)], "2026-03-06T10:00:00+03:00")
    assert db.execute("SELECT kind_id FROM entity WHERE tenant_id=%s AND entity_id='jira.FP-41'",
                      (tenant,)).fetchone()["kind_id"] == "Story"


def test_review_and_revalidate(db, tenant):
    apply(db, tenant, [rec("FP-50", fields={"summary": "a", "description": "x"}), rec("FP-51")], "2026-03-01T10:00:00+03:00")
    with journal_tx(db, tenant, "extract", at=at("2026-03-01T11:00:00+03:00")) as j:
        cand = j.propose_link(effect_version="jira.FP-51@1", cause_version="jira.FP-50@1", predicate="caused_by",
                              origin="inferred:rules@1", confidence=0.5, evidence={"quote": "because of FP-50"})
        assert j.propose_link(effect_version="jira.FP-51@1", cause_version="jira.FP-50@1", predicate="caused_by",
                              origin="inferred:rules@1", confidence=0.5, evidence={}) is None
    with journal_tx(db, tenant, "review", actor_id="user:admin") as j:
        new_id = j.accept_candidate(cand, reviewer="user:admin")
    link = db.execute("SELECT * FROM causal_link_status WHERE tenant_id=%s AND link_id=%s", (tenant, new_id)).fetchone()
    assert link["origin"] == "declared:review" and link["evidence"]["candidate"] == cand and link["status"] == "CURRENT"
    apply(db, tenant, [rec("FP-50", fields={"summary": "a", "description": "y"})], "2026-03-02T10:00:00+03:00")
    with journal_tx(db, tenant, "review", actor_id="user:admin") as j:
        reval = j.revalidate_link(new_id, reviewer="user:admin")
        with pytest.raises(JournalError):
            j.revalidate_link(new_id, reviewer="user:admin")
    st = db.execute("SELECT link_id, status, cause_version, revalidates FROM causal_link_status WHERE tenant_id=%s "
                    "AND link_id = ANY(%s)", (tenant, [new_id, reval])).fetchall()
    by = {r["link_id"]: r for r in st}
    assert by[new_id]["status"] == "RETRACTED"
    assert by[reval]["status"] == "CURRENT" and by[reval]["cause_version"] == "jira.FP-50@2" and by[reval]["revalidates"] == new_id


def test_ingest_dedupe_and_outbox_order(db, tenant):
    from kb.ingest.pipeline import ingest
    from kb.worker import framework

    payload = {"id": "VF-E999", "name": "Test Person", "role": "QA", "department": "QA", "accounts": {}}
    env = SourceEnvelope(tenant_id=tenant, source_id="directory-vf", delivery_id="d-1", object_type="person",
                         external_id="VF-E999", container="people", payload=payload,
                         observed_at=at("2026-03-01T10:00:00+03:00"))
    assert ingest(db, env).status == "processed"
    assert ingest(db, env).status == "duplicate"
    env2 = env.model_copy(update={"delivery_id": "d-2", "observed_at": at("2026-03-01T11:00:00+03:00")})
    assert ingest(db, env2).status == "unchanged"

    seen: list[int] = []

    class Probe:
        name = "probe"
        topics = frozenset({"entity.version"})

        def handle(self, conn, tenant_id, events):
            seen.extend(e.tx_seq for e in events)

        def reset(self, conn, tenant_id):
            pass

    framework.register_consumer(Probe())
    try:
        framework.drain(db, tenant, consumers=["probe"], batch=7)
        n = len(seen)
        assert n >= 19 and seen == sorted(seen)
        framework.drain(db, tenant, consumers=["probe"])
        assert len(seen) == n  # checkpoint: nothing re-delivered
    finally:
        framework.CONSUMERS.pop("probe", None)


def test_outbox_is_read_in_numeric_xid_order_across_a_digit_boundary(db, tenant):
    """xid 999 999 comes before 1 000 001 (T07: the rows were ordered by the text of the xid, so a consumer whose
    rows crossed 1 000 000 jumped to the 7-digit ones and skipped every 6-digit row behind its checkpoint)."""
    from kb.worker import framework

    # A drain reads only rows below pg_snapshot_xmin, so the xids must be in the past of the database: the largest
    # power of ten that a fresh database has already passed (a long-lived one crosses 1 000 000 as in T07).
    xmin = int(db.execute("SELECT pg_snapshot_xmin(pg_current_snapshot())::text AS x").fetchone()["x"])
    p = 10
    while p * 10 + 2 < xmin:
        p *= 10
    assert p + 2 < xmin, "the database is too fresh for a digit boundary below its snapshot"
    for seq, xid in ((1, p - 2), (2, p - 1), (3, p + 1), (4, p + 2)):
        db.execute("INSERT INTO outbox (xid, tenant_id, tx_seq, topic, payload) VALUES (%s::xid8, %s, %s, %s, %s)",
                   (str(xid), tenant, seq, "test.xid", '{"entity_id": "x"}'))
    seen: list[int] = []

    class Probe:
        name = "probe-xid"
        topics = frozenset({"test.xid"})

        def handle(self, conn, tenant_id, events):
            seen.extend(e.tx_seq for e in events)

        def reset(self, conn, tenant_id):
            pass

    framework.register_consumer(Probe())
    try:
        framework.drain(db, tenant, consumers=["probe-xid"], batch=2)
        assert seen == [1, 2, 3, 4]
    finally:
        framework.CONSUMERS.pop("probe-xid", None)
        framework.reset(db, tenant, ["probe-xid"])


def test_a_drain_session_runs_no_parallel_query_workers(db):
    """Many drain processes at once: parallelism comes from the processes; parallel query workers of each would
    exhaust the shared memory of the server (/dev/shm)."""
    from kb.worker import framework

    framework.prepare_session(db)
    assert db.execute("SHOW max_parallel_workers_per_gather").fetchone()["max_parallel_workers_per_gather"] == "0"
    # a commit does not wait for the WAL flush while it holds the tenant clock that every part's write waits for
    assert db.execute("SHOW synchronous_commit").fetchone()["synchronous_commit"] == "off"
    db.execute("RESET max_parallel_workers_per_gather")
    db.execute("RESET synchronous_commit")


def test_a_reset_drops_the_parts_of_a_consumer(db, tenant):
    """Parts left by a drain that was stopped would fold back at the next start: a reindex from zero must not
    resume from them (nor lose the steady run of a bulk load)."""
    from kb.worker import framework

    # a probe consumer: this copy has no extraction consumer, and resetting search would touch OpenSearch
    class Probe:
        name = "probe-reset"
        topics = frozenset({"test.reset"})

        def handle(self, conn, tenant_id, events):
            pass

        def reset(self, conn, tenant_id):
            pass

    framework.register_consumer(Probe())
    try:
        for k in range(2):
            db.execute("""INSERT INTO consumer_checkpoint (consumer, tenant_id, last_xid, last_id, updated_at)
                          VALUES (%s, %s, '5'::xid8, 5, now())""", (framework.part_name("probe-reset", k, 2), tenant))
            db.execute("INSERT INTO projection_watermark (tenant_id, projector, tx_seq, updated_at) VALUES (%s, %s, 5, now())",
                       (tenant, framework.part_name("probe-reset", k, 2)))
        framework.reset(db, tenant, ["probe-reset"])
        assert db.execute("SELECT count(*) AS n FROM consumer_checkpoint WHERE tenant_id = %s", (tenant,)).fetchone()["n"] == 0
        assert "probe-reset#0/2" not in framework.watermarks(db, tenant)
    finally:
        framework.CONSUMERS.pop("probe-reset", None)


def test_parts_of_a_failed_drain_fold_back_at_the_slowest_part(db, tenant):
    from kb.worker import framework

    for k, (xid, oid, seq) in enumerate((("1000002", 9, 4), ("999999", 5, 2), ("1000001", 7, 3))):
        db.execute("""INSERT INTO consumer_checkpoint (consumer, tenant_id, last_xid, last_id, updated_at)
                      VALUES (%s, %s, %s::xid8, %s, now())""", (framework.part_name("probe-fold", k, 3), tenant, xid, oid))
        db.execute("""INSERT INTO projection_watermark (tenant_id, projector, tx_seq, updated_at)
                      VALUES (%s, %s, %s, now())""", (tenant, framework.part_name("probe-fold", k, 3), seq))
    assert framework.fold_checkpoints(db, tenant, "probe-fold")
    cp = db.execute("""SELECT consumer, last_xid::text AS x, last_id FROM consumer_checkpoint
                       WHERE tenant_id = %s AND consumer LIKE 'probe-fold%%'""", (tenant,)).fetchall()
    assert [(r["consumer"], r["x"], r["last_id"]) for r in cp] == [("probe-fold", "999999", 5)]  # numeric minimum
    assert framework.watermarks(db, tenant).get("probe-fold") == 2
    assert not framework.fold_checkpoints(db, tenant, "probe-fold")  # nothing left to fold
    framework.reset(db, tenant, ["probe-fold"])


def test_empty_values_are_counted(db, tenant):
    """T07 A6: what the journal fills in for a record that did not say is counted (valid_from, author)."""
    res = apply(db, tenant, [rec("FP-901"), rec("FP-902", valid_from=at("2026-03-01T10:00:00+03:00"))],
                "2026-03-02T09:00:00+03:00")
    assert res.empty == {"valid_from_from_tx": 1, "no_author": 2}
    with journal_tx(db, tenant, "ingest", at=at("2026-03-03T09:00:00+03:00")) as j:
        res = j.apply_records([rec("FP-903")], origin="declared:jira_import", default_acl=["grp:flowpilot"],
                              source_modified_at=at("2026-03-03T08:58:00+03:00"))
    assert res.empty == {"valid_from_from_source_time": 1, "no_author": 1}
    v = db.execute("SELECT valid_from FROM entity_version WHERE tenant_id = %s AND entity_id = 'jira.FP-903'",
                   (tenant,)).fetchone()
    assert v["valid_from"] == at("2026-03-03T08:58:00+03:00")
