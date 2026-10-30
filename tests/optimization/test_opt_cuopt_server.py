"""cuOpt HTTP server client: payload, submit/poll/delete flow, auth and error mapping (respx)."""

import json

import httpx
import numpy as np
import pytest
import respx
import scipy.sparse as sp
from opt_testing import random_instance, solve_canonical

from variator.optimization import cuopt_local, cuopt_server, optimize_portfolio
from variator.optimization.cuopt_server import (
    CuoptServerClient,
    CuoptServerConfig,
    CuoptServerError,
    build_payload,
    parse_solution,
    solve_cuopt_server,
)
from variator.optimization.formulation import PortfolioProblem, build_formulation
from variator.optimization.highs import solve_highs

URL = "https://cuopt.example.nebius.cloud"


def _formulation():
    inst = random_instance(5, n_scenarios=40, n_options=4)
    problem = PortfolioProblem.from_inputs(
        inst["scenarios"], inst["names"], costs=list(inst["costs"]), budget=inst["budget"], max_cvar_loss=15.0
    )
    return build_formulation(problem)


def _decode(bounds):
    return np.array([np.inf if b == "inf" else -np.inf if b == "ninf" else b for b in bounds], dtype=float)


def _solve_payload(payload):
    """Act as the server: rebuild the MILP from the JSON body and solve it."""
    csr = payload["csr_constraint_matrix"]
    c = np.array(payload["objective_data"]["coefficients"])
    shape = (len(csr["offsets"]) - 1, len(c))
    a = sp.csr_matrix((csr["values"], csr["indices"], csr["offsets"]), shape=shape)
    x = solve_canonical(
        c,
        a,
        _decode(payload["constraint_bounds"]["lower_bounds"]),
        _decode(payload["constraint_bounds"]["upper_bounds"]),
        _decode(payload["variable_bounds"]["lower_bounds"]),
        _decode(payload["variable_bounds"]["upper_bounds"]),
        np.array(payload["variable_types"]) == "I",
        payload["maximize"],
    )
    return x


def _envelope(x, status="Optimal", req_id="req-1"):
    solution = {} if x is None else {
        "problem_category": "MIP",
        "primal_solution": list(map(float, x)),
        "primal_objective": 0.0,
        "milp_statistics": {"mip_gap": 0.0, "solution_bound": 0.0},
    }
    return {"response": {"solver_response": {"status": status, "solution": solution}}, "reqId": req_id}


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self):
        return self.now


def _client(token="secret", clock=None):
    clock = clock or FakeClock()
    config = CuoptServerConfig(URL, token)
    return CuoptServerClient(
        config, poll_interval_s=0.5, max_poll_interval_s=1.0, sleep=clock.sleep, clock=clock
    )


def test_payload_follows_lpdata_schema():
    f = _formulation()
    payload = build_payload(f, time_limit_s=7)
    json.dumps(payload, allow_nan=False)  # strict JSON: infinities must be encoded as strings
    assert payload["csr_constraint_matrix"]["offsets"] == f.a.indptr.tolist()
    assert payload["constraint_bounds"]["lower_bounds"][-1] == "ninf"  # the CVaR limit row is <=
    assert payload["variable_bounds"]["lower_bounds"][f.eta_index] == "ninf"
    assert payload["variable_bounds"]["upper_bounds"][-1] == "inf"
    assert payload["variable_types"] == ["I"] * 4 + ["C"] * (f.n_vars - 4)
    assert payload["maximize"] is False
    assert payload["solver_config"] == {"time_limit": 7.0}


