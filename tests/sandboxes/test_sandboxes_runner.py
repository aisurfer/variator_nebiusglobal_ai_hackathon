from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from variator.sandboxes import (
    DEFAULT_IMAGE,
    IMAGE_ENV_VAR,
    SandboxOperationFailed,
    SandboxTimeoutError,
    run_python,
)

CODE = "import json\njson.dump({'x': 1}, open('output.json', 'w'))\nprint('hi')\n"


def downloads(fake) -> list[str]:
    return [r.url.params["path"] for r in fake.requests if r.url.path.endswith("/download")]


def test_artifact_mode_places_files_and_reads_output_from_result_image(fake_sbx):
    fake_sbx.add_run(stdout="hi\n", output={"x": 1})
    sandbox = fake_sbx.client()
    result = run_python(sandbox, CODE, inputs={"data.csv": "a,b\n1,2\n", "raw/blob.bin": b"\x00\xff"})

    assert result.ok and result.output_json == {"x": 1}
    assert result.stdout == "hi\n" and result.exit_code == 0
    assert result.duration_s == pytest.approx(1.25)
    assert result.operation_id in fake_sbx.operations
    assert result.result_image is not None

    spec = fake_sbx.spawned[0]
    assert spec["networking"] == {"enabled": False}
    assert spec["disposable"] is False  # the result image is needed to read output.json
    assert spec["shell"] is True and spec["cwd"] == "/work"
    assert spec["timeout"] == 120 and spec["truncate_output_at"] == 1_048_576
    assert spec["image"] == DEFAULT_IMAGE
    assert "main.py" in spec["command"]
    assert fake_sbx.uploaded("/work/main.py") == CODE.encode()
    assert fake_sbx.uploaded("/work/data.csv") == b"a,b\n1,2\n"
    assert fake_sbx.uploaded("/work/raw/blob.bin") == b"\x00\xff"
    assert downloads(fake_sbx) == ["/work/output.json"]


def test_network_image_and_limits_are_configurable(fake_sbx):
    fake_sbx.add_run(output={})
    run_python(
        fake_sbx.client(),
        CODE,
        network=True,
        image="tag:custom:1",
        timeout_s=30,
        max_stream_bytes=4096,
        env={"MODE": "fast"},
    )
    spec = fake_sbx.spawned[0]
    assert spec["networking"] == {"enabled": True}
    assert spec["image"] == "tag:custom:1"
    assert spec["timeout"] == 30 and spec["truncate_output_at"] == 4096
    assert spec["env"] == {"MODE": "fast"}


def test_default_image_from_client_or_env(fake_sbx, monkeypatch):
    fake_sbx.add_run(output={}).add_run(output={})
    run_python(fake_sbx.client(default_image="tag:from-client:1"), CODE)
    monkeypatch.setenv(IMAGE_ENV_VAR, "tag:from-env:1")
    run_python(fake_sbx.client(), CODE)
    assert [s["image"] for s in fake_sbx.spawned] == ["tag:from-client:1", "tag:from-env:1"]


def test_identical_files_are_uploaded_once(fake_sbx):
    fake_sbx.add_run(output={}).add_run(output={})
    sandbox = fake_sbx.client()
    run_python(sandbox, CODE)
    run_python(sandbox, CODE)
    posts = [r for r in fake_sbx.requests if r.method == "POST" and r.url.path.endswith("/files")]
    assert len(posts) == 1


def test_missing_output_file(fake_sbx):
    fake_sbx.add_run(stdout="forgot\n", output=None)
    result = run_python(fake_sbx.client(), "print('forgot')")
    assert result.succeeded and not result.ok
    assert result.output_json is None
    assert "did not write output.json" in result.output_error


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("not json", "not valid JSON"),
        ("[1, 2]", "must contain a JSON object"),
        (b"\xff\xfe", "not valid JSON"),
    ],
)
def test_invalid_output_json(fake_sbx, output, message):
    fake_sbx.add_run(output=output)
    result = run_python(fake_sbx.client(), CODE)
    assert result.output_json is None and message in result.output_error


