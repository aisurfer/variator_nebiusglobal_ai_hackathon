-- Plancy KB journal (source of truth, KB-1). Append-only: the only allowed mutation of a journal row is
-- closing its tx range (and tx_to_seq). Projections (OpenSearch, Memgraph) are rebuilt from these tables.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------------------------------------------------------------- tenants, sequencer, principals
CREATE TABLE tenant (
  tenant_id   text PRIMARY KEY,
  name        text NOT NULL,
  settings    jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- One sequencer row per tenant. A journal transaction locks it first: writes of a tenant are serialised,
-- tx time is monotonic and tx_seq order equals commit order.
CREATE TABLE tenant_clock (
  tenant_id    text PRIMARY KEY REFERENCES tenant ON DELETE CASCADE,
  last_tx_at   timestamptz,
  last_tx_seq  bigint NOT NULL DEFAULT 0
);

CREATE TABLE tx_log (
  tenant_id  text NOT NULL REFERENCES tenant ON DELETE CASCADE,
  tx_seq     bigint NOT NULL,
  tx_at      timestamptz NOT NULL,
  kind       text NOT NULL,            -- ingest | editor | review | extract | admin
  actor_id   text,
  ref        jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, tx_seq)
);
CREATE INDEX tx_log_at ON tx_log (tenant_id, tx_at);

CREATE TABLE principal (
  tenant_id         text NOT NULL REFERENCES tenant ON DELETE CASCADE,
  principal_id      text NOT NULL,     -- user:sofia.lind | grp:exec | service:worker
  kind              text NOT NULL CHECK (kind IN ('user', 'group', 'service')),
  display_name      text,
  person_entity_id  text,
  attrs             jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, principal_id)
);

CREATE TABLE membership (
  tenant_id  text NOT NULL,
  group_id   text NOT NULL,
  member_id  text NOT NULL,
  PRIMARY KEY (tenant_id, group_id, member_id),
  FOREIGN KEY (tenant_id, group_id) REFERENCES principal ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, member_id) REFERENCES principal ON DELETE CASCADE
);

-- ---------------------------------------------------------------- sources and ingestion
CREATE TABLE source_instance (
  tenant_id     text NOT NULL REFERENCES tenant ON DELETE CASCADE,
  source_id     text NOT NULL,         -- jira-vf
  provider      text NOT NULL,         -- jira | confluence | gitlab | xwiki | liferay | directory
  display_name  text NOT NULL,
  base_url      text,
  config        jsonb NOT NULL DEFAULT '{}',   -- containers with their ACLs, key patterns
  status        text NOT NULL DEFAULT 'ready',
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, source_id)
);

-- Inbox: one row per delivery; delivery_id makes webhook/delta/backfill duplicates harmless.
CREATE TABLE ingest_event (
  id                  bigserial PRIMARY KEY,
  tenant_id           text NOT NULL,
  source_id           text NOT NULL,
  delivery_id         text NOT NULL,
  operation           text NOT NULL CHECK (operation IN ('upsert', 'delete', 'acl')),
  object_type         text NOT NULL,
  external_id         text NOT NULL,
  received_at         timestamptz NOT NULL DEFAULT now(),
  observed_at         timestamptz,
  status              text NOT NULL DEFAULT 'received',  -- received | processed | duplicate | unchanged | failed
  error               text,
  source_revision_id  bigint,
  tx_seq              bigint,
  UNIQUE (tenant_id, source_id, delivery_id),
  FOREIGN KEY (tenant_id, source_id) REFERENCES source_instance ON DELETE CASCADE
);

CREATE TABLE source_revision (
  id                  bigserial PRIMARY KEY,
  tenant_id           text NOT NULL,
  source_id           text NOT NULL,
  object_type         text NOT NULL,
  external_id         text NOT NULL,
  provider_version    text,
  content_hash        text NOT NULL,   -- sha256 of the raw payload
  blob_key            text,            -- content-addressed key in the blob store
  source_modified_at  timestamptz,
  observed_at         timestamptz NOT NULL,
  acl                 text[],
  FOREIGN KEY (tenant_id, source_id) REFERENCES source_instance ON DELETE CASCADE
);
CREATE INDEX source_revision_obj ON source_revision (tenant_id, source_id, object_type, external_id, id);

-- External identity -> stable entity. Several source objects may map to one entity (person accounts).
CREATE TABLE source_object (
  tenant_id         text NOT NULL,
  source_id         text NOT NULL,
  object_type       text NOT NULL,
  external_id       text NOT NULL,
  entity_id         text NOT NULL,
  parent_entity_id  text,              -- derived records (decisions of a meeting page) belong to a parent
  lifecycle         text NOT NULL DEFAULT 'active',    -- active | deleted | mapped | access_revoked
  first_seen_tx     bigint NOT NULL,
  last_revision_id  bigint,
  PRIMARY KEY (tenant_id, source_id, object_type, external_id)
);
CREATE INDEX source_object_entity ON source_object (tenant_id, entity_id);
CREATE INDEX source_object_parent ON source_object (tenant_id, parent_entity_id) WHERE parent_entity_id IS NOT NULL;

