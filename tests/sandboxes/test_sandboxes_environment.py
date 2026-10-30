from __future__ import annotations

import json

import httpx
import pytest

from variator.sandboxes import (
    BASE_PYTHON_OCI,
    NUMPY_IMAGE_TAG,
    Operation,
    SandboxError,
    ensure_python_image,
)

IMPORT_OP = "00000000-0000-0000-0000-00000000000a"
INSTALL_OP = "00000000-0000-0000-0000-00000000000b"
BASE_IMAGE = "00000000-0000-0000-0000-0000000000b1"
NUMPY_IMAGE = "00000000-0000-0000-0000-0000000000b2"


def operation(op_id: str, status: str = "SUCCESS", **extra) -> httpx.Response:
    return httpx.Response(200, json={"uuid": op_id, "status": status, **extra})


def install_result(exit_code: int, stderr: str = "") -> dict:
    return {
        "result": {
            "state": {"exit_code": exit_code, "signal": -1, "timed_out": False},
            "stdout": {"value": "", "encoding": "ascii"},
            "stderr": {"value": stderr, "encoding": "ascii"},
        }
    }


def test_existing_tag_is_reused(api, client):
    api.get("/images").mock(
        return_value=httpx.Response(200, json={"images": [{"uuid": NUMPY_IMAGE, "tag": NUMPY_IMAGE_TAG}]})
    )
    imp = api.post("/images/import").mock(return_value=httpx.Response(500))
    assert ensure_python_image(client) == NUMPY_IMAGE
    assert not imp.called


def test_builds_imports_installs_and_tags(api, client):
    api.get("/images").mock(return_value=httpx.Response(200, json={"images": []}))
    imp = api.post("/images/import").mock(return_value=httpx.Response(201, json={"uuid": IMPORT_OP}))
    api.get(f"/operations/{IMPORT_OP}").mock(
        return_value=operation(IMPORT_OP, kind="image_import", result={"image": BASE_IMAGE, "tag": None})
    )
    spawn = api.post("/instances").mock(return_value=httpx.Response(201, json={"uuid": INSTALL_OP}))
    api.get(f"/operations/{INSTALL_OP}").mock(
        return_value=operation(INSTALL_OP, result_image_uuid=NUMPY_IMAGE, metadata=install_result(0))
    )
    tag = api.patch(f"/images/{NUMPY_IMAGE}/tag").mock(
        return_value=httpx.Response(200, json={"uuid": NUMPY_IMAGE, "tag": NUMPY_IMAGE_TAG})
    )

    assert ensure_python_image(client) == NUMPY_IMAGE
    assert json.loads(imp.calls.last.request.content)["registry"] == {"url": BASE_PYTHON_OCI}
    body = json.loads(spawn.calls.last.request.content)
    assert body["image"] == BASE_IMAGE
    assert body["networking"] == {"enabled": True}  # pip needs the network
    assert body["disposable"] is False  # the installed packages must be saved
    assert "pip install" in body["command"] and "numpy" in body["command"]
    assert json.loads(tag.calls.last.request.content) == {"tag": NUMPY_IMAGE_TAG}


def test_failed_install_raises(api, client):
    api.get("/images").mock(return_value=httpx.Response(200, json={"images": []}))
    api.post("/images/import").mock(return_value=httpx.Response(201, json={"uuid": IMPORT_OP}))
    api.get(f"/operations/{IMPORT_OP}").mock(return_value=operation(IMPORT_OP, result={"image": BASE_IMAGE}))
    api.post("/instances").mock(return_value=httpx.Response(201, json={"uuid": INSTALL_OP}))
    api.get(f"/operations/{INSTALL_OP}").mock(
        return_value=operation(INSTALL_OP, metadata=install_result(1, "No matching distribution"))
    )
    with pytest.raises(SandboxError, match="No matching distribution"):
        ensure_python_image(client)


def test_failed_import_raises(api, client):
    api.get("/images").mock(return_value=httpx.Response(200, json={"images": []}))
    api.post("/images/import").mock(return_value=httpx.Response(201, json={"uuid": IMPORT_OP}))
    api.get(f"/operations/{IMPORT_OP}").mock(
        return_value=operation(IMPORT_OP, status="FAILED", error="manifest unknown")
    )
    with pytest.raises(SandboxError, match="manifest unknown"):
        ensure_python_image(client)


def test_status_is_case_insensitive():
    assert Operation.model_validate({"uuid": "x", "status": "success"}).terminal
