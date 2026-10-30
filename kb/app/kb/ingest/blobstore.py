"""Content-addressed blob store for raw source payloads (stand-in for the private S3 object store, 02 §11.3).

Write path: temp file -> fsync -> atomic rename; the key is the sha256 of the bytes, so a repeated write is a
no-op and a key always denotes exactly one content.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from kb.settings import get_settings
from kb.text import sha256_hex


class BlobStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or get_settings().blob_dir)

    def _path(self, key: str) -> Path:
        algo, _, digest = key.partition(":")
        return self.root / algo / digest[:2] / digest[2:4] / digest

    def put(self, data: bytes) -> str:
        key = f"sha256:{sha256_hex(data)}"
        path = self._path(key)
        if path.exists():
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()
