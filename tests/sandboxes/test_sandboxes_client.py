from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest
import respx

from variator.config import MissingCredentialsError, Settings
from variator.sandboxes import (
    FileMount,
    Networking,
    OperationStatus,
    SandboxAuthError,
    SandboxError,
    SandboxesClient,
    SandboxHTTPError,
    SandboxLimitError,
    SandboxNotFoundError,
    SandboxOutputTooLarge,
    SandboxPermissionError,
    SandboxRequestError,
    SandboxTimeoutError,
    SandboxTransportError,
    SpawnRequest,
)

BASE = "https://api.tokenfactory.nebius.com/sandboxes/v1"
OP_ID = "12345678-9abc-baba-deda-0123456789ab"
IMAGE_ID = "019eef27-7ecb-7771-8184-aaf5fa596ce2"


def op_payload(status: str, **extra) -> dict:
    body = {
        "uuid": OP_ID,
        "kind": "instance",
        "status": status,
        "error": None,
        "created_at": "2026-10-25T00:00:00+00:00",
        "duration": None,
        "image_uuid": IMAGE_ID,
        "result_image_uuid": None,
        "metadata": {"command": "echo hi", "image": IMAGE_ID, "result": None},
    }
    body.update(extra)
    return body


def success_payload(stdout: str = "ready\n", exit_code: int = 0) -> dict:
    return op_payload(
        "SUCCESS",
        duration=2.5,
        result_image_uuid="aaaaaaaa-0000-0000-0000-000000000001",
        metadata={
            "command": "echo ready",
            "image": IMAGE_ID,
            "result": {
                "state": {"exit_code": exit_code, "signal": -1, "timed_out": False, "pid": 4242},
                "stdout": {
                    "value": base64.b64encode(stdout.encode()).decode(),
                    "encoding": "base64",
                    "truncated": False,
                },
                "stderr": {"value": "diagnostic\n", "encoding": "ascii", "truncated": False},
                "resources": {"elapsed_time": 2.4},
            },
        },
    )


# -- construction --------------------------------------------------------------


def test_requires_api_key_and_project():
    with pytest.raises(MissingCredentialsError, match="NEBIUS_API_KEY"):
        SandboxesClient(Settings())
    with pytest.raises(MissingCredentialsError, match="NEBIUS_PROJECT_ID"):
        SandboxesClient(Settings(nebius_api_key="k"))


def test_base_url_override_appends_v1():
    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://example.test/sbx/v1/whoami").mock(
            return_value=httpx.Response(200, json={"token_uuid": "t", "permissions": {}})
        )
        settings = Settings(
            nebius_api_key="k", nebius_project_id="p", sandboxes_base_url="https://example.test/sbx/"
        )
        SandboxesClient(settings).whoami()
    assert route.called


# -- auth and limits -----------------------------------------------------------


def test_whoami_sends_bearer_and_project_headers(api, client):
    route = api.get("/whoami").mock(
        return_value=httpx.Response(
            200,
            json={
                "token_uuid": "tok",
                "token_expiration": None,
                "permissions": {"spawn": True, "spawn_disposable": True, "cancel": False},
                "limits": {"instance_max_timeout": 3600, "instance_max_concurrency": 50},
                "operations_stat": {},
            },
        )
    )
    who = client.whoami()
    request = route.calls.last.request
    assert request.method == "GET"
    assert request.url == f"{BASE}/whoami"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.headers["Project"] == "proj-123"
    assert who.can("spawn") and not who.can("cancel")
    assert who.max_timeout_s == 3600 and who.max_concurrency == 50


def test_limits_shortcut(api, client):
    api.get("/whoami").mock(
        return_value=httpx.Response(200, json={"token_uuid": "t", "permissions": {}, "limits": {"x": 1}})
    )
    assert client.limits() == {"x": 1}


# -- images --------------------------------------------------------------------


def test_list_images_and_find_exact_tag(api, client):
    route = api.get("/images").mock(
        return_value=httpx.Response(
            200,
            json={
                "images": [
                    {"uuid": "u1", "tag": "python:3.12-slim-extra", "created_at": "x"},
                    {"uuid": "u2", "tag": "python:3.12-slim", "created_at": "x"},
                ]
            },
        )
    )
    images = client.list_images(tag_prefix="python", tagged=True)
    params = route.calls.last.request.url.params
    assert (params["tag"], params["tagged"], params["limit"], params["offset"]) == ("python", "1", "100", "0")
    assert [i.uuid for i in images] == ["u1", "u2"]
    assert client.find_image("tag:python:3.12-slim").uuid == "u2"
    assert route.calls.last.request.url.params["tag"] == "python:3.12-slim"