-- ---------------------------------------------------------------- open schema (KB element 1-2)
CREATE TABLE kind (
  tenant_id    text NOT NULL REFERENCES tenant ON DELETE CASCADE,
  kind_id      text NOT NULL,
  label        text NOT NULL,
  description  text,
  status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'proposed', 'deprecated')),
  PRIMARY KEY (tenant_id, kind_id)
);

CREATE TABLE predicate (
  tenant_id               text NOT NULL REFERENCES tenant ON DELETE CASCADE,
  predicate_id            text NOT NULL,
  label                   text NOT NULL,
  inverse_label           text,
  aliases                 text[] NOT NULL DEFAULT '{}',
  description             text,
  causal_kind             text CHECK (causal_kind IN ('caused_by', 'decided_because_of', 'depends_on', 'based_on',
                                                      'supersedes', 'precondition', 'informed_by')),
  typical_sensitivity     text[],
  domain_kinds            text[] NOT NULL DEFAULT '{}',
  range_kinds             text[] NOT NULL DEFAULT '{}',
  status                  text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'proposed', 'deprecated')),
  canonical_predicate_id  text,        -- non-destructive canonicalisation, applied at query time
  created_at              timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, predicate_id)
);

-- ---------------------------------------------------------------- entities and immutable versions
CREATE TABLE entity (
  tenant_id        text NOT NULL REFERENCES tenant ON DELETE CASCADE,
  entity_id        text NOT NULL,
  kind_id          text NOT NULL,
  title            text,
  source_id        text,
  url              text,
  acl              text[] NOT NULL DEFAULT '{}',
  created_tx       bigint NOT NULL,
  tombstone_tx     bigint,
  head_seq         int NOT NULL,
  head_version_id  text NOT NULL,
  updated_tx       bigint NOT NULL,
  PRIMARY KEY (tenant_id, entity_id)
);
CREATE INDEX entity_kind ON entity (tenant_id, kind_id);
CREATE INDEX entity_acl ON entity USING gin (acl);

-- valid_to is not stored: as known at K, a version is valid until the next version (by valid_from) recorded by K.
CREATE TABLE entity_version (
  tenant_id           text NOT NULL,
  version_id          text NOT NULL,   -- '<entity_id>@<seq>'
  entity_id           text NOT NULL,
  seq                 int NOT NULL,
  op                  text NOT NULL CHECK (op IN ('create', 'update', 'delete')),
  kind_id             text NOT NULL,
  title               text,
  content             jsonb,           -- normalised fields; NULL for a tombstone
  body                text,            -- searchable text
  content_hash        text NOT NULL,
  changed_fields      text[] NOT NULL DEFAULT '{}',
  valid_from          timestamptz NOT NULL,
  tx                  tstzrange NOT NULL,   -- [recorded, superseded)
  tx_seq              bigint NOT NULL,
  tx_to_seq           bigint,
  actor_id            text,
  source_revision_id  bigint,
  source_modified_at  timestamptz,
  acl                 text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, version_id),
  UNIQUE (tenant_id, entity_id, seq),
  FOREIGN KEY (tenant_id, entity_id) REFERENCES entity ON DELETE CASCADE
);
CREATE INDEX entity_version_tx ON entity_version USING gist (tenant_id, entity_id, tx);
CREATE INDEX entity_version_seq ON entity_version (tenant_id, tx_seq);
CREATE INDEX entity_version_valid ON entity_version (tenant_id, entity_id, valid_from);

CREATE TABLE alias (
  tenant_id   text NOT NULL,
  entity_id   text NOT NULL,
  alias       text NOT NULL,
  alias_norm  text NOT NULL,
  alias_kind  text NOT NULL,           -- key | title | name | email | username | label
  valid       tstzrange NOT NULL,
  tx          tstzrange NOT NULL,
  tx_seq      bigint NOT NULL,
  origin      text NOT NULL,
  FOREIGN KEY (tenant_id, entity_id) REFERENCES entity ON DELETE CASCADE
);
CREATE INDEX alias_norm_idx ON alias (tenant_id, alias_norm);
CREATE INDEX alias_entity_idx ON alias (tenant_id, entity_id);

