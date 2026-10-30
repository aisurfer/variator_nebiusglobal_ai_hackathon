"""APPROXIMATE cost estimates for Serverless AI jobs and endpoints.

Serverless AI bills at Compute prices, per second, while a workload runs
(https://docs.nebius.com/serverless/pricing-quotas.md). The table below is a snapshot
of the public price list (https://nebius.com/prices,
https://docs.nebius.com/compute/resources/pricing.md) taken on ``PRICES_AS_OF``. It
ignores taxes, discounts, Object Storage and traffic, and preemptible H100/H200/B200
prices are dynamic spot prices of which only the floor is known. Use the numbers for
budgeting and UI hints, never for billing. AI Cloud is not covered by the hackathon
credits: it is paid from our card.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from typing import Literal

from pydantic import BaseModel, Field

from variator.cloud.resources import JobInfo
from variator.cloud.specs import GIB, EndpointSpec, JobSpec

PRICES_AS_OF = "2026-10-24"
PRICE_SOURCES = ("https://nebius.com/prices", "https://docs.nebius.com/compute/resources/pricing.md")
DISK_SSD_USD_PER_GIB_MONTH = 0.071  # Network SSD, per GiB per 730 hours
HOURS_PER_BILLING_MONTH = 730
# Judging runs until Dec 15: a hosted demo must stay reachable until then.
DEMO_UP_UNTIL = date(2026, 12, 15)
# VM start + image pull before the container runs; billed but not useful work.
DEFAULT_COLD_START_S = 300.0


class PlatformPrice(BaseModel):
    """USD per hour. GPU platforms with an all-in GPU-hour price have vcpu/ram = 0."""

    platform: str
    label: str
    gpu_hour: float = 0.0
    vcpu_hour: float = 0.0
    ram_gib_hour: float = 0.0
    preemptible_gpu_hour: float | None = None  # None: no preemptible VMs (CPU platforms)
    preemptible_vcpu_hour: float = 0.0
    preemptible_ram_gib_hour: float = 0.0
    spot_floor_only: bool = False  # dynamic spot price; the table holds its documented minimum


def _all_in(platform: str, label: str, on_demand: float, spot_floor: float) -> PlatformPrice:
    return PlatformPrice(
        platform=platform,
        label=label,
        gpu_hour=on_demand,
        preemptible_gpu_hour=spot_floor,
        spot_floor_only=True,
    )


PRICE_TABLE: dict[str, PlatformPrice] = {
    # L40S is priced per component: 1gpu-8vcpu-32gb = $1.55/h on demand, $0.75/h preemptible.
    "gpu-l40s-a": PlatformPrice(
        platform="gpu-l40s-a",
        label="NVIDIA L40S (Intel)",
        gpu_hour=1.35,
        vcpu_hour=0.012,
        ram_gib_hour=0.0032,
        preemptible_gpu_hour=0.65,
        preemptible_vcpu_hour=0.006,
        preemptible_ram_gib_hour=0.0016,
    ),
    "gpu-l40s-d": PlatformPrice(
        platform="gpu-l40s-d",
        label="NVIDIA L40S (AMD)",
        gpu_hour=1.35,
        vcpu_hour=0.01,
        ram_gib_hour=0.0032,
        preemptible_gpu_hour=0.65,
        preemptible_vcpu_hour=0.005,
        preemptible_ram_gib_hour=0.0016,
    ),
    "gpu-rtx6000": _all_in("gpu-rtx6000", "NVIDIA RTX PRO 6000", 1.80, 0.79),
    "gpu-rtx6000-a": _all_in("gpu-rtx6000-a", "NVIDIA RTX PRO 6000", 1.80, 0.79),
    "gpu-h100-sxm": _all_in("gpu-h100-sxm", "NVIDIA H100", 4.50, 0.79),
    "gpu-h200-sxm": _all_in("gpu-h200-sxm", "NVIDIA H200", 5.40, 0.79),
    "gpu-b200-sxm": _all_in("gpu-b200-sxm", "NVIDIA B200", 8.50, 0.99),
    "gpu-b200-sxm-a": _all_in("gpu-b200-sxm-a", "NVIDIA B200", 8.50, 0.99),
    "gpu-b300-sxm": _all_in("gpu-b300-sxm", "NVIDIA B300", 9.50, 0.99),
    # CPU: 2vcpu-8gb = $0.066/h, 4vcpu-16gb = $0.132/h.
    "cpu-d3": PlatformPrice(
        platform="cpu-d3", label="CPU AMD EPYC Genoa", vcpu_hour=0.015, ram_gib_hour=0.0045
    ),
    "cpu-e2": PlatformPrice(
        platform="cpu-e2", label="CPU Intel Ice Lake", vcpu_hour=0.012, ram_gib_hour=0.0045
    ),
}

_PRESET_RE = re.compile(r"^(?:(?P<gpus>\d+)gpu-)?(?P<vcpus>\d+)vcpu-(?P<ram>\d+)gb$")


class UnknownPrice(ValueError):
    """The platform is not in ``PRICE_TABLE``."""


class PresetShape(BaseModel):
    gpus: int
    vcpus: int
    ram_gib: int


def parse_preset(preset: str) -> PresetShape:
    """``"1gpu-8vcpu-32gb"`` -> 1 GPU, 8 vCPUs, 32 GiB (the naming documented for presets)."""
    match = _PRESET_RE.match(preset)
    if not match:
        raise ValueError(f"cannot parse preset {preset!r} (expected e.g. 1gpu-8vcpu-32gb or 4vcpu-16gb)")
    return PresetShape(gpus=int(match["gpus"] or 0), vcpus=int(match["vcpus"]), ram_gib=int(match["ram"]))


class CostEstimate(BaseModel):
    """An approximate cost; read ``note`` before showing the number to anyone."""

    approximate: Literal[True] = True
    platform: str
    preset: str
    preemptible: bool
    hours: float
    disk_gib: float
    compute_usd_per_hour: float
    disk_usd_per_hour: float
    usd_per_hour: float
    total_usd: float
    spot_floor_only: bool = Field(description="Preemptible price is the dynamic spot price's minimum")
    as_of: str = PRICES_AS_OF
    sources: tuple[str, ...] = PRICE_SOURCES
    note: str = (
        "Approximate: public list prices, excluding taxes, storage buckets and traffic. "
        "Check the billing page in the Nebius console for real charges."
    )


class JobCostEstimate(BaseModel):
    expected: CostEstimate
    worst_case: CostEstimate = Field(description="If the job runs until its timeout")


def compute_usd_per_hour(platform: str, preset: str, *, preemptible: bool = False) -> tuple[float, bool]:
    """Hourly compute price and whether it is only a spot-price floor."""
    price = PRICE_TABLE.get(platform)
    if price is None:
        raise UnknownPrice(f"no price for platform {platform!r}; add it to PRICE_TABLE ({PRICE_SOURCES[0]})")
    shape = parse_preset(preset)
    if preemptible:
        if price.preemptible_gpu_hour is None:
            raise ValueError(f"{platform} has no preemptible VMs (GPU platforms only)")
        hourly = (
            shape.gpus * price.preemptible_gpu_hour
            + shape.vcpus * price.preemptible_vcpu_hour
            + shape.ram_gib * price.preemptible_ram_gib_hour
        )
        return hourly, price.spot_floor_only
    hourly = shape.gpus * price.gpu_hour + shape.vcpus * price.vcpu_hour + shape.ram_gib * price.ram_gib_hour
    return hourly, False


def estimate_cost(
    platform: str, preset: str, *, hours: float, preemptible: bool = False, disk_gib: float = 0.0
) -> CostEstimate:
    """Approximate cost of running one VM of ``platform``/``preset`` for ``hours``."""
    if hours < 0:
        raise ValueError("hours must be >= 0")
    compute, floor_only = compute_usd_per_hour(platform, preset, preemptible=preemptible)
    disk = disk_gib * DISK_SSD_USD_PER_GIB_MONTH / HOURS_PER_BILLING_MONTH
    hourly = compute + disk
    return CostEstimate(
        platform=platform,
        preset=preset,
        preemptible=preemptible,
        hours=hours,
        disk_gib=disk_gib,
        compute_usd_per_hour=round(compute, 6),
        disk_usd_per_hour=round(disk, 6),
        usd_per_hour=round(hourly, 6),
        total_usd=round(hourly * hours, 4),
        spot_floor_only=floor_only,
    )


def estimate_job_cost(
    spec: JobSpec, *, expected_runtime_s: float, cold_start_s: float = DEFAULT_COLD_START_S
) -> JobCostEstimate:
    """Expected cost (cold start + runtime) and the cap if the job hits ``spec.timeout_s``."""

    def at(seconds: float) -> CostEstimate:
        return estimate_cost(
            spec.platform, spec.preset, hours=seconds / 3600, preemptible=spec.preemptible,
            disk_gib=spec.effective_disk_gib,
        )

    expected_s = min(cold_start_s + expected_runtime_s, cold_start_s + spec.timeout_s)
    return JobCostEstimate(expected=at(expected_s), worst_case=at(cold_start_s + spec.timeout_s))


def estimate_endpoint_cost(
    spec: EndpointSpec, *, until: date | datetime = DEMO_UP_UNTIL, start: datetime | None = None
) -> CostEstimate:
    """Cost of keeping the endpoint RUNNING 24/7 from ``start`` (now) to the end of ``until``.

    A stopped endpoint costs nothing (no compute, no disk) but is unreachable for judges.
    """
    start = start or datetime.now(UTC)
    end = until if isinstance(until, datetime) else datetime.combine(until, time.max, tzinfo=UTC)
    hours = max(0.0, (end - start).total_seconds() / 3600)
    return estimate_cost(
        spec.platform,
        spec.preset,
        hours=hours,
        preemptible=spec.preemptible,
        disk_gib=spec.effective_disk_gib,
    )


def job_run_cost(job: JobInfo) -> CostEstimate | None:
    """Approximate cost of a finished job, from creation to finish (covers provisioning and
    image pull, so it errs high). None when timestamps or the platform are unknown."""
    if not (job.platform and job.preset and job.finished_at):
        return None
    begin = job.created_at or job.started_at
    if begin is None:
        return None
    hours = max(0.0, (job.finished_at - begin).total_seconds() / 3600)
    disk_gib = (job.disk_size_bytes or 0) / GIB
    try:
        return estimate_cost(
            job.platform, job.preset, hours=hours, preemptible=job.preemptible, disk_gib=disk_gib
        )
    except ValueError:  # includes UnknownPrice
        return None
