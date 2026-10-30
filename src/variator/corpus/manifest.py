"""Deterministic provenance and integrity helpers for corpus output files."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from variator.corpus.canonical import sha256_hex


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file's uncompressed bytes."""
    digest = hashlib.sha256()
    handle = gzip.open(path, "rb") if path.suffix == ".gz" else path.open("rb")
    with handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_manifest_entry(path: Path, schema_version: str, record_count: int) -> dict[str, object]:
    """Build the required manifest entry for one emitted delivery file."""
    if record_count < 0:
        raise ValueError("record_count must not be negative")
    return {
        "path": path.name,
        "schema_version": schema_version,
        "sha256": sha256_file(path),
        "byte_size": path.stat().st_size,
        "record_count": record_count,
    }


def config_hash(config: dict[str, Any]) -> str:
    """Hash the normalized assembly configuration."""
    return sha256_hex(config)


def deterministic_run_id(seed: int, config: dict[str, Any], document_count: int) -> str:
    """Build a reproducible run identifier from inputs, not wall-clock time."""
    return f"redwood-{sha256_hex({'seed': seed, 'config': config, 'documents': document_count})[:16]}"


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Write a manifest with stable formatting and a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
