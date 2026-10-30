"""Pydantic models for the Sandboxes (Contree) REST API.

Field names follow the OpenAPI spec published with
https://docs.tokenfactory.nebius.com/api-reference/sandboxes/instances/spawn-a-new-container-instance.md.
Response models ignore unknown fields so additive API changes do not break parsing.
"""

from __future__ import annotations

import base64
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Spawn defaults and caps for recorded stdout/stderr (truncate_output_at).
DEFAULT_STREAM_CAP = 1_048_576
MAX_STREAM_CAP = 10_485_760


class OperationStatus(StrEnum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    EXECUTING = "EXECUTING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset(
    {OperationStatus.SUCCESS, OperationStatus.FAILED, OperationStatus.CANCELLED}
)


class WhoAmI(BaseModel):
    """``GET /v1/whoami``: token identity, permission map and account-effective limits."""

    token_uuid: str
    token_expiration: int | None = None
    permissions: dict[str, bool] = Field(default_factory=dict)
    limits: dict[str, int] = Field(default_factory=dict)
    operations_stat: dict[str, Any] = Field(default_factory=dict)

    @property
    def max_timeout_s(self) -> int | None:
        return self.limits.get("instance_max_timeout")

    @property
    def max_concurrency(self) -> int | None:
        return self.limits.get("instance_max_concurrency")

    def can(self, permission: str) -> bool:
        return bool(self.permissions.get(permission))


class Image(BaseModel):
    uuid: str
    tag: str | None = None
    created_at: str | None = None
    operation_uuid: str | None = None


class UploadedFile(BaseModel):
    """``POST /v1/files`` (FileResponse) or ``GET /v1/files/{sha256}`` (File)."""

    uuid: str
    sha256: str
    size: int = -1


class FileMount(BaseModel):
    """One entry of the spawn request ``files`` map (key: absolute guest path)."""

    uuid: str
    mode: str = "0644"
    uid: int | None = None
    gid: int | None = None


class Networking(BaseModel):
    # The API defaults to true; we default to false so untrusted code is offline
    # unless a caller opts in.
    enabled: bool = False


class StdinPayload(BaseModel):
    value: str
    encoding: str = "ascii"
    close: bool = True


class SpawnRequest(BaseModel):
    """Body of ``POST /v1/instances``. ``image`` is a UUID or ``tag:<name>``."""

    image: str
    command: str
    shell: bool = False
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    disposable: bool = True
    timeout: int | None = Field(default=None, ge=1)
    truncate_output_at: int | None = Field(default=None, ge=1, le=MAX_STREAM_CAP)
    networking: Networking = Field(default_factory=Networking)
    files: dict[str, FileMount] = Field(default_factory=dict)
    stdin: StdinPayload | None = None
    hostname: str | None = None
    preserve_env: bool | None = None

    def body(self) -> dict[str, Any]:
        # disposable and networking are always sent explicitly, as the docs advise.
        return self.model_dump(exclude_none=True)


class StreamOutput(BaseModel):
    """Recorded stdout/stderr (StreamRepr)."""

    value: str = ""
    encoding: str = "ascii"
    truncated: bool = False

    def data(self) -> bytes:
        return decode_value(self.value, self.encoding)

    def text(self) -> str:
        return self.data().decode("utf-8", errors="replace")


class ProcessState(BaseModel):
    exit_code: int | None = None
    signal: int | None = None
    timed_out: bool = False
    pid: int | None = None
    core_dump: bool | None = None
    stopped: bool | None = None

    @property
    def signalled(self) -> bool:
        # The API uses -1 (or null) for "no signal".
        return self.signal not in (None, -1, 0)


class InstanceResult(BaseModel):
    state: ProcessState | None = None
    stdout: StreamOutput | None = None
    stderr: StreamOutput | None = None
    resources: dict[str, Any] | None = None


class Operation(BaseModel):
    """``GET /v1/operations/{id}`` (OperationResponse)."""

    uuid: str
    kind: str | None = None
    status: OperationStatus
    error: str | None = None
    created_at: str | None = None
    duration: float | None = None
    image_uuid: str | None = None
    result_image_uuid: str | None = None
    metadata: dict[str, Any] | None = None
    # Top-level ``result`` is used by image-import operations: {"image": uuid, "tag": ...}.
    result: dict[str, Any] | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        # The enum is upper-case but the spec's own example shows "success".
        return value.upper() if isinstance(value, str) else value

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    @property
    def instance_result(self) -> InstanceResult | None:
        raw = (self.metadata or {}).get("result")
        return InstanceResult.model_validate(raw) if isinstance(raw, dict) else None

    @property
    def produced_image(self) -> str | None:
        """Result image of an instance run or an image import, if any."""
        return self.result_image_uuid or (self.result or {}).get("image")


class OperationEvent(BaseModel):
    """One event of ``GET /v1/operations/{id}/events``. ``spid`` is absent on ``completion``."""

    id: int
    ts: str | None = None
    spid: int | None = None
    type: str
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_output(self) -> bool:
        return self.type in ("stdout", "stderr")

    def output_text(self) -> str:
        """Decoded chunk of a ``stdout``/``stderr``/``stdin`` event."""
        return decode_value(self.data.get("value", ""), self.data.get("encoding", "ascii")).decode(
            "utf-8", errors="replace"
        )


def decode_value(value: str, encoding: str) -> bytes:
    """Decode an API stream value (``ascii`` or ``base64``) to bytes."""
    if encoding == "base64":
        return base64.b64decode(value)
    return value.encode("utf-8")
