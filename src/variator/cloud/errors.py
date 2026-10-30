"""Exceptions for Nebius Serverless AI calls and their mapping from API errors.

The REST API returns ``{"code": <gRPC code>, "message": "..."}`` with the HTTP status
from https://docs.nebius.com/rest-api/errors.md. Finished operations carry the same
gRPC code in ``status.code``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from variator.config import MissingCredentialsError

__all__ = [
    "AuthenticationError",
    "CliError",
    "CloudError",
    "ConflictError",
    "InputTooLarge",
    "InvalidRequestError",
    "MissingCredentialsError",
    "NebiusApiError",
    "NotEnoughResources",
    "NotFoundError",
    "PermissionDeniedError",
    "QuotaExceeded",
    "RateLimitedError",
    "ServiceUnavailableError",
    "WaitTimeout",
    "WorkloadFailed",
    "error_from_response",
    "error_from_status",
]

QUOTAS_URL = "https://console.nebius.com/quota"


class CloudError(RuntimeError):
    """Base class for everything raised by ``variator.cloud``."""


class NebiusApiError(CloudError):
    """An error returned by the Nebius AI Cloud API (HTTP or operation status)."""

    hint = ""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        grpc_code: int | None = None,
        details: Any = None,
    ) -> None:
        self.api_message = message
        self.status_code = status_code
        self.grpc_code = grpc_code
        self.details = details
        prefix = f"HTTP {status_code}: " if status_code else ""
        text = f"{prefix}{message}"
        if self.hint:
            text = f"{text} ({self.hint})"
        super().__init__(text)


class AuthenticationError(NebiusApiError):
    hint = (
        "IAM access tokens expire after 12 hours: run `nebius iam get-access-token` "
        "and update NEBIUS_IAM_TOKEN"
    )


class PermissionDeniedError(NebiusApiError):
    hint = "the account needs at least the `editor` role in the project"


class NotFoundError(NebiusApiError):
    pass


class ConflictError(NebiusApiError):
    pass


class InvalidRequestError(NebiusApiError):
    pass


class QuotaExceeded(NebiusApiError):
    hint = (
        f"check VMs, GPUs for the platform, disks, public IPs and allocations at {QUOTAS_URL}; "
        "see https://docs.nebius.com/serverless/pricing-quotas.md"
    )


class RateLimitedError(NebiusApiError):
    pass


class ServiceUnavailableError(NebiusApiError):
    pass


class NotEnoughResources(CloudError):
    """No capacity for the platform/preset within the 30-minute provisioning window."""


class WorkloadFailed(CloudError):
    """A job or endpoint reached a failure state (FAILED, ERROR, CANCELLED, STOPPED)."""

    def __init__(self, message: str, *, resource_id: str, state: str, code: str | None = None):
        self.resource_id = resource_id
        self.state = state
        self.code = code
        super().__init__(message)


class WaitTimeout(CloudError, TimeoutError):
    """A polling loop gave up before the resource reached the wanted state."""


class InputTooLarge(CloudError, ValueError):
    """The job input does not fit the chosen transport."""


class CliError(CloudError):
    """The ``nebius`` CLI is missing or exited with an unmapped error."""

    def __init__(self, message: str, *, exit_code: int | None = None, stderr: str = ""):
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(message)


# gRPC canonical code -> exception, per https://docs.nebius.com/rest-api/errors.md
_BY_GRPC: dict[int, type[NebiusApiError]] = {
    3: InvalidRequestError,
    4: ServiceUnavailableError,
    5: NotFoundError,
    6: ConflictError,
    7: PermissionDeniedError,
    8: QuotaExceeded,
    9: InvalidRequestError,
    10: ConflictError,
    11: InvalidRequestError,
    13: ServiceUnavailableError,
    14: ServiceUnavailableError,
    16: AuthenticationError,
}

_BY_HTTP: dict[int, type[NebiusApiError]] = {
    400: InvalidRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    429: QuotaExceeded,
    500: ServiceUnavailableError,
    502: ServiceUnavailableError,
    503: ServiceUnavailableError,
    504: ServiceUnavailableError,
}


def _mentions(details: Any, *needles: str) -> bool:
    if details is None:
        return False
    blob = json.dumps(details, default=str)
    return any(needle in blob for needle in needles)


def _pick(grpc_code: int | None, status_code: int | None, details: Any) -> type[NebiusApiError]:
    # RESOURCE_EXHAUSTED covers both quota exhaustion and request rate limits; ServiceError
    # details tell them apart (nebius.common.v1.ServiceError.tooManyRequests / quotaFailure).
    if (grpc_code == 8 or status_code == 429) and _mentions(details, "tooManyRequests", "TooManyRequests"):
        return RateLimitedError
    if grpc_code is not None and grpc_code in _BY_GRPC:
        return _BY_GRPC[grpc_code]
    if status_code is not None:
        if status_code in _BY_HTTP:
            return _BY_HTTP[status_code]
        if status_code >= 500:
            return ServiceUnavailableError
    return NebiusApiError


def error_from_status(grpc_code: int | None, message: str, details: Any = None) -> NebiusApiError:
    """Build an exception from a gRPC status (e.g. a failed operation's ``status``)."""
    cls = _pick(grpc_code, None, details)
    return cls(message or f"gRPC code {grpc_code}", grpc_code=grpc_code, details=details)


def error_from_response(response: httpx.Response) -> NebiusApiError:
    """Build an exception from a non-2xx REST response."""
    grpc_code: int | None = None
    details: Any = None
    message = response.reason_phrase or "request failed"
    try:
        body = response.json()
    except ValueError:
        body = None
        if response.text:
            message = response.text[:500]
    if isinstance(body, dict):
        raw_code = body.get("code")
        grpc_code = raw_code if isinstance(raw_code, int) else None
        message = str(body.get("message") or message)
        details = body.get("details")
    cls = _pick(grpc_code, response.status_code, details)
    return cls(message, status_code=response.status_code, grpc_code=grpc_code, details=details)