def test_import_and_tag_image(api, client):
    imp = api.post("/images/import").mock(return_value=httpx.Response(201, json={"uuid": OP_ID}))
    tag = api.patch(f"/images/{IMAGE_ID}/tag").mock(
        return_value=httpx.Response(200, json={"uuid": IMAGE_ID, "tag": "variator-numpy:py3.12"})
    )
    assert client.import_image("docker://docker.io/library/python:3.12-slim") == OP_ID
    assert json.loads(imp.calls.last.request.content) == {
        "registry": {"url": "docker://docker.io/library/python:3.12-slim"},
        "timeout": 300,
    }
    assert client.tag_image(IMAGE_ID, "variator-numpy:py3.12").tag == "variator-numpy:py3.12"
    assert json.loads(tag.calls.last.request.content) == {"tag": "variator-numpy:py3.12"}


def test_inspect_image_uses_trailing_slash(api, client):
    route = api.get(f"/inspect/{IMAGE_ID}/").mock(
        return_value=httpx.Response(200, json={"uuid": IMAGE_ID, "tag": None})
    )
    assert client.inspect_image(IMAGE_ID).uuid == IMAGE_ID
    assert route.called


# -- files ---------------------------------------------------------------------


def test_upload_checks_sha256_then_posts_raw_bytes(api, client):
    data = b"print('hello')\n"
    digest = hashlib.sha256(data).hexdigest()
    lookup = api.get(f"/files/{digest}").mock(return_value=httpx.Response(404, json={"error": "nope"}))
    upload = api.post("/files").mock(
        return_value=httpx.Response(201, json={"uuid": "f-1", "sha256": digest, "size": len(data)})
    )
    first = client.upload_bytes(data)
    second = client.upload_bytes(data)  # served from the in-process cache
    assert first.uuid == second.uuid == "f-1"
    assert lookup.call_count == 1 and upload.call_count == 1
    request = upload.calls.last.request
    assert request.content == data
    assert request.headers["Content-Type"] == "application/octet-stream"


def test_upload_skipped_when_file_already_exists(api, client):
    data = b"abc"
    digest = hashlib.sha256(data).hexdigest()
    api.get(f"/files/{digest}").mock(
        return_value=httpx.Response(
            200,
            json={"uuid": "f-9", "sha256": digest, "size": 3, "created_at": "x", "updated_at": "y"},
        )
    )
    upload = api.post("/files").mock(return_value=httpx.Response(500))
    assert client.upload_bytes(data).uuid == "f-9"
    assert not upload.called


# -- spawn ---------------------------------------------------------------------


def test_spawn_body_defaults_to_offline_and_disposable(api, client):
    route = api.post("/instances").mock(return_value=httpx.Response(201, json={"uuid": OP_ID}))
    op_id = client.spawn(
        SpawnRequest(
            image="tag:python:3.12-slim",
            command="python3 main.py",
            shell=True,
            cwd="/work",
            timeout=60,
            files={"/work/main.py": FileMount(uuid="f-1")},
        )
    )
    assert op_id == OP_ID
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "image": "tag:python:3.12-slim",
        "command": "python3 main.py",
        "shell": True,
        "args": [],
        "cwd": "/work",
        "disposable": True,
        "timeout": 60,
        "networking": {"enabled": False},
        "files": {"/work/main.py": {"uuid": "f-1", "mode": "0644"}},
    }


def test_spawn_with_network_and_saved_filesystem(api, client):
    route = api.post("/instances").mock(return_value=httpx.Response(201, json={"uuid": OP_ID}))
    client.spawn(
        SpawnRequest(
            image=IMAGE_ID,
            command="/bin/echo",
            args=["ready"],
            env={"PATH": "/usr/bin:/bin"},
            disposable=False,
            networking=Networking(enabled=True),
            truncate_output_at=65535,
        )
    )
    body = json.loads(route.calls.last.request.content)
    assert body["networking"] == {"enabled": True}
    assert body["disposable"] is False
    assert body["args"] == ["ready"] and body["env"] == {"PATH": "/usr/bin:/bin"}
    assert body["truncate_output_at"] == 65535


def test_spawn_is_not_retried_after_transport_error(api, client):
    route = api.post("/instances").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(SandboxTransportError, match="submission outcome unknown"):
        client.spawn(SpawnRequest(image=IMAGE_ID, command="true"))
    assert route.call_count == 1


