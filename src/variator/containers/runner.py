"""Run tools in containers: create, configure, run, deploy, redeploy, collect results.

``DockerCLI`` calls the ``docker`` command (no SDK dependency). Defaults are safe for
code analysis: no network, inputs mounted read-only, CPU and memory limits, a
timeout, and only the ``/out`` folder is writable. Results are files in ``/out`` on
the host side; a recipe parses them into compact metrics without an LLM.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

OUT_DIR = "/out"
TAIL_CHARS = 4_000


class ContainerError(RuntimeError):
    """Docker is missing, the image cannot be built or pulled, or the run timed out."""


@dataclass(frozen=True)
class Mount:
    host: str
    container: str
    read_only: bool = True


@dataclass(frozen=True)
class ContainerSpec:
    image: str
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()
    workdir: str | None = None
    network: Literal["none", "bridge"] = "none"
    cpus: float = 2.0
    memory: str = "2g"
    timeout_s: float = 900.0
    name: str | None = None  # for deployed (long-lived) containers
    ports: dict[int, int] = field(default_factory=dict)  # host -> container
    user: str | None = None

    def run_args(self, out_dir: Path | None, *, detach: bool = False) -> list[str]:
        args = ["run"]
        args += ["-d"] if detach else ["--rm"]
        args += ["--network", self.network, "--cpus", str(self.cpus), "--memory", self.memory]
        args += ["--security-opt", "no-new-privileges"]
        if self.name:
            args += ["--name", self.name]
        if self.user:
            args += ["--user", self.user]
        if self.workdir:
            args += ["-w", self.workdir]
        for key, value in sorted(self.env.items()):
            args += ["-e", f"{key}={value}"]
        for mount in self.mounts:
            suffix = ":ro" if mount.read_only else ""
            args += ["-v", f"{Path(mount.host).resolve()}:{mount.container}{suffix}"]
        if out_dir is not None:
            args += ["-v", f"{out_dir.resolve()}:{OUT_DIR}"]
        for host, container in sorted(self.ports.items()):
            args += ["-p", f"127.0.0.1:{host}:{container}"]
        return [*args, self.image, *self.command]


@dataclass
class ContainerResult:
    exit_code: int
    stdout: str
    stderr: str
    elapsed_s: float
    out_dir: Path | None
    files: list[str] = field(default_factory=list)  # names inside out_dir

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def read_json(self, name: str) -> object:
        if self.out_dir is None:
            raise FileNotFoundError(name)
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))


Executor = Callable[[list[str], float], subprocess.CompletedProcess[str]]


class Runner(Protocol):
    def run(self, spec: ContainerSpec, *, out_dir: Path | None = None) -> ContainerResult: ...


def _execute(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,  # a stdio MCP server must not share its stdin pipe
    )


class DockerCLI:
    """Container operations through the docker CLI. ``executor`` is replaceable in tests."""

    def __init__(self, binary: str = "docker", *, executor: Executor | None = None):
        self.binary = binary
        self._execute = executor or _execute

    def available(self) -> bool:
        if self._execute is _execute and shutil.which(self.binary) is None:
            return False
        try:
            return self._call(["version", "--format", "{{.Server.Version}}"], 20).returncode == 0
        except ContainerError:
            return False

    def _call(self, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        command = [self.binary, *args]
        try:
            return self._execute(command, timeout)
        except FileNotFoundError as exc:
            raise ContainerError(f"{self.binary!r} not found: install Docker or set the runner") from exc
        except subprocess.TimeoutExpired as exc:
            raise ContainerError(f"timeout after {timeout:.0f}s: {' '.join(command[:4])} ...") from exc

    def _checked(self, args: Sequence[str], timeout: float) -> str:
        done = self._call(args, timeout)
        if done.returncode != 0:
            raise ContainerError(f"docker {args[0]} failed ({done.returncode}): {done.stderr[-TAIL_CHARS:]}")
        return done.stdout

    # --- images ----------------------------------------------------------------------
    def image_exists(self, image: str) -> bool:
        return self._call(["image", "inspect", image], 60).returncode == 0

    def pull(self, image: str, timeout_s: float = 900) -> None:
        self._checked(["pull", image], timeout_s)

    def build(
        self,
        context: str | Path,
        tag: str,
        *,
        dockerfile: str | None = None,
        build_args: dict[str, str] | None = None,
        timeout_s: float = 1800,
    ) -> str:
        args = ["build", "-t", tag]
        if dockerfile:
            args += ["-f", dockerfile]
        for key, value in sorted((build_args or {}).items()):
            args += ["--build-arg", f"{key}={value}"]
        self._checked([*args, str(context)], timeout_s)
        return tag

    # --- one-shot runs -----------------------------------------------------------------
    def run(self, spec: ContainerSpec, *, out_dir: Path | None = None) -> ContainerResult:
        """Run to completion; files written to /out appear in ``out_dir`` (a temp dir if None)."""
        target = out_dir or Path(tempfile.mkdtemp(prefix="variator-run-"))
        target.mkdir(parents=True, exist_ok=True)
        start = time.perf_counter()
        done = self._call(spec.run_args(target), spec.timeout_s)
        return ContainerResult(
            exit_code=done.returncode,
            stdout=done.stdout[-TAIL_CHARS:],
            stderr=done.stderr[-TAIL_CHARS:],
            elapsed_s=time.perf_counter() - start,
            out_dir=target,
            files=sorted(p.name for p in target.iterdir()),
        )

    # --- long-lived services -------------------------------------------------------------
    def deploy(self, spec: ContainerSpec) -> str:
        """Start a named detached container; returns its id."""
        if not spec.name:
            raise ValueError("deploy needs ContainerSpec.name")
        return self._checked(spec.run_args(None, detach=True), 120).strip()

    def redeploy(self, spec: ContainerSpec, *, pull: bool = False) -> str:
        """Stop and remove the named container, optionally pull the image, and start it again."""
        if not spec.name:
            raise ValueError("redeploy needs ContainerSpec.name")
        if pull:
            self.pull(spec.image)
        self.remove(spec.name)
        return self.deploy(spec)

    def remove(self, name: str) -> None:
        self._call(["rm", "-f", name], 60)  # no error if it does not exist

    def status(self, name: str) -> str:
        done = self._call(["inspect", "-f", "{{.State.Status}}", name], 30)
        return done.stdout.strip() if done.returncode == 0 else "absent"

    def logs(self, name: str, tail: int = 200) -> str:
        return self._checked(["logs", "--tail", str(tail), name], 60)[-TAIL_CHARS:]
