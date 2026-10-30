# Redwood corpus assembly

This package builds the deterministic, no-LLM Redwood training corpus and its
mandatory Plancy projection. Offline assembly uses the committed fixtures:

```bash
uv run python -m variator.corpus.cli assemble \
  --fixtures tests/fixtures/corpus \
  --output data/corpus/redwood
```

The output contains canonical JSONL documents, factor indexes, temporal splits,
provenance, and `plancy/` with the compressed event timeline, source
configuration, directory, adapters, schemas, evaluation cases, and gold links.

Raw downloads belong in `data/external/`; assembled runtime output belongs in
`data/corpus/`. Both locations are intentionally ignored by Git.
