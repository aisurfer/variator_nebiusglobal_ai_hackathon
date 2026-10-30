-- K12 extraction bookkeeping. Not part of the journal: the journal keeps what extraction produced (derived
-- mentions, inferred candidates); these tables only remember what was already processed and per-run counters,
-- so a re-delivered event does not repeat an LLM call. Safe to truncate (the next run recomputes).
CREATE TABLE IF NOT EXISTS extract_chunk_state (
  tenant_id     text NOT NULL REFERENCES tenant ON DELETE CASCADE,
  chunk_id      text NOT NULL,
  extractor     text NOT NULL,          -- llm:<model>
  entity_id     text NOT NULL,
  input_hash    text NOT NULL,          -- prompt version + model + chunk text + candidate mentions
  result        jsonb NOT NULL DEFAULT '{}',
  processed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, chunk_id, extractor)
);

CREATE TABLE IF NOT EXISTS extract_run (
  tenant_id    text NOT NULL REFERENCES tenant ON DELETE CASCADE,
  run_id       bigserial,
  started_at   timestamptz NOT NULL,
  finished_at  timestamptz NOT NULL DEFAULT now(),
  max_tx_seq   bigint,
  counters     jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, run_id)
);
CREATE INDEX IF NOT EXISTS extract_run_recent ON extract_run (tenant_id, finished_at DESC);
