"""VARIATOR web API (MVP): read-only, no LLM, no database.

Run: `uvicorn variator.web.app:app` (deploy/app/Dockerfile, deploy/mvp/compose.yaml).

Routes (the canonical API path is /api/variator/*):
- GET /api/variator/health
- GET /api/variator/version
- GET /api/variator/artifacts
- GET /api/variator/artifacts/{set}/{path}
- GET /health: the same answer as /api/variator/health, for container health checks only

Caddy and the workspace proxy (workspace/server/variatorProxy.ts) both forward the full path unchanged,
so no other route exists without the prefix.

Every error is JSON: {"error": {"status": <int>, "message": <str>}}.
"""

from __future__ import annotations

import logging

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route

from variator.web.artifacts import ArtifactError, ArtifactStore
from variator.web.month import routes as month_routes
from variator.web.settings import WebSettings
from variator.web.version import package_version, read_git_commit, valid_commit

log = logging.getLogger(__name__)

API = "/api/variator"


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    body = {"error": {"status": status, "message": message}}
    return JSONResponse(body, status_code=status, headers=headers)


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    return _error(exc.status_code, str(exc.detail), dict(exc.headers or {}))


async def _server_error(request: Request, exc: Exception) -> Response:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return _error(500, "internal server error")


def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def version(request: Request) -> JSONResponse:
    return JSONResponse(request.app.state.version)


def list_artifacts(request: Request) -> JSONResponse:
    store: ArtifactStore = request.app.state.artifacts
    return JSONResponse({"sets": store.list_sets()})


def get_artifact(request: Request) -> Response:
    store: ArtifactStore = request.app.state.artifacts
    set_id, rel_path = request.path_params["set"], request.path_params["path"]
    try:
        path = store.file(set_id, rel_path)
    except ArtifactError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return FileResponse(path, media_type="application/json", headers={"X-Content-Type-Options": "nosniff"})


def _api_routes() -> list[BaseRoute]:
    """Routes relative to the API prefix. _routes() mounts them under API."""
    routes: list[BaseRoute] = [
        Route("/health", health, methods=["GET"]),
        Route("/version", version, methods=["GET"]),
        Route("/artifacts", list_artifacts, methods=["GET"]),
        Route("/artifacts/{set}/{path:path}", get_artifact, methods=["GET"]),
        *month_routes(),  # POST /cases/{case_id}/month: the live month run on actors (variator.web.month)
    ]
    # ----------------------------------------------------------------------------------------------
    # PLACEHOLDER: future routes (cases, forks, runs). Not implemented in the MVP skeleton.
    # Add them here (relative paths; the API prefix is added by _routes), with JSON errors as above:
    #   GET  /cases                      decision cases of the company
    #   GET  /cases/{case}/forks         forks (interventions) of a case
    #   POST /runs                       start a live run (needs a run store; not read-only)
    #   GET  /runs/{run}                 status and results of a run
    # A write route changes the contract "read-only, no LLM, no database": update the module docstring,
    # deploy/mvp/README.md and the compose mounts (the artifacts volume is read-only) at the same time.
    # ----------------------------------------------------------------------------------------------
    return routes


def _routes() -> list[BaseRoute]:
    # /health without the prefix is for the container health check (deploy/mvp/compose.yaml); nothing else is.
    return [Route("/health", health, methods=["GET"]), Mount(API, routes=_api_routes())]


def create_app(settings: WebSettings | None = None) -> Starlette:
    """Build the ASGI app. settings=None: read them from the environment (WebSettings.from_env)."""
    settings = settings or WebSettings.from_env()
    app = Starlette(
        routes=_routes(),
        exception_handlers={HTTPException: _http_error, Exception: _server_error},
    )
    app.state.settings = settings
    app.state.artifacts = ArtifactStore(settings.artifacts_dir)
    commit = valid_commit(settings.git_commit) or read_git_commit(settings.repo_root)
    app.state.version = {"name": "variator", "version": package_version(), "git_commit": commit}
    return app


app = create_app()
