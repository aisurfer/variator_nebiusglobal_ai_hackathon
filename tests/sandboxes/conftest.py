from __future__ import annotations

import pytest
import respx

from variator.config import Settings
from variator.sandboxes import SandboxesClient
from variator.sandboxes.testing import FakeSandboxes

BASE = "https://api.tokenfactory.nebius.com/sandboxes/v1"


class FakeClock:
    """Deterministic monotonic clock; ``sleep`` advances time and is recorded."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings() -> Settings:
    return Settings(nebius_api_key="test-key", nebius_project_id="proj-123", max_retries=2)


@pytest.fixture
def api():
    """respx router for the Sandboxes REST API; routes are relative to ``BASE``."""
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client(settings: Settings, clock: FakeClock) -> SandboxesClient:
    return SandboxesClient(settings, sleep=clock.sleep, clock=clock.now)


@pytest.fixture
def fake_sbx() -> FakeSandboxes:
    return FakeSandboxes()
