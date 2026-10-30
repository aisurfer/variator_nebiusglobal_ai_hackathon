-- Search projection (K08/K09). The projections themselves live in OpenSearch; PostgreSQL keeps only a
-- representation cache: (encoder, sha256(text)) -> vector. Chunk text is immutable, so a reindex or an encoder
-- round-trip never re-embeds a text it has embedded once. Rows hold no text and no tenant data beyond the hash;
-- the table is a cache and may be truncated at any time.
CREATE TABLE IF NOT EXISTS search_embedding_cache (
  encoder     text NOT NULL,           -- e.g. fastembed:sentence-transformers/paraphrase-multilingual-mpnet-base-v2
  text_hash   text NOT NULL,           -- sha256 hex of the embedded text
  dim         int NOT NULL,
  vec         bytea NOT NULL,          -- float32 little-endian, unit length
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (encoder, text_hash)
);
