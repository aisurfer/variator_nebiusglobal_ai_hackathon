"""FastAPI application. Domain routers are optional modules: each subsystem owns its router file."""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from importlib import import_module

from fastapi import FastAPI

from kb.api.routes_core import router as core_router
from kb.db import connect, migrate
from kb.settings import get_settings, llm_available

log = logging.getLogger("kb.api")
OPTIONAL_ROUTERS = ("kb.api.routes_search", "kb.api.routes_graph", "kb.api.routes_causal", "kb.api.routes_ask")



def _warm_up() -> None:
    """Load the embedding model in the background so the first query does not pay the 3-5 s cold start."""
    try:
        from kb.search.embeddings import get_embedder

        get_embedder().embed_query("warm up")
    except Exception:
        log.warning("embedder warm-up failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    migrate()
    threading.Thread(target=_warm_up, name="kb-warmup", daemon=True).start()
    yield


app = FastAPI(
    title="Knowledge Backend",
    version="0.1.0",
    description="Bitemporal journal, knowledge graph, hybrid search and causal queries over organisational sources.",
    lifespan=lifespan,
)
app.include_router(core_router)
for _mod in OPTIONAL_ROUTERS:
    try:
        app.include_router(import_module(_mod).router)
    except ModuleNotFoundError as exc:
        if exc.name != _mod:
            raise


@app.get("/health", tags=["admin"])
def health() -> dict:
    s = get_settings()
    out = {"status": "ok", "env": s.env, "llm": llm_available(), "embed_provider": s.embed_provider}
    with connect() as conn:
        conn.execute("SELECT 1")
    return out
