"""REST client for Nebius Serverless AI jobs and endpoints.

Uses ``https://api.nebius.cloud/ai/v1/...`` with ``Authorization: Bearer <IAM token>``
(https://docs.nebius.com/rest-api/authentication.md). Paths and bodies follow the REST
variants in https://docs.nebius.com/serverless/quickstart/jobs.md,
https://docs.nebius.com/serverless/endpoints/manage.md,
https://docs.nebius.com/serverless/maintenance/stop-start.md and the OpenAPI spec at
https://api.nebius.cloud/openapi.json.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from variator.cloud.errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    WaitTimeout,
    error_from_response,
)
from variator.cloud.resources import EndpointInfo, JobInfo, Operation, ResourceKind
from variator.cloud.specs import EndpointSpec, EndpointState, JobSpec
from variator.config import Settings

DEFAULT_API_BASE_URL = "https://api.nebius.cloud"
AI_PREFIX = "/ai/v1"

# Provisioning retries for up to 30 minutes before NotEnoughResources
# (https://docs.nebius.com/serverless/lifecycle.md), then image pull and start.
DEFAULT_ENDPOINT_WAIT_S = 40 * 60
DEFAULT_JOB_WAIT_S = 3 * 3600

_RETRYABLE = (ServiceUnavailableError, RateLimitedError)


class ServerlessClient:
    """Create, inspect and delete Serverless AI jobs and endpoints.

    The IAM token is required at construction (``NEBIUS_IAM_TOKEN``); the project ID
    (``NEBIUS_CLOUD_PROJECT_ID``) only for create and list calls. Pass ``token_provider``
    (e.g. ``variator.cloud.cli.cli_access_token``) to refresh an expired token once on 401.
    ``sleep``/``clock`` are injectable so polling loops can be tested without waiting.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        project_id: str | None = None,
        base_url: str = DEFAULT_API_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self._token_provider = token_provider
        if token:
            self._token = token
        elif self.settings.nebius_iam_token or not token_provider:
            self._token = self.settings.require("nebius_iam_token")
        else:
            self._token = token_provider()
        self._project_id = project_id or self.settings.nebius_cloud_project_id
        self.default_subnet_id = self.settings.nebius_subnet_id
        self.max_retries = self.settings.max_retries if max_retries is None else max_retries
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + AI_PREFIX,
            transport=transport,
            timeout=timeout_s or self.settings.request_timeout_s,
            headers={"Accept": "application/json"},
        )

    # ------------------------------------------------------------------ plumbing

    @property
    def project_id(self) -> str:
        if self._project_id:
            return self._project_id
        return self.settings.require("nebius_cloud_project_id")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ServerlessClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _send(self, method: str, path: str, *, json: Any = None, params: Any = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"}
        return self._http.request(method, path, json=json, params=params, headers=headers)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        """Send a request and return the JSON body; raise mapped errors on failure.

        Only reads are retried: repeating a create after a lost response could start (and
        bill) a second VM.
        """
        refreshed = False
        attempt = 0
        while True:
            try:
                response = self._send(method, path, json=json, params=params)
            except httpx.TransportError as exc:
                if retry and attempt < self.max_retries:
                    attempt += 1
                    self._sleep(min(2**attempt, 30))
                    continue
                raise ServiceUnavailableError(f"{method} {path}: {exc}") from exc
            if response.is_success:
                return response.json() if response.content else {}
            error = error_from_response(response)
            if isinstance(error, AuthenticationError) and self._token_provider and not refreshed:
                self._token = self._token_provider()
                refreshed = True
                continue
            if retry and isinstance(error, _RETRYABLE) and attempt < self.max_retries:
                attempt += 1
                self._sleep(min(2**attempt, 30))
                continue
            raise error

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params, retry=True)

    def _list(self, kind: ResourceKind, page_size: int | None) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"parentId": self.project_id}
        if page_size:
            params["pageSize"] = page_size
        while True:
            body = self._get(f"/{kind}", params)
            yield from body.get("items") or []
            token = body.get("nextPageToken")
            if not token:
                return
            params = {**params, "pageToken": token}

    def _operation(self, body: dict[str, Any]) -> Operation:
        operation = Operation.from_api(body)
        operation.raise_for_status()
        return operation

    # ---------------------------------------------------------------- operations

    def get_operation(self, operation_id: str, kind: ResourceKind) -> Operation:
        """``GET /ai/v1/{jobs|endpoints}/operations/{id}``."""
        return Operation.from_api(self._get(f"/{kind}/operations/{operation_id}"))

    def wait_operation(
        self,
        operation: Operation,
        kind: ResourceKind,
        *,
        poll_interval_s: float = 2.0,
        timeout_s: float = 600.0,
    ) -> Operation:
        """Poll an operation until it completes; raise the mapped error if it failed."""
        deadline = self._clock() + timeout_s
        while not operation.done:
            if self._clock() >= deadline:
                raise WaitTimeout(f"operation {operation.id} did not finish within {timeout_s:.0f}s")
            self._sleep(poll_interval_s)
            operation = self.get_operation(operation.id, kind)
        operation.raise_for_status()
        return operation

    # ---------------------------------------------------------------------- jobs

    def create_job(self, spec: JobSpec, *, dry_run: bool = False) -> Operation:
        """``POST /ai/v1/jobs``. The returned operation's ``resource_id`` is the job ID.

        ``dry_run=True`` validates the request without creating (or billing) anything.
        """
        spec = spec.with_defaults(subnet_id=self.default_subnet_id)
        body = spec.to_request(self.project_id, dry_run=dry_run)
        return self._operation(self._request("POST", "/jobs", json=body))

    def get_job(self, job_id: str) -> JobInfo:
        return JobInfo.from_api(self._get(f"/jobs/{job_id}"))

    def get_job_by_name(self, name: str) -> JobInfo:
        return JobInfo.from_api(self._get("/jobs/get-by-name", {"parentId": self.project_id, "name": name}))

    def list_jobs(self, *, page_size: int | None = None) -> list[JobInfo]:
        """All jobs in the project (follows ``nextPageToken``)."""
        return [JobInfo.from_api(item) for item in self._list("jobs", page_size)]

    def cancel_job(self, job_id: str) -> Operation:
        """``POST /ai/v1/jobs/cancel``: stops the VM; the job stays in the list as CANCELLED."""
        return self._operation(self._request("POST", "/jobs/cancel", json={"id": job_id}))

    def delete_job(self, job_id: str) -> Operation:
        """``DELETE /ai/v1/jobs/{id}``: cancels a running job and removes it from the list."""
        return self._operation(self._request("DELETE", f"/jobs/{job_id}"))

    def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval_s: float = 15.0,
        timeout_s: float = DEFAULT_JOB_WAIT_S,
        operation: Operation | None = None,
        on_update: Callable[[JobInfo], None] | None = None,
        raise_on_failure: bool = False,
        not_found_retries: int = 3,
    ) -> JobInfo:
        """Poll until the job is COMPLETED, FAILED, CANCELLED or ERROR and return it.

        ``on_update`` is called whenever the state changes. With ``raise_on_failure`` a
        non-COMPLETED end raises ``QuotaExceeded``, ``NotEnoughResources`` or
        ``WorkloadFailed``; by default the job is returned so partial output can be fetched.
        A job that is briefly not found right after creation is retried; if ``operation`` is
        given and has failed, its error is raised instead. ``WaitTimeout`` only stops
        waiting: the job keeps running (and billing) until its own timeout or ``cancel_job``.
        """
        deadline = self._clock() + timeout_s
        last_state = None
        misses = 0
        job: JobInfo | None = None
        while True:
            try:
                job = self.get_job(job_id)
            except NotFoundError:
                if operation is not None:
                    self.get_operation(operation.id, "jobs").raise_for_status()
                misses += 1
                if misses > not_found_retries:
                    raise
            else:
                if job.state != last_state:
                    last_state = job.state
                    if on_update:
                        on_update(job)
                if job.is_terminal:
                    if raise_on_failure:
                        job.raise_for_state()
                    return job
            remaining = deadline - self._clock()
            if remaining <= 0:
                state = job.state.value if job else "not found"
                raise WaitTimeout(f"job {job_id} is still {state} after {timeout_s:.0f}s (it keeps running)")
            self._sleep(min(poll_interval_s, remaining))

    # ----------------------------------------------------------------- endpoints

    def create_endpoint(self, spec: EndpointSpec, *, dry_run: bool = False) -> Operation:
        """``POST /ai/v1/endpoints``. The returned operation's ``resource_id`` is the endpoint ID."""
        spec = spec.with_defaults(subnet_id=self.default_subnet_id)
        body = spec.to_request(self.project_id, dry_run=dry_run)
        return self._operation(self._request("POST", "/endpoints", json=body))

    def get_endpoint(self, endpoint_id: str) -> EndpointInfo:
        return EndpointInfo.from_api(self._get(f"/endpoints/{endpoint_id}"))

    def get_endpoint_by_name(self, name: str) -> EndpointInfo:
        params = {"parentId": self.project_id, "name": name}
        return EndpointInfo.from_api(self._get("/endpoints/get-by-name", params))

    def list_endpoints(self, *, page_size: int | None = None) -> list[EndpointInfo]:
        return [EndpointInfo.from_api(item) for item in self._list("endpoints", page_size)]

    def start_endpoint(self, endpoint_id: str) -> Operation:
        """``POST /ai/v1/endpoints/start``: same URL and IP as before the stop."""
        return self._operation(self._request("POST", "/endpoints/start", json={"id": endpoint_id}))

    def stop_endpoint(self, endpoint_id: str) -> Operation:
        """``POST /ai/v1/endpoints/stop``: compute and disk stop billing; the container disk is lost."""
        return self._operation(self._request("POST", "/endpoints/stop", json={"id": endpoint_id}))

    def restart_endpoint(self, endpoint_id: str) -> Operation:
        """``POST /ai/v1/endpoints/restart``: re-pulls the image tag (how to roll out a new build)."""
        return self._operation(self._request("POST", "/endpoints/restart", json={"id": endpoint_id}))

    def delete_endpoint(self, endpoint_id: str) -> Operation:
        """``DELETE /ai/v1/endpoints/{id}``: a new endpoint gets a new URL."""
        return self._operation(self._request("DELETE", f"/endpoints/{endpoint_id}"))

    def wait_until_running(
        self,
        endpoint_id: str,
        *,
        poll_interval_s: float = 10.0,
        timeout_s: float = DEFAULT_ENDPOINT_WAIT_S,
        on_update: Callable[[EndpointInfo], None] | None = None,
        stopped_grace_polls: int = 3,
    ) -> str:
        """Poll until the endpoint is RUNNING with a public URL and return that URL.

        Raises on ERROR (``QuotaExceeded`` / ``NotEnoughResources`` / ``WorkloadFailed``) and
        when it stays STOPPED/DELETING for more than ``stopped_grace_polls`` polls (a start
        request may take a moment to leave STOPPED).
        """
        deadline = self._clock() + timeout_s
        last_state = None
        idle_polls = 0
        while True:
            endpoint = self.get_endpoint(endpoint_id)
            if endpoint.state != last_state:
                last_state = endpoint.state
                if on_update:
                    on_update(endpoint)
            endpoint.raise_for_state()
            if endpoint.state is EndpointState.RUNNING:
                url = endpoint.https_url or _first_public(endpoint)
                if url:
                    return url
            if endpoint.state in (EndpointState.STOPPED, EndpointState.DELETING):
                idle_polls += 1
                if idle_polls > stopped_grace_polls:
                    raise endpoint.failure()
            remaining = deadline - self._clock()
            if remaining <= 0:
                state = endpoint.state.value
                raise WaitTimeout(f"endpoint {endpoint_id} is still {state} after {timeout_s:.0f}s")
            self._sleep(min(poll_interval_s, remaining))


def _first_public(endpoint: EndpointInfo) -> str | None:
    # Some older endpoints expose "IP:port" instead of a managed https:// URL
    # (https://docs.nebius.com/serverless/endpoints/manage.md#how-to-call-an-endpoint).
    for address in endpoint.public_endpoints:
        return address if "://" in address else f"http://{address}"
    return None