-- Text spans of versions. An unchanged chunk is not duplicated: its version interval is extended.
CREATE TABLE chunk (
  tenant_id         text NOT NULL,
  chunk_id          text NOT NULL,
  entity_id         text NOT NULL,
  version_from_seq  int NOT NULL,
  version_to_seq    int,               -- NULL: part of the head version
  tx_from           timestamptz NOT NULL,
  tx_to             timestamptz,
  ordinal           int NOT NULL,
  section           text,
  text              text NOT NULL,
  char_start        int,
  char_end          int,
  content_hash      text NOT NULL,
  PRIMARY KEY (tenant_id, chunk_id),
  FOREIGN KEY (tenant_id, entity_id) REFERENCES entity ON DELETE CASCADE
);
CREATE INDEX chunk_open ON chunk (tenant_id, entity_id) WHERE version_to_seq IS NULL;

-- ---------------------------------------------------------------- statements (bitemporal, reified)
CREATE TABLE statement (
  tenant_id           text NOT NULL,
  stmt_id             bigserial,
  stmt_key            text NOT NULL,   -- identity for set diffs: subject|predicate|object|qualifier key|origin
  subject_id          text NOT NULL,
  subject_version_id  text NOT NULL,
  predicate_id        text NOT NULL,
  object_id           text,
  object_literal      jsonb,
  qualifiers          jsonb NOT NULL DEFAULT '{}',
  valid               tstzrange NOT NULL,
  tx                  tstzrange NOT NULL,
  tx_seq              bigint NOT NULL,
  tx_to_seq           bigint,
  origin              text NOT NULL,   -- declared:<provider>_import | declared:editor | derived:mention@1 | inferred:*
  confidence          real NOT NULL DEFAULT 1.0,
  provenance          jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, stmt_id)
);
CREATE INDEX statement_subject_open ON statement (tenant_id, subject_id) WHERE upper_inf(tx);
CREATE INDEX statement_object_open ON statement (tenant_id, object_id) WHERE upper_inf(tx);
CREATE INDEX statement_predicate ON statement (tenant_id, predicate_id);
CREATE INDEX statement_tx ON statement USING gist (tenant_id, subject_id, tx);
CREATE UNIQUE INDEX statement_key_open ON statement (tenant_id, stmt_key) WHERE upper_inf(tx);

-- ---------------------------------------------------------------- version-bound causal links (research §4.3)
CREATE TABLE causal_link (
  tenant_id         text NOT NULL,
  link_id           text NOT NULL,
  link_key          text NOT NULL,     -- effect_entity|predicate|cause_entity|origin
  effect_entity_id  text NOT NULL,
  effect_version    text NOT NULL,
  cause_entity_id   text NOT NULL,
  cause_version     text NOT NULL,
  causal_kind       text NOT NULL,
  predicate_id      text NOT NULL,
  sensitivity       text[],            -- cause fields the link depends on; NULL = any change
  valid             tstzrange NOT NULL,
  tx                tstzrange NOT NULL,   -- [asserted, retracted)
  tx_seq            bigint NOT NULL,
  tx_to_seq         bigint,
  retract_reason    text,
  origin            text NOT NULL,     -- declared:* | inferred:*
  confidence        real NOT NULL DEFAULT 1.0,
  review_status     text NOT NULL DEFAULT 'accepted' CHECK (review_status IN ('accepted', 'pending', 'rejected')),
  evidence          jsonb NOT NULL DEFAULT '{}',
  revalidates       text,
  PRIMARY KEY (tenant_id, link_id),
  FOREIGN KEY (tenant_id, effect_version) REFERENCES entity_version (tenant_id, version_id),
  FOREIGN KEY (tenant_id, cause_version) REFERENCES entity_version (tenant_id, version_id)
);
CREATE INDEX causal_link_cause ON causal_link (tenant_id, cause_version);
CREATE INDEX causal_link_effect ON causal_link (tenant_id, effect_version);
CREATE INDEX causal_link_effect_open ON causal_link (tenant_id, effect_entity_id) WHERE upper_inf(tx);
CREATE INDEX causal_link_cause_open ON causal_link (tenant_id, cause_entity_id) WHERE upper_inf(tx);
CREATE INDEX causal_link_tx ON causal_link USING gist (tenant_id, effect_entity_id, tx);
CREATE UNIQUE INDEX causal_link_key_open ON causal_link (tenant_id, link_key) WHERE upper_inf(tx);

-- Current status of every link (research §4.3). For "as known at K" use kb.causal (parameterised query).
CREATE VIEW causal_link_status AS
SELECT l.*,
       cv.seq             AS cause_seq,
       e.head_version_id  AS cause_current_version,
       CASE
         WHEN NOT upper_inf(l.tx)                 THEN 'RETRACTED'
         WHEN e.tombstone_tx IS NOT NULL          THEN 'BROKEN'
         WHEN e.head_version_id = l.cause_version THEN 'CURRENT'
         WHEN EXISTS (SELECT 1 FROM entity_version v
                      WHERE v.tenant_id = l.tenant_id AND v.entity_id = l.cause_entity_id AND v.seq > cv.seq
                        AND (l.sensitivity IS NULL OR v.changed_fields && l.sensitivity))
                                                  THEN 'STALE'
         ELSE 'CURRENT_COMPATIBLE'
       END AS status