@respx.mock(base_url=URL, assert_all_called=True)
def test_submit_poll_solution_delete(respx_mock):
    f = _formulation()
    submitted = {}

    def on_submit(request):
        submitted.update(json.loads(request.content))
        return httpx.Response(200, json={"reqId": "req-1"})

    polls = []

    def on_poll(request):
        polls.append(request)
        if len(polls) < 3:  # queued, running, then done
            return httpx.Response(200, json={"reqId": "req-1"})
        return httpx.Response(200, json=_envelope(_solve_payload(submitted)))

    submit = respx_mock.post("/cuopt/request").mock(side_effect=on_submit)
    poll = respx_mock.get("/cuopt/solution/req-1").mock(side_effect=on_poll)
    delete = respx_mock.delete("/cuopt/solution/req-1").respond(200)
    clock = FakeClock()

    with _client(clock=clock) as client:
        outcome = solve_cuopt_server(f, time_limit_s=5, client=client)

    headers = submit.calls.last.request.headers
    assert headers["authorization"] == "Bearer secret"
    assert headers["accept"] == "application/json"
    assert headers["client-version"] == "custom"
    assert poll.calls.last.request.headers["authorization"] == "Bearer secret"
    assert poll.call_count == 3 and delete.called
    assert clock.sleeps == [0.5, 1.0, 1.0]  # backoff capped at max_poll_interval_s
    assert outcome.solver == "cuopt" and outcome.status == "optimal" and outcome.proven_optimal
    np.testing.assert_array_equal(f.selection(outcome.x), f.selection(solve_highs(f, time_limit_s=10).x))


@respx.mock(base_url=URL)
def test_no_token_means_no_authorization_header(respx_mock):
    route = respx_mock.post("/cuopt/request").respond(200, json=_envelope(None, status="Infeasible"))
    respx_mock.delete("/cuopt/solution/req-1").respond(200)
    with _client(token=None) as client:
        envelope = client.solve({"x": 1}, wait_s=10)
    assert "authorization" not in route.calls.last.request.headers
    assert envelope["response"]["solver_response"]["status"] == "Infeasible"  # finished on submit


def test_base_url_with_path_prefix():
    with respx.mock(base_url=f"{URL}/cuopt-api") as mock:
        route = mock.post("/cuopt/request").respond(200, json={"reqId": "r"})
        with CuoptServerClient(CuoptServerConfig(f"{URL}/cuopt-api")) as client:
            client.submit({})
        assert route.calls.last.request.url.path == "/cuopt-api/cuopt/request"


@pytest.mark.parametrize(
    ("status_code", "body", "match"),
    [
        (401, {"detail": "Unauthorized"}, "CUOPT_SERVER_TOKEN"),
        (403, {"detail": "Forbidden"}, "credentials"),
        (422, {"error": "csr_constraint_matrix: field required", "error_result": True}, "field required"),
        (413, {"error": "too big"}, "too large"),
        (500, {"error": "cuOpt unhandled exception"}, "HTTP 500"),
    ],
)
def test_submit_errors_are_mapped(status_code, body, match):
    with respx.mock(base_url=URL) as mock:
        mock.post("/cuopt/request").respond(status_code, json=body)
        with _client() as client, pytest.raises(CuoptServerError, match=match) as info:
            client.submit({})
    assert info.value.status_code == status_code


@respx.mock(base_url=URL)
def test_failed_job_while_polling(respx_mock):
    respx_mock.post("/cuopt/request").respond(200, json={"reqId": "req-1"})
    failed = {"error": "job req-1 failed", "error_result": True}
    respx_mock.get("/cuopt/solution/req-1").respond(409, json=failed)
    with _client() as client, pytest.raises(CuoptServerError, match="job req-1 failed"):
        client.solve({}, wait_s=10)


@respx.mock(base_url=URL)
def test_poll_times_out(respx_mock):
    respx_mock.post("/cuopt/request").respond(200, json={"reqId": "req-1"})
    poll = respx_mock.get("/cuopt/solution/req-1").respond(200, json={"reqId": "req-1"})
    delete = respx_mock.delete("/cuopt/solution/req-1").respond(200)
    with _client() as client, pytest.raises(CuoptServerError, match="did not finish"):
        client.solve({}, wait_s=3)
    assert poll.call_count == 4  # sleeps 0.5 + 1 + 1 + 1 reach the 3 s deadline
    assert delete.called  # the abandoned job is cleaned up


