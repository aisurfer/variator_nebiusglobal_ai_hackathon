"""Typed errors for the Sandboxes REST API.

HTTP statuses are mapped per the Contree OpenAPI spec behind
https://docs.tokenfactory.nebius.com/api-reference/sandboxes (error body:
``{"error": str | object | list, "status": int, "traceback": [...]}``).
"""

from __future__ import annotations

import re
from typing import Any

# ASSUMPTION: the spec documents no dedicated status for concurrency/timeout limit
# rejections; besides 429 we classify 400/403/409 bodies that talk about limits as
# limit errors (https://docs.tokenfactory.nebius.com/sandboxes/guides/run-concurrent-workloads.md).
_LIMIT_RE = re.compile(r"\b(limit|quota|concurren\w*|too many)\b", re.IGNORECASE)


class SandboxError(RuntimeError):
    """Base class for everything this package raises."""

    def __init__(self, message: str, *, operation_id: str | None = None) -> None:
        super().__init__(message)
        self.operation_id = operation_id


class SandboxHTTPError(SandboxError):
    """The API answered with an error status."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        body: Any = None,
        retry_after_s: float | None = None,
        operation_id: str | None = None,
    ) -> None:
        super().__init__(f"HTTP {status_code}: {message}", operation_id=operation_id)
        self.status_code = status_code
        self.body = body
        self.retry_after_s = retry_after_s


class SandboxAuthError(SandboxHTTPError):
    """401: missing/invalid API key or missing ``Project`` header."""


class SandboxPermissionError(SandboxAuthError):
    """403: token lacks a Sandboxes permission, or the project has no Beta access yet."""


class SandboxLimitError(SandboxHTTPError):
    """429 or a limit/quota rejection (e.g. the 50 concurrent operations cap)."""


class SandboxNotFoundError(SandboxHTTPError):
    """404: unknown operation, image, tag or file."""


class SandboxRequestError(SandboxHTTPError):
    """400/422: the request was rejected as invalid."""


class SandboxConflictError(SandboxHTTPError):
    """409: e.g. cancelling an operation that is already terminal."""


class SandboxTransportError(SandboxError):
    """Network failure. For a create request the submission outcome is unknown."""


class SandboxTimeoutError(SandboxError):
    """A client-side deadline expired while waiting; remote work may still be running."""


class SandboxOperationFailed(SandboxError):
    """The operation ended ``FAILED`` or ``CANCELLED`` (a platform outcome, not a process exit code)."""

    def __init__(self, message: str, *, operation_id: str | None, status: str) -> None:
        super().__init__(message, operation_id=operation_id)
        self.status = status


class SandboxStreamError(SandboxError):
    """The SSE event stream kept failing after all reconnect attempts."""


class SandboxOutputTooLarge(SandboxError):
    """A downloaded artifact exceeded the caller's size limit."""


def error_message(body: Any, fallback: str) -> str:
    """Extract a readable message from an API error body."""
    if isinstance(body, dict) and "error" in body:
        err = body["error"]
        return err if isinstance(err, str) else str(err)
    if isinstance(body, str) and body.strip():
        return body.strip()[:500]
    return fallback


def http_error(
    status_code: int,
    body: Any,
    *,
    retry_after_s: float | None = None,
    reason: str = "",
    operation_id: str | None = None,
) -> SandboxHTTPError:
    """Build the most specific ``SandboxHTTPError`` for a status code and body."""
    message = error_message(body, reason or "request failed")
    cls: type[SandboxHTTPError]
    if status_code == 429 or (status_code in (400, 403, 409) and _LIMIT_RE.search(message)):
        cls = SandboxLimitError
    elif status_code == 401:
        cls = SandboxAuthError
    elif status_code == 403:
        cls = SandboxPermissionError
        message += (
            " (Sandboxes is in Beta: check that access was granted for this project, "
            "that the Project header is correct, and that the token has the needed permission)"
        )
    elif status_code == 404:
        cls = SandboxNotFoundError
    elif status_code == 409:
        cls = SandboxConflictError
    elif status_code in (400, 422):
        cls = SandboxRequestError
    else:
        cls = SandboxHTTPError
    return cls(
        message,
        status_code=status_code,
        body=body,
        retry_after_s=retry_after_s,
        operation_id=operation_id,
    )
