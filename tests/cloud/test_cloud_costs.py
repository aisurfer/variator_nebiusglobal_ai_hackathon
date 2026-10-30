from datetime import UTC, date, datetime

import pytest

from variator.cloud import (
    PRICE_TABLE,
    EndpointSpec,
    JobInfo,
    JobSpec,
    estimate_cost,
    estimate_endpoint_cost,
    estimate_job_cost,
    job_run_cost,
    parse_preset,
)
from variator.cloud.costs import UnknownPrice


def test_parse_preset():
    assert parse_preset("1gpu-8vcpu-32gb").model_dump() == {"gpus": 1, "vcpus": 8, "ram_gib": 32}
    assert parse_preset("4vcpu-16gb").model_dump() == {"gpus": 0, "vcpus": 4, "ram_gib": 16}
    with pytest.raises(ValueError):
        parse_preset("large")


def test_l40s_matches_published_from_prices():
    on_demand = estimate_cost("gpu-l40s-a", "1gpu-8vcpu-32gb", hours=1)
    preemptible = estimate_cost("gpu-l40s-a", "1gpu-8vcpu-32gb", hours=1, preemptible=True)
    assert on_demand.usd_per_hour == pytest.approx(1.55, abs=0.01)  # "from $1.55/GPU-h"
    assert preemptible.usd_per_hour == pytest.approx(0.75, abs=0.01)  # "$0.74 preemptible"
    assert on_demand.approximate is True
    assert "https://nebius.com/prices" in on_demand.sources
    assert not preemptible.spot_floor_only


@pytest.mark.parametrize(
    ("platform", "preset", "hourly"),
    [
        ("gpu-h100-sxm", "1gpu-16vcpu-200gb", 4.50),
        ("gpu-h200-sxm", "1gpu-16vcpu-200gb", 5.40),
        ("gpu-b200-sxm", "1gpu-20vcpu-224gb", 8.50),
        ("gpu-h100-sxm", "8gpu-128vcpu-1600gb", 36.0),
        ("cpu-d3", "2vcpu-8gb", 0.066),
        ("cpu-d3", "4vcpu-16gb", 0.132),
    ],
)
def test_hourly_prices(platform, preset, hourly):
    assert estimate_cost(platform, preset, hours=1).compute_usd_per_hour == pytest.approx(hourly)


def test_dynamic_spot_prices_are_flagged_as_floor():
    estimate = estimate_cost("gpu-h100-sxm", "1gpu-16vcpu-200gb", hours=2, preemptible=True)
    assert estimate.spot_floor_only and estimate.total_usd == pytest.approx(1.58)


def test_disk_and_total():
    estimate = estimate_cost("cpu-d3", "2vcpu-8gb", hours=730, disk_gib=30)
    assert estimate.disk_usd_per_hour * 730 == pytest.approx(30 * 0.071, rel=1e-3)
    assert estimate.total_usd == pytest.approx(0.066 * 730 + 30 * 0.071, rel=1e-3)


def test_errors():
    with pytest.raises(UnknownPrice):
        estimate_cost("gpu-x9000", "1gpu-8vcpu-32gb", hours=1)
    with pytest.raises(ValueError, match="preemptible"):
        estimate_cost("cpu-d3", "2vcpu-8gb", hours=1, preemptible=True)
    with pytest.raises(ValueError):
        estimate_cost("cpu-d3", "2vcpu-8gb", hours=-1)
    assert all(p.platform == key for key, p in PRICE_TABLE.items())


def test_job_estimate_has_expected_and_worst_case():
    spec = JobSpec(name="j", image="i", timeout_s=2 * 3600)
    estimate = estimate_job_cost(spec, expected_runtime_s=600, cold_start_s=300)
    assert estimate.expected.hours == pytest.approx(0.25)
    assert estimate.worst_case.hours == pytest.approx(2 + 300 / 3600)
    assert estimate.expected.disk_gib == 100
    assert estimate.worst_case.total_usd > estimate.expected.total_usd


def test_endpoint_until_end_of_judging():
    spec = EndpointSpec(name="app", image="i", platform="cpu-d3", preset="2vcpu-8gb")
    start = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    estimate = estimate_endpoint_cost(spec, start=start, until=date(2026, 12, 15))
    assert estimate.hours == pytest.approx(47 * 24, abs=0.01)
    assert estimate.total_usd == pytest.approx(estimate.usd_per_hour * estimate.hours, rel=1e-3)
    assert 70 < estimate.total_usd < 90  # ~$0.07/h compute + 30 GiB disk


def test_job_run_cost_from_job_info(api):
    job = JobInfo.from_api(
        api.job("COMPLETED", created="2026-10-25T10:00:00Z", finished="2026-10-25T10:30:00Z")
    )
    cost = job_run_cost(job)
    assert cost is not None and cost.hours == pytest.approx(0.5) and cost.disk_gib == pytest.approx(100)
    assert job_run_cost(JobInfo.from_api(api.job("RUNNING"))) is None
    assert (
        job_run_cost(
            JobInfo.from_api(api.job("COMPLETED", platform="gpu-zz", finished="2026-10-25T11:00:00Z"))
        )
        is None
    )
