"""Settings of the web API. Every value comes from a VARIATOR_* environment variable."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# src/variator/web/settings.py -> repository root when the package runs from a source checkout.
# In an installed package this path has no .git, so the commit is simply unknown.
SOURCE_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class WebSettings:
    """Settings of one app instance.

    artifacts_dir: directory with one subdirectory per artifact set (env VARIATOR_ARTIFACTS_DIR).
    git_commit: commit to report (env VARIATOR_GIT_COMMIT); None: read it from repo_root/.git.
    repo_root: checkout to read the commit from; None: do not read git files.
    """

    artifacts_dir: Path = Path("artifacts")
    git_commit: str | None = None
    repo_root: Path | None = SOURCE_ROOT

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WebSettings:
        env = os.environ if environ is None else environ
        artifacts = env.get("VARIATOR_ARTIFACTS_DIR", "").strip() or "./artifacts"
        commit = env.get("VARIATOR_GIT_COMMIT", "").strip() or None
        return cls(artifacts_dir=Path(artifacts).expanduser(), git_commit=commit)
