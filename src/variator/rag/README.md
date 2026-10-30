# variator.rag

RAG (red, amber, green) bands per option and criterion, and 2D tunnel maps over two drivers.

| Need | Use |
|---|---|
| Limits | `default_thresholds(run, baseline)`, `resolve_thresholds(run, overrides)` |
| Shares | `rag_report(run, limits, weights)` |
| Map over two drivers | `tunnel(model, x=, y=, limits=, rules=, grid=)` -> `TunnelMap` |

Details: `docs/knowledge/platform.md`.
