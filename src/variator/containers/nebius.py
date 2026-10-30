"""Run a ``ContainerSpec`` as a Nebius Serverless AI job (same ``Runner`` interface as Docker).

A job cannot mount host folders, so the runner stages them in a bucket:

1. Copy each read-only mount into ``<staging>/<run>/m<i>/``; ``staging`` is the bucket
   mounted on the host (for example with ``rclone mount``).
2. Create a job that mounts ``<bucket>:<run>/m<i>`` read-only at the same container
   paths, and ``<bucket>:<run>/out`` at ``/out``.
3. Wait for the job; copy ``<staging>/<run>/out/*`` to ``out_dir``.

# ASSUMPTION: Serverless jobs have no option to switch the network off; the spec's
# ``network="none"`` cannot be enforced here. Each job bills at least the minimum timeout.
"""

from __future__ import annotations

import shlex
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from variator.cloud.specs import MIN_JOB_TIMEOUT_S, JobSpec, JobState, VolumeMount
from variator.containers.runner import OUT_DIR, ContainerResult, ContainerSpec

SKIP = {".git", ".venv", "node_modules", "__pycache__", ".cache", ".variator"}


class JobClient(Protocol):
    def create_job(self, spec: JobSpec, *, dry_run: bool = False) -> Any: ...

    def wait_for_job(self, job_id: str, **kwargs: Any) -> Any: ...


class NebiusJobRunner:
    def __init__(
        self,
        client: JobClient,
        *,
        bucket: str,
        staging: str | Path,
        platform: str = "cpu-d3",
        preset: str = "2vcpu-8gb",
        poll_interval_s: float = 15.0,
    ):
        self.client = client
        self.bucket = bucket
        self.staging = Path(staging)
        self.platform = platform
        self.preset = preset
        self.poll_interval_s = poll_interval_s

    def job_spec(self, spec: ContainerSpec, run_id: str) -> JobSpec:
        volumes = [
            VolumeMount(
                source=self.bucket,
                container_path=m.container,
                mode="ro" if m.read_only else "rw",
                source_path=f"{run_id}/m{i}",
            )
            for i, m in enumerate(spec.mounts)
        ]
        volumes.append(
            VolumeMount(source=self.bucket, container_path=OUT_DIR, mode="rw", source_path=f"{run_id}/out")
        )
        return JobSpec(
            name=f"scan-{run_id}",
            image=spec.image,
            args=shlex.join(spec.command) if spec.command else None,
            env=dict(spec.env),
            working_dir=spec.workdir,
            platform=self.platform,
            preset=self.preset,
            volumes=volumes,
            timeout_s=max(MIN_JOB_TIMEOUT_S, int(spec.timeout_s)),
            labels={"app": "variator", "kind": "scan"},
        )

    def run(self, spec: ContainerSpec, *, out_dir: Path | None = None) -> ContainerResult:
        run_id = uuid.uuid4().hex[:12]
        root = self.staging / run_id
        for i, mount in enumerate(spec.mounts):
            if not mount.read_only:  # a scanner cache: start empty, do not upload the local database
                (root / f"m{i}").mkdir(parents=True, exist_ok=True)
                continue
            shutil.copytree(mount.host, root / f"m{i}", ignore=shutil.ignore_patterns(*SKIP))
        (root / "out").mkdir(parents=True, exist_ok=True)
        start = time.perf_counter()
        operation = self.client.create_job(self.job_spec(spec, run_id))
        job = self.client.wait_for_job(
            operation.resource_id, poll_interval_s=self.poll_interval_s, timeout_s=spec.timeout_s + 900
        )
        target = out_dir or root / "collected"
        target.mkdir(parents=True, exist_ok=True)
        for item in (root / "out").iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)
        ok = job.state == JobState.COMPLETED
        return ContainerResult(
            exit_code=0 if ok else 1,
            stdout="",
            stderr="" if ok else f"job {operation.resource_id} ended in {job.state}",
            elapsed_s=time.perf_counter() - start,
            out_dir=target,
            files=sorted(p.name for p in target.iterdir()),
        )


def runner_from_env(env: dict[str, str] | None = None) -> Any:
    """``VARIATOR_RUNNER`` = docker (default) | nebius (needs ``VARIATOR_SCAN_BUCKET`` and
    ``VARIATOR_SCAN_STAGING``, the bucket mounted on the host, plus the Nebius settings)."""
    import os

    from variator.containers.runner import DockerCLI

    env = dict(os.environ if env is None else env)
    kind = env.get("VARIATOR_RUNNER", "docker").lower()
    if kind == "docker":
        return DockerCLI()
    if kind == "nebius":
        from variator.cloud import ServerlessClient

        missing = [k for k in ("VARIATOR_SCAN_BUCKET", "VARIATOR_SCAN_STAGING") if not env.get(k)]
        if missing:
            raise ValueError(f"VARIATOR_RUNNER=nebius needs {', '.join(missing)}")
        return NebiusJobRunner(
            ServerlessClient(), bucket=env["VARIATOR_SCAN_BUCKET"], staging=env["VARIATOR_SCAN_STAGING"]
        )
    raise ValueError(f"unknown VARIATOR_RUNNER {kind!r} (docker | nebius)")
