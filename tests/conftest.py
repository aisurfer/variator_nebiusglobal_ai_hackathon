from __future__ import annotations

import shutil
import subprocess

import pytest

from variator.domain.examples import warehouse_decision
from variator.llm.testing import FakeTokenFactory


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live", action="store_true", default=False, help="run tests that call real APIs"
    )


def _has_gpu() -> bool:
    """True when nvidia-smi lists a GPU. Tests needing CuPy/cuOpt importorskip them."""
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and "GPU" in out.stdout


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_live = pytest.mark.skip(reason="needs --live and real API keys")
    skip_gpu = pytest.mark.skip(reason="needs an NVIDIA GPU")
    gpu = _has_gpu()
    for item in items:
        if "live" in item.keywords and not config.getoption("--live"):
            item.add_marker(skip_live)
        if "gpu" in item.keywords and not gpu:
            item.add_marker(skip_gpu)


@pytest.fixture(autouse=True)
def _code_cache(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Code metrics cache in a temporary folder, not in the repository."""
    monkeypatch.setenv("VARIATOR_CODE_CACHE", str(tmp_path_factory.mktemp("code_cache")))


@pytest.fixture
def fake_tf() -> FakeTokenFactory:
    """Offline Token Factory: queue replies, then build a client with ``fake_tf.client()``."""
    return FakeTokenFactory()


@pytest.fixture
def decision():
    return warehouse_decision()
