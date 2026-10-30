"""Smoke tests against the real Nebius AI Cloud API. Run with ``pytest --live tests/cloud``.

Needs NEBIUS_IAM_TOKEN (``nebius iam get-access-token``), NEBIUS_CLOUD_PROJECT_ID and
optionally NEBIUS_SUBNET_ID. The tiny job costs well under one cent (2 vCPUs for a few
minutes) and needs one free VM and 2 vCPUs of quota.
"""

import pytest

from variator.cloud import JobSpec, JobState, ServerlessClient, job_run_cost
from variator.cloud.job_io import new_run_id
from variator.config import Settings


@pytest.fixture
def live_client():
    with ServerlessClient(Settings.from_env()) as client:
        yield client


def tiny_cpu_job() -> JobSpec:
    return JobSpec(
        name=f"variator-smoke-{new_run_id()}",
        image="python:3.12-slim",
        container_command="python",
        args="-c print(6*7)",
        platform="cpu-d3",
        preset="2vcpu-8gb",
        disk_size_gib=20,
        timeout_s=3600,
    )


@pytest.mark.live
def test_live_list_jobs_and_endpoints(live_client):
    assert isinstance(live_client.list_jobs(page_size=10), list)
    assert isinstance(live_client.list_endpoints(page_size=10), list)


@pytest.mark.live
def test_live_dry_run_create_validates_spec(live_client):
    operation = live_client.create_job(tiny_cpu_job(), dry_run=True)
    assert not operation.done or operation.succeeded


@pytest.mark.live
def test_live_tiny_cpu_job_runs_to_completion(live_client):
    operation = live_client.create_job(tiny_cpu_job())
    assert operation.resource_id
    try:
        job = live_client.wait_for_job(
            operation.resource_id,
            poll_interval_s=10,
            timeout_s=30 * 60,
            operation=operation,
            raise_on_failure=True,
        )
        assert job.state is JobState.COMPLETED
        cost = job_run_cost(job)
        assert cost is None or cost.total_usd < 0.5
    finally:
        live_client.delete_job(operation.resource_id)
