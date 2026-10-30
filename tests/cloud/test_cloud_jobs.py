import base64
import io
import json

import httpx
import pytest
import respx

from variator.cloud import (
    InputTooLarge,
    InputTransport,
    JobInput,
    JobOutput,
    JobResultMissing,
    LocalDirStore,
    WorkloadFailed,
    fetch_job_result,
    job_entry,
    plan_simulation_job,
    run_simulation_job,
    submit_simulation_job,
    wait_for_result,
)
from variator.cloud.job_io import INPUT_B64_ENV, INPUT_PATH_ENV, OUTPUT_PATH_ENV, format_marker
from variator.cloud.jobs import INPUT_FILE_PATH, SubmittedJob

BASE = "https://api.nebius.cloud/ai/v1"
IMAGE = "cr.eu-north1.nebius.cloud/e00reg/variator-sim:latest"


def sent_spec(route: respx.Route) -> dict:
    return json.loads(route.calls[-1].request.content)["spec"]


def env_of(spec: dict) -> dict[str, str]:
    return {e["name"]: e["value"] for e in spec.get("environmentVariables", []) if "value" in e}


@respx.mock
def test_submit_with_injected_file_and_bucket(client, api, decision):
    route = respx.post(f"{BASE}/jobs").mock(return_value=httpx.Response(200, json=api.operation()))
    submitted = submit_simulation_job(
        client,
        decision,
        image=IMAGE,
        n_scenarios=5_000_000,
        task="simulate_and_optimize",
        budget=3e6,
        seed=1,
        bucket="storagebucket-e00b",
        run_id="20261025-100000-abc123",
        expected_runtime_s=900,
    )
    spec = sent_spec(route)
    assert (
        json.loads(route.calls[-1].request.content)["metadata"]["name"]
        == "variator-sim-20261025-100000-abc123"
    )
    assert spec["image"] == IMAGE and spec["platform"] == "gpu-l40s-a" and spec["preset"] == "1gpu-8vcpu-32gb"
    assert spec["volumes"] == [
        {"source": "storagebucket-e00b", "containerPath": "/data", "mode": "READ_WRITE"}
    ]
    env = env_of(spec)
    assert env[INPUT_PATH_ENV] == INPUT_FILE_PATH
    assert env[OUTPUT_PATH_ENV] == "/data/variator-runs/20261025-100000-abc123/output.json"
    injected = spec["injectedFiles"][0]
    assert injected["containerPath"] == INPUT_FILE_PATH
    job_input = JobInput.model_validate_json(base64.b64decode(injected["content"]))
    assert job_input.decision == decision and job_input.n_scenarios == 5_000_000
    assert (job_input.task, job_input.budget, job_input.seed) == ("simulate_and_optimize", 3e6, 1)

    assert submitted.job_id == "aijob-e00abc" and submitted.operation_id == "aiop-e00op1"
    assert submitted.output_key == "variator-runs/20261025-100000-abc123/output.json"
    assert submitted.transport is InputTransport.FILE and submitted.input_key is None
    assert submitted.cost.expected.hours == pytest.approx((300 + 900) / 3600)
    assert submitted.cost.worst_case.total_usd > submitted.cost.expected.total_usd


@respx.mock
def test_submit_with_env_transport_and_no_bucket(client, api, decision):
    route = respx.post(f"{BASE}/jobs").mock(return_value=httpx.Response(200, json=api.operation()))
    submitted = submit_simulation_job(
        client, decision, image=IMAGE, transport="env", platform="cpu-d3", preset="8vcpu-32gb"
    )
    spec = sent_spec(route)
    env = env_of(spec)
    decoded = JobInput.from_env_value(env[INPUT_B64_ENV])
    assert decoded.run_id == submitted.run_id
    assert "volumes" not in spec and "injectedFiles" not in spec and OUTPUT_PATH_ENV not in env
    assert submitted.output_key is None and submitted.output_path is None
    assert submitted.cost.expected.compute_usd_per_hour == pytest.approx(8 * 0.015 + 32 * 0.0045)


