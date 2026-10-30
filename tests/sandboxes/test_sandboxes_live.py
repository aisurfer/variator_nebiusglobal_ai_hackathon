"""Smoke tests against the real Sandboxes API. Run with ``pytest --live`` and
NEBIUS_API_KEY / NEBIUS_PROJECT_ID set (the project needs Beta access).
They verify the documented assumptions listed in src/variator/sandboxes/README.md.
"""

from __future__ import annotations

import os

import pytest

from variator.config import Settings
from variator.sandboxes import (
    IMAGE_ENV_VAR,
    SandboxesClient,
    SpawnRequest,
    ensure_python_image,
    run_python,
)

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_sandbox():
    with SandboxesClient(Settings.from_env()) as sandbox:
        yield sandbox


@pytest.fixture(scope="module")
def live_image(live_sandbox) -> str:
    """VARIATOR_SANDBOX_IMAGE, or the tagged Python + NumPy image (built on first use)."""
    return os.environ.get(IMAGE_ENV_VAR) or ensure_python_image(live_sandbox)


def test_live_whoami(live_sandbox):
    who = live_sandbox.whoami()
    assert who.token_uuid
    assert who.can("spawn") or who.can("spawn_disposable"), who.permissions


def test_live_run_print_one(live_sandbox, live_image):
    result = run_python(live_sandbox, "print(1)", image=live_image, timeout_s=60)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "1"
    assert "did not write output.json" in (result.output_error or "")


def test_live_output_json_both_modes(live_sandbox, live_image):
    code = "import json, numpy as np\njson.dump({'n': int(np.arange(4).sum())}, open('output.json', 'w'))\n"
    for mode in ("artifact", "stdout"):
        result = run_python(live_sandbox, code, image=live_image, timeout_s=60, output_mode=mode)
        assert result.output_json == {"n": 6}, (mode, result.stderr, result.output_error)


def test_live_networking_disabled(live_sandbox, live_image):
    code = "import os\nprint(sorted(os.listdir('/sys/class/net')))\n"
    result = run_python(live_sandbox, code, image=live_image, timeout_s=60)
    assert result.stdout.strip() in ("['lo']", "[]"), result.stdout


def test_live_event_stream(live_sandbox, live_image):
    op_id = live_sandbox.spawn(
        SpawnRequest(image=live_image, command="echo streamed", shell=True, disposable=True, timeout=30)
    )
    events = list(live_sandbox.stream_events(op_id))
    assert events[-1].type == "completion"
    assert "streamed" in "".join(e.output_text() for e in events if e.type == "stdout")
