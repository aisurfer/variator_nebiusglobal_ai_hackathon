"""Prepare-once Python + NumPy image for generated simulations.

Follows the "prepare once, reuse safely" pattern from
https://docs.tokenfactory.nebius.com/sandboxes/guides/prepare-and-reuse-environments.md:
import a Python OCI image, install NumPy with networking on, save the result
image, tag it, and run every untrusted program from that image with networking off.
"""

from __future__ import annotations

from collections.abc import Sequence

from variator.sandboxes.client import SandboxesClient
from variator.sandboxes.errors import SandboxError
from variator.sandboxes.models import Networking, OperationStatus, SpawnRequest

# ASSUMPTION: Docker Hub images can be imported by URL in this form, as in the SDK docs
# (https://docs.tokenfactory.nebius.com/sandboxes/sdk/python_sdk/images.md).
BASE_PYTHON_OCI = "docker://docker.io/library/python:3.12-slim"
NUMPY_IMAGE_TAG = "variator-numpy:py3.12"
# Image used by run_python/generate_and_run unless the caller or VARIATOR_SANDBOX_IMAGE says otherwise.
DEFAULT_IMAGE = f"tag:{NUMPY_IMAGE_TAG}"


def ensure_python_image(
    sandbox: SandboxesClient,
    *,
    tag: str = NUMPY_IMAGE_TAG,
    base: str = BASE_PYTHON_OCI,
    packages: Sequence[str] = ("numpy",),
    timeout_s: int = 600,
) -> str:
    """Return the UUID of the tagged image, building it on first use.

    Idempotent: an existing image with exactly this tag is reused. Building
    needs network access inside the sandbox (pip) and the ``spawn``,
    ``import`` and ``set_image_tag`` permissions.
    """
    existing = sandbox.find_image(tag)
    if existing is not None:
        return existing.uuid

    import_op = sandbox.wait(sandbox.import_image(base, timeout_s=timeout_s), timeout_s=timeout_s + 120)
    base_uuid = import_op.produced_image
    if import_op.status != OperationStatus.SUCCESS or not base_uuid:
        raise SandboxError(
            f"importing {base} ended {import_op.status}: {import_op.error}", operation_id=import_op.uuid
        )

    install = sandbox.execute(
        SpawnRequest(
            image=base_uuid,
            command="pip install --no-cache-dir --disable-pip-version-check " + " ".join(packages),
            shell=True,
            disposable=False,
            networking=Networking(enabled=True),
            timeout=timeout_s,
        )
    )
    result = install.instance_result
    exit_code = result.state.exit_code if result and result.state else None
    if install.status != OperationStatus.SUCCESS or exit_code != 0 or not install.result_image_uuid:
        stderr = result.stderr.text()[-1000:] if result and result.stderr else ""
        raise SandboxError(
            f"installing {list(packages)} failed ({install.status}, exit {exit_code}): {stderr}",
            operation_id=install.uuid,
        )
    sandbox.tag_image(install.result_image_uuid, tag)
    return install.result_image_uuid