@respx.mock
def test_submit_with_bucket_transport_uploads_input(client, api, decision, tmp_path):
    route = respx.post(f"{BASE}/jobs").mock(return_value=httpx.Response(200, json=api.operation()))
    store = LocalDirStore(tmp_path)
    submitted = submit_simulation_job(
        client,
        decision,
        image=IMAGE,
        transport="bucket",
        bucket="storagebucket-e00b",
        store=store,
        run_id="r1",
    )
    assert submitted.input_key == "variator-runs/r1/input.json"
    assert JobInput.model_validate_json(store.read_bytes(submitted.input_key)).run_id == "r1"
    assert env_of(sent_spec(route))[INPUT_PATH_ENV] == "/data/variator-runs/r1/input.json"


def test_bucket_transport_requires_bucket_and_store(client, decision):
    with pytest.raises(ValueError, match="bucket to mount"):
        submit_simulation_job(client, decision, image=IMAGE, transport="bucket")
    with pytest.raises(ValueError, match="store"):
        submit_simulation_job(client, decision, image=IMAGE, transport="bucket", bucket="b")


def test_large_input_needs_bucket_transport(decision):
    big = decision.model_copy(update={"assumptions": ["x" * 1000] * 80})
    job_input = JobInput(decision=big)
    with pytest.raises(InputTooLarge, match="bucket"):
        plan_simulation_job(job_input, image=IMAGE)
    with pytest.raises(InputTooLarge):
        plan_simulation_job(job_input, image=IMAGE, transport="env")
    plan = plan_simulation_job(job_input, image=IMAGE, transport="bucket", bucket="b")
    assert plan.input_key.endswith("/input.json")


def test_reserved_env_vars_are_rejected(decision):
    with pytest.raises(ValueError, match="reserved"):
        plan_simulation_job(JobInput(decision=decision), image=IMAGE, env={OUTPUT_PATH_ENV: "/tmp/x"})


def test_plan_passes_secrets_and_preemptible(decision):
    plan = plan_simulation_job(
        JobInput(decision=decision),
        image=IMAGE,
        preemptible=True,
        env_secrets={"NEBIUS_API_KEY": "mbsec-e00k@mbsecver-e00v"},
        timeout_s=2 * 3600,
    )
    api_spec = plan.spec.to_api()
    assert api_spec["preemptible"] is True and api_spec["timeout"] == "7200s"
    secret = next(e for e in api_spec["environmentVariables"] if e["name"] == "NEBIUS_API_KEY")
    assert secret["mysteryboxSecret"] == {"secretId": "mbsec-e00k", "versionId": "mbsecver-e00v"}


@respx.mock
def test_dry_run_submits_nothing_billable(client, api, decision):
    route = respx.post(f"{BASE}/jobs").mock(
        return_value=httpx.Response(200, json=api.operation(resource_id=None, done=True))
    )
    submitted = submit_simulation_job(client, decision, image=IMAGE, dry_run=True)
    assert json.loads(route.calls[0].request.content)["dryRun"] is True
    assert submitted.dry_run and submitted.job_id == ""


# ------------------------------------------------------------------- results


def submitted_job(**kw) -> SubmittedJob:
    fields = {
        "job_id": "aijob-e00abc",
        "job_name": "variator-sim-r1",
        "run_id": "r1",
        "transport": InputTransport.FILE,
        "output_key": "variator-runs/r1/output.json",
        "output_path": "/data/variator-runs/r1/output.json",
        "platform": "gpu-l40s-a",
        "preset": "1gpu-8vcpu-32gb",
        "preemptible": False,
        "submitted_at": "2026-10-25T10:00:00Z",
    }
    return SubmittedJob(**{**fields, **kw})


def job_output(sim_result, run_id="r1") -> JobOutput:
    return JobOutput(
        run_id=run_id,
        task="simulate",
        ok=True,
        exit_code=0,
        simulation=sim_result,
        started_at="2026-10-25T10:01:00Z",
    )


def test_fetch_from_store(tmp_path, sim_result):
    store = LocalDirStore(tmp_path)
    store.write_bytes("variator-runs/r1/output.json", job_output(sim_result).model_dump_json().encode())
    assert fetch_job_result(submitted_job(), store=store).simulation == sim_result


