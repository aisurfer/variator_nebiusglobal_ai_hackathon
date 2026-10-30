"""Run a Python program in a fresh sandbox and collect its ``output.json``.

Protocol: the program is ``/work/main.py``; inputs are placed next to it under
``/work``; it runs with ``cwd=/work`` and writes ``/work/output.json``.

Output retrieval (``output_mode``):

* ``"artifact"`` (default, the documented route): the run saves its filesystem
  (``disposable=false``) and ``output.json`` is read from the result image via
  ``GET /inspect/{image}/download`` without guest compute
  (https://docs.tokenfactory.nebius.com/sandboxes/guides/work-with-files-and-artifacts.md).
  Needs the ``spawn`` permission and keeps a small image (180-day retention).
* ``"stdout"``: a disposable run; after the program exits, a shell epilogue prints
  ``output.json`` base64-encoded between markers on stdout. Use it when the token
  only has ``spawn_disposable`` or retained images are undesirable. It is bounded
  by the recorded-stdout cap (1 MiB default, 10 MiB max), so large outputs fail.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel

from variator.sandboxes.client import SandboxesClient
from variator.sandboxes.environment import DEFAULT_IMAGE
from variator.sandboxes.errors import (
    SandboxNotFoundError,
    SandboxOperationFailed,
    SandboxOutputTooLarge,
)
from variator.sandboxes.models import (
    DEFAULT_STREAM_CAP,
    FileMount,
    InstanceResult,
    Networking,
    OperationStatus,
    ProcessState,
    SpawnRequest,
)

WORK_DIR = "/work"
CODE_FILE = "main.py"
OUTPUT_FILE = "output.json"
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576

OutputMode = Literal["artifact", "stdout"]

_BEGIN = "<<<VARIATOR_OUTPUT_JSON:"
_END = ":VARIATOR_OUTPUT_JSON>>>"
_MARKER_RE = re.compile("\n?" + re.escape(_BEGIN) + r"([A-Za-z0-9+/=\s]*?)" + re.escape(_END) + "\n?")

_ARTIFACT_COMMAND = f"exec python3 -u {CODE_FILE}"
_ENCODE_OUTPUT = (
    f'import base64,sys;sys.stdout.write(base64.b64encode(open("{OUTPUT_FILE}","rb").read()).decode())'
)
# Keeps the program's exit status; the marker block is appended only if output.json exists.
_STDOUT_COMMAND = f"""python3 -u {CODE_FILE}
rc=$?
if [ -f {OUTPUT_FILE} ]; then
  printf '\\n%s' '{_BEGIN}'
  python3 -c '{_ENCODE_OUTPUT}'
  printf '%s\\n' '{_END}'
