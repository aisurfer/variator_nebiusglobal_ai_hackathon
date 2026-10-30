import json

import httpx
import pytest
import respx

from variator.cloud import (
    AuthenticationError,
    EndpointSpec,
    EndpointState,
    JobSpec,
    JobState,
    MissingCredentialsError,
    NotEnoughResources,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceeded,
    RateLimitedError,
    ServerlessClient,
    ServiceUnavailableError,
    WaitTimeout,
    WorkloadFailed,
)
from variator.config import Settings

BASE = "https://api.nebius.cloud/ai/v1"


def sim_spec(**kw) -> JobSpec:
    return JobSpec(**{"name": "deep-sim-001", "image": "img:1", **kw})


def body(route: respx.Route, index: int = -1) -> dict:
    return json.loads(route.calls[index].request.content)


# ---------------------------------------------------------------- credentials


def test_requires_iam_token():
    with pytest.raises(MissingCredentialsError, match="NEBIUS_IAM_TOKEN"):
        ServerlessClient(Settings())


@respx.mock
def test_project_id_is_required_only_for_create_and_list(api):
    respx.get(f"{BASE}/jobs/aijob-1").mock(return_value=httpx.Response(200, json=api.job(job_id="aijob-1")))
    client = ServerlessClient(Settings(nebius_iam_token="t"))
    assert client.get_job("aijob-1").id == "aijob-1"
    with pytest.raises(MissingCredentialsError, match="NEBIUS_CLOUD_PROJECT_ID"):
        client.create_job(sim_spec())
    with pytest.raises(MissingCredentialsError, match="NEBIUS_CLOUD_PROJECT_ID"):
        client.list_jobs()


# ----------------------------------------------------------------------- jobs


@respx.mock
def test_create_job_sends_documented_request(client, api):
    route = respx.post(f"{BASE}/jobs").mock(return_value=httpx.Response(200, json=api.operation()))
    op = client.create_job(sim_spec())
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer iam-test-token"
    sent = body(route)
    assert sent["metadata"] == {"parentId": "project-e00test", "name": "deep-sim-001"}
    assert sent["spec"]["subnetId"] == "vpcsubnet-e00test"  # default from NEBIUS_SUBNET_ID
    assert sent["spec"]["timeout"] == "3600s"
    assert op.resource_id == "aijob-e00abc" and not op.done


@respx.mock
def test_create_job_dry_run(client, api):
    route = respx.post(f"{BASE}/jobs").mock(
        return_value=httpx.Response(200, json=api.operation(resource_id=None, done=True))
    )
    op = client.create_job(sim_spec(), dry_run=True)
    assert body(route)["dryRun"] is True
    assert op.succeeded and op.resource_id is None


@respx.mock
def test_create_job_failed_operation_is_raised(client, api):
    respx.post(f"{BASE}/jobs").mock(
        return_value=httpx.Response(200, json=api.operation(done=True, code=8, message="QuotaExceeded: gpu"))
    )
    with pytest.raises(QuotaExceeded, match="gpu"):
        client.create_job(sim_spec())


@respx.mock
def test_get_job_parses_status(client, api):
    payload = api.job(
        "FAILED",
        details={"code": "ContainerFailed", "message": "exit code 4"},
        started="2026-10-25T10:03:00Z",
        finished="2026-10-25T10:05:30Z",
    )
    respx.get(f"{BASE}/jobs/aijob-e00abc").mock(return_value=httpx.Response(200, json=payload))
    job = client.get_job("aijob-e00abc")
    assert job.state is JobState.FAILED and job.is_terminal and not job.succeeded
    assert job.details.code == "ContainerFailed"
    assert job.runtime_s == 150
    assert (job.platform, job.preset, job.disk_size_bytes) == ("gpu-l40s-a", "1gpu-8vcpu-32gb", 100 * 1024**3)
    with pytest.raises(WorkloadFailed, match="ContainerFailed") as info:
        job.raise_for_state()
    assert info.value.code == "ContainerFailed"


@respx.mock
def test_unknown_state_does_not_break_parsing(client, api):
    respx.get(f"{BASE}/jobs/j").mock(
        return_value=httpx.Response(200, json=api.job("HIBERNATING", job_id="j"))
    )
    job = client.get_job("j")
    assert job.state is JobState.STATE_UNSPECIFIED and job.raw_state == "HIBERNATING"


