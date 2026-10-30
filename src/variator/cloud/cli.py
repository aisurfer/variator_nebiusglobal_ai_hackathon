"""Thin wrappers over the ``nebius`` CLI for what the REST API does not cover.

* Access tokens: ``nebius iam get-access-token`` (valid 12 h,
  https://docs.nebius.com/rest-api/authentication.md).
* Job logs: "Checking job logs is available only in the web console and CLI"
  (https://docs.nebius.com/serverless/jobs/manage.md#how-to-check-job-logs).

Exit codes follow https://docs.nebius.com/cli/exit-codes.md.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from variator.cloud.errors import AuthenticationError, CliError, NotFoundError, QuotaExceeded

INSTALL_URL = "https://docs.nebius.com/cli/install.md"

Runner = Callable[..., subprocess.CompletedProcess[str]]

_AUTH_CODES = {7}
_NOT_FOUND_CODES = {13, 53}
_QUOTA_CODES = {16, 24, 56, 64}  # RESOURCE_EXHAUSTED / QuotaFailure, target or extra call


def run_cli(args: Sequence[str], *, runner: Runner | None = None, timeout_s: float = 120.0) -> str:
    """Run ``nebius <args>`` and return stdout; map failures to ``variator.cloud`` errors."""
    run = runner or subprocess.run
    command = ["nebius", *args]
    try:
        proc: Any = run(
            command, capture_output=True, text=True, timeout=timeout_s, check=False, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError as exc:
        raise CliError(f"the nebius CLI is not installed or not on PATH ({INSTALL_URL})") from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError(f"`{' '.join(command)}` timed out after {timeout_s:.0f}s") from exc
    if proc.returncode == 0:
        return proc.stdout
    stderr = (proc.stderr or "").strip()
    message = f"`{' '.join(command)}` exited with {proc.returncode}: {stderr[-500:]}"
    if proc.returncode in _AUTH_CODES:
        raise AuthenticationError(message)
    if proc.returncode in _NOT_FOUND_CODES:
        raise NotFoundError(message)
    if proc.returncode in _QUOTA_CODES:
        raise QuotaExceeded(message)
    raise CliError(message, exit_code=proc.returncode, stderr=stderr)


def cli_access_token(*, runner: Runner | None = None) -> str:
    """A fresh IAM access token from the CLI profile; usable as ``token_provider``."""
    token = run_cli(["iam", "get-access-token"], runner=runner, timeout_s=60).strip()
    if not token:
        raise CliError("`nebius iam get-access-token` printed nothing")
    return token.splitlines()[-1].strip()


def cli_job_logs(job_id: str, *, tail: int | None = None, runner: Runner | None = None) -> str:
    """The job's container logs (stdout and stderr) as text."""
    args = ["ai", "job", "logs", job_id]
    if tail:
        args += ["--tail", str(tail)]
    return run_cli(args, runner=runner)
