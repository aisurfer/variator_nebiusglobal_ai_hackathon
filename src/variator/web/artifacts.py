"""Read-only access to precomputed artifact sets.

Layout: <artifacts_dir>/<set_id>/manifest.json plus JSON files in any subdirectory of the set.
A directory without manifest.json is not a set. Nothing outside the artifacts directory is ever served:
names are checked, then the real path (after symlinks) must stay inside the set.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST = "manifest.json"
MAX_MANIFEST_BYTES = 1024 * 1024
SET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SERVED_SUFFIXES = (".json",)


class ArtifactError(Exception):
    status = 400


class InvalidArtifactPath(ArtifactError):
    status = 400


class ArtifactNotFound(ArtifactError):
    status = 404


@dataclass(frozen=True)
class ArtifactStore:
    root: Path

    def _real_root(self) -> Path:
        return self.root.resolve()

    def set_dir(self, set_id: str) -> Path:
        """Real path of an existing set. Raises InvalidArtifactPath or ArtifactNotFound."""
        if not SET_ID.fullmatch(set_id) or ".." in set_id:
            raise InvalidArtifactPath("invalid artifact set name")
        root = self._real_root()
        try:
            real = (root / set_id).resolve(strict=True)
        except (OSError, RuntimeError):
            raise ArtifactNotFound(f"unknown artifact set: {set_id}") from None
        manifest = real / MANIFEST
        if real.parent != root or not real.is_dir() or not manifest.is_file():
            raise ArtifactNotFound(f"unknown artifact set: {set_id}")
        if not manifest.resolve().is_relative_to(real):
            raise ArtifactNotFound(f"unknown artifact set: {set_id}")
        return real

    def file(self, set_id: str, rel_path: str) -> Path:
        """Real path of a served file of a set. Raises InvalidArtifactPath or ArtifactNotFound."""
        base = self.set_dir(set_id)
        parts = rel_path.split("/")
        for part in parts:
            # empty: "//" or a leading "/" (an absolute path); ".": hidden files, "." and ".."
            if not part or part.startswith(".") or "\\" in part or "\x00" in part:
                raise InvalidArtifactPath("invalid artifact path")
        if not parts[-1].lower().endswith(SERVED_SUFFIXES):
            raise InvalidArtifactPath("only .json files are served")
        try:
            real = base.joinpath(*parts).resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError):
            raise ArtifactNotFound(f"no such artifact: {set_id}/{rel_path}") from None
        except (OSError, RuntimeError):  # symlink loop, permission
            raise InvalidArtifactPath("invalid artifact path") from None
        if not real.is_relative_to(base):
            raise InvalidArtifactPath("artifact path leaves the artifact set")
        if not real.is_file():
            raise ArtifactNotFound(f"no such artifact: {set_id}/{rel_path}")
        return real

    def list_sets(self) -> list[dict[str, Any]]:
        """One entry per set, sorted by id: {"id", "manifest"}, or {"id", "error"} for a bad manifest."""
        root = self.root
        if not root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            try:
                real = self.set_dir(entry.name)
            except ArtifactError:
                continue
            out.append(_summary(entry.name, real / MANIFEST))
        return out


def _summary(set_id: str, manifest: Path) -> dict[str, Any]:
    try:
        if manifest.stat().st_size > MAX_MANIFEST_BYTES:
            return {"id": set_id, "error": f"{MANIFEST} is larger than {MAX_MANIFEST_BYTES} bytes"}
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except OSError:
        return {"id": set_id, "error": f"{MANIFEST} cannot be read"}
    except ValueError:  # JSONDecodeError and UnicodeDecodeError
        return {"id": set_id, "error": f"{MANIFEST} is not valid JSON"}
    if not isinstance(data, dict):
        return {"id": set_id, "error": f"{MANIFEST} is not a JSON object"}
    return {"id": set_id, "manifest": data}