@respx.mock
def test_list_jobs_follows_pages(client, api):
    route = respx.get(f"{BASE}/jobs").mock(
        side_effect=[
            httpx.Response(200, json={"items": [api.job(job_id="a")], "nextPageToken": "p2"}),
            httpx.Response(200, json={"items": [api.job(job_id="b")]}),
        ]
    )
    jobs = client.list_jobs(page_size=1)
    assert [j.id for j in jobs] == ["a", "b"]
    first, second = (call.request.url.params for call in route.calls)
    assert first["parentId"] == "project-e00test" and first["pageSize"] == "1"
    assert second["pageToken"] == "p2"


@respx.mock
def test_get_job_by_name(client, api):
    route = respx.get(f"{BASE}/jobs/get-by-name").mock(return_value=httpx.Response(200, json=api.job()))
    assert client.get_job_by_name("variator-sim-test").name == "variator-sim-test"
    params = route.calls[0].request.url.params
    assert (params["parentId"], params["name"]) == ("project-e00test", "variator-sim-test")


@respx.mock
def test_cancel_and_delete_job(client, api):
    cancel = respx.post(f"{BASE}/jobs/cancel").mock(return_value=httpx.Response(200, json=api.operation()))
    delete = respx.delete(f"{BASE}/jobs/aijob-e00abc").mock(
        return_value=httpx.Response(200, json=api.operation())
    )
    client.cancel_job("aijob-e00abc")
    client.delete_job("aijob-e00abc")
    assert body(cancel) == {"id": "aijob-e00abc"}
    assert delete.called


# -------------------------------------------------------------------- polling


@respx.mock
def test_wait_for_job_walks_the_lifecycle(client, api, fake_time):
    states = ["PROVISIONING", "PROVISIONING", "STARTING", "IMAGE_PULLING", "RUNNING", "COMPLETED"]
    respx.get(f"{BASE}/jobs/aijob-e00abc").mock(
        side_effect=[httpx.Response(200, json=api.job(s)) for s in states]
    )
    seen = []
    job = client.wait_for_job(
        "aijob-e00abc", poll_interval_s=10, on_update=lambda j: seen.append(j.state.value)
    )
    assert job.succeeded
    assert seen == ["PROVISIONING", "STARTING", "IMAGE_PULLING", "RUNNING", "COMPLETED"]
    assert fake_time.sleeps == [10] * 5


@respx.mock
def test_wait_for_job_timeout(client, api, fake_time):
    respx.get(f"{BASE}/jobs/aijob-e00abc").mock(return_value=httpx.Response(200, json=api.job("RUNNING")))
    with pytest.raises(WaitTimeout, match="still RUNNING"):
        client.wait_for_job("aijob-e00abc", poll_interval_s=30, timeout_s=100)
    assert fake_time.now == pytest.approx(100)


@respx.mock
def test_wait_for_job_returns_failed_job_by_default(client, api):
    respx.get(f"{BASE}/jobs/aijob-e00abc").mock(return_value=httpx.Response(200, json=api.job("FAILED")))
    assert client.wait_for_job("aijob-e00abc").state is JobState.FAILED


@pytest.mark.parametrize(
    ("details", "error"),
    [
        ({"code": "QuotaExceeded", "message": "GPU quota"}, QuotaExceeded),
        (
            {"code": "Error", "serviceError": {"quotaFailure": {"violations": [{"quota": "gpu"}]}}},
            QuotaExceeded,
        ),
        ({"code": "NotEnoughResources", "message": "Due to high demand"}, NotEnoughResources),
        ({"code": "StartFailed", "message": "bad image"}, WorkloadFailed),
    ],
)
@respx.mock
def test_wait_for_job_raise_on_failure(client, api, details, error):
    respx.get(f"{BASE}/jobs/aijob-e00abc").mock(
        return_value=httpx.Response(200, json=api.job("ERROR", details=details))
    )
    with pytest.raises(error):
        client.wait_for_job("aijob-e00abc", raise_on_failure=True)


@respx.mock
def test_wait_for_job_tolerates_brief_not_found_then_uses_operation(client, api):
    respx.get(f"{BASE}/jobs/aijob-e00abc").mock(
        side_effect=[
            httpx.Response(404, json={"code": 5, "message": "not found"}),
            httpx.Response(200, json=api.job("COMPLETED")),
        ]
    )
    respx.get(f"{BASE}/jobs/operations/aiop-e00op1").mock(
        return_value=httpx.Response(200, json=api.operation())
    )
    created = client.get_operation("aiop-e00op1", "jobs")
    assert client.wait_for_job("aijob-e00abc", operation=created).succeeded