def test_fetch_falls_back_to_logs(tmp_path, sim_result):
    logs_text = "boot\n" + format_marker(job_output(sim_result), None) + "\n"
    calls = []

    def logs(job_id: str) -> str:
        calls.append(job_id)
        return logs_text

    result = fetch_job_result(submitted_job(), store=LocalDirStore(tmp_path), logs=logs)
    assert result.run_id == "r1" and calls == ["aijob-e00abc"]
    other_run = format_marker(job_output(sim_result, run_id="other"), None)
    assert fetch_job_result(submitted_job(), logs=lambda _: other_run) is None
    assert fetch_job_result(submitted_job()) is None


def test_store_rejects_escaping_keys(tmp_path):
    store = LocalDirStore(tmp_path)
    for key in ("../x", "/etc/passwd", "a//b", ""):
        with pytest.raises(ValueError):
            store.read_bytes(key)


@respx.mock
def test_wait_for_result_reads_bucket_after_completion(client, api, tmp_path, sim_result):
    store = LocalDirStore(tmp_path)
    store.write_bytes("variator-runs/r1/output.json", job_output(sim_result).model_dump_json().encode())
    respx.get(f"{BASE}/jobs/aijob-e00abc").mock(
        side_effect=[httpx.Response(200, json=api.job(s)) for s in ("RUNNING", "COMPLETED")]
    )
    output = wait_for_result(client, submitted_job(), store=store, poll_interval_s=5)
    assert output.ok and output.simulation == sim_result


@respx.mock
def test_wait_for_result_failed_job_without_output(client, api, tmp_path):
    details = {"code": "ContainerFailed", "message": "exit 137"}
    respx.get(f"{BASE}/jobs/aijob-e00abc").mock(
        return_value=httpx.Response(200, json=api.job("FAILED", details=details))
    )
    with pytest.raises(WorkloadFailed, match="ContainerFailed"):
        wait_for_result(client, submitted_job(), store=LocalDirStore(tmp_path))


@respx.mock
def test_wait_for_result_completed_without_output(client, api, tmp_path):
    respx.get(f"{BASE}/jobs/aijob-e00abc").mock(return_value=httpx.Response(200, json=api.job("COMPLETED")))
    with pytest.raises(JobResultMissing):
        wait_for_result(client, submitted_job(), store=LocalDirStore(tmp_path))


@respx.mock
def test_round_trip_through_the_real_entrypoint(client, api, decision, tmp_path, engine):
    """Submit, run job_entry on exactly what the API received, read the result back from the bucket."""
    bucket_root = tmp_path / "bucket"
    container_files = tmp_path / "container"

    def fake_nebius_job(request: httpx.Request) -> httpx.Response:
        spec = json.loads(request.content)["spec"]
        env = env_of(spec)
        for injected in spec["injectedFiles"]:
            local = container_files / injected["containerPath"].lstrip("/")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(base64.b64decode(injected["content"]))
        mount = spec["volumes"][0]["containerPath"]
        environ = {
            INPUT_PATH_ENV: str(container_files / env[INPUT_PATH_ENV].lstrip("/")),
            OUTPUT_PATH_ENV: env[OUTPUT_PATH_ENV].replace(mount, str(bucket_root), 1),
        }
        code = job_entry.main([], environ=environ, stdout=io.StringIO(), stderr=io.StringIO())
        assert code == 0
        return httpx.Response(200, json=api.operation())

    respx.post(f"{BASE}/jobs").mock(side_effect=fake_nebius_job)
    respx.get(f"{BASE}/jobs/aijob-e00abc").mock(
        side_effect=[httpx.Response(200, json=api.job(s)) for s in ("PROVISIONING", "RUNNING", "COMPLETED")]
    )
    updates = []
    output = run_simulation_job(
        client,
        decision,
        image=IMAGE,
        bucket="storagebucket-e00b",
        store=LocalDirStore(bucket_root),
        n_scenarios=3_000_000,
        task="simulate_and_optimize",
        on_update=lambda job: updates.append(job.state.value),
        poll_interval_s=1,
    )
    assert output.ok and output.optimization is not None
    assert engine.simulate_calls[0][1]["n_scenarios"] == 3_000_000
    assert updates == ["PROVISIONING", "RUNNING", "COMPLETED"]
