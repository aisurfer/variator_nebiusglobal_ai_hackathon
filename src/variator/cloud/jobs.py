"""Deep simulation as a Serverless AI job: submit, wait, fetch the result.

Input transports (``InputTransport``):

* ``file`` (default): the ``JobInput`` JSON is an injected file at
  ``/etc/variator/job-input.json``. Documented, up to 64 KiB, enough for any realistic
  ``DecisionModel``; no storage needed for the input.
* ``env``: base64 JSON in ``VARIATOR_JOB_INPUT_B64`` (same size class).
* ``bucket``: the JSON is written through a ``ResultStore`` to
  ``variator-runs/<run_id>/input.json`` in the mounted bucket. For large inputs.

Results: with ``bucket`` set, the bucket is mounted at ``mount_path`` (``/data``) and the
job writes ``variator-runs/<run_id>/output.json`` there; read it back with a
``ResultStore`` over the same bucket. Without a bucket the container disk is wiped at the
end, so the only copy is the stdout marker, readable through ``nebius ai job logs``
(pass ``logs=variator.cloud.cli.cli_job_logs``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from variator.cloud.client import DEFAULT_JOB_WAIT_S, ServerlessClient
from variator.cloud.costs import JobCostEstimate, estimate_job_cost
from variator.cloud.errors import CloudError, InputTooLarge
from variator.cloud.job_io import (
    INPUT_B64_ENV,
    INPUT_PATH_ENV,
    OUTPUT_PATH_ENV,
    JobInput,
    JobOutput,
    TaskName,
    parse_marker,
)
from variator.cloud.resources import JobInfo, Operation
from variator.cloud.specs import (
    MAX_INJECTED_FILE_BYTES,
    InjectedFile,
    JobSpec,
    RegistryCredentials,
    SecretRef,
    VolumeMount,
)
from variator.cloud.storage import ResultStore
from variator.domain import DecisionModel

INPUT_FILE_PATH = "/etc/variator/job-input.json"
DEFAULT_MOUNT_PATH = "/data"
RUNS_PREFIX = "variator-runs"
DEFAULT_PLATFORM = "gpu-l40s-a"
DEFAULT_PRESET = "1gpu-8vcpu-32gb"
RESERVED_ENV = frozenset({INPUT_B64_ENV, INPUT_PATH_ENV, OUTPUT_PATH_ENV})

LogsReader = Callable[[str], str]


class InputTransport(StrEnum):
    FILE = "file"
    ENV = "env"
    BUCKET = "bucket"


class JobResultMissing(CloudError):
    """The job ended but neither the bucket nor the logs hold its output."""


class JobPlan(BaseModel):
    """A fully rendered job, before submission."""

    spec: JobSpec
    job_input: JobInput
    transport: InputTransport
    input_key: str | None = None  # bucket-relative, BUCKET transport only
    output_key: str | None = None  # bucket-relative, when a bucket is mounted
    output_path: str | None = None  # the same file as seen inside the container


class SubmittedJob(BaseModel):
    """Handle for a submitted deep simulation; serializable so the UI can poll later."""

    job_id: str
    job_name: str
    run_id: str
    operation_id: str | None = None
    transport: InputTransport
    input_key: str | None = None
    output_key: str | None = None
    output_path: str | None = None
    platform: str
    preset: str
    preemptible: bool
    cost: JobCostEstimate | None = None
    submitted_at: datetime
    dry_run: bool = False


def run_keys(run_id: str) -> tuple[str, str]:
    """Bucket-relative ``(input_key, output_key)`` for a run."""
    base = f"{RUNS_PREFIX}/{run_id}"
    return f"{base}/input.json", f"{base}/output.json"


def job_name_for(run_id: str) -> str:
    name = f"variator-sim-{run_id}".lower().replace("_", "-").replace(".", "-")
    return name[:63].rstrip("-")


def plan_simulation_job(
    job_input: JobInput,
    *,
    image: str,
    platform: str = DEFAULT_PLATFORM,
    preset: str = DEFAULT_PRESET,
    transport: InputTransport | str = InputTransport.FILE,
    bucket: str | None = None,
    mount_path: str = DEFAULT_MOUNT_PATH,
    timeout_s: int = 3600,
    preemptible: bool = False,
    subnet_id: str | None = None,
    env: Mapping[str, str] | None = None,
    env_secrets: Mapping[str, str | SecretRef] | None = None,
    registry: RegistryCredentials | None = None,
    disk_size_gib: int | None = None,
    restart_on_failure: bool = False,
) -> JobPlan:
    """Render the ``JobSpec`` that runs ``python -m variator.cloud.job_entry`` on ``job_input``.

    The image's ENTRYPOINT must be the job entrypoint (see ``deploy/sim-job/Dockerfile``);
    paths travel in env vars so no argument quoting is involved.
    """
    transport = InputTransport(transport)
    reserved = RESERVED_ENV & {*(env or {}), *(env_secrets or {})}
    if reserved:
        raise ValueError(f"environment variables reserved for the job protocol: {sorted(reserved)}")
    if transport is InputTransport.BUCKET and not bucket:
        raise ValueError("the bucket transport needs a bucket to mount")

    run_env = dict(env or {})
    injected: list[InjectedFile] = []
    volumes: list[VolumeMount] = []
    input_key = output_key = output_path = None
    in_key, out_key = run_keys(job_input.run_id)
    payload = job_input.to_json_bytes()

    if bucket:
        volumes.append(VolumeMount(source=bucket, container_path=mount_path, mode="rw"))
        output_key = out_key
        output_path = f"{mount_path}/{out_key}"
        run_env[OUTPUT_PATH_ENV] = output_path

    if transport is InputTransport.FILE:
        if len(payload) > MAX_INJECTED_FILE_BYTES:
            raise InputTooLarge(
                f"job input is {len(payload)} bytes; injected files are limited to "
                f"{MAX_INJECTED_FILE_BYTES} bytes: use transport='bucket'"
            )
        injected.append(InjectedFile(container_path=INPUT_FILE_PATH, content=payload))
        run_env[INPUT_PATH_ENV] = INPUT_FILE_PATH
    elif transport is InputTransport.ENV:
        run_env[INPUT_B64_ENV] = job_input.to_env_value()
    else:
        input_key = in_key
        run_env[INPUT_PATH_ENV] = f"{mount_path}/{in_key}"

    spec = JobSpec(
        name=job_name_for(job_input.run_id),
        image=image,
        platform=platform,
        preset=preset,
        timeout_s=timeout_s,
        preemptible=preemptible,
        subnet_id=subnet_id,
        env=run_env,
        env_secrets={k: SecretRef.parse(v) for k, v in (env_secrets or {}).items()},
        volumes=volumes,
        injected_files=injected,
        registry=registry,
        disk_size_gib=disk_size_gib,
        restart_on_failure=restart_on_failure,
        labels={"app": "variator"},
    )
    return JobPlan(
        spec=spec, job_input=job_input, transport=transport,
        input_key=input_key, output_key=output_key, output_path=output_path,
    )


def submit_simulation_job(
    client: ServerlessClient,
    decision: DecisionModel,
    *,
    image: str,
    n_scenarios: int = 1_000_000,
    task: TaskName = "simulate",
    seed: int | None = None,
    backend: Literal["auto", "numpy", "cupy"] = "auto",
    sensitivity: bool = True,
    budget: float | None = None,
    max_cvar_loss: float | None = None,
    solver: Literal["auto", "cuopt", "highs"] = "auto",
    platform: str = DEFAULT_PLATFORM,
    preset: str = DEFAULT_PRESET,
    transport: InputTransport | str = InputTransport.FILE,
    bucket: str | None = None,
    mount_path: str = DEFAULT_MOUNT_PATH,
    store: ResultStore | None = None,
    timeout_s: int = 3600,
    preemptible: bool = False,
    env: Mapping[str, str] | None = None,
    env_secrets: Mapping[str, str | SecretRef] | None = None,
    registry: RegistryCredentials | None = None,
    disk_size_gib: int | None = None,
    expected_runtime_s: float = 600.0,
    run_id: str | None = None,
    dry_run: bool = False,
) -> SubmittedJob:
    """Create a job that simulates (and optionally optimizes) ``decision``.

    ``bucket`` is the Object Storage bucket ID (or name) mounted at ``mount_path`` for the
    output; ``store`` is the host-side view of the same bucket, needed for the ``bucket``
    input transport. ``expected_runtime_s`` only feeds the cost estimate. The job is billed
    per second from VM start until it ends or hits ``timeout_s`` (minimum 1 h).
    """
    fields = {
        "task": task, "decision": decision, "n_scenarios": n_scenarios, "seed": seed, "backend": backend,
        "sensitivity": sensitivity, "budget": budget, "max_cvar_loss": max_cvar_loss, "solver": solver,
    }
    job_input = JobInput(**fields, **({"run_id": run_id} if run_id else {}))
    plan = plan_simulation_job(
        job_input, image=image, platform=platform, preset=preset, transport=transport, bucket=bucket,
        mount_path=mount_path, timeout_s=timeout_s, preemptible=preemptible, env=env,
        env_secrets=env_secrets, registry=registry, disk_size_gib=disk_size_gib,
    )
    if plan.input_key:
        if store is None:
            raise ValueError("the bucket transport needs a store to upload the input")
        if not dry_run:
            store.write_bytes(plan.input_key, job_input.to_json_bytes())
    operation = client.create_job(plan.spec, dry_run=dry_run)
    try:
        cost = estimate_job_cost(plan.spec, expected_runtime_s=expected_runtime_s)
    except ValueError:  # platform not in the price table
        cost = None
    return SubmittedJob(
        job_id=operation.resource_id or "",
        job_name=plan.spec.name,
        run_id=job_input.run_id,
        operation_id=operation.id or None,
        transport=plan.transport,
        input_key=plan.input_key,
        output_key=plan.output_key,
        output_path=plan.output_path,
        platform=plan.spec.platform,
        preset=plan.spec.preset,
        preemptible=plan.spec.preemptible,
        cost=cost,
        submitted_at=datetime.now(UTC),
        dry_run=dry_run,
    )


def fetch_job_result(
    submitted: SubmittedJob, *, store: ResultStore | None = None, logs: LogsReader | None = None
) -> JobOutput | None:
    """The job's output from the bucket, else from the stdout marker in its logs; None if absent."""
    if store is not None and submitted.output_key:
        data = store.read_bytes(submitted.output_key)
        if data:
            return JobOutput.model_validate_json(data)
    if logs is not None:
        marker = parse_marker(logs(submitted.job_id))
        if marker and marker.output and marker.run_id in (None, submitted.run_id):
            return marker.output
    return None


