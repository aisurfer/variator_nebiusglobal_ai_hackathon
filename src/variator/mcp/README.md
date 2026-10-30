# variator.mcp

FastMCP server `variator-core`: tools return handles and short summaries; the arrays stay on the server.

| Need | Use |
|---|---|
| Server | `build_server(plugins, services=Services(...))`; CLI `variator-mcp [--http PORT] [--store PATH\|memory]` |
| State | `store.Store` (memory + SQLite), `case.Case`, `case.apply_patch` |
| Plugins | `plugins.Plugin`, `plugins.discover` (entry points, `VARIATOR_PLUGINS`), `mount_all` (+ `VARIATOR_REMOTE_MCP`) |
| Guides for clients | resources `variator://guide/{flow,rules,patch}` (`guides.GUIDES`) |
| Tool groups | tags `core`/`build`/`analyze`/`code`/`ops`; tool `tools_enable(groups)`; env `VARIATOR_TOOLS=all` |
| Owners (HTTP) | `sessions.OwnerMiddleware`, `Store.visible`; `--allow-remote` for a non-loopback host |
| Windows stdio | `warmup.warm_up()` before `run()` |

Details: `docs/knowledge/platform.md`, `docs/knowledge/pitfalls.md`.
