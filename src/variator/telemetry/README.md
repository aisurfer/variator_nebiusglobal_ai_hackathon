# variator.telemetry

Run statistics in SQLite: one row per stage (tool call or agent run) and per LLM call.

| Need | Use |
|---|---|
| Store | `StatsStore(path)`; `stats_from_env()` (`VARIATOR_STATS`, default `.variator/stats.sqlite`, `off`) |
| Record tool calls | `StatsMiddleware(store, plugins)` (added by `build_server` when stats are on) |
| Link LLM calls | `store.attach(ledger)`; context variable `CURRENT` |
| Analyse | MCP `stats_summary(group_by=...)`; `summary`, `by_model`, SQL on `stages` / `llm_calls` |

Details: `docs/knowledge/resources.md`.
