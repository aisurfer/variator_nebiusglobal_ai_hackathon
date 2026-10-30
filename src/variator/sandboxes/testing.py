"""Offline fake of the Sandboxes REST API for unit tests (no network, no keys).

Usage::

    fake = FakeSandboxes()
    fake.add_run(stderr="Traceback ...", exit_code=1)
    fake.add_run(stdout="done\\n", output={"options": {...}})
    sandbox = fake.client()
    result = run_python(sandbox, "print('hi')")
    assert fake.spawned[0]["networking"] == {"enabled": False}

Each spawned operation consumes the next queued run. Uploaded files are
content-addressed like the real API; a retained run stores its files plus
``/work/output.json`` in a result image readable via the inspect download route.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import string
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from variator.config import DEFAULT_SANDBOXES_BASE_URL, Settings
from variator.sandboxes.client import SandboxesClient

_PRINTABLE = set(string.printable.encode()) - set(b"\x0b\x0c")
_OUTPUT_PATH = "/work/output.json"
_STDOUT_MARKER = "<<<VARIATOR_OUTPUT_JSON:"
_STDOUT_END = ":VARIATOR_OUTPUT_JSON>>>"


@dataclass
class FakeRun:
    """Scripted outcome of one spawned operation."""

    exit_code: int | None = 0
    stdout: str = ""
    stderr: str = ""
    output: dict[str, Any] | str | bytes | None = None  # content of /work/output.json
    timed_out: bool = False
    signal: int = -1
    status: str = "SUCCESS"  # platform outcome: SUCCESS / FAILED / CANCELLED
    error: str | None = None
    polls_before_done: int = 1  # EXECUTING answers before the terminal one
    duration: float = 1.25
    stdout_truncated: bool = False


@dataclass
class _Op:
    id: str
    spec: dict[str, Any]
    run: FakeRun
    polls: int = 0
    status: str = "PENDING"
    result_image: str | None = None
    image_uuid: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def stream_repr(data: str | bytes, truncated: bool = False) -> dict[str, Any]:
    """StreamRepr the way the API encodes it: ascii if printable, else base64."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if all(byte in _PRINTABLE for byte in raw):
        return {"value": raw.decode("ascii"), "encoding": "ascii", "truncated": truncated}
    return {"value": base64.b64encode(raw).decode("ascii"), "encoding": "base64", "truncated": truncated}


