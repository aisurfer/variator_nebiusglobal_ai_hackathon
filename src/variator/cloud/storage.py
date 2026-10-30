"""Where job inputs and outputs live: a small interface over an Object Storage bucket.

The job container sees the bucket as a directory (a ``--volume`` mount). On the host,
``LocalDirStore`` works wherever the same bucket is mounted as a directory: for example
the app running on a Serverless AI endpoint with the same volume mounted at ``/data``.
An S3 implementation (boto3 against ``https://storage.<region>.nebius.cloud``) can be
added behind the same two methods when S3 credentials are available.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable


@runtime_checkable
class ResultStore(Protocol):
    """Byte storage addressed by bucket-relative keys such as ``variator-runs/<id>/output.json``."""

    def read_bytes(self, key: str) -> bytes | None:
        """The object's bytes, or None if it does not exist."""
        ...

    def write_bytes(self, key: str, data: bytes) -> None: ...


def check_key(key: str) -> str:
    """Reject keys that could escape the bucket root."""
    path = PurePosixPath(key)
    if not key or path.is_absolute() or any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError(f"invalid storage key {key!r}")
    return key


class LocalDirStore:
    """A directory standing in for the bucket root (a mounted bucket, or a temp dir in tests)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def read_bytes(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def write_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def __repr__(self) -> str:
        return f"LocalDirStore({str(self.root)!r})"
