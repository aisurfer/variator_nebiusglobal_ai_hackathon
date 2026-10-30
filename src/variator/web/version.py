"""Package version and git commit. The commit is read from the .git files; no subprocess, no network."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")  # SHA-1 or SHA-256 object name
_MAX_READ = 64 * 1024  # bytes of a small git file (HEAD, ref, gitdir pointer)
_MAX_PACKED = 8 * 1024 * 1024  # bytes of packed-refs


def package_version() -> str:
    try:
        return importlib.metadata.version("variator")
    except importlib.metadata.PackageNotFoundError:
        from variator import __version__

        return __version__


def valid_commit(value: str | None) -> str | None:
    """The value as a lowercase object name, or None when it is not one."""
    if not value:
        return None
    value = value.strip().lower()
    return value if _SHA.fullmatch(value) else None


def _read_small(path: Path, limit: int = _MAX_READ) -> str:
    with path.open("rb") as fh:
        return fh.read(limit).decode("utf-8", errors="replace").strip()


def _git_dir(root: Path) -> Path | None:
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():  # a worktree or a submodule: "gitdir: <path>"
        text = _read_small(dot_git)
        if text.startswith("gitdir:"):
            target = Path(text[len("gitdir:") :].strip())
            return target if target.is_absolute() else root / target
    return None


def _ref_from_packed(common: Path, ref: str) -> str | None:
    packed = common / "packed-refs"
    if not packed.is_file():
        return None
    for line in _read_small(packed, _MAX_PACKED).splitlines():
        sha, _, name = line.partition(" ")
        if name.strip() == ref:
            return valid_commit(sha)
    return None


def read_git_commit(root: Path | None) -> str | None:
    """Commit of HEAD in the checkout at root, or None. Never raises."""
    if root is None:
        return None
    try:
        git_dir = _git_dir(root)
        if git_dir is None or not git_dir.is_dir():
            return None
        head = _read_small(git_dir / "HEAD")
        if not head.startswith("ref:"):
            return valid_commit(head)  # detached HEAD
        ref = head[len("ref:") :].strip()
        if not ref.startswith("refs/") or any(p in ("", ".", "..") for p in ref.split("/")):
            return None
        common = git_dir
        if (git_dir / "commondir").is_file():  # linked worktree: refs live in the main git dir
            common = git_dir / _read_small(git_dir / "commondir")
        for base in (git_dir, common):
            ref_file = base / ref
            if ref_file.is_file():
                return valid_commit(_read_small(ref_file))
        return _ref_from_packed(common, ref)
    except (OSError, ValueError):
        return None
