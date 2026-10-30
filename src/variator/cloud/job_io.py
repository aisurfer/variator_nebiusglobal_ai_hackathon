"""The contract between the host (``jobs.py``) and the job container (``job_entry.py``).

Input reaches the container as a JSON file (an injected file or a file in a mounted
bucket) or as base64 JSON in an environment variable. The container writes a
``JobOutput`` JSON file (normally into a mounted bucket) and prints one marker line to
stdout so the result can also be recovered from the job logs.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from datetime import UTC, datetime
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from variator.cloud.errors import InputTooLarge
from variator.domain import DecisionModel, OptimizationResult, SimulationResult

INPUT_B64_ENV = "VARIATOR_JOB_INPUT_B64"
INPUT_PATH_ENV = "VARIATOR_JOB_INPUT_PATH"
OUTPUT_PATH_ENV = "VARIATOR_JOB_OUTPUT_PATH"
MARKER_PREFIX = "VARIATOR_JOB_RESULT "

# ASSUMPTION: the API and the container runtime accept env values of this size; the docs
# give no limit for environment variables (Linux allows 128 KiB per string). The default
# transport is an injected file, whose 64 KiB limit is documented.
MAX_ENV_INPUT_CHARS = 64 * 1024
MAX_FILE_INPUT_BYTES = 16 * 1024 * 1024
# The whole JobOutput is inlined into the marker line only while it stays this small.
# ASSUMPTION: job log lines of this length are not truncated (no documented limit).
MAX_INLINE_OUTPUT_BYTES = 32 * 1024

TaskName = Literal["simulate", "simulate_and_optimize"]
Stage = Literal["input", "simulate", "optimize", "output"]


class ExitCode(IntEnum):
    """Container exit codes. Any non-zero code makes the job FAILED (``ContainerFailed``)."""

    OK = 0
    UNEXPECTED = 1
    USAGE = 2  # no input given
    INVALID_INPUT = 3  # unreadable, too large or schema-invalid input
    SIMULATION_FAILED = 4
    OPTIMIZATION_FAILED = 5  # simulation result was still written
    OUTPUT_FAILED = 6  # result computed but the output file could not be written


def new_run_id() -> str:
    """Sortable, unique enough for job names and bucket keys: ``20261025-141502-a1b2c3``."""
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


class JobInput(BaseModel):
    """Everything the container needs to run one deep simulation."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default_factory=new_run_id, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    task: TaskName = "simulate"
    decision: DecisionModel
    n_scenarios: int = Field(default=1_000_000, ge=1, le=200_000_000)
    seed: int | None = None
    backend: Literal["auto", "numpy", "cupy"] = "auto"
    sensitivity: bool = True
    budget: float | None = None
    max_cvar_loss: float | None = None
    solver: Literal["auto", "cuopt", "highs"] = "auto"

    def to_json_bytes(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    def to_env_value(self, *, limit: int = MAX_ENV_INPUT_CHARS) -> str:
        """Base64 JSON for ``VARIATOR_JOB_INPUT_B64``; raises ``InputTooLarge`` above ``limit``."""
        value = base64.b64encode(self.to_json_bytes()).decode("ascii")
        if len(value) > limit:
            raise InputTooLarge(
                f"job input is {len(value)} chars in base64, above the {limit}-char env limit; "
                "use the bucket transport"
            )
        return value

    @classmethod
    def from_json_bytes(cls, data: bytes, *, limit: int = MAX_FILE_INPUT_BYTES) -> JobInput:
        if len(data) > limit:
            raise InputTooLarge(f"job input is {len(data)} bytes, above the {limit}-byte limit")
        return cls.model_validate_json(data)

    @classmethod
    def from_env_value(cls, value: str, *, limit: int = MAX_ENV_INPUT_CHARS) -> JobInput:
        value = value.strip()
        if len(value) > limit:
            raise InputTooLarge(f"{INPUT_B64_ENV} is {len(value)} chars, above the {limit}-char limit")
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"{INPUT_B64_ENV} is not valid base64: {exc}") from exc
        return cls.model_validate_json(raw)


class JobError(BaseModel):
    stage: Stage
    type: str
    message: str


class JobTimings(BaseModel):
    load_s: float = 0.0
    simulate_s: float | None = None
    optimize_s: float | None = None
    write_s: float | None = None
    total_s: float = 0.0


class JobOutput(BaseModel):
    """What the container produced. ``ok`` is False when any requested step failed."""

    run_id: str | None = None
    task: TaskName | None = None
    ok: bool
    exit_code: int
    n_scenarios: int | None = None
    backend: str | None = None
    simulation: SimulationResult | None = None
    optimization: OptimizationResult | None = None
    timings: JobTimings = Field(default_factory=JobTimings)
    errors: list[JobError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime | None = None


class JobMarker(BaseModel):
    """The compact stdout line; carries the full output when it is small enough."""

    run_id: str | None
    ok: bool
    exit_code: int
    output_path: str | None = None
    backend: str | None = None
    n_scenarios: int | None = None
    total_s: float = 0.0
    errors: list[str] = Field(default_factory=list)
    output: JobOutput | None = None


def format_marker(
    output: JobOutput, output_path: str | None, *, inline_limit: int = MAX_INLINE_OUTPUT_BYTES
) -> str:
    """One line: ``VARIATOR_JOB_RESULT {json}``."""
    inline = output if len(output.model_dump_json()) <= inline_limit else None
    marker = JobMarker(
        run_id=output.run_id,
        ok=output.ok,
        exit_code=output.exit_code,
        output_path=output_path,
        backend=output.backend,
        n_scenarios=output.n_scenarios,
        total_s=output.timings.total_s,
        errors=[f"{e.stage}: {e.type}: {e.message}" for e in output.errors],
        output=inline,
    )
    return MARKER_PREFIX + marker.model_dump_json()


def parse_marker(text: str) -> JobMarker | None:
    """The last valid marker in ``text`` (log lines may carry timestamps before it)."""
    for line in reversed(text.splitlines()):
        index = line.find(MARKER_PREFIX)
        if index < 0:
            continue
        try:
            return JobMarker.model_validate_json(line[index + len(MARKER_PREFIX):])
        except (ValidationError, json.JSONDecodeError):
            continue
    return None
