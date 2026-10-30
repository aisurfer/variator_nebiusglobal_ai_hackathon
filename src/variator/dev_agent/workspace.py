"""Workspace tools for the developer agent.

The tools never use a shell. File paths stay inside one repository root, patch
application uses ``git apply``, and commands use an explicit allowlist.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

MAX_READ_BYTES = 200_000
MAX_COMMAND_BYTES = 40_000


class WorkspaceError(ValueError):
    """A requested developer-agent operation is not permitted."""


class ReadFileParams(BaseModel):
    path: str
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=300, ge=1, le=1_000)


class FindFilesParams(BaseModel):
    pattern: str = Field(min_length=1, max_length=200)
    max_results: int = Field(default=100, ge=1, le=500)


class ApplyPatchParams(BaseModel):
    patch: str = Field(min_length=1, max_length=200_000)


class RunCommandParams(BaseModel):
    program: Literal["git", "rg", "uv"]
    arguments: list[str] = Field(default_factory=list, max_length=32)


@dataclass(frozen=True)
class DeveloperWorkspace:
    """A repository root and the safe operations that a developer agent can request."""

    root: Path
    allow_git_write: bool = False

    def __post_init__(self) -> None:
        root = self.root.resolve()
        if not (root / ".git").exists():
            raise WorkspaceError(f"not a Git workspace: {root}")
        object.__setattr__(self, "root", root)

    def read_file(self, params: ReadFileParams) -> str:
        path = self._path(params.path)
        if not path.is_file():
            raise WorkspaceError(f"not a file: {params.path}")
        if path.stat().st_size > MAX_READ_BYTES:
            raise WorkspaceError(f"file exceeds {MAX_READ_BYTES} bytes: {params.path}")
        lines = path.read_text(encoding="utf-8").splitlines()
        start = params.start_line - 1
        end = start + params.max_lines
        return "\n".join(f"{number}: {line}" for number, line in enumerate(lines[start:end], params.start_line))

    def find_files(self, params: FindFilesParams) -> list[str]:
        if Path(params.pattern).is_absolute() or ".." in Path(params.pattern).parts:
            raise WorkspaceError("pattern must stay inside the workspace")
        matches = sorted(path for path in self.root.glob(params.pattern) if path.is_file())
        return [str(path.relative_to(self.root)) for path in matches[: params.max_results]]

    def apply_patch(self, params: ApplyPatchParams) -> str:
        self._validate_patch(params.patch)
        self._git_apply(["--check", "--whitespace=error", "-"], params.patch)
        self._git_apply(["--whitespace=error", "-"], params.patch)
        return "patch applied"

    def run_command(self, params: RunCommandParams) -> dict[str, str | int]:
        command = [params.program, *params.arguments]
        self._validate_command(command)
        completed = subprocess.run(
            command,
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": self._trim(completed.stdout),
            "stderr": self._trim(completed.stderr),
        }

    def _path(self, relative: str) -> Path:
        raw = Path(relative)
        if raw.is_absolute():
            raise WorkspaceError("absolute paths are not allowed")
        path = (self.root / raw).resolve()
        if self.root not in path.parents and path != self.root:
            raise WorkspaceError("path escapes the workspace")
        return path

    def _validate_patch(self, patch: str) -> None:
        if "deleted file mode" in patch or "rename from" in patch or "rename to" in patch:
            raise WorkspaceError("deletion and rename patches are not allowed")
        for line in patch.splitlines():
            if not line.startswith(("--- ", "+++ ")):
                continue
            candidate = line[4:].split("\t", 1)[0]
            if candidate == "/dev/null":
                continue
            if not candidate.startswith(("a/", "b/")):
                raise WorkspaceError("patch paths must use a/ or b/ prefixes")
            self._path(candidate[2:])

    def _validate_command(self, command: list[str]) -> None:
        if not command[1:]:
            raise WorkspaceError("a command needs arguments")
        program, args = command[0], command[1:]
        if any("\x00" in item for item in args):
            raise WorkspaceError("command contains a NUL byte")
        if program == "git":
            readonly = {"status", "diff", "log", "show", "branch", "rev-parse", "check-ignore"}
            writable = {"add", "commit"}
            if args[0] not in readonly | writable:
                raise WorkspaceError(f"git {args[0]!r} is not allowed")
            if args[0] in writable and not self.allow_git_write:
                raise WorkspaceError("Git writes require --allow-git-write")
            return
        if program == "rg":
            if any(argument.startswith("--pre") or argument == "--glob=!**" for argument in args):
                raise WorkspaceError("rg preprocessor options are not allowed")
            return
        if program == "uv" and args[0] == "run":
            return
        raise WorkspaceError(f"command is not allowed: {' '.join(command[:2])}")

    def _git_apply(self, arguments: list[str], patch: str) -> None:
        completed = subprocess.run(
            ["git", "apply", *arguments],
            cwd=self.root,
            input=patch,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if completed.returncode:
            raise WorkspaceError(self._trim(completed.stderr) or "git apply failed")

    @staticmethod
    def _trim(text: str) -> str:
        if len(text) <= MAX_COMMAND_BYTES:
            return text
        return text[:MAX_COMMAND_BYTES] + f"\n[cut: {len(text)} bytes]"