def test_spawn_is_not_retried_on_502(api, client):
    route = api.post("/instances").mock(return_value=httpx.Response(502, json={"error": "bad gateway"}))
    with pytest.raises(SandboxHTTPError) as info:
        client.spawn(SpawnRequest(image=IMAGE_ID, command="true"))
    assert info.value.status_code == 502 and route.call_count == 1


def test_spawn_retries_rate_limit_honouring_retry_after(api, client, clock):
    route = api.post("/instances").mock(
        side_effect=[
            httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "2"}),
            httpx.Response(201, json={"uuid": OP_ID}),
        ]
    )
    assert client.spawn(SpawnRequest(image=IMAGE_ID, command="true")) == OP_ID
    assert route.call_count == 2
    assert clock.sleeps == [2.0]


# -- operations ----------------------------------------------------------------


def test_get_operation_parses_process_result(api, client):
    route = api.get(f"/operations/{OP_ID}").mock(return_value=httpx.Response(200, json=success_payload()))
    op = client.get_operation(OP_ID)
    assert "inflight" not in route.calls.last.request.url.params
    assert op.status is OperationStatus.SUCCESS and op.terminal
    result = op.instance_result
    assert result.state.exit_code == 0 and not result.state.signalled
    assert result.stdout.text() == "ready\n"  # base64-decoded
    assert result.stderr.text() == "diagnostic\n"
    assert op.produced_image == "aaaaaaaa-0000-0000-0000-000000000001"


def test_get_operation_inflight_flag(api, client):
    executing = httpx.Response(200, json=op_payload("EXECUTING"))
    route = api.get(f"/operations/{OP_ID}").mock(return_value=executing)
    client.get_operation(OP_ID, inflight=True)
    assert route.calls.last.request.url.params["inflight"] == "1"


def test_wait_polls_through_states_and_honours_retry_after(api, client, clock):
    api.get(f"/operations/{OP_ID}").mock(
        side_effect=[
            httpx.Response(200, json=op_payload("PENDING")),
            httpx.Response(200, json=op_payload("ASSIGNED")),
            httpx.Response(200, json=op_payload("EXECUTING"), headers={"Retry-After": "3"}),
            httpx.Response(200, json=success_payload()),
        ]
    )
    seen = []
    op = client.wait(OP_ID, timeout_s=60, on_poll=lambda o: seen.append(o.status.value))
    assert seen == ["PENDING", "ASSIGNED", "EXECUTING", "SUCCESS"]
    assert op.status is OperationStatus.SUCCESS
    assert clock.sleeps == [1.0, 1.5, 3.0]


@pytest.mark.parametrize("terminal", ["FAILED", "CANCELLED"])
def test_wait_returns_failed_and_cancelled_states(api, client, terminal):
    api.get(f"/operations/{OP_ID}").mock(
        return_value=httpx.Response(200, json=op_payload(terminal, error="no capacity"))
    )
    op = client.wait(OP_ID)
    assert op.status == terminal and op.error == "no capacity"


def test_wait_times_out_without_cancelling(api, client, clock):
    api.get(f"/operations/{OP_ID}").mock(return_value=httpx.Response(200, json=op_payload("EXECUTING")))
    cancel = api.delete(f"/operations/{OP_ID}").mock(return_value=httpx.Response(202))
    with pytest.raises(SandboxTimeoutError) as info:
        client.wait(OP_ID, timeout_s=5)
    assert info.value.operation_id == OP_ID
    assert clock.t == pytest.approx(5.0)
    assert not cancel.called


def test_execute_cancels_owned_operation_on_timeout(api, client):
    api.post("/instances").mock(return_value=httpx.Response(201, json={"uuid": OP_ID}))
    api.get(f"/operations/{OP_ID}").mock(return_value=httpx.Response(200, json=op_payload("EXECUTING")))
    cancel = api.delete(f"/operations/{OP_ID}").mock(return_value=httpx.Response(202))
    with pytest.raises(SandboxTimeoutError):
        client.execute(SpawnRequest(image=IMAGE_ID, command="sleep 999", timeout=10), wait_timeout_s=3)
    assert cancel.call_count == 1


def test_cancel_accepted_and_conflict(api, client):
    route = api.delete(f"/operations/{OP_ID}").mock(
        side_effect=[httpx.Response(202), httpx.Response(409, json={"error": "already finished"})]
    )
    assert client.cancel(OP_ID) is True
    assert client.cancel(OP_ID) is False
    assert route.calls.last.request.method == "DELETE"


