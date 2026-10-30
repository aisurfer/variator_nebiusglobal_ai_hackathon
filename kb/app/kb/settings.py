"""Runtime settings. Every value comes from KB_* environment variables; defaults suit `make infra` + host runs."""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

KB_ROOT = Path(__file__).resolve().parents[2]  # .../kb


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KB_", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"

    pg_dsn: str = "postgresql://kb:kb@127.0.0.1:55432/kb"
    pg_pool_max: int = 10

    os_url: str = "http://127.0.0.1:9201"
    os_http_compress: bool = False  # gzip of bulk bodies: worth it over a slow link, pure CPU next to the cluster
    os_index_prefix: str = "kb"

    memgraph_uri: str = "bolt://127.0.0.1:7688"
    graph_enabled: bool = True

    config_dir: Path = KB_ROOT / "config"
    fixtures_dir: Path = KB_ROOT / "fixtures"
    blob_dir: Path = KB_ROOT / ".data" / "blobs"

    # Representation provider (KB-5). `fastembed` (default): the local model baked into the image, works offline.
    # `auto` resolves to fastembed. `hash` is a deterministic offline stub for unit tests only.
    embed_provider: Literal["auto", "fastembed", "hash"] = "fastembed"
    embed_model: str = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
    embed_cache_dir: Path | None = None
    embed_batch: int = 32
    # ONNX threads of the fastembed model per process; 0: one per core (slow on hybrid CPUs and with drain parts)
    embed_threads: int = 0
    drain_parts: int = 0  # drain --until-idle: processes for search, split by entity (0: half the cores, up to 8)
    # kNN noise floor; None = the encoder's default (query.MIN_COS_BY_PROVIDER)
    vector_min_cos: float | None = None
    # weighted reciprocal rank fusion of the hybrid search channels; None = the encoder's default
    # (query.LEXICAL_WEIGHT_BY_PROVIDER)
    search_lexical_weight: float | None = None
    search_vector_weight: float = 1.0

    # LLM gateway (KB-8): a chat completions API (Nebius Token Factory). The key is read from KB_LLM_API_KEY, else
    # KB_LLM_KEY_FILE (docker secret, one line). No file outside these is read. The key is never logged.
    llm_enabled: bool = True
    llm_model: str = "nvidia/Nemotron-3_5-Lightning"
    llm_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    llm_api_key: str | None = None
    llm_key_file: Path | None = None
    llm_timeout_s: float = 60.0

    # In dev the simulated connector replay may set the transaction time (observed_at) of an envelope.
    allow_simulated_time: bool = True
    # Principal used when a request carries no X-KB-Principal header (dev only).
    default_principal: str = "user:admin"

    worker_poll_s: float = 1.0
    worker_batch: int = 200


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.embed_cache_dir is None:
        s.embed_cache_dir = KB_ROOT / ".data" / "models"
    if s.env == "prod":
        s.allow_simulated_time = False
    return s


@lru_cache
def llm_api_key() -> str | None:
    s = get_settings()
    if s.llm_api_key:
        return s.llm_api_key
    if s.llm_key_file and s.llm_key_file.exists():
        try:
            key = s.llm_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:  # unreadable secret -> deterministic mode, never a crash
            logging.getLogger(__name__).warning("LLM key file unreadable: %s", type(exc).__name__)
            key = ""
        if key:
            return key
    return None


def llm_available() -> bool:
    return get_settings().llm_enabled and bool(llm_api_key())