FROM causal_link l
JOIN entity_version cv ON cv.tenant_id = l.tenant_id AND cv.version_id = l.cause_version
JOIN entity e ON e.tenant_id = l.tenant_id AND e.entity_id = l.cause_entity_id;

-- References to objects not ingested yet; turned into statements/links when the target arrives.
CREATE TABLE unresolved_ref (
  tenant_id           text NOT NULL,
  id                  bigserial,
  from_entity_id      text NOT NULL,
  from_version_id     text NOT NULL,
  ref_kind            text NOT NULL CHECK (ref_kind IN ('statement', 'causal')),
  ref_key             text NOT NULL,   -- stmt_key / link_key computed with the target's external identity
  spec                jsonb NOT NULL,
  origin              text NOT NULL,
  target_source_id    text,
  target_object_type  text,
  target_external_id  text,
  target_entity_id    text,
  status              text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'resolved', 'dropped')),
  created_tx          bigint NOT NULL,
  closed_tx           bigint,
  PRIMARY KEY (tenant_id, id)
);
CREATE INDEX unresolved_ref_target ON unresolved_ref (tenant_id, target_source_id, target_object_type, target_external_id)
  WHERE status = 'pending';
CREATE INDEX unresolved_ref_target_entity ON unresolved_ref (tenant_id, target_entity_id) WHERE status = 'pending';
CREATE INDEX unresolved_ref_from ON unresolved_ref (tenant_id, from_entity_id) WHERE status = 'pending';

CREATE TABLE review_decision (
  tenant_id        text NOT NULL,
  id               bigserial,
  target_type      text NOT NULL,      -- causal_link | statement | predicate | mapping
  target_id        text NOT NULL,
  decision         text NOT NULL CHECK (decision IN ('accept', 'reject', 'revalidate', 'retract')),
  reviewer         text NOT NULL,
  reason           text,
  tx_seq           bigint NOT NULL,
  created_link_id  text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id)
);

-- ---------------------------------------------------------------- outbox and projections (KB-2, KB-6)
-- Consumers read rows with xid < pg_snapshot_xmin(pg_current_snapshot()) in (xid, id) order: every
-- transaction below xmin is finished, so a checkpoint never skips a late-committing row.
CREATE TABLE outbox (
  id          bigserial PRIMARY KEY,
  xid         xid8 NOT NULL DEFAULT pg_current_xact_id(),
  tenant_id   text NOT NULL,
  tx_seq      bigint NOT NULL,
  topic       text NOT NULL,   -- entity.version | entity.acl | statements.changed | causal.changed | chunks.changed | predicate.changed
  payload     jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_order ON outbox (tenant_id, xid, id);

CREATE TABLE consumer_checkpoint (
  consumer    text NOT NULL,
  tenant_id   text NOT NULL,
  last_xid    xid8 NOT NULL DEFAULT '0',
  last_id     bigint NOT NULL DEFAULT 0,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, tenant_id)
);

CREATE TABLE projection_watermark (
  tenant_id   text NOT NULL,
  projector   text NOT NULL,
  tx_seq      bigint NOT NULL DEFAULT 0,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, projector)
);

CREATE TABLE index_registry (
  index_name       text PRIMARY KEY,
  alias            text NOT NULL,
  encoder          text,
  dim              int,
  mapping_version  int NOT NULL,
  active           boolean NOT NULL DEFAULT true,
  created_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- derived artefacts and audit
CREATE TABLE scenario_run (
  tenant_id   text NOT NULL,
  run_id      text NOT NULL,
  kind        text NOT NULL,           -- simulate | impact
  principal   text,
  request     jsonb NOT NULL,
  result      jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, run_id)
);

-- Agent runs keep typed tool receipts and evidence ids, never hidden reasoning (07 §8).
CREATE TABLE investigation_run (
  tenant_id     text NOT NULL,
  run_id        text NOT NULL,
  principal     text NOT NULL,
  question      text NOT NULL,
  mode          text NOT NULL,         -- deterministic | agent
  status        text NOT NULL,
  receipts      jsonb NOT NULL DEFAULT '[]',
  evidence_ids  text[] NOT NULL DEFAULT '{}',
  budget        jsonb NOT NULL DEFAULT '{}',
  answer        jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, run_id)
);

CREATE TABLE audit_event (
  id          bigserial PRIMARY KEY,
  tenant_id   text,
  principal   text,
  operation   text NOT NULL,
  target      text,
  result      text,
  meta        jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);
