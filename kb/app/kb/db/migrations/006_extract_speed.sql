-- T07 A7: indexes of the extraction hot paths on dense corpora.
-- Candidates of implicit references: versions of a tenant inside a time window (kb.extract.implicit.candidates).
CREATE INDEX IF NOT EXISTS entity_version_valid_from ON entity_version (tenant_id, valid_from) WHERE op <> 'delete';
-- Late mentions: open chunks citing a key or title of a new entity (ILIKE '%key%', kb.extract.worker).
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS chunk_text_trgm ON chunk USING gin (text gin_trgm_ops) WHERE version_to_seq IS NULL;