# -- error mapping -------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [
        (401, {"error": "invalid token", "status": 401}, SandboxAuthError),
        (403, {"error": "permission denied", "status": 403}, SandboxPermissionError),
        (403, {"error": "instance concurrency limit reached"}, SandboxLimitError),
        (400, {"error": "timeout exceeds instance_max_timeout limit"}, SandboxLimitError),
        (400, {"error": {"command": ["required"]}}, SandboxRequestError),
        (404, {"error": "operation not found"}, SandboxNotFoundError),
        (422, {"error": "not a file"}, SandboxRequestError),
        (500, "internal error", SandboxHTTPError),
    ],
)
def test_error_mapping(api, client, status, body, error_type):
    kwargs = {"json": body} if isinstance(body, dict) else {"text": body}
    api.get(f"/operations/{OP_ID}").mock(return_value=httpx.Response(status, **kwargs))
    with pytest.raises(error_type) as info:
        client.get_operation(OP_ID)
    assert info.value.status_code == status
    assert isinstance(info.value, SandboxError)


def test_permission_error_mentions_beta_access(api, client):
    api.get("/whoami").mock(return_value=httpx.Response(403, json={"error": "forbidden"}))
    with pytest.raises(SandboxPermissionError, match="Beta"):
        client.whoami()


def test_rate_limit_retried_then_raised_with_retry_after(api, client, clock):
    route = api.get("/whoami").mock(
        return_value=httpx.Response(429, json={"error": "too many requests"}, headers={"Retry-After": "4"})
    )
    with pytest.raises(SandboxLimitError) as info:
        client.whoami()
    assert route.call_count == 3  # max_retries=2
    assert info.value.retry_after_s == 4.0
    assert clock.sleeps == [4.0, 4.0]


def test_idempotent_get_retries_transient_errors(api, client):
    route = api.get("/whoami").mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectError("reset"),
            httpx.Response(200, json={"token_uuid": "t", "permissions": {}}),
        ]
    )
    assert client.whoami().token_uuid == "t"
    assert route.call_count == 3


# -- image inspection ----------------------------------------------------------


def test_download_file_from_result_image(api, client):
    route = api.get(f"/inspect/{IMAGE_ID}/download").mock(
        return_value=httpx.Response(200, content=b'{"ok": true}')
    )
    assert client.download_file(IMAGE_ID, "/work/output.json") == b'{"ok": true}'
    assert route.calls.last.request.url.params["path"] == "/work/output.json"


def test_download_rejects_declared_size_over_limit(api, client):
    api.get(f"/inspect/{IMAGE_ID}/download").mock(return_value=httpx.Response(200, content=b"x" * 100))
    with pytest.raises(SandboxOutputTooLarge, match="100 bytes"):
        client.download_file(IMAGE_ID, "/work/output.json", max_bytes=10)


def test_download_stops_streaming_over_limit(api, client):
    api.get(f"/inspect/{IMAGE_ID}/download").mock(
        return_value=httpx.Response(200, content=iter([b"a" * 8, b"b" * 8, b"c" * 8]))
    )
    with pytest.raises(SandboxOutputTooLarge, match="12-byte limit"):
        client.download_file(IMAGE_ID, "/work/output.json", max_bytes=12)


def test_download_missing_file_and_retry(api, client):
    route = api.get(f"/inspect/{IMAGE_ID}/download").mock(
        side_effect=[httpx.Response(502), httpx.Response(404, json={"error": "no such file"})]
    )
    with pytest.raises(SandboxNotFoundError):
        client.download_file(IMAGE_ID, "/work/output.json")
    assert route.call_count == 2


def test_file_exists_and_list_files(api, client):
    head = api.head(f"/inspect/{IMAGE_ID}/download").mock(
        side_effect=[httpx.Response(200), httpx.Response(404)]
    )
    assert client.file_exists(IMAGE_ID, "/work/output.json") is True
    assert client.file_exists(IMAGE_ID, "/work/missing.json") is False
    assert head.calls.last.request.url.params["path"] == "/work/missing.json"
    api.get(f"/inspect/{IMAGE_ID}/list").mock(
        return_value=httpx.Response(200, json={"path": "/work", "files": [{"path": "main.py", "size": 10}]})
    )
    assert client.list_image_files(IMAGE_ID, "/work")[0]["path"] == "main.py"