def test_output_size_limit_in_artifact_mode(fake_sbx):
    fake_sbx.add_run(output={"blob": "x" * 5000})
    result = run_python(fake_sbx.client(), CODE, max_output_bytes=1000)
    assert result.output_json is None
    assert "too large" in result.output_error


def test_nonzero_exit_returns_traceback_without_reading_output(fake_sbx):
    traceback = 'Traceback (most recent call last):\n  File "main.py"\nZeroDivisionError: division by zero\n'
    fake_sbx.add_run(exit_code=1, stderr=traceback, output={"x": 1})
    result = run_python(fake_sbx.client(), CODE)
    assert result.exit_code == 1 and not result.succeeded
    assert "ZeroDivisionError" in result.stderr
    assert result.output_json is None and result.output_error is None
    assert downloads(fake_sbx) == []


def test_guest_timeout_is_reported(fake_sbx):
    fake_sbx.add_run(exit_code=None, signal=9, timed_out=True, stdout="step 1\n")
    result = run_python(fake_sbx.client(), "while True: pass", timeout_s=5)
    assert result.timed_out and result.signal == 9
    assert not result.succeeded and result.output_json is None


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_platform_failure_raises(fake_sbx, status):
    fake_sbx.add_run(status=status, error="no capacity")
    with pytest.raises(SandboxOperationFailed, match="no capacity") as info:
        run_python(fake_sbx.client(), CODE)
    assert info.value.status == status and info.value.operation_id


def test_client_deadline_cancels_operation(fake_sbx):
    fake_sbx.add_run(polls_before_done=10_000)
    with pytest.raises(SandboxTimeoutError):
        run_python(fake_sbx.client(), CODE, wait_timeout_s=0.0)
    assert fake_sbx.cancelled == [next(iter(fake_sbx.operations))]


def test_stdout_mode_uses_disposable_run_and_marker(fake_sbx):
    fake_sbx.add_run(stdout="hi\n", output={"x": 2})
    result = run_python(fake_sbx.client(), CODE, output_mode="stdout")
    assert result.output_json == {"x": 2}
    assert result.stdout == "hi\n"  # marker stripped
    assert fake_sbx.spawned[0]["disposable"] is True
    assert result.result_image is None
    assert downloads(fake_sbx) == []


def test_stdout_mode_reports_truncation_and_size(fake_sbx):
    fake_sbx.add_run(stdout="x" * 100, stdout_truncated=True, output=None)
    fake_sbx.add_run(output={"blob": "y" * 3000})
    sandbox = fake_sbx.client()
    truncated = run_python(sandbox, CODE, output_mode="stdout")
    assert truncated.stdout_truncated and "truncated" in truncated.output_error
    too_big = run_python(sandbox, CODE, output_mode="stdout", max_output_bytes=500)
    assert too_big.output_json is None and "too large" in too_big.output_error


def test_stdout_mode_rejects_garbled_marker(fake_sbx):
    fake_sbx.add_run(stdout="\n<<<VARIATOR_OUTPUT_JSON:not*base64:VARIATOR_OUTPUT_JSON>>>\n")
    result = run_python(fake_sbx.client(), CODE, output_mode="stdout")
    assert result.output_json is None
    assert "did not write" in result.output_error  # an invalid marker is not treated as output


BAD_NAMES = ["../escape.txt", "/etc/passwd", "main.py", "output.json", "", "a/../b", "a\\b"]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_input_names_are_validated(fake_sbx, name):
    with pytest.raises(ValueError):
        run_python(fake_sbx.client(), CODE, inputs={name: "x"})
    assert fake_sbx.spawned == []


def test_stream_cap_above_api_maximum_is_rejected(fake_sbx):
    with pytest.raises(ValidationError):
        run_python(fake_sbx.client(), CODE, max_stream_bytes=20_000_000)


def test_result_is_json_serialisable_for_the_ui(fake_sbx):
    fake_sbx.add_run(output={"x": 1})
    result = run_python(fake_sbx.client(), CODE)
    assert json.loads(result.model_dump_json())["output_json"] == {"x": 1}
