"""Client for a self-hosted NVIDIA cuOpt HTTP server.

Intended deployment: the ``nvidia/cuopt`` container on a Nebius Serverless Endpoint (GPU), with
``--auth token`` so every call carries ``Authorization: Bearer <token>``. Flow: ``POST
/cuopt/request`` returns ``{"reqId": ...}``; ``GET /cuopt/solution/{reqId}`` returns just the
``reqId`` while the job is queued or running and the full envelope ``{"response":
{"solver_response": {"status", "solution"}}}`` once done; ``DELETE /cuopt/solution/{reqId}``
frees it. The same endpoints exist on the 26.10 HTTP proxy (default container command) and on
the deprecated legacy server.

Verified against https://github.com/NVIDIA/cuopt/tree/v26.10.00 :
``python/cuopt_server/cuopt_server/proxy_webserver.py`` (routes, Accept handling, errors),
``.../utils/linear_programming/data_definition.py`` (LPData payload) and
``docs/cuopt/source/cuopt-server/examples/milp-examples.rst`` (MILP request/response).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np

from variator.optimization.formulation import MilpFormulation
from variator.optimization.outcome import SolverOutcome, cuopt_status

# TODO(settings): move into variator.config.Settings (cuopt_server_url / cuopt_server_token).
SERVER_URL_ENV = "CUOPT_SERVER_URL"
SERVER_TOKEN_ENV = "CUOPT_SERVER_TOKEN"

# Extra wall time beyond the solver time limit for upload, queueing and container warm-up.
POLL_GRACE_S = 60.0
# First wait between polls; doubles up to the client's max_poll_interval_s.
POLL_INTERVAL_S = 0.5

_JSON = "application/json"


class CuoptServerError(RuntimeError):
    """The cuOpt server could not be reached, rejected the request, or the job failed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CuoptServerConfig:
    url: str
    token: str | None = None

    @classmethod
    def from_env(cls) -> CuoptServerConfig | None:
        """Read ``CUOPT_SERVER_URL`` / ``CUOPT_SERVER_TOKEN``; ``None`` when no URL is set."""
        url = os.environ.get(SERVER_URL_ENV, "").strip()
        if not url:
            return None
        token = os.environ.get(SERVER_TOKEN_ENV, "").strip() or None
        return cls(url=url, token=token)


def is_configured() -> bool:
    """True when a cuOpt server URL is configured in the environment."""
    return CuoptServerConfig.from_env() is not None


def _bounds(values: np.ndarray) -> list[float | str]:
    # LPData encodes infinities as the strings "inf" / "ninf" (strict JSON has no Infinity).
    return ["inf" if v == np.inf else "ninf" if v == -np.inf else float(v) for v in values]


def build_payload(formulation: MilpFormulation, *, time_limit_s: float) -> dict[str, Any]:
    """Build the ``POST /cuopt/request`` body (LPData schema; MILP via ``variable_types``)."""
    f = formulation
    return {
        "csr_constraint_matrix": {
            "offsets": f.a.indptr.astype(np.int64).tolist(),
            "indices": f.a.indices.astype(np.int64).tolist(),
            "values": f.a.data.astype(np.float64).tolist(),
        },
        "constraint_bounds": {"upper_bounds": _bounds(f.row_upper), "lower_bounds": _bounds(f.row_lower)},
        "objective_data": {"coefficients": f.c.tolist(), "scalability_factor": 1.0, "offset": 0.0},
        "variable_bounds": {"upper_bounds": _bounds(f.var_upper), "lower_bounds": _bounds(f.var_lower)},
        "variable_types": ["I" if flag == 1 else "C" for flag in f.integrality],
        "maximize": False,
        # time_limit is mandatory for MILP on the server (SolverConfig docs).
        "solver_config": {"time_limit": float(time_limit_s)},
    }


def parse_solution(envelope: dict[str, Any], formulation: MilpFormulation) -> SolverOutcome:
    """Turn a finished ``/cuopt/solution`` envelope into a ``SolverOutcome``."""
    try:
        solver_response = envelope["response"]["solver_response"]
    except (KeyError, TypeError) as exc:
        raise CuoptServerError(f"unexpected cuOpt response shape: {str(envelope)[:300]}") from exc
    if isinstance(solver_response, list):  # batch-LP shape; we always send a single problem
        solver_response = solver_response[0]
    name = str(solver_response.get("status", "NoTermination"))
    status = cuopt_status(name)
    solution = solver_response.get("solution") or {}
    x = None
    if status in ("optimal", "feasible"):
        primal = solution.get("primal_solution")
        if primal is None or len(primal) != formulation.n_vars:
            size = None if primal is None else len(primal)
            return SolverOutcome(
                solver="cuopt",
                status="error",
                x=None,
                proven_optimal=False,
                message=f"cuOpt server returned {size} values for {formulation.n_vars} variables",
            )
        x = np.asarray(primal, dtype=np.float64)
    gap = (solution.get("milp_statistics") or {}).get("mip_gap")
    return SolverOutcome(
        solver="cuopt",
        status=status,
        x=x,
        proven_optimal=status == "optimal",
        message=f"cuOpt server termination status {name}",
        mip_gap=None if gap is None else float(gap),
    )