@respx.mock(base_url=URL)
def test_unreachable_server(respx_mock):
    respx_mock.post("/cuopt/request").mock(side_effect=httpx.ConnectError("connection refused"))
    with _client() as client, pytest.raises(CuoptServerError, match="cannot reach"):
        client.submit({})


@respx.mock(base_url=URL)
def test_non_json_body(respx_mock):
    respx_mock.post("/cuopt/request").respond(
        200, content=b"\x81\xa5reqId\xa1r", headers={"content-type": "application/vnd.msgpack"}
    )
    with _client() as client, pytest.raises(CuoptServerError, match="non-JSON"):
        client.submit({})


def test_parse_solution_statuses():
    f = _formulation()
    infeasible = parse_solution(_envelope(None, status="Infeasible"), f)
    assert infeasible.status == "infeasible" and infeasible.x is None
    feasible = parse_solution(_envelope(np.zeros(f.n_vars), status="FeasibleFound"), f)
    assert feasible.status == "feasible" and not feasible.proven_optimal
    short = parse_solution(_envelope([0.0, 1.0]), f)
    assert short.status == "error" and "2 values" in short.message
    with pytest.raises(CuoptServerError, match="shape"):
        parse_solution({"reqId": "x"}, f)


def test_config_from_env(monkeypatch):
    assert CuoptServerConfig.from_env() is None
    monkeypatch.setenv("CUOPT_SERVER_URL", "  ")
    assert CuoptServerConfig.from_env() is None and not cuopt_server.is_configured()
    monkeypatch.setenv("CUOPT_SERVER_URL", URL)
    monkeypatch.setenv("CUOPT_SERVER_TOKEN", "tok")
    assert CuoptServerConfig.from_env() == CuoptServerConfig(URL, "tok")


def test_solve_without_configuration_raises():
    with pytest.raises(CuoptServerError, match="CUOPT_SERVER_URL"):
        solve_cuopt_server(_formulation(), time_limit_s=1)


def _tail_case():
    a = np.full(100, 130.0)
    a[::20] = -470.0
    return np.column_stack([a, np.full(100, 60.0)])


@respx.mock(base_url=URL, assert_all_called=True)
def test_optimize_portfolio_uses_server_from_env(respx_mock, monkeypatch):
    monkeypatch.setenv("CUOPT_SERVER_URL", URL)
    monkeypatch.setenv("CUOPT_SERVER_TOKEN", "tok")
    submitted = {}

    def on_submit(request):
        submitted.update(json.loads(request.content))
        return httpx.Response(200, json={"reqId": "req-1"})

    respx_mock.post("/cuopt/request").mock(side_effect=on_submit)
    respx_mock.get("/cuopt/solution/req-1").mock(
        side_effect=lambda request: httpx.Response(200, json=_envelope(_solve_payload(submitted)))
    )
    respx_mock.delete("/cuopt/solution/req-1").respond(200)

    result = optimize_portfolio(_tail_case(), ["a", "b"], max_cvar_loss=0.0, time_limit_s=4)

    assert not cuopt_local.is_available()
    assert result.solver == "cuopt" and result.selected == ["b"]
    assert submitted["solver_config"]["time_limit"] == 4.0
    assert any("cuOpt (cuOpt server" in note for note in result.notes)


@respx.mock(base_url=URL)
def test_server_failure_falls_back_to_highs(respx_mock, monkeypatch):
    monkeypatch.setenv("CUOPT_SERVER_URL", URL)
    respx_mock.post("/cuopt/request").respond(503, json={"error": "gRPC client is not connected"})

    result = optimize_portfolio(_tail_case(), ["a", "b"], max_cvar_loss=0.0)

    assert result.solver == "highs" and result.selected == ["b"]
    assert any("cuOpt (cuOpt server) failed" in note and "HTTP 503" in note for note in result.notes)
