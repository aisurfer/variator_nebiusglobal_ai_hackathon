from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from variator.cloud import ServerlessClient
from variator.config import Settings
from variator.domain import HistogramBin, MetricSpec, OptimizationResult, OptionStats, SimulationResult

BASE = "https://api.nebius.cloud/ai/v1"
PROJECT = "project-e00test"
SUBNET = "vpcsubnet-e00test"
TOKEN = "iam-test-token"


class FakeTime:
    """Deterministic clock: ``sleep`` advances ``clock`` instead of waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


class Api:
    """Builders for REST payloads shaped like https://api.nebius.cloud/openapi.json."""

    base = BASE
    project = PROJECT

    @staticmethod
    def job(
        state: str = "RUNNING",
        *,
        job_id: str = "aijob-e00abc",
        name: str = "variator-sim-test",
        details: dict[str, Any] | None = None,
        platform: str = "gpu-l40s-a",
        preset: str = "1gpu-8vcpu-32gb",
        created: str | None = "2026-10-25T10:00:00Z",
        started: str | None = None,
        finished: str | None = None,
    ) -> dict[str, Any]:
        status: dict[str, Any] = {"state": state}
        if details:
            status["stateDetails"] = details
        if started:
            status["startedAt"] = started
        if finished:
            status["finishedAt"] = finished
        return {
            "metadata": {"id": job_id, "parentId": PROJECT, "name": name, "createdAt": created},
            "spec": {
                "image": "cr.eu-north1.nebius.cloud/e00reg/variator-sim:latest",
                "platform": platform,
                "preset": preset,
                "timeout": "3600s",
                "disk": {"type": "NETWORK_SSD", "sizeBytes": "107374182400"},
            },
            "status": status,
        }

    @staticmethod
    def endpoint(
        state: str = "RUNNING",
        *,
        endpoint_id: str = "aiendpoint-e00xyz",
        public: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status: dict[str, Any] = {"state": state}
        if public is not None:
            status["publicEndpoints"] = public
        if details:
            status["stateDetails"] = details
        return {
            "metadata": {"id": endpoint_id, "parentId": PROJECT, "name": "variator-app"},
            "spec": {"image": "img", "platform": "cpu-d3", "preset": "2vcpu-8gb"},
            "status": status,
        }

    @staticmethod
    def operation(
        *,
        op_id: str = "aiop-e00op1",
        resource_id: str | None = "aijob-e00abc",
        done: bool = False,
        code: int | None = None,
        message: str | None = None,
        details: list[Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"id": op_id, "createdAt": "2026-10-25T10:00:00Z"}
        if resource_id:
            body["resourceId"] = resource_id
        if done:
            status: dict[str, Any] = {}
            if code:
                status["code"] = code
            if message:
                status["message"] = message
            if details:
                status["details"] = details
            body["status"] = status
            body["finishedAt"] = "2026-10-25T10:00:05Z"
        return body


@pytest.fixture
def api() -> type[Api]:
    return Api


@pytest.fixture
def settings() -> Settings:
    return Settings(
        nebius_iam_token=TOKEN,
        nebius_cloud_project_id=PROJECT,
        nebius_subnet_id=SUBNET,
        max_retries=2,
    )


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def client(settings: Settings, fake_time: FakeTime):
    with ServerlessClient(settings, sleep=fake_time.sleep, clock=fake_time.clock) as c:
        yield c


def make_simulation_result(backend: str = "cupy", n: int = 1_000_000) -> SimulationResult:
    stats = [
        OptionStats(
            option=name,
            mean=mean,
            std=1.0,
            p5=mean - 2,
            p25=mean - 1,
            p50=mean,
            p75=mean + 1,
            p95=mean + 2,
            prob_loss=0.1,
            var_95=mean - 2,
            cvar_95=mean - 3,
            prob_best=best,
            histogram=[HistogramBin(left=mean - 1, right=mean + 1, count=n)],
        )
        for name, mean, best in (("second_warehouse", 5.0, 0.6), ("expand_current", 4.0, 0.3))
    ]
    return SimulationResult(
        metric=MetricSpec(name="3-year profit contribution"),
        n_scenarios=n,
        seed=7,
        backend=backend,
        elapsed_s=1.5,
        options=stats,
    )


def make_optimization_result() -> OptimizationResult:
    return OptimizationResult(
        selected=["second_warehouse"],
        expected_value=5.0,
        cvar=2.0,
        budget_used=2_200_000,
        solver="cuopt",
        status="Optimal",
        proven_optimal=False,
        elapsed_s=0.4,
    )


@pytest.fixture
def sim_result() -> SimulationResult:
    return make_simulation_result()


@pytest.fixture
def opt_result() -> OptimizationResult:
    return make_optimization_result()


@pytest.fixture
def engine(monkeypatch, sim_result, opt_result):
    """Fake ``variator.simulation`` / ``variator.optimization`` behind the real lazy imports."""
    state = SimpleNamespace(simulate_calls=[], optimize_calls=[], simulate_error=None, optimize_error=None)

    def simulate(model, **kwargs):
        state.simulate_calls.append((model, kwargs))
        if state.simulate_error:
            raise state.simulate_error
        return SimpleNamespace(
            result=sim_result,
            option_names=[o.option for o in sim_result.options],
            scenarios=np.zeros((4, 2)),
            notes=["correlation matrix repaired"],
        )

    def optimize_decision(run, model, **kwargs):
        state.optimize_calls.append((run, model, kwargs))
        if state.optimize_error:
            raise state.optimize_error
        return opt_result

    sim_module = types.ModuleType("variator.simulation")
    sim_module.simulate = simulate
    opt_module = types.ModuleType("variator.optimization")
    opt_module.optimize_decision = optimize_decision
    monkeypatch.setitem(sys.modules, "variator.simulation", sim_module)
    monkeypatch.setitem(sys.modules, "variator.optimization", opt_module)
    return state
