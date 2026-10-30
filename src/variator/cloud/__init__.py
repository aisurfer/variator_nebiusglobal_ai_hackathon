"""Nebius AI Cloud Serverless: run deep simulations as jobs, host the app as an endpoint.

Main entry points:

* ``ServerlessClient``: REST client for jobs and endpoints (create/get/list/cancel/delete,
  start/stop, polling with ``wait_for_job`` / ``wait_until_running``).
* ``submit_simulation_job`` / ``wait_for_result`` / ``run_simulation_job``: a Monte Carlo
  (+ cuOpt) run on a GPU job, input and output via ``variator.cloud.job_io``.
* ``estimate_cost`` and friends: APPROXIMATE prices; AI Cloud is paid from our card.

The container side lives in ``variator.cloud.job_entry`` (``python -m variator.cloud.job_entry``).
"""

from variator.cloud.cli import cli_access_token, cli_job_logs
from variator.cloud.client import DEFAULT_API_BASE_URL, ServerlessClient
from variator.cloud.costs import (
    DEMO_UP_UNTIL,
    PRICE_TABLE,
    CostEstimate,
    JobCostEstimate,
    estimate_cost,
    estimate_endpoint_cost,
    estimate_job_cost,
    job_run_cost,
    parse_preset,
)
from variator.cloud.errors import (
    AuthenticationError,
    CliError,
    CloudError,
    ConflictError,
    InputTooLarge,
    InvalidRequestError,
    MissingCredentialsError,
    NebiusApiError,
    NotEnoughResources,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceeded,
    RateLimitedError,
    ServiceUnavailableError,
    WaitTimeout,
    WorkloadFailed,
)
from variator.cloud.job_io import ExitCode, JobInput, JobMarker, JobOutput, parse_marker
from variator.cloud.jobs import (
    InputTransport,
    JobPlan,
    JobResultMissing,
    SubmittedJob,
    fetch_job_result,
    plan_simulation_job,
    run_simulation_job,
    submit_simulation_job,
    wait_for_result,
)
from variator.cloud.resources import EndpointInfo, JobInfo, Operation, StateDetails
from variator.cloud.specs import (
    EndpointSpec,
    EndpointState,
    InjectedFile,
    JobSpec,
    JobState,
    RegistryCredentials,
    SecretRef,
    VolumeMount,
)
from variator.cloud.storage import LocalDirStore, ResultStore

__all__ = [
    "DEFAULT_API_BASE_URL",
    "DEMO_UP_UNTIL",
    "PRICE_TABLE",
    "AuthenticationError",
    "CliError",
    "CloudError",
    "ConflictError",
    "CostEstimate",
    "EndpointInfo",
    "EndpointSpec",
    "EndpointState",
    "ExitCode",
    "InjectedFile",
    "InputTooLarge",
    "InputTransport",
    "InvalidRequestError",
    "JobCostEstimate",
    "JobInfo",
    "JobInput",
    "JobMarker",
    "JobOutput",
    "JobPlan",
    "JobResultMissing",
    "JobSpec",
    "JobState",
    "LocalDirStore",
    "MissingCredentialsError",
    "NebiusApiError",
    "NotEnoughResources",
    "NotFoundError",
    "Operation",
    "PermissionDeniedError",
    "QuotaExceeded",
    "RateLimitedError",
    "RegistryCredentials",
    "ResultStore",
    "SecretRef",
    "ServerlessClient",
    "ServiceUnavailableError",
    "StateDetails",
    "SubmittedJob",
    "VolumeMount",
    "WaitTimeout",
    "WorkloadFailed",
    "cli_access_token",
    "cli_job_logs",
    "estimate_cost",
    "estimate_endpoint_cost",
    "estimate_job_cost",
    "fetch_job_result",
    "job_run_cost",
    "parse_marker",
    "parse_preset",
    "plan_simulation_job",
    "run_simulation_job",
    "submit_simulation_job",
    "wait_for_result",
]