@respx.mock
def test_wait_for_job_raises_failed_operation_error(client, api):
    respx.get(f"{BASE}/jobs/aijob-e00abc").mock(
        return_value=httpx.Response(404, json={"code": 5, "message": "nf"})
    )
    respx.get(f"{BASE}/jobs/operations/aiop-e00op1").mock(
        return_value=httpx.Response(200, json=api.operation(done=True, code=8, message="quota"))
    )
    op = client.get_operation("aiop-e00op1", "jobs")
    with pytest.raises(QuotaExceeded):
        client.wait_for_job("aijob-e00abc", operation=op)


@respx.mock
def test_wait_for_job_gives_up_when_never_found(client):
    respx.get(f"{BASE}/jobs/missing").mock(
        return_value=httpx.Response(404, json={"code": 5, "message": "nf"})
    )
    with pytest.raises(NotFoundError):
        client.wait_for_job("missing", not_found_retries=2)


@respx.mock
def test_wait_operation(client, api, fake_time):
    respx.get(f"{BASE}/endpoints/operations/aiop-e00op1").mock(
        side_effect=[
            httpx.Response(200, json=api.operation()),
            httpx.Response(200, json=api.operation(done=True)),
        ]
    )
    pending = client.get_operation("aiop-e00op1", "endpoints")
    done = client.wait_operation(pending, "endpoints", poll_interval_s=1)
    assert done.succeeded and fake_time.sleeps == [1]


# ------------------------------------------------------------------ endpoints


@respx.mock
def test_endpoint_lifecycle_calls(client, api):
    create = respx.post(f"{BASE}/endpoints").mock(
        return_value=httpx.Response(200, json=api.operation(resource_id="aiendpoint-e00xyz"))
    )
    routes = {
        action: respx.post(f"{BASE}/endpoints/{action}").mock(
            return_value=httpx.Response(200, json=api.operation())
        )
        for action in ("start", "stop", "restart")
    }
    delete = respx.delete(f"{BASE}/endpoints/aiendpoint-e00xyz").mock(
        return_value=httpx.Response(200, json=api.operation())
    )
    spec = EndpointSpec(name="variator-app", image="img:1", platform="cpu-d3", preset="2vcpu-8gb")
    op = client.create_endpoint(spec)
    assert op.resource_id == "aiendpoint-e00xyz"
    sent = body(create)
    assert sent["spec"]["ports"] == [{"containerPort": 8000, "protocol": "HTTP"}]
    assert sent["spec"]["disk"]["type"] == "NETWORK_SSD"  # required for endpoints
    client.start_endpoint("aiendpoint-e00xyz")
    client.stop_endpoint("aiendpoint-e00xyz")
    client.restart_endpoint("aiendpoint-e00xyz")
    client.delete_endpoint("aiendpoint-e00xyz")
    for route in routes.values():
        assert body(route) == {"id": "aiendpoint-e00xyz"}
    assert delete.called


@respx.mock
def test_list_and_get_endpoint_by_name(client, api):
    respx.get(f"{BASE}/endpoints").mock(return_value=httpx.Response(200, json={"items": [api.endpoint()]}))
    respx.get(f"{BASE}/endpoints/get-by-name").mock(return_value=httpx.Response(200, json=api.endpoint()))
    assert [e.id for e in client.list_endpoints()] == ["aiendpoint-e00xyz"]
    assert client.get_endpoint_by_name("variator-app").state is EndpointState.RUNNING


@respx.mock
def test_wait_until_running_returns_https_url(client, api):
    url = "https://e00xyz.endpoints.nebius.cloud"
    respx.get(f"{BASE}/endpoints/aiendpoint-e00xyz").mock(
        side_effect=[
            httpx.Response(200, json=api.endpoint("PROVISIONING")),
            httpx.Response(200, json=api.endpoint("IMAGE_PULLING")),
            httpx.Response(200, json=api.endpoint("RUNNING", public=[])),
            httpx.Response(200, json=api.endpoint("RUNNING", public=["203.0.113.5:8000", url])),
        ]
    )
    assert client.wait_until_running("aiendpoint-e00xyz", poll_interval_s=5) == url


@respx.mock
def test_wait_until_running_falls_back_to_ip_port(client, api):
    respx.get(f"{BASE}/endpoints/e").mock(
        return_value=httpx.Response(
            200, json=api.endpoint("RUNNING", endpoint_id="e", public=["203.0.113.5:80"])
        )
    )
    assert client.wait_until_running("e") == "http://203.0.113.5:80"


