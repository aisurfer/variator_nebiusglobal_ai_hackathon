-- As-of guarantee (Docs/KB/AS-OF.md). `known` is the knowledge interval of a statement or a causal link: from the
-- moment the fact became known to the system (for a derived fact: the moment its inputs were all recorded, i.e. the
-- transaction time of the journal event that derived it) to the moment it stopped being known. Reads "as known at T"
-- filter on it. `tx` stays the physical write interval (audit): extraction may run long after the data it reads.
-- For rows written by ingestion, review and the editor both intervals are equal.
ALTER TABLE statement ADD COLUMN IF NOT EXISTS known tstzrange;
ALTER TABLE causal_link ADD COLUMN IF NOT EXISTS known tstzrange;
UPDATE statement SET known = tx WHERE known IS NULL;
UPDATE causal_link SET known = tx WHERE known IS NULL;
ALTER TABLE statement ALTER COLUMN known SET NOT NULL;
ALTER TABLE causal_link ALTER COLUMN known SET NOT NULL;

-- A row written without `known` (code that does not set it) is known when it is written; a row closed without
-- touching `known` stops being known when it is closed.
CREATE OR REPLACE FUNCTION kb_known_follows_tx() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.known IS NULL THEN NEW.known := NEW.tx; END IF;
  ELSIF NEW.known IS NOT DISTINCT FROM OLD.known AND upper_inf(OLD.tx) AND NOT upper_inf(NEW.tx)
        AND upper_inf(OLD.known) AND NOT isempty(OLD.known) THEN
    NEW.known := tstzrange(lower(OLD.known), GREATEST(lower(OLD.known), upper(NEW.tx)), '[)');
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS statement_known ON statement;
CREATE TRIGGER statement_known BEFORE INSERT OR UPDATE ON statement FOR EACH ROW EXECUTE FUNCTION kb_known_follows_tx();
DROP TRIGGER IF EXISTS causal_link_known ON causal_link;
CREATE TRIGGER causal_link_known BEFORE INSERT OR UPDATE ON causal_link FOR EACH ROW EXECUTE FUNCTION kb_known_follows_tx();

CREATE INDEX IF NOT EXISTS statement_known ON statement USING gist (tenant_id, subject_id, known);
CREATE INDEX IF NOT EXISTS causal_link_known ON causal_link USING gist (tenant_id, effect_entity_id, known);
CREATE INDEX IF NOT EXISTS causal_link_pair ON causal_link (tenant_id, effect_entity_id, cause_entity_id);

-- Late mentions are searched in the texts as they were at the moment an entity appeared, not only in the head texts.
DROP INDEX IF EXISTS chunk_text_trgm;
CREATE INDEX IF NOT EXISTS chunk_text_trgm_all ON chunk USING gin (text gin_trgm_ops);

-- The status view selects l.*: recreate it so that it carries `known`.
DROP VIEW IF EXISTS causal_link_status;
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
