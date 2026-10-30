"""Fixtures for optimization tests (helpers live in ``opt_testing``)."""

from __future__ import annotations

import sys
import types

import pytest
from opt_testing import FakeCuopt

from variator.optimization import cuopt_local, cuopt_server


@pytest.fixture
def fake_cuopt(no_cuopt_by_default: None, monkeypatch: pytest.MonkeyPatch) -> FakeCuopt:
    """Install a fake ``cuopt`` package and pretend a GPU is present."""
    fake = FakeCuopt()
    package = types.ModuleType("cuopt")
    lp = fake.module()
    package.linear_programming = lp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cuopt", package)
    monkeypatch.setitem(sys.modules, "cuopt.linear_programming", lp)
    monkeypatch.setattr(cuopt_local, "gpu_available", lambda: True)
    return fake


@pytest.fixture(autouse=True)
def no_cuopt_by_default(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the machine from leaking into solver selection: no server, no GPU, no poll waits.

    ``gpu`` and ``live`` tests see the real environment.
    """
    if request.node.get_closest_marker("gpu") or request.node.get_closest_marker("live"):
        return
    monkeypatch.delenv(cuopt_server.SERVER_URL_ENV, raising=False)
    monkeypatch.delenv(cuopt_server.SERVER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(cuopt_server, "POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(cuopt_local, "gpu_available", lambda: False)
