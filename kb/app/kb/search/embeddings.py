"""Representation Provider (KB-5): text -> unit-length dense vectors.

Providers (settings.embed_provider; `auto` means fastembed):
- `fastembed` (default): local multilingual ONNX model (settings.embed_model, 768-d), no external calls;
- `hash`: deterministic feature-hashing stub for offline unit tests (no semantics beyond shared tokens).

The encoder identity (`name`, `dim`) is recorded in index_registry by kb.search.opensearch; vectors of different
encoders never share a physical index. `cached_embed` adds a PostgreSQL cache keyed by (encoder, sha256(text)):
chunk text is immutable, so a reindex never re-embeds what was embedded once.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import unicodedata
from typing import Protocol

import numpy as np
import psycopg

from kb.settings import get_settings

log = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str
    dim: int
    cacheable: bool

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _unit(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 1:
        n = float(np.linalg.norm(m))
        return m / n if n > 0 else m
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


class _QueryCache:
    """Query embeddings are cached per instance (the agent loop asks the same thing repeatedly)."""

    _qcache: dict[str, list[float]]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - overridden
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        cache = self.__dict__.setdefault("_qcache", {})
        v = cache.get(text)
        if v is None:
            v = self.embed_documents([text])[0]
            if len(cache) >= 2048:
                cache.clear()
            cache[text] = v
        return v


class FastEmbedder(_QueryCache):
    cacheable = True

    def __init__(self, model: str, cache_dir: str | None, batch: int = 32, threads: int = 0):
        from fastembed import TextEmbedding

        dims = {m["model"]: m["dim"] for m in TextEmbedding.list_supported_models()}
        if model not in dims:
            raise ValueError(f"fastembed does not support {model}")
        self.model = model
        self.dim = int(dims[model])
        self.name = f"fastembed:{model}"
        self.batch = batch
        # threads 0: the ONNX Runtime default, one thread per core. On a CPU with slow efficiency cores, or with
        # several drain processes, a few threads each are much faster (KB_EMBED_THREADS).
        self._m = TextEmbedding(model_name=model, cache_dir=cache_dir, threads=threads or None)
        self._lock = threading.Lock()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Batches of texts of similar length: a batch is padded to its longest text, and the projector mixes short
        # chunks with long version texts. Same vectors, about half the time (2026-10-27); results keep the input order.
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        with self._lock:  # one ONNX session; it uses all cores per call anyway
            vecs = np.stack(list(self._m.embed([texts[i] for i in order], batch_size=self.batch)))
        out = np.empty_like(vecs)
        out[order] = vecs
        return _unit(out).tolist()


_TOKEN = re.compile(r"\w+", re.UNICODE)


class HashEmbedder:
    """Signed feature hashing of word tokens and character trigrams. Deterministic, offline, tests only."""

    cacheable = False

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.name = f"hash:v1-{dim}"

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        t = unicodedata.normalize("NFKC", text).casefold()
        for tok in _TOKEN.findall(t):
            feats = [f"w:{tok}"] + [f"c:{tok[i:i + 3]}" for i in range(max(1, len(tok) - 2))]
            for f in feats:
                h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
                v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        return _unit(v)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t).tolist() for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text).tolist()


_lock = threading.Lock()
_instances: dict[tuple, Embedder] = {}


def resolved_provider() -> str:
    """The configured provider, with `auto` resolved to the local model (fastembed) in every environment."""
    s = get_settings()
    return "fastembed" if s.embed_provider == "auto" else s.embed_provider


def get_embedder() -> Embedder:
    """The process-wide embedder chosen by settings (model loaded once per process)."""
    s = get_settings()
    provider = resolved_provider()
    key = (provider, s.embed_model)
    with _lock:
        e = _instances.get(key)
        if e is None:
            if provider == "fastembed":
                e = FastEmbedder(s.embed_model, str(s.embed_cache_dir) if s.embed_cache_dir else None, s.embed_batch,
                                 s.embed_threads)
            elif provider == "hash":
                e = HashEmbedder()
            else:  # pragma: no cover - settings validation prevents it
                raise ValueError(f"unknown embed provider {s.embed_provider}")
            _instances[key] = e
            log.info("embedder %s (dim %s) ready", e.name, e.dim)
        return e


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cached_embed(conn: psycopg.Connection, embedder: Embedder, texts: list[str]) -> list[list[float]]:
    """Embed texts through the (encoder, sha256) cache in PostgreSQL; only cache misses reach the model."""
    if not texts:
        return []
    if not embedder.cacheable:
        return embedder.embed_documents(texts)
    hashes = [text_hash(t) for t in texts]
    uniq = list(dict.fromkeys(hashes))
    found: dict[str, list[float]] = {}
    rows = conn.execute(
        "SELECT text_hash, vec FROM search_embedding_cache WHERE encoder = %s AND dim = %s AND text_hash = ANY(%s)",
        (embedder.name, embedder.dim, uniq),
    ).fetchall()
    for r in rows:
        found[r["text_hash"]] = np.frombuffer(r["vec"], dtype="<f4").tolist()
    missing = [h for h in uniq if h not in found]
    if missing:
        by_hash = {h: t for h, t in zip(hashes, texts)}
        vecs = embedder.embed_documents([by_hash[h] for h in missing])
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO search_embedding_cache (encoder, text_hash, dim, vec) VALUES (%s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    [(embedder.name, h, embedder.dim, np.asarray(v, dtype="<f4").tobytes()) for h, v in zip(missing, vecs)],
                )
        found.update(zip(missing, vecs))
    return [found[h] for h in hashes]