class FakeSandboxes:
    """In-memory Sandboxes API behind ``httpx.MockTransport``."""

    def __init__(self, base_url: str = DEFAULT_SANDBOXES_BASE_URL) -> None:
        self.base_path = httpx.URL(base_url.rstrip("/") + "/v1/").path
        self.files: dict[str, bytes] = {}  # file uuid -> content
        self._by_sha: dict[str, str] = {}
        self.images: dict[str, dict[str, bytes]] = {}  # image uuid -> {path: content}
        self.tags: dict[str, str] = {}  # tag -> image uuid
        self.operations: dict[str, _Op] = {}
        self.requests: list[httpx.Request] = []
        self.spawned: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.permissions = {"spawn": True, "spawn_disposable": True, "list": True, "cancel": True}
        self.limits = {"instance_max_timeout": 3600, "instance_max_concurrency": 50}
        self._runs: deque[FakeRun] = deque()
        self._ids = itertools.count(1)

    # -- scripting ---------------------------------------------------------------

    def add_run(self, **kwargs: Any) -> FakeSandboxes:
        """Queue the outcome of the next spawned operation (kwargs: ``FakeRun`` fields)."""
        self._runs.append(FakeRun(**kwargs))
        return self

    def add_image(self, tag: str | None = None, files: dict[str, bytes] | None = None) -> str:
        image = self._new_id()
        self.images[image] = dict(files or {})
        if tag:
            self.tags[tag] = image
        return image

    @property
    def pending_runs(self) -> int:
        return len(self._runs)

    def uploaded(self, path: str, spawn_index: int = -1) -> bytes:
        """Content mounted at guest ``path`` in the given spawn request."""
        mount = self.spawned[spawn_index]["files"][path]
        return self.files[mount["uuid"]]

    # -- wiring ------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, **kwargs: Any) -> SandboxesClient:
        """A ``SandboxesClient`` wired to this fake, with no-op sleeps."""
        kwargs.setdefault("settings", Settings(nebius_api_key="test-key", nebius_project_id="test-project"))
        kwargs.setdefault("sleep", lambda _: None)
        return SandboxesClient(transport=self.transport(), **kwargs)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not request.headers.get("Authorization", "").startswith("Bearer ") or not request.headers.get(
            "Project"
        ):
            return _error(401, "missing credentials or Project header")
        path = request.url.path.removeprefix(self.base_path)
        parts = path.strip("/").split("/")
        method = request.method
        if method == "GET" and path == "whoami":
            return httpx.Response(
                200,
                json={
                    "token_uuid": "tok-1",
                    "token_expiration": None,
                    "permissions": self.permissions,
                    "limits": self.limits,
                    "operations_stat": {},
                },
            )
        if method == "GET" and path == "images":
            return self._list_images(request)
        if parts[0] == "files":
            return self._files(request, parts)
        if method == "POST" and path == "instances":
            return self._spawn(request)
        if parts[0] == "operations" and len(parts) == 2:
            return self._operation(method, parts[1])
        if parts[0] == "inspect" and len(parts) == 3 and parts[2] == "download":
            return self._download(request, parts[1])
        return _error(404, f"fake: no route for {method} {path}")

    # -- routes ------------------------------------------------------------------

    def _list_images(self, request: httpx.Request) -> httpx.Response:
        prefix = request.url.params.get("tag", "")
        images = [
            {"uuid": image, "tag": tag, "created_at": "2026-10-25T00:00:00+00:00", "operation_uuid": None}
            for tag, image in self.tags.items()
            if tag.startswith(prefix)
        ]
        return httpx.Response(200, json={"images": images})

    def _files(self, request: httpx.Request, parts: list[str]) -> httpx.Response:
        if request.method == "POST" and len(parts) == 1:
            content = request.content
            digest = hashlib.sha256(content).hexdigest()
            file_id = self._by_sha.get(digest) or self._new_id()
            self._by_sha[digest] = file_id
            self.files[file_id] = content
            return httpx.Response(201, json={"uuid": file_id, "sha256": digest, "size": len(content)})
        if request.method in ("GET", "HEAD") and len(parts) == 2:
            file_id = self._by_sha.get(parts[1])
            if file_id is None:
                return _error(404, "file not found")
            return httpx.Response(
                200,
                json={
                    "uuid": file_id,
                    "sha256": parts[1],
                    "size": len(self.files[file_id]),
                    "created_at": "2026-10-25T00:00:00Z",
                    "updated_at": "2026-10-25T00:00:00Z",
                },
            )
        return _error(404, "fake: unsupported files route")

    def _spawn(self, request: httpx.Request) -> httpx.Response:
        spec = json.loads(request.content)
        self.spawned.append(spec)
        if not self._runs:
            raise AssertionError(f"FakeSandboxes: unexpected spawn #{len(self.spawned)}")
        missing = [path for path, mount in spec.get("files", {}).items() if mount["uuid"] not in self.files]
        if missing:
            return _error(400, f"unknown file uuids for {missing}")
        image = spec["image"]
        if image.startswith("tag:"):
            image = self.tags.get(image.removeprefix("tag:"), image)
        op = _Op(id=self._new_id(), spec=spec, run=self._runs.popleft(), image_uuid=image)
        self.operations[op.id] = op
        return httpx.Response(
            201,
            json={**spec, "uuid": op.id, "image": image, "result": None},
            headers={"Location": f"/v1/operations/{op.id}"},
        )

    def _operation(self, method: str, op_id: str) -> httpx.Response:
        op = self.operations.get(op_id)
        if op is None:
            return _error(404, "operation not found")
        if method == "DELETE":
            if op.status in ("SUCCESS", "FAILED", "CANCELLED"):
                return _error(409, "operation already finished")
            op.status = "CANCELLED"
            self.cancelled.append(op_id)
            return httpx.Response(202)
        if op.status not in ("SUCCESS", "FAILED", "CANCELLED"):
            op.polls += 1
            if op.polls > op.run.polls_before_done:
                self._finish(op)
            else:
                op.status = "EXECUTING"
        return httpx.Response(200, json=self._op_json(op))

    def _finish(self, op: _Op) -> None:
        run = op.run
        op.status = run.status
        if run.status != "SUCCESS" or op.spec.get("disposable", False):
            return
        image_files = {path: self.files[mount["uuid"]] for path, mount in op.spec.get("files", {}).items()}
        if run.output is not None:
            image_files[_OUTPUT_PATH] = _as_bytes(run.output)
        op.result_image = self._new_id()
        self.images[op.result_image] = image_files

    def _op_json(self, op: _Op) -> dict[str, Any]:
        run = op.run
        result = None
        if op.status in ("SUCCESS", "FAILED") and run.status == op.status:
            stdout = run.stdout
            if run.output is not None and _STDOUT_MARKER in op.spec.get("command", ""):
                encoded = base64.b64encode(_as_bytes(run.output)).decode()
                stdout += f"\n{_STDOUT_MARKER}{encoded}{_STDOUT_END}\n"
            result = {
                "state": {
                    "exit_code": run.exit_code,
                    "signal": run.signal,
                    "timed_out": run.timed_out,
                    "pid": 7,
                },
                "stdout": stream_repr(stdout, run.stdout_truncated),
                "stderr": stream_repr(run.stderr),
                "resources": {"elapsed_time": run.duration},
            }
        terminal = op.status in ("SUCCESS", "FAILED", "CANCELLED")
        return {
            "uuid": op.id,
            "kind": "instance",
            "status": op.status,
            "error": run.error if op.status != "SUCCESS" and terminal else None,
            "created_at": "2026-10-25T00:00:00+00:00",
            "duration": run.duration if terminal else None,
            "image_uuid": op.image_uuid,
            "result_image_uuid": op.result_image,
            "metadata": {**op.spec, "image": op.image_uuid, "result": result},
        }

    def _download(self, request: httpx.Request, image: str) -> httpx.Response:
        files = self.images.get(image)
        path = request.url.params.get("path", "")
        if files is None or path not in files:
            return _error(404, "file not found in image")
        return httpx.Response(200, content=files[path], headers={"Content-Type": "application/octet-stream"})

    def _new_id(self) -> str:
        return str(uuid.UUID(int=next(self._ids)))


def _as_bytes(value: dict[str, Any] | str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value).encode("utf-8")


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": message, "status": status})
