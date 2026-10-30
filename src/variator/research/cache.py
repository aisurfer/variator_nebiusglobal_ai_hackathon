"""JSON file cache for Tavily responses, with an offline mode for demos and judging.

Entries are keyed by a SHA-256 of the endpoint plus the canonical request body (the
API key travels in a header, so it never affects the key or lands on disk). Offline
mode serves any cached entry regardless of age and raises ``OfflineCacheMissError``
on a miss, so a recorded demo never depends on live search or burns credits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from variator.research.errors import OfflineCacheMissError

log = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(".cache/tavily")
DEFAULT_TTL_S = 7 * 24 * 3600.0

# Local workaround until Settings grows these fields (see README).
ENV_CACHE_DIR = "VARIATOR_TAVILY_CACHE_DIR"
ENV_CACHE_TTL_S = "VARIATOR_TAVILY_CACHE_TTL_S"
ENV_OFFLINE = "VARIATOR_TAVILY_OFFLINE"


class CacheEntry(BaseModel):
    endpoint: str
    request: dict[str, Any]
    response: dict[str, Any]
    credits: float = 0.0  # what the live call cost, to report credits saved
    created_at: float


def cache_key(endpoint: str, payload: Mapping[str, Any]) -> str:
    """Stable hash of a request: same endpoint and body (any key order) -> same key."""
    canonical = json.dumps(
        {"endpoint": endpoint, "request": payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class TavilyCache:
    """File cache: ``<directory>/<endpoint>/<sha256>.json``.

    ``ttl_s=None`` never expires. Writes are atomic (temp file + rename) so concurrent
    agents never read half-written JSON; unreadable files count as misses.
    """

    def __init__(
        self,
        directory: str | os.PathLike[str] = DEFAULT_CACHE_DIR,
        *,
        ttl_s: float | None = DEFAULT_TTL_S,
        offline: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = Path(directory)
        self.ttl_s = ttl_s
        self.offline = offline
        self._clock = clock

    @classmethod
    def from_env(cls) -> TavilyCache:
        """Build from ``VARIATOR_TAVILY_CACHE_DIR``, ``VARIATOR_TAVILY_CACHE_TTL_S``
        (0 = never expire) and ``VARIATOR_TAVILY_OFFLINE``."""
        ttl_raw = os.environ.get(ENV_CACHE_TTL_S, "").strip()
        ttl = DEFAULT_TTL_S if not ttl_raw else (float(ttl_raw) or None)
        return cls(
            os.environ.get(ENV_CACHE_DIR, "").strip() or DEFAULT_CACHE_DIR,
            ttl_s=ttl,
            offline=_truthy(os.environ.get(ENV_OFFLINE)),
        )

    def path_for(self, endpoint: str, payload: Mapping[str, Any]) -> Path:
        return self.directory / endpoint / f"{cache_key(endpoint, payload)}.json"

    def get(self, endpoint: str, payload: Mapping[str, Any]) -> CacheEntry | None:
        """Return a fresh entry or None. In offline mode return any entry or raise on a miss."""
        path = self.path_for(endpoint, payload)
        entry = self._read(path)
        if entry is not None and (self.offline or not self._expired(entry)):
            return entry
        if self.offline:
            raise OfflineCacheMissError(
                f"offline mode: no cached Tavily {endpoint} for {_describe(payload)} ({path.name})"
            )
        return None

    def put(
        self, endpoint: str, payload: Mapping[str, Any], response: Mapping[str, Any], credits: float
    ) -> None:
        """Store a successful response. No-op in offline mode."""
        if self.offline:
            return
        entry = CacheEntry(
            endpoint=endpoint,
            request=dict(payload),
            response=dict(response),
            credits=credits,
            created_at=self._clock(),
        )
        path = self.path_for(endpoint, payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(entry.model_dump_json(indent=2))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def clear(self) -> int:
        """Delete all cached entries; return how many were removed."""
        removed = 0
        for path in self.directory.glob("*/*.json"):
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def _expired(self, entry: CacheEntry) -> bool:
        return self.ttl_s is not None and self._clock() - entry.created_at > self.ttl_s

    @staticmethod
    def _read(path: Path) -> CacheEntry | None:
        try:
            return CacheEntry.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValidationError) as exc:
            log.warning("ignoring unreadable Tavily cache file %s: %s", path, exc)
            return None


def _describe(payload: Mapping[str, Any]) -> str:
    if "query" in payload and "urls" not in payload:
        return repr(payload["query"])
    return repr(payload.get("urls"))