@respx.mock
def test_wait_until_running_raises_on_error(client, api):
    respx.get(f"{BASE}/endpoints/e").mock(
        return_value=httpx.Response(
            200, json=api.endpoint("ERROR", endpoint_id="e", details={"code": "NotEnoughResources"})
        )
    )
    with pytest.raises(NotEnoughResources):
        client.wait_until_running("e")


@respx.mock
def test_wait_until_running_gives_up_on_stopped(client, api, fake_time):
    respx.get(f"{BASE}/endpoints/e").mock(
        return_value=httpx.Response(200, json=api.endpoint("STOPPED", endpoint_id="e"))
    )
    with pytest.raises(WorkloadFailed, match="STOPPED"):
        client.wait_until_running("e", poll_interval_s=1, stopped_grace_polls=2)
    assert len(fake_time.sleeps) == 2


# -------------------------------------------------------------- error mapping


@respx.mock
def test_401_maps_to_authentication_error_with_hint(client):
    respx.get(f"{BASE}/jobs/j").mock(
        return_value=httpx.Response(401, json={"code": 16, "message": "bad token"})
    )
    with pytest.raises(AuthenticationError, match="nebius iam get-access-token") as info:
        client.get_job("j")
    assert (info.value.status_code, info.value.grpc_code) == (401, 16)


@respx.mock
def test_403_maps_to_permission_denied(client):
    respx.post(f"{BASE}/jobs").mock(return_value=httpx.Response(403, json={"code": 7, "message": "denied"}))
    with pytest.raises(PermissionDeniedError, match="editor"):
        client.create_job(sim_spec())


@respx.mock
def test_quota_error_on_create(client):
    route = respx.post(f"{BASE}/jobs").mock(
        return_value=httpx.Response(429, json={"code": 8, "message": "quota compute.gpu.l40s exceeded"})
    )
    with pytest.raises(QuotaExceeded, match="console.nebius.com/quota"):
        client.create_job(sim_spec())
    assert route.call_count == 1  # creates are never retried


@respx.mock
def test_rate_limit_on_read_is_retried(client, api, fake_time):
    limited = {"code": 8, "message": "slow down", "details": [{"tooManyRequests": {"violation": "rps"}}]}
    route = respx.get(f"{BASE}/jobs/j").mock(
        side_effect=[httpx.Response(429, json=limited), httpx.Response(200, json=api.job(job_id="j"))]
    )
    assert client.get_job("j").id == "j"
    assert route.call_count == 2 and fake_time.sleeps == [2]


@respx.mock
def test_rate_limit_error_after_retries(client):
    limited = {"code": 8, "message": "slow down", "details": [{"tooManyRequests": {}}]}
    respx.get(f"{BASE}/jobs/j").mock(return_value=httpx.Response(429, json=limited))
    with pytest.raises(RateLimitedError):
        client.get_job("j")


@respx.mock
def test_server_errors_retry_reads_but_not_writes(client, api):
    get = respx.get(f"{BASE}/jobs/j").mock(
        side_effect=[httpx.Response(503, text="unavailable"), httpx.Response(200, json=api.job(job_id="j"))]
    )
    post = respx.post(f"{BASE}/jobs/cancel").mock(
        return_value=httpx.Response(500, json={"code": 13, "message": "x"})
    )
    assert client.get_job("j").id == "j" and get.call_count == 2
    with pytest.raises(ServiceUnavailableError):
        client.cancel_job("j")
    assert post.call_count == 1


@respx.mock
def test_404_maps_to_not_found(client):
    respx.delete(f"{BASE}/jobs/nope").mock(
        return_value=httpx.Response(404, json={"code": 5, "message": "no job"})
    )
    with pytest.raises(NotFoundError, match="no job"):
        client.delete_job("nope")


@respx.mock
def test_token_provider_refreshes_once_on_401(settings, api):
    tokens = iter(["fresh-token"])
    route = respx.get(f"{BASE}/jobs/j").mock(
        side_effect=[
            httpx.Response(401, json={"code": 16, "message": "expired"}),
            httpx.Response(200, json=api.job(job_id="j")),
        ]
    )
    with ServerlessClient(settings, token_provider=lambda: next(tokens)) as client:
        client.get_job("j")
    auth = [call.request.headers["Authorization"] for call in route.calls]
    assert auth == ["Bearer iam-test-token", "Bearer fresh-token"]


def test_token_provider_used_when_env_token_missing():
    client = ServerlessClient(Settings(nebius_cloud_project_id="p"), token_provider=lambda: "from-cli")
    assert client._token == "from-cli"
