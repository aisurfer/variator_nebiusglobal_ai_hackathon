"""Jobs, endpoints and operations as returned by the Nebius Serverless AI REST API.

Parsed from ``nebius.ai.v1.Job`` / ``Endpoint`` and ``nebius.common.v1.Operation``
(https://api.nebius.cloud/openapi.json). Proto3 JSON omits default values, so every
field is optional here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from variator.cloud.errors import (
    CloudError,
    NotEnoughResources,
    QuotaExceeded,
    WorkloadFailed,
    error_from_status,
)
from variator.cloud.specs import EndpointState, JobState

ResourceKind = Literal["jobs", "endpoints"]


class StateDetails(BaseModel):
    """``status.stateDetails``: ``code`` is the stable identifier, ``message`` may change."""

    code: str | None = None
    message: str | None = None
    service_error: dict[str, Any] | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any] | None) -> StateDetails | None:
        if not data:
            return None
        return cls(code=data.get("code"), message=data.get("message"), service_error=data.get("serviceError"))

    @property
    def is_quota(self) -> bool:
        se = self.service_error or {}
        return self.code == "QuotaExceeded" or "quotaFailure" in se or se.get("code") == "QuotaExceeded"

    @property
    def is_capacity(self) -> bool:
        se = self.service_error or {}
        return self.code == "NotEnoughResources" or "notEnoughResources" in se

    def describe(self) -> str:
        return ": ".join(part for part in (self.code, self.message) if part) or "no details"


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _parse_state[S: (JobState, EndpointState)](enum: type[S], raw: Any) -> S:
    try:
        return enum(raw)
    except ValueError:
        return enum("STATE_UNSPECIFIED")


class _Workload(BaseModel):
    kind: ClassVar[str] = "workload"

    id: str
    name: str | None = None
    parent_id: str | None = None
    raw_state: str = "STATE_UNSPECIFIED"
    details: StateDetails | None = None
    created_at: datetime | None = None
    platform: str | None = None
    preset: str | None = None
    preemptible: bool = False
    disk_size_bytes: int | None = None
    public_endpoints: list[str] = Field(default_factory=list)
    private_endpoints: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @staticmethod
    def _common(data: dict[str, Any]) -> dict[str, Any]:
        meta = data.get("metadata") or {}
        spec = data.get("spec") or {}
        status = data.get("status") or {}
        disk = spec.get("disk") or {}
        return {
            "id": meta.get("id", ""),
            "name": meta.get("name"),
            "parent_id": meta.get("parentId"),
            "raw_state": status.get("state") or "STATE_UNSPECIFIED",
            "details": StateDetails.from_api(status.get("stateDetails")),
            "created_at": _ts(meta.get("createdAt")),
            "platform": spec.get("platform"),
            "preset": spec.get("preset"),
            "preemptible": bool(spec.get("preemptible", False)),
            "disk_size_bytes": int(disk["sizeBytes"]) if disk.get("sizeBytes") else None,
            "public_endpoints": list(status.get("publicEndpoints") or []),
            "private_endpoints": list(status.get("privateEndpoints") or []),
            "raw": data,
        }

    def failure(self) -> CloudError:
        """The exception that describes the current (failed) state."""
        state = self.raw_state
        detail = self.details.describe() if self.details else "no details"
        message = f"{self.kind} {self.id} ({self.name}) is {state}: {detail}"
        if self.details and self.details.is_quota:
            return QuotaExceeded(message, details=self.details.service_error)
        if self.details and self.details.is_capacity:
            return NotEnoughResources(f"{message}; try another platform/preset or region, or retry later")
        code = self.details.code if self.details else None
        return WorkloadFailed(message, resource_id=self.id, state=state, code=code)


class JobInfo(_Workload):
    kind: ClassVar[str] = "job"

    state: JobState = JobState.STATE_UNSPECIFIED
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> JobInfo:
        status = data.get("status") or {}
        common = cls._common(data)
        return cls(
            **common,
            state=_parse_state(JobState, common["raw_state"]),
            started_at=_ts(status.get("startedAt")),
            finished_at=_ts(status.get("finishedAt")),
        )

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def succeeded(self) -> bool:
        return self.state is JobState.COMPLETED

    @property
    def runtime_s(self) -> float | None:
        if self.started_at and self.finished_at:
            return max(0.0, (self.finished_at - self.started_at).total_seconds())
        return None

    def raise_for_state(self) -> None:
        """Raise ``QuotaExceeded``, ``NotEnoughResources`` or ``WorkloadFailed`` for a failed job."""
        if self.state in (JobState.FAILED, JobState.ERROR, JobState.CANCELLED):
            raise self.failure()


class EndpointInfo(_Workload):
    kind: ClassVar[str] = "endpoint"

    state: EndpointState = EndpointState.STATE_UNSPECIFIED

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> EndpointInfo:
        common = cls._common(data)
        return cls(**common, state=_parse_state(EndpointState, common["raw_state"]))

    @property
    def https_url(self) -> str | None:
        """The managed public URL; documented to be available once the endpoint is RUNNING."""
        return next((url for url in self.public_endpoints if url.startswith("https://")), None)

    def raise_for_state(self) -> None:
        if self.state is EndpointState.ERROR:
            raise self.failure()


class Operation(BaseModel):
    """A long-running operation returned by create/cancel/delete/start/stop calls."""

    id: str
    resource_id: str | None = None
    description: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    done: bool = False
    status_code: int | None = None
    status_message: str | None = None
    status_details: list[Any] = Field(default_factory=list)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Operation:
        # `status` is set only once the operation has completed; a successful status may be
        # serialized as {} because proto3 JSON omits code 0.
        status = data.get("status")
        done = status is not None or bool(data.get("finishedAt"))
        status = status or {}
        return cls(
            id=data.get("id", ""),
            resource_id=data.get("resourceId") or None,
            description=data.get("description"),
            created_at=_ts(data.get("createdAt")),
            finished_at=_ts(data.get("finishedAt")),
            done=done,
            status_code=int(status.get("code", 0)) if done else None,
            status_message=status.get("message"),
            status_details=list(status.get("details") or []),
        )

    @property
    def succeeded(self) -> bool:
        return self.done and not self.status_code

    def raise_for_status(self) -> None:
        if self.done and self.status_code:
            raise error_from_status(
                self.status_code, self.status_message or f"operation {self.id} failed", self.status_details
            )
