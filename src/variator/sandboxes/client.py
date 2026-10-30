"""HTTP client for Nebius Token Factory Sandboxes (REST API, Beta).

Endpoints, bodies and statuses follow the Contree OpenAPI spec behind
https://docs.tokenfactory.nebius.com/api-reference/sandboxes and the guides under
https://docs.tokenfactory.nebius.com/sandboxes/. Base URL: ``{sandboxes_base_url}/v1``;
every request carries ``Authorization: Bearer <NEBIUS_API_KEY>`` and ``Project: <NEBIUS_PROJECT_ID>``.

Retry policy: idempotent requests are retried on transport errors, 429 and
502/503/504 (honouring ``Retry-After``). Create requests (``POST /instances``,
``POST /images/import``) are retried only on 429, because the docs warn that a
transport failure leaves the submission outcome unknown and must not be blindly resent.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from variator.config import Settings
from variator.sandboxes.errors import (
    SandboxConflictError,
    SandboxError,
    SandboxHTTPError,
    SandboxNotFoundError,
    SandboxOutputTooLarge,
    SandboxStreamError,
    SandboxTimeoutError,
    SandboxTransportError,
    http_error,
)
from variator.sandboxes.models import (
    Image,
    Operation,
    OperationEvent,
    SpawnRequest,
    UploadedFile,
    WhoAmI,
)
from variator.sandboxes.sse import SSE_ERROR_EVENT, iter_sse_frames, parse_event

# Env override for the image used by run_python when none is given (local
# workaround until Settings grows a field for it).
IMAGE_ENV_VAR = "VARIATOR_SANDBOX_IMAGE"

_RETRY_STATUSES = frozenset({429, 502, 503, 504})
# Statuses the events endpoint documents as "retry the observation".
_STREAM_RETRY_STATUSES = frozenset({410, 425, 502, 504})
_MAX_RETRY_AFTER_S = 30.0
# Time allowed for queueing and VM start on top of the guest execution timeout.
DEFAULT_START_GRACE_S = 120.0


class _StreamBroken(Exception):
    """Internal: the current SSE connection must be re-opened from the cursor."""


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return min(max(float(value), 0.0), _MAX_RETRY_AFTER_S)
    except ValueError:
        return None  # HTTP-date form is not used by this API


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return response.text


class SandboxesClient:
    """Synchronous client for the Sandboxes REST API. Thread-safe (one shared ``httpx.Client``).

    ``transport`` lets tests inject an ``httpx.MockTransport``; ``sleep``/``clock``
    make polling and back-off deterministic in tests.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        api_key: str | None = None,
        project_id: str | None = None,
        default_image: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or Settings.from_env()
        key = api_key or self.settings.require("nebius_api_key")
        project = project_id or self.settings.require("nebius_project_id")
        self.base_url = self.settings.sandboxes_base_url.rstrip("/") + "/v1/"
        self.default_image = default_image or os.environ.get(IMAGE_ENV_VAR) or None
        self.max_retries = self.settings.max_retries if max_retries is None else max_retries
        self._timeout_s = timeout_s or self.settings.request_timeout_s
        self._sleep = sleep
        self._clock = clock
        self._uploads: dict[str, UploadedFile] = {}
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {key}",
                "Project": project,
                "Accept": "application/json",
            },
            timeout=self._timeout_s,
            transport=transport,
        )

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SandboxesClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth and limits ---------------------------------------------------------

    def whoami(self) -> WhoAmI:
        """Token identity, permission map (``spawn``, ``spawn_disposable``, ...) and limits."""
        return WhoAmI.model_validate(self._request("GET", "whoami").json())

    def limits(self) -> dict[str, int]:
        """Account-effective limits, e.g. ``instance_max_timeout``, ``instance_max_concurrency``."""
        return self.whoami().limits

    # -- images ------------------------------------------------------------------

    def list_images(
        self,
        *,
        tag_prefix: str | None = None,
        tagged: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Image]:
        """List images visible to the project (public ones plus the project's own)."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if tag_prefix:
            params["tag"] = tag_prefix
        if tagged is not None:
            params["tagged"] = "1" if tagged else "0"
        payload = self._request("GET", "images", params=params).json()
        return [Image.model_validate(item) for item in payload.get("images", [])]

    def find_image(self, tag: str) -> Image | None:
        """Return the image whose tag equals ``tag`` exactly (the API filters by prefix)."""
        tag = tag.removeprefix("tag:")
        return next((img for img in self.list_images(tag_prefix=tag) if img.tag == tag), None)

    def inspect_image(self, image_uuid: str) -> Image:
        return Image.model_validate(self._request("GET", f"inspect/{image_uuid}/").json())

    def import_image(self, registry_url: str, *, tag: str | None = None, timeout_s: int = 300) -> str:
        """Start an OCI import (``docker://docker.io/library/python:3.12-slim``); return the operation id.

        Leave ``tag`` empty unless you mean it: a tag equal to a public tag replaces it.
        """
        body: dict[str, Any] = {"registry": {"url": registry_url}, "timeout": timeout_s}
        if tag:
            body["tag"] = tag
        response = self._request("POST", "images/import", json=body, idempotent=False)
        return response.json()["uuid"]

    def tag_image(self, image_uuid: str, tag: str) -> Image:
        """Assign ``tag`` to an image (moves the tag if another image had it)."""
        response = self._request("PATCH", f"images/{image_uuid}/tag", json={"tag": tag})
        return Image.model_validate(response.json())

    # -- files -------------------------------------------------------------------

    def file_by_sha256(self, sha256: str) -> UploadedFile | None:
        """Look up an already uploaded file of this project by content hash."""
        try:
            response = self._request("GET", f"files/{sha256}")
        except SandboxNotFoundError:
            return None
        return UploadedFile.model_validate(response.json())

    def upload_bytes(self, data: bytes, *, dedupe: bool = True) -> UploadedFile:
        """Upload content for a spawn ``files`` map; skip the upload if the hash already exists."""
        digest = hashlib.sha256(data).hexdigest()
        if dedupe:
            cached = self._uploads.get(digest)
            if cached is not None:
                return cached
            existing = self.file_by_sha256(digest)
            if existing is not None:
                self._uploads[digest] = existing
                return existing
        response = self._request(
            "POST",
            "files",
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        uploaded = UploadedFile.model_validate(response.json())
        self._uploads[digest] = uploaded
        return uploaded

    # -- operations --------------------------------------------------------------

    def spawn(self, request: SpawnRequest) -> str:
        """Submit ``POST /instances``; return the operation id (not an image id)."""
        response = self._request("POST", "instances", json=request.body(), idempotent=False)
        return response.json()["uuid"]

    def get_operation(self, operation_id: str, *, inflight: bool = False) -> Operation:
        """Current status; ``inflight=True`` also returns partial output of a running instance."""
        return self._fetch_operation(operation_id, inflight=inflight)[0]

    def wait(
        self,
        operation_id: str,
        *,
        timeout_s: float = 600.0,
        poll_interval_s: float = 1.0,
        max_poll_interval_s: float = 5.0,
        on_poll: Callable[[Operation], None] | None = None,
    ) -> Operation:
        """Poll until ``SUCCESS``/``FAILED``/``CANCELLED``; raise ``SandboxTimeoutError`` at the deadline.

        A client timeout does not cancel remote work; see ``execute`` for that.
        """
        deadline = self._clock() + timeout_s
        interval = poll_interval_s
        while True:
            operation, retry_after = self._fetch_operation(operation_id)
            if on_poll is not None:
                on_poll(operation)
            if operation.terminal:
                return operation
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise SandboxTimeoutError(
                    f"operation {operation_id} still {operation.status} after {timeout_s:.0f}s",
                    operation_id=operation_id,
                )
            delay = retry_after if retry_after is not None else interval
            self._sleep(max(0.0, min(delay, remaining)))
            interval = min(interval * 1.5, max_poll_interval_s)

    def execute(
        self,
        request: SpawnRequest,
        *,
        wait_timeout_s: float | None = None,
        cancel_on_timeout: bool = True,
        on_poll: Callable[[Operation], None] | None = None,
    ) -> Operation:
        """Spawn and wait. On a client deadline, best-effort cancel the owned operation and re-raise."""
        operation_id = self.spawn(request)
        if wait_timeout_s is None:
            wait_timeout_s = (request.timeout or 600) + DEFAULT_START_GRACE_S
        try:
            return self.wait(operation_id, timeout_s=wait_timeout_s, on_poll=on_poll)
        except SandboxTimeoutError:
            if cancel_on_timeout:
                try:
                    self.cancel(operation_id)
                except SandboxError:
                    pass  # the timeout is the error worth reporting
            raise

    def cancel(self, operation_id: str) -> bool:
        """Request cancellation. Returns False if the operation was already terminal (409)."""
        try:
            self._request("DELETE", f"operations/{operation_id}")
        except SandboxConflictError:
            return False
        return True

    def stream_events(
        self,
        operation_id: str,
        *,
        follow: bool = True,
        last_event_id: int | None = None,
        spid: int | None = None,
        max_reconnects: int = 5,
        read_timeout_s: float = 90.0,
    ) -> Iterator[OperationEvent]:
        """Yield operation events over SSE, resuming with ``Last-Event-Id`` after drops.

        The cursor advances only after the consumer has taken an event, and events
        at or below the cursor are skipped, so a reconnect never yields duplicates.
        Ends after the ``completion`` event (or, with ``follow=False``, at end of log).
        Stream completion is not execution success: query ``get_operation`` afterwards.
        """
        cursor = last_event_id
        failures = 0
        while True:
            params: dict[str, Any] = {"follow": "1" if follow else "0"}
            if spid is not None:
                params["spid"] = spid
            headers = {"Accept": "text/event-stream"}
            if cursor is not None:
                headers["Last-Event-Id"] = str(cursor)
            timeout = httpx.Timeout(self._timeout_s, read=read_timeout_s)
            delay: float | None = None
            try:
                with self._http.stream(
                    "GET",
                    f"operations/{operation_id}/events",
                    params=params,
                    headers=headers,
                    timeout=timeout,
                ) as response:
                    if response.status_code in _STREAM_RETRY_STATUSES:
                        response.read()
                        delay = _retry_after(response)
                        raise _StreamBroken(f"HTTP {response.status_code}")
                    if response.status_code >= 400:
                        response.read()
                        raise self._error(response, operation_id=operation_id)
                    for frame in iter_sse_frames(response.iter_lines()):
                        if frame.event == SSE_ERROR_EVENT:
                            raise _StreamBroken(f"sse_error: {frame.data}")
                        try:
                            event = parse_event(frame)
                        except ValueError as exc:
                            raise _StreamBroken(f"malformed event: {exc}") from exc
                        if cursor is not None and event.id <= cursor:
                            continue
                        failures = 0
                        yield event
                        cursor = event.id
                        if event.type == "completion":
                            return
                    if not follow:
                        return
                    raise _StreamBroken("stream closed before the completion event")
            except (_StreamBroken, httpx.TransportError) as exc:
                failures += 1
                if failures > max_reconnects:
                    raise SandboxStreamError(
                        f"event stream for {operation_id} failed {failures} times: {exc}",
                        operation_id=operation_id,
                    ) from exc
                self._sleep(delay if delay is not None else self._backoff(failures - 1))

    # -- image inspection --------------------------------------------------------

    def download_file(self, image_uuid: str, path: str, *, max_bytes: int | None = None) -> bytes:
        """Read one file from a stored image (no guest compute). Retries transient failures."""
        attempt = 0
        while True:
            try:
                return self._download_once(image_uuid, path, max_bytes)
            except (SandboxTransportError, SandboxHTTPError) as exc:
                status = getattr(exc, "status_code", None)
                retryable = status is None or status in _RETRY_STATUSES
                if not retryable or attempt >= self.max_retries:
                    raise
                self._sleep(getattr(exc, "retry_after_s", None) or self._backoff(attempt))
                attempt += 1

    def file_exists(self, image_uuid: str, path: str) -> bool:
        """True if ``path`` is a regular file in the image."""
        try:
            self._request("HEAD", f"inspect/{image_uuid}/download", params={"path": path})
        except SandboxNotFoundError:
            return False
        return True

    def list_image_files(self, image_uuid: str, path: str) -> list[dict[str, Any]]:
        """Directory listing inside a stored image (``DirectoryList.files``)."""
        response = self._request("GET", f"inspect/{image_uuid}/list", params={"path": path})
        return list(response.json().get("files", []))

    # -- internals ---------------------------------------------------------------

    def _fetch_operation(
        self, operation_id: str, *, inflight: bool = False
    ) -> tuple[Operation, float | None]:
        params = {"inflight": "1"} if inflight else None
        response = self._request("GET", f"operations/{operation_id}", params=params)
        return Operation.model_validate(response.json()), _retry_after(response)

    def _download_once(self, image_uuid: str, path: str, max_bytes: int | None) -> bytes:
        try:
            with self._http.stream(
                "GET", f"inspect/{image_uuid}/download", params={"path": path}
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise self._error(response)
                declared = response.headers.get("Content-Length")
                if max_bytes is not None and declared and declared.isdigit() and int(declared) > max_bytes:
                    raise SandboxOutputTooLarge(f"{path} is {declared} bytes, limit is {max_bytes}")
                buffer = bytearray()
                for chunk in response.iter_bytes():
                    buffer += chunk
                    if max_bytes is not None and len(buffer) > max_bytes:
                        raise SandboxOutputTooLarge(f"{path} exceeds the {max_bytes}-byte limit")
                return bytes(buffer)
        except httpx.TransportError as exc:
            raise SandboxTransportError(f"download {path} from {image_uuid}: {exc}") from exc

    def _request(self, method: str, path: str, *, idempotent: bool = True, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if idempotent and attempt < self.max_retries:
                    self._sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                hint = "" if idempotent else " (submission outcome unknown: list operations before resending)"
                raise SandboxTransportError(f"{method} {path} failed: {exc}{hint}") from exc
            if response.status_code < 400:
                return response
            status = response.status_code
            retryable = status == 429 or (idempotent and status in _RETRY_STATUSES)
            if retryable and attempt < self.max_retries:
                self._sleep(_retry_after(response) or self._backoff(attempt))
                attempt += 1
                continue
            raise self._error(response)

    @staticmethod
    def _error(response: httpx.Response, *, operation_id: str | None = None) -> SandboxHTTPError:
        return http_error(
            response.status_code,
            _body(response),
            retry_after_s=_retry_after(response),
            reason=response.reason_phrase,
            operation_id=operation_id,
        )

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(0.5 * 2**attempt, 8.0)
