"""Nebius Token Factory Sandboxes (Beta): run LLM-generated code in isolated VMs.

* ``SandboxesClient`` - REST client (operations, files, images, SSE events).
* ``run_python`` - run one program with inputs and collect ``output.json``.
* ``generate_and_run`` - Nemotron writes a simulation, the sandbox runs it, failures go back for a fix.
* ``ensure_python_image`` - build/reuse the tagged Python + NumPy image once.
"""

from variator.sandboxes.client import IMAGE_ENV_VAR, SandboxesClient
from variator.sandboxes.codegen import (
    CodegenAttempt,
    CodegenResult,
    OptionOutcome,
    OutputValidationError,
    SimulationOutput,
    build_task_prompt,
    extract_code,
    generate_and_run,
    validate_output,
)
from variator.sandboxes.environment import (
    BASE_PYTHON_OCI,
    DEFAULT_IMAGE,
    NUMPY_IMAGE_TAG,
    ensure_python_image,
)
from variator.sandboxes.errors import (
    SandboxAuthError,
    SandboxConflictError,
    SandboxError,
    SandboxHTTPError,
    SandboxLimitError,
    SandboxNotFoundError,
    SandboxOperationFailed,
    SandboxOutputTooLarge,
    SandboxPermissionError,
    SandboxRequestError,
    SandboxStreamError,
    SandboxTimeoutError,
    SandboxTransportError,
)
from variator.sandboxes.models import (
    FileMount,
    Image,
    InstanceResult,
    Networking,
    Operation,
    OperationEvent,
    OperationStatus,
    ProcessState,
    SpawnRequest,
    StreamOutput,
    UploadedFile,
    WhoAmI,
)
from variator.sandboxes.runner import SandboxRunResult, run_python
from variator.sandboxes.sse import SSEFrame, iter_sse_frames

__all__ = [
    "BASE_PYTHON_OCI",
    "DEFAULT_IMAGE",
    "IMAGE_ENV_VAR",
    "NUMPY_IMAGE_TAG",
    "CodegenAttempt",
    "CodegenResult",
    "FileMount",
    "Image",
    "InstanceResult",
    "Networking",
    "Operation",
    "OperationEvent",
    "OperationStatus",
    "OptionOutcome",
    "OutputValidationError",
    "ProcessState",
    "SSEFrame",
    "SandboxAuthError",
    "SandboxConflictError",
    "SandboxError",
    "SandboxHTTPError",
    "SandboxLimitError",
    "SandboxNotFoundError",
    "SandboxOperationFailed",
    "SandboxOutputTooLarge",
    "SandboxPermissionError",
    "SandboxRequestError",
    "SandboxRunResult",
    "SandboxStreamError",
    "SandboxTimeoutError",
    "SandboxTransportError",
    "SandboxesClient",
    "SimulationOutput",
    "SpawnRequest",
    "StreamOutput",
    "UploadedFile",
    "WhoAmI",
    "build_task_prompt",
    "ensure_python_image",
    "extract_code",
    "generate_and_run",
    "iter_sse_frames",
    "run_python",
    "validate_output",
]
