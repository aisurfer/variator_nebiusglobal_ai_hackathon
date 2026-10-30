# variator.web

Read-only web API of the MVP (Starlette). No LLM, no database. Deploy: `deploy/mvp/README.md`.

| Need | Use |
|---|---|
| ASGI app | `variator.web.app:app`; for tests `create_app(WebSettings(...))` |
| Settings | `WebSettings.from_env`: env `VARIATOR_ARTIFACTS_DIR` (default `./artifacts`), `VARIATOR_GIT_COMMIT` |
| Artifact sets | `ArtifactStore.list_sets`, `ArtifactStore.file` (path checks: names, symlinks, `.json` only) |
| Version | `version.package_version`, `version.read_git_commit` (reads `.git` files, no subprocess) |
| Tests | `tests/web/test_app.py` (tmp directories only) |

Routes: `GET /api/variator/{health,version,artifacts}`, `GET /api/variator/artifacts/{set}/{path}`.
The canonical API path is `/api/variator/*`: caddy and the workspace proxy forward the full path unchanged.
Only `GET /health` also works without the prefix (container health checks); any other unprefixed path is 404.

Errors: JSON `{"error": {"status", "message"}}`. Bad name or path: 400. Unknown set or file: 404.

Artifact set: `<VARIATOR_ARTIFACTS_DIR>/<set>/manifest.json` (a JSON object) and JSON files below it.

Future routes (cases, forks, runs): see the placeholder in `app._routes`.