def wait_for_result(
    client: ServerlessClient,
    submitted: SubmittedJob,
    *,
    store: ResultStore | None = None,
    logs: LogsReader | None = None,
    poll_interval_s: float = 15.0,
    timeout_s: float = DEFAULT_JOB_WAIT_S,
    on_update: Callable[[JobInfo], None] | None = None,
) -> JobOutput:
    """Wait for the job to end and return its output (which may report ``ok=False``).

    Raises ``QuotaExceeded`` / ``NotEnoughResources`` / ``WorkloadFailed`` when the job
    failed without writing output, ``JobResultMissing`` when it completed without output.
    """
    operation = Operation(id=submitted.operation_id) if submitted.operation_id else None
    job = client.wait_for_job(
        submitted.job_id, poll_interval_s=poll_interval_s, timeout_s=timeout_s,
        operation=operation, on_update=on_update,
    )
    output = fetch_job_result(submitted, store=store, logs=logs)
    if output is not None:
        return output
    job.raise_for_state()
    where = submitted.output_key or "no bucket mounted"
    raise JobResultMissing(
        f"job {submitted.job_id} is {job.state.value} but has no output ({where}); "
        "mount a bucket and pass its store, or pass logs=cli_job_logs"
    )


def run_simulation_job(
    client: ServerlessClient,
    decision: DecisionModel,
    *,
    image: str,
    store: ResultStore | None = None,
    logs: LogsReader | None = None,
    poll_interval_s: float = 15.0,
    wait_timeout_s: float = DEFAULT_JOB_WAIT_S,
    on_update: Callable[[JobInfo], None] | None = None,
    **submit_kwargs: object,
) -> JobOutput:
    """Submit, wait and fetch in one call (blocking for minutes: cold start included)."""
    submitted = submit_simulation_job(client, decision, image=image, store=store, **submit_kwargs)  # type: ignore[arg-type]
    return wait_for_result(
        client, submitted, store=store, logs=logs, poll_interval_s=poll_interval_s,
        timeout_s=wait_timeout_s, on_update=on_update,
    )
