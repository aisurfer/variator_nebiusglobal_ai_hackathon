"""Bitemporal journal writer (K03). All writes to the source-of-truth tables go through `journal_tx`.

Invariants (01-ARCHITECTURE §4):
- a journal transaction first locks the tenant sequencer row: writes of a tenant are serialised, tx_at is
  monotonic, tx_seq order equals commit order;
- versions are immutable; the only mutation of a journal row is closing its tx range (plus review_status of an
  inferred candidate);
- statements, aliases and declared causal links are synchronised by set difference per (entity, origin):
  disappeared ones are closed, new ones inserted, unchanged ones untouched;
- a causal link binds the effect version that asserted it and the head version of the cause at save time;
  an unchanged link stays bound to the old versions when either side changes later (research §2.3);
- references to objects not seen yet are parked in unresolved_ref and become statements/links on arrival.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import orjson
import psycopg

from kb.contracts import (
    TOPIC_CAUSAL,
    TOPIC_CHUNKS,
    TOPIC_ENTITY_ACL,
    TOPIC_ENTITY_VERSION,
    TOPIC_STATEMENTS,
    CanonicalRecord,
    CausalSpec,
    Ref,
    StatementSpec,
)
from kb.db import Jsonb
from kb.ontology import ensure_kind, ensure_predicate
from kb.text import canonical_json, chunk_text, norm, sha256_hex, short_hash

log = logging.getLogger(__name__)
_EPS = timedelta(microseconds=1)
_CYCLE_DEPTH = 12
# closing a row at the logical time of the transaction (a row known from a later moment is never known)
_CLOSE_KNOWN = "known = tstzrange(lower(known), GREATEST(lower(known), %s), '[)')"


class JournalError(Exception):
    pass


class UnknownTenant(JournalError):
    pass


@dataclass(frozen=True)
class TxContext:
    tenant_id: str
    tx_seq: int
    tx_at: datetime
    kind: str
    actor_id: str | None
    # the logical time of what the transaction writes (Docs/KB/AS-OF.md): statements and links become known (and stop
    # being known) at `known_at`. Ingestion, review and the editor: the transaction time. Extraction: the transaction
    # time of the journal event whose state it derives, however late the extractor runs.
    known_at: datetime | None = None

    @property
    def k(self) -> datetime:
        return self.known_at or self.tx_at


@dataclass
class ApplyResult:
    entities: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    links_opened: list[str] = field(default_factory=list)
    links_closed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # empty-value policy (Docs/KB/03-SIMULATION.md): what the journal filled in for a record that did not say
    empty: dict[str, int] = field(default_factory=dict)  # valid_from_from_source_time | valid_from_from_tx | no_author


def _plain(value: Any) -> Any:
    """JSON-normalise (datetimes -> ISO strings) so hashes and jsonb comparisons are stable."""
    return orjson.loads(canonical_json(value))


def _key_target(ref: Ref) -> str:
    return ref.external_key() or f"entity:{ref.entity_id}"


@contextmanager
def journal_tx(
    conn: psycopg.Connection,
    tenant_id: str,
    kind: str,
    *,
    actor_id: str | None = None,
    at: datetime | None = None,
    ref: dict | None = None,
    known_at: datetime | None = None,
) -> Iterator["Journal"]:
    """Open one journal transaction. `at` (simulated observed time) is honoured only if it does not go back
    in time; tx_at = max(at or now, last_tx_at + 1µs), except an extraction on the simulated axis, which takes
    last_tx_at itself. `known_at` (extraction only): the logical time of the rows it writes, never after tx_at."""
    with conn.transaction():
        row = conn.execute(
            "SELECT last_tx_at, last_tx_seq FROM tenant_clock WHERE tenant_id = %s FOR UPDATE", (tenant_id,)
        ).fetchone()
        if row is None:
            raise UnknownTenant(tenant_id)
        wanted = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        last = row["last_tx_at"]
        if kind == "extract" and known_at is not None and last is not None and wanted <= last:
            # derived rows on the simulated axis do not move the clock: the next ingested event gets the same time
            # whether extraction ran between the events or after all of them (Docs/KB/AS-OF.md)
            tx_at = last
        else:
            tx_at = wanted if last is None or wanted > last else last + _EPS
        tx_seq = row["last_tx_seq"] + 1
        conn.execute(
            "UPDATE tenant_clock SET last_tx_at = %s, last_tx_seq = %s WHERE tenant_id = %s", (tx_at, tx_seq, tenant_id)
        )
        conn.execute(
            "INSERT INTO tx_log (tenant_id, tx_seq, tx_at, kind, actor_id, ref) VALUES (%s, %s, %s, %s, %s, %s)",
            (tenant_id, tx_seq, tx_at, kind, actor_id, Jsonb(ref or {})),
        )
        if known_at is not None and known_at > tx_at:
            raise JournalError("known_at after the transaction time")
        j = Journal(conn, TxContext(tenant_id, tx_seq, tx_at, kind, actor_id, known_at))
        yield j
        j._flush_outbox()


class Journal:
    def __init__(self, conn: psycopg.Connection, txc: TxContext):
        self.conn = conn
        self.txc = txc
        self._link_n = 0
        self._events: dict[tuple[str, str], dict[str, Any]] = {}

    # ------------------------------------------------------------------ helpers
    @property
    def t(self) -> str:
        return self.txc.tenant_id

    def _x(self, sql: str, params: tuple | dict = ()) -> psycopg.Cursor:
        return self.conn.execute(sql, params)

    def _event(self, topic: str, key: str, payload: dict[str, Any]) -> None:
        """Collect outbox events; list values of the same (topic, key) are merged."""
        cur = self._events.get((topic, key))
        if cur is None:
            self._events[(topic, key)] = payload
            return
        for k, v in payload.items():
            if isinstance(v, list) and isinstance(cur.get(k), list):
                cur[k] = list(dict.fromkeys(cur[k] + v))
            else:
                cur[k] = v

    def _flush_outbox(self) -> None:
        for (topic, _), payload in self._events.items():
            self._x(
                "INSERT INTO outbox (tenant_id, tx_seq, topic, payload) VALUES (%s, %s, %s, %s)",
                (self.t, self.txc.tx_seq, topic, Jsonb(payload)),
            )
        self._events.clear()

    def _next_link_id(self) -> str:
        self._link_n += 1
        return f"cl-{self.txc.tx_seq}-{self._link_n}"

    def resolve_ref(self, ref: Ref) -> str | None:
        if ref.entity_id:
            row = self._x("SELECT 1 FROM entity WHERE tenant_id = %s AND entity_id = %s", (self.t, ref.entity_id)).fetchone()
            return ref.entity_id if row else None
        row = self._x(
            """SELECT entity_id FROM source_object
               WHERE tenant_id = %s AND source_id = %s AND object_type = %s AND external_id = %s""",
            (self.t, ref.source_id, ref.object_type, ref.external_id),
        ).fetchone()
        return row["entity_id"] if row else None

    def resolve_ref_external(self, source_id: str, object_type: str, external_id: str) -> str | None:
        return self.resolve_ref(Ref(source_id=source_id, object_type=object_type, external_id=external_id))

    def children_of(self, entity_id: str) -> list[str]:
        rows = self._x(
            "SELECT entity_id FROM source_object WHERE tenant_id = %s AND parent_entity_id = %s", (self.t, entity_id)
        ).fetchall()
        return [r["entity_id"] for r in rows]

    def _entity(self, entity_id: str) -> dict | None:
        return self._x("SELECT * FROM entity WHERE tenant_id = %s AND entity_id = %s", (self.t, entity_id)).fetchone()

    def _version(self, version_id: str) -> dict | None:
        return self._x(
            "SELECT * FROM entity_version WHERE tenant_id = %s AND version_id = %s", (self.t, version_id)
        ).fetchone()

    # ------------------------------------------------------------------ records
    def apply_records(
        self,
        records: list[CanonicalRecord],
        *,
        origin: str,
        source_revision_id: int | None = None,
        default_acl: list[str] | None = None,
        source_modified_at: datetime | None = None,
    ) -> ApplyResult:
        """Apply the records of one envelope: records[0] is the primary, the rest are its derived children."""
        res = ApplyResult()
        if not records:
            return res
        primary_id = self._apply_record(records[0], None, origin, source_revision_id, default_acl, source_modified_at, res)
        if primary_id is None:
            return res
        produced: set[str] = set()
        for child in records[1:]:
            cid = self._apply_record(child, primary_id, origin, source_revision_id, default_acl, source_modified_at, res)
            if cid:
                produced.add(cid)
        # Derived children that the new revision no longer contains (or all, if the parent is deleted) are tombstoned.
        rows = self._x(
            """SELECT so.source_id, so.object_type, so.external_id, so.entity_id, e.kind_id, e.title
               FROM source_object so JOIN entity e ON e.tenant_id = so.tenant_id AND e.entity_id = so.entity_id
               WHERE so.tenant_id = %s AND so.parent_entity_id = %s AND e.tombstone_tx IS NULL""",
            (self.t, primary_id),
        ).fetchall()
        for r in rows:
            if r["entity_id"] in produced:
                continue
            gone = CanonicalRecord(
                key=Ref(source_id=r["source_id"], object_type=r["object_type"], external_id=r["external_id"]),
                entity_id=r["entity_id"], kind=r["kind_id"], title=r["title"] or r["entity_id"], deleted=True,
            )
            self._apply_record(gone, primary_id, origin, source_revision_id, default_acl, source_modified_at, res)
        return res

    def _map_entity(self, rec: CanonicalRecord, parent_entity_id: str | None) -> tuple[str | None, bool]:
        """Find or create the entity for the record's external identity. Returns (entity_id, is_new_mapping)."""
        k = rec.key
        row = self._x(
            """SELECT entity_id FROM source_object
               WHERE tenant_id = %s AND source_id = %s AND object_type = %s AND external_id = %s""",
            (self.t, k.source_id, k.object_type, k.external_id),
        ).fetchone()
        if row:
            return row["entity_id"], False
        if rec.deleted:
            return None, False
        entity_id = rec.entity_id
        n = 1
        while self._entity(entity_id) is not None:  # never steal an id that belongs to another source object
            n += 1
            entity_id = f"{rec.entity_id}~{n}"
        self._x(
            """INSERT INTO source_object (tenant_id, source_id, object_type, external_id, entity_id, parent_entity_id,
                                          first_seen_tx) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (self.t, k.source_id, k.object_type, k.external_id, entity_id, parent_entity_id, self.txc.tx_seq),
        )
        return entity_id, True

    def _apply_record(
        self,
        rec: CanonicalRecord,
        parent_entity_id: str | None,
        origin: str,
        source_revision_id: int | None,
        default_acl: list[str] | None,
        source_modified_at: datetime | None,
        res: ApplyResult,
    ) -> str | None:
        entity_id, _ = self._map_entity(rec, parent_entity_id)
        if entity_id is None:
            res.warnings.append(f"delete of unknown object {rec.key.external_key()}")
            return None
        ent = self._entity(entity_id)
        head = self._version(ent["head_version_id"]) if ent else None
        if rec.deleted and ent is not None:  # a delete envelope has no payload: keep the last known kind
            rec = rec.model_copy(update={"kind": ent["kind_id"]})
        ensure_kind(self.conn, self.t, rec.kind)
        acl = sorted(set(rec.acl if rec.acl is not None else (default_acl or [])))
        tx_at, tx_seq = self.txc.tx_at, self.txc.tx_seq

        if rec.deleted:
            if head is None or head["op"] == "delete":
                res.unchanged.append(entity_id)
                return entity_id
            fields: dict[str, Any] = {}
            body = None
            content_hash = "tombstone"
            changed = ["@deleted"]
        else:
            fields = _plain(rec.fields)
            body = rec.body
            relations = _plain(
                {"s": [s.model_dump(exclude_none=True) for s in rec.statements],
                 "c": [c.model_dump(exclude_none=True) for c in rec.causes]}
            )
            content_hash = sha256_hex(canonical_json(
                {"kind": rec.kind, "title": rec.title, "fields": fields, "body": body, "rel": relations,
                 "valid_from": rec.valid_from}
            ))
            if head is not None and head["op"] != "delete" and head["content_hash"] == content_hash:
                if ent and sorted(ent["acl"] or []) != acl:
                    self.set_acl(entity_id, acl)
                res.unchanged.append(entity_id)
                return entity_id
            if head is None or head["op"] == "delete":
                changed = sorted(set(fields) | {"title", "body"})
            else:
                old = head["content"] or {}
                changed = sorted(
                    {f for f in set(old) | set(fields) if not f.startswith("@") and old.get(f) != fields.get(f)}
                    | ({"title"} if head["title"] != rec.title else set())
                    | ({"body"} if (head["body"] or None) != (body or None) else set())
                    | ({"@relations"} if (old.get("@relations_hash") != short_hash(relations)) else set())
                )
            fields_store = dict(fields)
            fields_store["@relations_hash"] = short_hash(relations)
            fields = fields_store

        seq = (head["seq"] + 1) if head else 1
        version_id = f"{entity_id}@{seq}"
        op = "delete" if rec.deleted else ("create" if head is None or head["op"] == "delete" else "update")
        valid_from = rec.valid_from or source_modified_at or tx_at
        actor_id = self.resolve_ref(rec.actor) if rec.actor else None
        if rec.valid_from is None and not rec.deleted:
            k = "valid_from_from_source_time" if source_modified_at else "valid_from_from_tx"
            res.empty[k] = res.empty.get(k, 0) + 1
        if actor_id is None and not rec.deleted:
            res.empty["no_author"] = res.empty.get("no_author", 0) + 1

        if ent is None:
            self._x(
                """INSERT INTO entity (tenant_id, entity_id, kind_id, title, source_id, url, acl, created_tx, head_seq,
                                       head_version_id, updated_tx) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (self.t, entity_id, rec.kind, rec.title, rec.key.source_id, rec.url, acl, tx_seq, seq, version_id, tx_seq),
            )
        if head is not None:
            self._x(
                """UPDATE entity_version SET tx = tstzrange(lower(tx), %s, '[)'), tx_to_seq = %s
                   WHERE tenant_id = %s AND version_id = %s AND upper_inf(tx)""",
                (tx_at, tx_seq, self.t, head["version_id"]),
            )
        self._x(
            """INSERT INTO entity_version (tenant_id, version_id, entity_id, seq, op, kind_id, title, content, body,
                 content_hash, changed_fields, valid_from, tx, tx_seq, actor_id, source_revision_id, source_modified_at, acl)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, tstzrange(%s, NULL, '[)'), %s, %s, %s, %s, %s)""",
            (self.t, version_id, entity_id, seq, op, rec.kind, rec.title if not rec.deleted else (head or {}).get("title"),
             None if rec.deleted else Jsonb(fields), body, content_hash, changed, valid_from, tx_at, tx_seq,
             actor_id, source_revision_id, source_modified_at, acl),
        )
        if ent is not None:
            self._x(
                """UPDATE entity SET kind_id = %s, title = COALESCE(%s, title), url = COALESCE(%s, url), acl = %s,
                     head_seq = %s, head_version_id = %s, updated_tx = %s,
                     tombstone_tx = CASE WHEN %s THEN %s ELSE NULL END
                   WHERE tenant_id = %s AND entity_id = %s""",
                (rec.kind, None if rec.deleted else rec.title, rec.url, acl, seq, version_id, tx_seq,
                 rec.deleted, tx_seq, self.t, entity_id),
            )
            if sorted(ent["acl"] or []) != acl:
                self._event(TOPIC_ENTITY_ACL, entity_id, {"entity_id": entity_id, "acl": acl})
        self._x(
            """UPDATE source_object SET last_revision_id = COALESCE(%s, last_revision_id), lifecycle = %s
               WHERE tenant_id = %s AND source_id = %s AND object_type = %s AND external_id = %s""",
            (source_revision_id, "deleted" if rec.deleted else "active", self.t, rec.key.source_id, rec.key.object_type,
             rec.key.external_id),
        )
        self._event(
            TOPIC_ENTITY_VERSION,
            version_id,
            {"entity_id": entity_id, "version_id": version_id, "seq": seq, "op": op, "kind": rec.kind,
             "changed_fields": changed},
        )
        res.entities.append(entity_id)
        res.versions.append(version_id)

        new_version = {"version_id": version_id, "seq": seq, "valid_from": valid_from}
        self._sync_aliases(entity_id, rec, new_version, origin)
        self._sync_chunks(entity_id, None if rec.deleted else body, seq)
        self._sync_statements(entity_id, [] if rec.deleted else rec.statements, new_version, origin, source_revision_id,
                              deleted=rec.deleted)
        if not rec.deleted:
            self._sync_causes(entity_id, rec.causes, new_version, origin, source_revision_id, res)
        else:
            self._drop_pending_refs(entity_id, origin=None, keep=set())
        if op == "create":
            self._resolve_pending_for(entity_id, rec.key, version_id, res)
        return entity_id

    # ------------------------------------------------------------------ aliases
    def _sync_aliases(self, entity_id: str, rec: CanonicalRecord, ver: dict, origin: str) -> None:
        desired: dict[tuple[str, str], str] = {}
        if not rec.deleted:
            for a in [*rec.aliases]:
                desired[(norm(a.alias), a.kind)] = a.alias
            if rec.title:
                desired.setdefault((norm(rec.title), "title"), rec.title)
        rows = self._x(
            """SELECT ctid, alias_norm, alias_kind FROM alias
               WHERE tenant_id = %s AND entity_id = %s AND origin = %s AND upper_inf(tx)""",
            (self.t, entity_id, origin),
        ).fetchall()
        have = {(r["alias_norm"], r["alias_kind"]): r for r in rows}
        for k, r in have.items():
            if k not in desired:  # historical alias: its validity ends where the new version starts
                self._x(
                    """UPDATE alias SET tx = tstzrange(lower(tx), %s, '[)'),
                         valid = tstzrange(lower(valid), GREATEST(lower(valid), %s), '[)')
                       WHERE ctid = %s""",
                    (self.txc.tx_at, ver["valid_from"], r["ctid"]),
                )
        for (an, kind), alias in desired.items():
            if (an, kind) in have:
                continue
            self._x(
                """INSERT INTO alias (tenant_id, entity_id, alias, alias_norm, alias_kind, valid, tx, tx_seq, origin)
                   VALUES (%s, %s, %s, %s, %s, tstzrange(%s, NULL, '[)'), tstzrange(%s, NULL, '[)'), %s, %s)""",
                (self.t, entity_id, alias, an, kind, ver["valid_from"], self.txc.tx_at, self.txc.tx_seq, origin),
            )

    # ------------------------------------------------------------------ chunks
    def _sync_chunks(self, entity_id: str, body: str | None, seq: int) -> None:
        new = chunk_text(body) if body else []
        rows = self._x(
            "SELECT chunk_id, content_hash FROM chunk WHERE tenant_id = %s AND entity_id = %s AND version_to_seq IS NULL",
            (self.t, entity_id),
        ).fetchall()
        open_by_hash = {r["content_hash"]: r["chunk_id"] for r in rows}
        new_hashes = {c.content_hash for c in new}
        closed = [cid for h, cid in open_by_hash.items() if h not in new_hashes]
        if closed:
            self._x(
                """UPDATE chunk SET version_to_seq = %s, tx_to = %s
                   WHERE tenant_id = %s AND chunk_id = ANY(%s)""",
                (seq - 1, self.txc.tx_at, self.t, closed),
            )
        opened = []
        seen: set[str] = set()
        for c in new:
            if c.content_hash in open_by_hash or c.content_hash in seen:
                continue
            seen.add(c.content_hash)
            chunk_id = f"{entity_id}#{seq}.{c.ordinal}"
            self._x(
                """INSERT INTO chunk (tenant_id, chunk_id, entity_id, version_from_seq, tx_from, ordinal, section, text,
                                      char_start, char_end, content_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (self.t, chunk_id, entity_id, seq, self.txc.tx_at, c.ordinal, c.section, c.text, c.char_start,
                 c.char_end, c.content_hash),
            )
            opened.append(chunk_id)
        if opened or closed:
            self._event(TOPIC_CHUNKS, entity_id, {"entity_id": entity_id, "opened": opened, "closed": closed})

    # ------------------------------------------------------------------ statements
    def _stmt_key(self, subject_id: str, spec: StatementSpec, origin: str) -> str:
        target = _key_target(spec.object) if spec.object else None
        literal = _plain(spec.literal) if spec.object is None else None
        return short_hash(subject_id, spec.predicate, target, literal, _plain(spec.qualifiers), origin, n=24)

    def _sync_statements(
        self, subject_id: str, specs: list[StatementSpec], ver: dict, origin: str, rev_id: int | None, *, deleted: bool
    ) -> None:
        opened: list[int] = []
        closed: list[int] = []
        if deleted:  # the source no longer asserts anything about a deleted object
            rows = self._x(
                f"""UPDATE statement SET tx = tstzrange(lower(tx), %s, '[)'), tx_to_seq = %s, {_CLOSE_KNOWN}
                   WHERE tenant_id = %s AND subject_id = %s AND upper_inf(tx) RETURNING stmt_id""",
                (self.txc.tx_at, self.txc.tx_seq, self.txc.k, self.t, subject_id),
            ).fetchall()
            closed = [r["stmt_id"] for r in rows]
        else:
            desired = {self._stmt_key(subject_id, s, origin): s for s in specs}
            rows = self._x(
                """SELECT stmt_id, stmt_key FROM statement
                   WHERE tenant_id = %s AND subject_id = %s AND origin = %s AND upper_inf(tx)""",
                (self.t, subject_id, origin),
            ).fetchall()
            have = {r["stmt_key"]: r["stmt_id"] for r in rows}
            pending = self._pending_keys(subject_id, "statement", origin)
            for key, sid in have.items():
                if key not in desired:
                    self._x(
                        f"""UPDATE statement SET tx = tstzrange(lower(tx), %s, '[)'), tx_to_seq = %s, {_CLOSE_KNOWN}
                           WHERE tenant_id = %s AND stmt_id = %s""",
                        (self.txc.tx_at, self.txc.tx_seq, self.txc.k, self.t, sid),
                    )
                    closed.append(sid)
            for key, spec in desired.items():
                if key in have or key in pending:
                    continue
                obj_id = None
                if spec.object is not None:
                    obj_id = self.resolve_ref(spec.object)
                    if obj_id is None:
                        self._park(subject_id, ver["version_id"], "statement", key, spec, origin, spec.object)
                        continue
                sid = self._insert_statement(subject_id, ver, spec, obj_id, key, origin, rev_id)
                opened.append(sid)
            self._drop_pending_refs(subject_id, origin, keep=set(desired), ref_kind="statement")
        if opened or closed:
            self._event(TOPIC_STATEMENTS, subject_id, {"subject_id": subject_id, "opened": opened, "closed": closed})

    def _insert_statement(
        self, subject_id: str, ver: dict, spec: StatementSpec, obj_id: str | None, key: str, origin: str,
        rev_id: int | None,
    ) -> int:
        ensure_predicate(self.conn, self.t, spec.predicate)
        valid_from = spec.valid_from or ver["valid_from"]
        prov = {"source_revision_id": rev_id}
        if spec.evidence:
            prov["evidence"] = spec.evidence.model_dump(exclude_none=True)
        row = self._x(
            """INSERT INTO statement (tenant_id, stmt_key, subject_id, subject_version_id, predicate_id, object_id,
                 object_literal, qualifiers, valid, tx, known, tx_seq, origin, confidence, provenance)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, tstzrange(%s, %s, '[)'), tstzrange(%s, NULL, '[)'),
                       tstzrange(%s, NULL, '[)'), %s, %s, %s, %s)
               RETURNING stmt_id""",
            (self.t, key, subject_id, ver["version_id"], spec.predicate, obj_id,
             None if obj_id else Jsonb(_plain(spec.literal)), Jsonb(_plain(spec.qualifiers)), valid_from, spec.valid_to,
             self.txc.tx_at, self.txc.k, self.txc.tx_seq, origin, spec.confidence, Jsonb(prov)),
        ).fetchone()
        return row["stmt_id"]

    # ------------------------------------------------------------------ causal links
    def _link_key(self, effect_entity_id: str, predicate: str, cause: Ref | str, origin: str) -> str:
        target = cause if isinstance(cause, str) else _key_target(cause)
        return short_hash(effect_entity_id, predicate, target, origin, n=24)

    def _sync_causes(
        self, effect_id: str, specs: list[CausalSpec], ver: dict, origin: str, rev_id: int | None, res: ApplyResult
    ) -> None:
        desired = {self._link_key(effect_id, s.predicate, s.cause, origin): s for s in specs}
        rows = self._x(
            """SELECT link_id, link_key FROM causal_link
               WHERE tenant_id = %s AND effect_entity_id = %s AND origin = %s AND upper_inf(tx)""",
            (self.t, effect_id, origin),
        ).fetchall()
        have = {r["link_key"]: r["link_id"] for r in rows}
        pending = self._pending_keys(effect_id, "causal", origin)
        opened, closed = [], []
        for key, lid in have.items():
            if key not in desired:
                self._close_link(lid, "source_removed")
                closed.append(lid)
        for key, spec in desired.items():
            if key in have or key in pending:
                continue
            cause_id = self.resolve_ref(spec.cause)
            if cause_id is None:
                self._park(effect_id, ver["version_id"], "causal", key, spec, origin, spec.cause)
                continue
            lid = self._insert_link_from_spec(effect_id, ver["version_id"], ver["valid_from"], cause_id, spec, key,
                                              origin, rev_id, res)
            if lid:
                opened.append(lid)
        self._drop_pending_refs(effect_id, origin, keep=set(desired), ref_kind="causal")
        res.links_opened += opened
        res.links_closed += closed
        if opened or closed:
            self._event(TOPIC_CAUSAL, effect_id, {"effect_entity_id": effect_id, "opened": opened, "closed": closed})

    def _cause_version(self, cause_id: str, explicit_seq: int | None) -> str | None:
        if explicit_seq:
            v = self._version(f"{cause_id}@{explicit_seq}")
            if v:
                return v["version_id"]
        ent = self._entity(cause_id)
        return ent["head_version_id"] if ent else None

    def _creates_cycle(self, effect_version: str, cause_version: str) -> bool:
        """Acyclicity holds on the version graph (research §2.3); entity-level cycles are legitimate
        (EXP-7@5 -> dec@1 -> meeting@1 -> EXP-7@4). Reject only if the effect version is already a cause of the
        cause version through accepted open links, or both sides are versions of one entity."""
        if effect_version.rsplit("@", 1)[0] == cause_version.rsplit("@", 1)[0]:
            return True
        row = self._x(
            """WITH RECURSIVE up(v, depth) AS (
                 SELECT cause_version, 1 FROM causal_link
                 WHERE tenant_id = %(t)s AND effect_version = %(c)s AND upper_inf(tx) AND review_status = 'accepted'
                 UNION
                 SELECT l.cause_version, up.depth + 1 FROM causal_link l JOIN up ON l.effect_version = up.v
                 WHERE l.tenant_id = %(t)s AND upper_inf(l.tx) AND l.review_status = 'accepted' AND up.depth < %(d)s)
               SELECT 1 FROM up WHERE v = %(e)s LIMIT 1""",
            {"t": self.t, "c": cause_version, "e": effect_version, "d": _CYCLE_DEPTH},
        ).fetchone()
        return row is not None

    def _insert_link_from_spec(
        self, effect_id: str, effect_version: str, effect_valid_from: datetime, cause_id: str, spec: CausalSpec,
        key: str, origin: str, rev_id: int | None, res: ApplyResult,
    ) -> str | None:
        info, _ = ensure_predicate(self.conn, self.t, spec.predicate, causal_kind="depends_on")
        causal_kind = info.causal_kind or "depends_on"
        cause_version = self._cause_version(cause_id, spec.cause_version_seq)
        if cause_version is None:
            return None
        if self._creates_cycle(effect_version, cause_version):
            res.warnings.append(f"cycle rejected: {effect_id} -{spec.predicate}-> {cause_id}")
            return None
        sensitivity = spec.sensitivity if spec.sensitivity is not None else (
            list(info.typical_sensitivity) if info.typical_sensitivity is not None else None)
        evidence: dict[str, Any] = {"source_revision_id": rev_id}
        if spec.evidence:
            evidence.update(spec.evidence.model_dump(exclude_none=True))
        return self._insert_link(
            effect_id=effect_id, effect_version=effect_version, cause_id=cause_id, cause_version=cause_version,
            predicate=spec.predicate, causal_kind=causal_kind, sensitivity=sensitivity,
            valid_from=spec.valid_from or effect_valid_from, valid_to=spec.valid_to, origin=origin,
            confidence=spec.confidence, review_status="accepted", evidence=evidence, link_key=key,
        )

    def _insert_link(
        self, *, effect_id: str, effect_version: str, cause_id: str, cause_version: str, predicate: str,
        causal_kind: str, sensitivity: list[str] | None, valid_from: datetime, valid_to: datetime | None, origin: str,
        confidence: float, review_status: str, evidence: dict, link_key: str, revalidates: str | None = None,
    ) -> str:
        link_id = self._next_link_id()
        self._x(
            """INSERT INTO causal_link (tenant_id, link_id, link_key, effect_entity_id, effect_version, cause_entity_id,
                 cause_version, causal_kind, predicate_id, sensitivity, valid, tx, known, tx_seq, origin, confidence,
                 review_status, evidence, revalidates)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, tstzrange(%s, %s, '[)'), tstzrange(%s, NULL, '[)'),
                       tstzrange(%s, NULL, '[)'), %s, %s, %s, %s, %s, %s)""",
            (self.t, link_id, link_key, effect_id, effect_version, cause_id, cause_version, causal_kind, predicate,
             sensitivity, valid_from, valid_to, self.txc.tx_at, self.txc.k, self.txc.tx_seq, origin, confidence,
             review_status, Jsonb(_plain(evidence)), revalidates),
        )
        return link_id

    def _close_link(self, link_id: str, reason: str, *, never_known: bool = False) -> None:
        """Close a link at the transaction; `never_known`: it is withdrawn as if it had never been derived (its
        knowledge interval becomes empty)."""
        known = "known = 'empty'::tstzrange" if never_known else _CLOSE_KNOWN
        self._x(
            f"""UPDATE causal_link SET tx = tstzrange(lower(tx), %s, '[)'), tx_to_seq = %s, retract_reason = %s, {known}
               WHERE tenant_id = %s AND link_id = %s AND upper_inf(tx)""",
            (self.txc.tx_at, self.txc.tx_seq, reason, *(() if never_known else (self.txc.k,)), self.t, link_id),
        )

    # ------------------------------------------------------------------ unresolved references
    def _pending_keys(self, from_entity_id: str, ref_kind: str, origin: str) -> set[str]:
        rows = self._x(
            """SELECT ref_key FROM unresolved_ref WHERE tenant_id = %s AND from_entity_id = %s AND ref_kind = %s
                 AND origin = %s AND status = 'pending'""",
            (self.t, from_entity_id, ref_kind, origin),
        ).fetchall()
        return {r["ref_key"] for r in rows}

    def _park(self, from_entity_id: str, from_version_id: str, ref_kind: str, key: str, spec: Any, origin: str,
              target: Ref) -> None:
        self._x(
            """INSERT INTO unresolved_ref (tenant_id, from_entity_id, from_version_id, ref_kind, ref_key, spec, origin,
                 target_source_id, target_object_type, target_external_id, target_entity_id, created_tx)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (self.t, from_entity_id, from_version_id, ref_kind, key, Jsonb(_plain(spec.model_dump(exclude_none=True))),
             origin, target.source_id, target.object_type, target.external_id, target.entity_id, self.txc.tx_seq),
        )

    def _drop_pending_refs(self, from_entity_id: str, origin: str | None, keep: set[str], ref_kind: str | None = None) -> None:
        self._x(
            """UPDATE unresolved_ref SET status = 'dropped', closed_tx = %s
               WHERE tenant_id = %s AND from_entity_id = %s AND status = 'pending'
                 AND (%s::text IS NULL OR origin = %s) AND (%s::text IS NULL OR ref_kind = %s)
                 AND NOT (ref_key = ANY(%s::text[]))""",
            (self.txc.tx_seq, self.t, from_entity_id, origin, origin, ref_kind, ref_kind, list(keep)),
        )

    def _resolve_pending_for(self, entity_id: str, key: Ref, version_id: str, res: ApplyResult) -> None:
        rows = self._x(
            """SELECT * FROM unresolved_ref WHERE tenant_id = %s AND status = 'pending'
                 AND ((target_source_id = %s AND target_object_type = %s AND target_external_id = %s)
                      OR target_entity_id = %s)
               ORDER BY id""",
            (self.t, key.source_id, key.object_type, key.external_id, entity_id),
        ).fetchall()
        for r in rows:
            src = self._entity(r["from_entity_id"])
            ver = self._version(r["from_version_id"])
            if src is None or src["tombstone_tx"] is not None or ver is None:
                status = "dropped"
            elif r["ref_kind"] == "statement":
                spec = StatementSpec.model_validate(r["spec"])
                sid = self._insert_statement(r["from_entity_id"], ver, spec, entity_id, r["ref_key"], r["origin"],
                                             ver["source_revision_id"])
                self._event(TOPIC_STATEMENTS, r["from_entity_id"],
                            {"subject_id": r["from_entity_id"], "opened": [sid], "closed": []})
                status = "resolved"
            else:
                spec = CausalSpec.model_validate(r["spec"])
                lid = self._insert_link_from_spec(r["from_entity_id"], r["from_version_id"], ver["valid_from"],
                                                  entity_id, spec, r["ref_key"], r["origin"],
                                                  ver["source_revision_id"], res)
                if lid:
                    res.links_opened.append(lid)
                    self._event(TOPIC_CAUSAL, r["from_entity_id"],
                                {"effect_entity_id": r["from_entity_id"], "opened": [lid], "closed": []})
                status = "resolved" if lid else "dropped"
            self._x(
                "UPDATE unresolved_ref SET status = %s, closed_tx = %s, target_entity_id = %s WHERE tenant_id = %s AND id = %s",
                (status, self.txc.tx_seq, entity_id, self.t, r["id"]),
            )

    # ------------------------------------------------------------------ ACL
    def set_acl(self, entity_id: str, acl: list[str]) -> None:
        acl = sorted(set(acl))
        self._x("UPDATE entity SET acl = %s, updated_tx = %s WHERE tenant_id = %s AND entity_id = %s",
                (acl, self.txc.tx_seq, self.t, entity_id))
        self._event(TOPIC_ENTITY_ACL, entity_id, {"entity_id": entity_id, "acl": acl})

    # ------------------------------------------------------------------ editor and review operations
    def declare_link(
        self, *, effect_entity_id: str, cause_entity_id: str, predicate: str, origin: str = "declared:editor",
        sensitivity: list[str] | None = None, effect_version: str | None = None, cause_version: str | None = None,
        evidence: dict | None = None, confidence: float = 1.0, review_status: str = "accepted",
        valid_from: datetime | None = None,
    ) -> str:
        """Declare effect <- cause with version binding at save time (default: both head versions)."""
        eff = self._entity(effect_entity_id)
        cau = self._entity(cause_entity_id)
        if eff is None or cau is None:
            raise JournalError("unknown effect or cause entity")
        effect_version = effect_version or eff["head_version_id"]
        cause_version = cause_version or cau["head_version_id"]
        key = self._link_key(effect_entity_id, predicate, f"entity:{cause_entity_id}", origin)
        existing = self._x(
            "SELECT link_id FROM causal_link WHERE tenant_id = %s AND link_key = %s AND upper_inf(tx)", (self.t, key)
        ).fetchone()
        if existing:
            return existing["link_id"]
        if review_status == "accepted" and self._creates_cycle(effect_version, cause_version):
            raise JournalError(f"cycle: {cause_entity_id} already depends on {effect_entity_id}")
        info, _ = ensure_predicate(self.conn, self.t, predicate, causal_kind="depends_on")
        if sensitivity is None and info.typical_sensitivity is not None:
            sensitivity = list(info.typical_sensitivity)
        ev = self._version(effect_version)
        lid = self._insert_link(
            effect_id=effect_entity_id, effect_version=effect_version, cause_id=cause_entity_id,
            cause_version=cause_version, predicate=predicate, causal_kind=info.causal_kind or "depends_on",
            sensitivity=sensitivity, valid_from=valid_from or (ev["valid_from"] if ev else self.txc.tx_at), valid_to=None,
            origin=origin, confidence=confidence, review_status=review_status, evidence=evidence or {}, link_key=key,
        )
        self._event(TOPIC_CAUSAL, effect_entity_id, {"effect_entity_id": effect_entity_id, "opened": [lid], "closed": []})
        return lid

    def propose_link(
        self, *, effect_version: str, cause_version: str, predicate: str, origin: str, confidence: float,
        evidence: dict,
    ) -> str | None:
        """Add an inferred candidate (review_status=pending). Skips duplicates and previously rejected candidates."""
        ev, cv = self._version(effect_version), self._version(cause_version)
        if ev is None or cv is None or ev["entity_id"] == cv["entity_id"]:
            return None
        key = self._link_key(ev["entity_id"], predicate, f"entity:{cv['entity_id']}", origin)
        k = self.txc.k
        # what is known at the logical time of the transaction (a row known later does not exist yet for it)
        seen = self._x(
            """SELECT 1 FROM causal_link WHERE tenant_id = %s AND link_key = %s AND lower(known) <= %s
                 AND NOT isempty(known)
                 AND (upper_inf(known) OR upper(known) > %s OR retract_reason IN ('rejected', 'accepted')) LIMIT 1""",
            (self.t, key, k, k),
        ).fetchone()
        # an accepted link between the same pair already explains it, whatever its predicate
        known = self._x(
            """SELECT 1 FROM causal_link WHERE tenant_id = %s AND effect_entity_id = %s AND cause_entity_id = %s
                 AND known @> %s::timestamptz AND review_status = 'accepted' AND origin NOT LIKE 'inferred:%%'
               LIMIT 1""",
            (self.t, ev["entity_id"], cv["entity_id"], k),
        ).fetchone()
        if known:
            return None
        if seen:
            return None
        info, _ = ensure_predicate(self.conn, self.t, predicate, causal_kind="caused_by")
        lid = self._insert_link(
            effect_id=ev["entity_id"], effect_version=effect_version, cause_id=cv["entity_id"],
            cause_version=cause_version, predicate=predicate, causal_kind=info.causal_kind or "caused_by",
            sensitivity=list(info.typical_sensitivity) if info.typical_sensitivity else None,
            valid_from=ev["valid_from"], valid_to=None, origin=origin, confidence=confidence, review_status="pending",
            evidence=evidence, link_key=key,
        )
        self._event(TOPIC_CAUSAL, ev["entity_id"], {"effect_entity_id": ev["entity_id"], "opened": [lid], "closed": []})
        return lid

    def _link(self, link_id: str) -> dict:
        row = self._x("SELECT * FROM causal_link WHERE tenant_id = %s AND link_id = %s", (self.t, link_id)).fetchone()
        if row is None:
            raise JournalError(f"unknown link {link_id}")
        return row

    def _review(self, target_id: str, decision: str, reviewer: str, reason: str | None, created: str | None) -> None:
        self._x(
            """INSERT INTO review_decision (tenant_id, target_type, target_id, decision, reviewer, reason, tx_seq,
                 created_link_id) VALUES (%s, 'causal_link', %s, %s, %s, %s, %s, %s)""",
            (self.t, target_id, decision, reviewer, reason, self.txc.tx_seq, created),
        )

    def retract_link(self, link_id: str, *, reviewer: str, reason: str) -> None:
        link = self._link(link_id)
        if link["tx"].upper is not None:
            raise JournalError("link already closed")
        self._close_link(link_id, f"retracted: {reason}")
        self._review(link_id, "retract", reviewer, reason, None)
        self._event(TOPIC_CAUSAL, link["effect_entity_id"],
                    {"effect_entity_id": link["effect_entity_id"], "opened": [], "closed": [link_id]})

    def withdraw_candidate(self, link_id: str, *, reason: str) -> None:
        """Close an open pending inferred candidate that another candidate for the same pair replaces. Not a review
        decision: the pair stays open to proposals (a parallel drain keeps the candidate of the earliest source)."""
        link = self._link(link_id)
        if link["tx"].upper is not None or link["review_status"] != "pending" or not link["origin"].startswith("inferred:"):
            raise JournalError("only an open pending inferred candidate can be withdrawn")
        self._close_link(link_id, f"withdrawn: {reason}", never_known=True)
        self._event(TOPIC_CAUSAL, link["effect_entity_id"],
                    {"effect_entity_id": link["effect_entity_id"], "opened": [], "closed": [link_id]})

    def revalidate_link(self, link_id: str, *, reviewer: str, reason: str | None = None) -> str:
        """The editor confirms the effect still holds for the cause's current version: a NEW link to the cause
        head with `revalidates` -> old; the old one is closed and kept (research §2.3)."""
        link = self._link(link_id)
        if link["tx"].upper is not None:
            raise JournalError("link already closed")
        cause = self._entity(link["cause_entity_id"])
        if cause is None or cause["tombstone_tx"] is not None:
            raise JournalError("cause entity is deleted")
        self._close_link(link_id, "revalidated")
        evidence = dict(link["evidence"] or {})
        evidence.update({"revalidated_by": reviewer, "revalidated_from": link["cause_version"]})
        new_id = self._insert_link(
            effect_id=link["effect_entity_id"], effect_version=link["effect_version"],
            cause_id=link["cause_entity_id"], cause_version=cause["head_version_id"], predicate=link["predicate_id"],
            causal_kind=link["causal_kind"], sensitivity=link["sensitivity"], valid_from=link["valid"].lower,
            valid_to=link["valid"].upper, origin=link["origin"], confidence=link["confidence"],
            review_status=link["review_status"], evidence=evidence, link_key=link["link_key"], revalidates=link_id,
        )
        self._review(link_id, "revalidate", reviewer, reason, new_id)
        self._event(TOPIC_CAUSAL, link["effect_entity_id"],
                    {"effect_entity_id": link["effect_entity_id"], "opened": [new_id], "closed": [link_id]})
        return new_id

    def accept_candidate(self, link_id: str, *, reviewer: str, reason: str | None = None) -> str:
        """Accepting an inferred candidate creates a new declared:review link that references it."""
        cand = self._link(link_id)
        if cand["review_status"] != "pending" or cand["tx"].upper is not None:
            raise JournalError("not a pending candidate")
        if self._creates_cycle(cand["effect_version"], cand["cause_version"]):
            raise JournalError("accepting would create a cycle")
        self._close_link(link_id, "accepted")
        self._x("UPDATE causal_link SET review_status = 'accepted' WHERE tenant_id = %s AND link_id = %s", (self.t, link_id))
        evidence = dict(cand["evidence"] or {})
        evidence.update({"candidate": link_id, "candidate_origin": cand["origin"], "accepted_by": reviewer})
        origin = "declared:review"
        key = self._link_key(cand["effect_entity_id"], cand["predicate_id"], f"entity:{cand['cause_entity_id']}", origin)
        dup = self._x("SELECT link_id FROM causal_link WHERE tenant_id = %s AND link_key = %s AND upper_inf(tx)",
                      (self.t, key)).fetchone()
        if dup:
            self._review(link_id, "accept", reviewer, reason, dup["link_id"])
            return dup["link_id"]
        new_id = self._insert_link(
            effect_id=cand["effect_entity_id"], effect_version=cand["effect_version"],
            cause_id=cand["cause_entity_id"], cause_version=cand["cause_version"], predicate=cand["predicate_id"],
            causal_kind=cand["causal_kind"], sensitivity=cand["sensitivity"], valid_from=cand["valid"].lower,
            valid_to=None, origin=origin, confidence=1.0, review_status="accepted", evidence=evidence, link_key=key,
        )
        self._review(link_id, "accept", reviewer, reason, new_id)
        self._event(TOPIC_CAUSAL, cand["effect_entity_id"],
                    {"effect_entity_id": cand["effect_entity_id"], "opened": [new_id], "closed": [link_id]})
        return new_id

    def reject_candidate(self, link_id: str, *, reviewer: str, reason: str | None = None) -> None:
        """Rejected candidates stay as negative examples (closed, review_status=rejected)."""
        cand = self._link(link_id)
        if cand["review_status"] != "pending":
            raise JournalError("not a pending candidate")
        self._close_link(link_id, "rejected")
        self._x("UPDATE causal_link SET review_status = 'rejected' WHERE tenant_id = %s AND link_id = %s", (self.t, link_id))
        self._review(link_id, "reject", reviewer, reason, None)
        self._event(TOPIC_CAUSAL, cand["effect_entity_id"],
                    {"effect_entity_id": cand["effect_entity_id"], "opened": [], "closed": [link_id]})

    # ------------------------------------------------------------------ derived statements (extraction)
    def sync_derived_statements(self, subject_id: str, specs: list[StatementSpec], origin: str,
                                version_id: str | None = None) -> None:
        """Replace the open statements of one derived origin (e.g. derived:mention@1) of the subject: those of the
        given version (extraction: the head at the logical time), by default of the head."""
        if version_id is None:
            ent = self._entity(subject_id)
            if ent is None:
                return
            version_id = ent["head_version_id"]
        ver = self._version(version_id)
        self._sync_statements(subject_id, specs, ver, origin, ver.get("source_revision_id"), deleted=False)

    def close_derived_statements(self, subject_id: str, origin: str) -> int:
        """Close the open statements of one derived origin of a subject that was deleted at the logical time."""
        rows = self._x(
            f"""UPDATE statement SET tx = tstzrange(lower(tx), %s, '[)'), tx_to_seq = %s, {_CLOSE_KNOWN}
               WHERE tenant_id = %s AND subject_id = %s AND origin = %s AND upper_inf(tx) RETURNING stmt_id""",
            (self.txc.tx_at, self.txc.tx_seq, self.txc.k, self.t, subject_id, origin),
        ).fetchall()
        if rows:
            self._event(TOPIC_STATEMENTS, subject_id,
                        {"subject_id": subject_id, "opened": [], "closed": [r["stmt_id"] for r in rows]})
        return len(rows)
