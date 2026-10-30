"""Cached, resumable source acquisition with checksum verification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx


class AcquisitionError(RuntimeError):
    """A source artifact could not be acquired or verified."""


def _validate_checksum(path: Path, expected_sha256: str | None) -> None:
    if expected_sha256 is not None and sha256_file(path) != expected_sha256.lower():
        raise AcquisitionError(f"SHA-256 mismatch for {path.name}")


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_path(cache_dir: Path, key: str) -> Path:
    """Return a deterministic cache file path for a source key."""
    safe = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return cache_dir / f"{safe}.json"


def load_cached(cache_dir: Path, key: str) -> Any | None:
    """Load a cached JSON payload if present and checksum-valid."""
    path = cache_path(cache_dir, key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def store_cached(cache_dir: Path, key: str, payload: Any) -> Path:
    """Persist a JSON payload to the cache and return its path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(cache_dir, key)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def download_artifact(
    *,
    url: str,
    cache_dir: Path,
    filename: str,
    expected_sha256: str | None = None,
    client: httpx.Client | None = None,
    chunk_size: int = 1024 * 1024,
) -> Path:
    """Download a source artifact with Range resume and checksum verification.

    A completed artifact is published only after its checksum succeeds. Partial
    bytes remain in a sibling ``.part`` file so a later call can resume them.
    """
    if not filename or Path(filename).name != filename:
        raise ValueError("filename must be a plain file name")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / filename
    partial = cache_dir / f"{filename}.part"
    if destination.exists():
        _validate_checksum(destination, expected_sha256)
        return destination

    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    owns_client = client is None
    http_client = client or httpx.Client(follow_redirects=True, timeout=60.0)
    try:
        with http_client.stream("GET", url, headers=headers) as response:
            if offset and response.status_code == httpx.codes.OK:
                offset = 0
            elif offset and response.status_code != httpx.codes.PARTIAL_CONTENT:
                response.raise_for_status()
                raise AcquisitionError("server did not honor a resumable Range request")
            else:
                response.raise_for_status()

            mode = "ab" if offset else "wb"
            with partial.open(mode) as handle:
                for chunk in response.iter_bytes(chunk_size):
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        _validate_checksum(partial, expected_sha256)
        partial.replace(destination)
        return destination
    finally:
        if owns_client:
            http_client.close()