fi
exit $rc"""


class SandboxRunResult(BaseModel):
    """Outcome of one ``run_python`` call. ``output_error`` explains a missing ``output_json``."""

    stdout: str
    stderr: str
    exit_code: int | None
    output_json: dict[str, Any] | None
    duration_s: float | None
    operation_id: str
    timed_out: bool = False
    signal: int | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    output_error: str | None = None
    image: str | None = None
    result_image: str | None = None

    @property
    def succeeded(self) -> bool:
        """The process exited 0 within its time limit (output may still be missing)."""
        return self.exit_code == 0 and not self.timed_out

    @property
    def ok(self) -> bool:
        return self.succeeded and self.output_json is not None


def run_python(
    sandbox: SandboxesClient,
    code: str,
    *,
    inputs: Mapping[str, bytes | str] | None = None,
    timeout_s: int = 120,
    network: bool = False,
    image: str | None = None,
    output_mode: OutputMode = "artifact",
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_stream_bytes: int = DEFAULT_STREAM_CAP,
    env: Mapping[str, str] | None = None,
    wait_timeout_s: float | None = None,
) -> SandboxRunResult:
    """Execute ``code`` as ``/work/main.py`` in a new operation and return its result.

    ``inputs`` maps relative file names to contents placed under ``/work``.
    Networking is off unless ``network=True``. A non-zero exit or timeout is
    returned (not raised); platform outcomes ``FAILED``/``CANCELLED`` raise
    ``SandboxOperationFailed``, a client deadline raises ``SandboxTimeoutError``
    (after cancelling the operation), and API errors raise ``SandboxHTTPError``.
    """
    files: dict[str, bytes] = {f"{WORK_DIR}/{CODE_FILE}": code.encode("utf-8")}
    for name, content in (inputs or {}).items():
        files[_guest_path(name)] = content.encode("utf-8") if isinstance(content, str) else content

    mounts = {path: FileMount(uuid=sandbox.upload_bytes(data).uuid) for path, data in files.items()}
    image_ref = image or sandbox.default_image or DEFAULT_IMAGE
    request = SpawnRequest(
        image=image_ref,
        command=_STDOUT_COMMAND if output_mode == "stdout" else _ARTIFACT_COMMAND,
        shell=True,  # inherit the image ENV (PATH, PYTHON*) as documented for shell runs
        cwd=WORK_DIR,
        disposable=output_mode == "stdout",
        timeout=timeout_s,
        truncate_output_at=max_stream_bytes,
        networking=Networking(enabled=network),
        files=mounts,
        env=dict(env) if env else None,
    )

    started = time.monotonic()
    operation = sandbox.execute(request, wait_timeout_s=wait_timeout_s)
    elapsed = time.monotonic() - started
    if operation.status != OperationStatus.SUCCESS:
        raise SandboxOperationFailed(
            f"sandbox operation {operation.uuid} ended {operation.status}: {operation.error or 'no error'}",
            operation_id=operation.uuid,
            status=operation.status,
        )

    result = operation.instance_result or InstanceResult()
    state = result.state or ProcessState()
    stdout = result.stdout.text() if result.stdout else ""
    stderr = result.stderr.text() if result.stderr else ""
    stdout_truncated = bool(result.stdout and result.stdout.truncated)
    succeeded = state.exit_code == 0 and not state.timed_out

    raw: bytes | None = None
    output_error: str | None = None
    if output_mode == "stdout":
        stdout, raw, output_error = _take_marker(stdout, stdout_truncated, max_output_bytes)
    elif succeeded:
        raw, output_error = _download_output(sandbox, operation.result_image_uuid, max_output_bytes)

    output_json: dict[str, Any] | None = None
    if succeeded and raw is not None:
        output_json, output_error = _parse_output(raw)
    elif not succeeded:
        output_error = None  # the process failure is the error to report

    return SandboxRunResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=state.exit_code,
        output_json=output_json,
        duration_s=operation.duration if operation.duration is not None else elapsed,
        operation_id=operation.uuid,
        timed_out=state.timed_out,
        signal=state.signal if state.signalled else None,
        stdout_truncated=stdout_truncated,
        stderr_truncated=bool(result.stderr and result.stderr.truncated),
        output_error=output_error,
        image=image_ref,
        result_image=operation.result_image_uuid,
    )


def _guest_path(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError(f"input name {name!r} must be a relative path without '..'")
    if str(path) in (CODE_FILE, OUTPUT_FILE):
        raise ValueError(f"input name {name!r} is reserved")
    return f"{WORK_DIR}/{path}"


def _download_output(
    sandbox: SandboxesClient, result_image: str | None, max_bytes: int
) -> tuple[bytes | None, str | None]:
    if not result_image:
        return None, "the operation returned no result image, so output.json cannot be read"
    try:
        return sandbox.download_file(result_image, f"{WORK_DIR}/{OUTPUT_FILE}", max_bytes=max_bytes), None
    except SandboxNotFoundError:
        return None, f"the program did not write {OUTPUT_FILE}"
    except SandboxOutputTooLarge as exc:
        return None, f"{OUTPUT_FILE} is too large: {exc}"


def _take_marker(stdout: str, truncated: bool, max_bytes: int) -> tuple[str, bytes | None, str | None]:
    matches = list(_MARKER_RE.finditer(stdout))
    if not matches:
        if truncated:
            return stdout, None, "stdout was truncated before output.json could be read"
        return stdout, None, f"the program did not write {OUTPUT_FILE}"
    match = matches[-1]
    cleaned = stdout[: match.start()] + stdout[match.end() :]
    try:
        raw = base64.b64decode(re.sub(r"\s", "", match.group(1)), validate=True)
    except (binascii.Error, ValueError) as exc:
        return cleaned, None, f"could not decode {OUTPUT_FILE} from stdout: {exc}"
    if len(raw) > max_bytes:
        return cleaned, None, f"{OUTPUT_FILE} is too large: {len(raw)} bytes, limit is {max_bytes}"
    return cleaned, raw, None


def _parse_output(raw: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{OUTPUT_FILE} is not valid JSON: {exc}"
    if not isinstance(value, dict):
        return None, f"{OUTPUT_FILE} must contain a JSON object, got {type(value).__name__}"
    return value, None