class CuoptServerClient:
    """Submit-and-poll client. ``sleep`` and ``clock`` are injectable so tests never wait."""

    def __init__(
        self,
        config: CuoptServerConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        request_timeout_s: float = 60.0,
        poll_interval_s: float | None = None,
        max_poll_interval_s: float = 4.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        headers = {
            "Accept": _JSON,  # the proxy defaults to msgpack for wildcard Accept values
            "CLIENT-VERSION": "custom",  # skips the server's client-version check
        }
        if config.token:
            # Nebius Serverless Endpoints with --auth token expect a Bearer token:
            # https://docs.nebius.com/serverless/endpoints/manage
            headers["Authorization"] = f"Bearer {config.token}"
        self.url = config.url
        self._http = httpx.Client(
            base_url=config.url, headers=headers, timeout=request_timeout_s, transport=transport
        )
        self._poll_interval_s = POLL_INTERVAL_S if poll_interval_s is None else poll_interval_s
        self._max_poll_interval_s = max_poll_interval_s
        self._sleep = sleep
        self._clock = clock

    def __enter__(self) -> CuoptServerClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /cuopt/request``; returns the envelope (normally just ``{"reqId": ...}``)."""
        return self._call("POST", "/cuopt/request", json=payload)

    def get_solution(self, req_id: str) -> dict[str, Any]:
        """``GET /cuopt/solution/{reqId}``; just ``{"reqId": ...}`` while the job is pending."""
        return self._call("GET", f"/cuopt/solution/{req_id}")

    def delete_solution(self, req_id: str) -> None:
        """Best-effort ``DELETE /cuopt/solution/{reqId}`` so a long-lived endpoint does not pile up jobs."""
        try:
            self._http.delete(f"/cuopt/solution/{req_id}")
        except httpx.HTTPError:
            pass

    def solve(self, payload: dict[str, Any], *, wait_s: float) -> dict[str, Any]:
        """Submit, poll until finished or ``wait_s`` elapses, then free the job; returns the envelope."""
        envelope = self.submit(payload)
        req_id = envelope.get("reqId")
        deadline = self._clock() + wait_s
        interval = self._poll_interval_s
        while not _finished(envelope):
            if not req_id:
                raise CuoptServerError(f"cuOpt server returned no reqId: {str(envelope)[:300]}")
            if self._clock() >= deadline:
                self.delete_solution(str(req_id))  # do not leave an abandoned job running
                raise CuoptServerError(f"cuOpt server did not finish request {req_id} within {wait_s:.0f}s")
            self._sleep(interval)
            interval = min(interval * 2, self._max_poll_interval_s)
            envelope = self.get_solution(str(req_id))
        if req_id:
            self.delete_solution(str(req_id))
        if "result_file" in envelope:
            raise CuoptServerError("cuOpt server wrote the result to a server-side file (result dir enabled)")
        return envelope

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CuoptServerError(f"cannot reach cuOpt server at {self.url}: {exc}") from exc
        if response.is_success:
            return _json_body(response)
        raise _http_error(response)


def _finished(envelope: dict[str, Any]) -> bool:
    return "response" in envelope or "result_file" in envelope


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        content_type = response.headers.get("content-type", "?")
        raise CuoptServerError(f"cuOpt server sent a non-JSON body ({content_type})") from exc
    if not isinstance(body, dict):
        raise CuoptServerError(f"unexpected cuOpt response: {str(body)[:300]}")
    return body


def _http_error(response: httpx.Response) -> CuoptServerError:
    code = response.status_code
    try:
        detail = str(response.json().get("error", ""))
    except Exception:  # error bodies may be HTML from a gateway
        detail = response.text[:300]
    if code in (401, 403):
        message = f"cuOpt server rejected the credentials (HTTP {code}); check {SERVER_TOKEN_ENV}"
    elif code == 404:
        message = f"cuOpt request not found (HTTP 404): {detail}"
    elif code == 409:
        message = f"cuOpt solver job failed (HTTP 409): {detail}"
    elif code == 413:
        message = "problem too large for the cuOpt server (HTTP 413)"
    elif code == 422:
        message = f"cuOpt server rejected the problem (HTTP 422): {detail}"
    else:
        message = f"cuOpt server error (HTTP {code}): {detail}"
    return CuoptServerError(message, status_code=code)


def solve_cuopt_server(
    formulation: MilpFormulation,
    *,
    time_limit_s: float,
    client: CuoptServerClient | None = None,
) -> SolverOutcome:
    """Solve on a remote cuOpt server configured via env vars (or the given client)."""
    owned = client is None
    if client is None:
        config = CuoptServerConfig.from_env()
        if config is None:
            raise CuoptServerError(f"{SERVER_URL_ENV} is not set")
        client = CuoptServerClient(config)
    try:
        payload = build_payload(formulation, time_limit_s=time_limit_s)
        envelope = client.solve(payload, wait_s=time_limit_s + POLL_GRACE_S)
    finally:
        if owned:
            client.close()
    return parse_solution(envelope, formulation)
