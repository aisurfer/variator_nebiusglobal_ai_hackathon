"""What to run on Nebius Serverless AI: job/endpoint specs and lifecycle states.

Field names are ours (snake_case, friendly units). ``to_request()`` renders the REST body
of ``POST /ai/v1/jobs`` / ``POST /ai/v1/endpoints`` as documented in
https://docs.nebius.com/serverless/jobs/manage.md, https://docs.nebius.com/serverless/endpoints/manage.md
and the OpenAPI schemas ``nebius.ai.v1.JobSpec`` / ``EndpointSpec`` at https://api.nebius.cloud/openapi.json.
``to_cli_args()`` renders the equivalent ``nebius ai job|endpoint create`` command.
"""

from __future__ import annotations

import base64
import re
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

GIB = 1024**3
MAX_INJECTED_FILE_BYTES = 64 * 1024  # documented limit per injected file
MIN_JOB_TIMEOUT_S = 3600  # documented: minimum 1h
MAX_JOB_TIMEOUT_S = 168 * 3600  # documented: maximum 168h
MIN_GPU_DISK_GIB = 50  # documented minimum for GPU workloads
MIN_CPU_DISK_GIB = 20  # documented minimum for CPU-only workloads
DEFAULT_GPU_DISK_GIB = 100  # CUDA + CuPy + cuOpt image is ~10 GiB; the API default (250 GiB) costs more
DEFAULT_CPU_DISK_GIB = 30

# ASSUMPTION: resource names follow the usual DNS-label rules (lowercase, digits, hyphens,
# <= 63 chars). The docs only show examples like "my-job"
# (https://docs.nebius.com/serverless/quickstart/jobs.md).
NAME_RE = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class JobState(StrEnum):
    """Job ``status.state`` values (https://docs.nebius.com/serverless/lifecycle.md)."""

    STATE_UNSPECIFIED = "STATE_UNSPECIFIED"
    PROVISIONING = "PROVISIONING"
    STARTING = "STARTING"
    IMAGE_PULLING = "IMAGE_PULLING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    DELETING = "DELETING"
    FAILED = "FAILED"  # the workload failed: exit code, bad entrypoint, timeout
    ERROR = "ERROR"  # Serverless AI could not run it: capacity, quota, internal error

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_JOB_STATES


TERMINAL_JOB_STATES = frozenset(
    {JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED, JobState.ERROR}
)


class EndpointState(StrEnum):
    """Endpoint ``status.state`` values (https://docs.nebius.com/serverless/lifecycle.md)."""

    STATE_UNSPECIFIED = "STATE_UNSPECIFIED"
    PROVISIONING = "PROVISIONING"
    STARTING = "STARTING"
    IMAGE_PULLING = "IMAGE_PULLING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    DELETING = "DELETING"
    ERROR = "ERROR"  # endpoints have no FAILED: ERROR covers workload problems too


DiskType = Literal["NETWORK_SSD", "NETWORK_HDD", "NETWORK_SSD_NON_REPLICATED", "NETWORK_SSD_IO_M3"]


class SecretRef(BaseModel):
    """A SecretStash (formerly MysteryBox) secret, optionally pinned to a version.

    The CLI accepts selectors like ``mbsec-e00...@mbsecver-e00...`` or a secret name. The REST
    API takes IDs only (``mysteryboxSecret.secretId`` / ``versionId``), so pass IDs here.
    """

    model_config = ConfigDict(frozen=True)

    secret_id: str = Field(min_length=1)
    # ASSUMPTION: omitting versionId selects the secret's primary version, as with the CLI
    # selector without "@version" (https://docs.nebius.com/serverless/jobs/manage.md).
    version_id: str | None = None

    @classmethod
    def parse(cls, selector: str | SecretRef) -> SecretRef:
        """Parse ``"<secret_id>[@<version_id>]"``."""
        if isinstance(selector, SecretRef):
            return selector
        secret_id, _, version_id = selector.strip().partition("@")
        return cls(secret_id=secret_id, version_id=version_id or None)

    @property
    def selector(self) -> str:
        return f"{self.secret_id}@{self.version_id}" if self.version_id else self.secret_id

    def to_api(self) -> dict[str, str]:
        body = {"secretId": self.secret_id}
        if self.version_id:
            body["versionId"] = self.version_id
        return body


class VolumeMount(BaseModel):
    """An Object Storage bucket or shared filesystem mounted into the container."""

    source: str = Field(min_length=1, description="Bucket or filesystem ID (or name)")
    container_path: str
    mode: Literal["rw", "ro"] = "rw"
    source_path: str | None = Field(default=None, description="Sub-path inside the volume")

    @field_validator("container_path")
    @classmethod
    def _absolute(cls, v: str) -> str:
        return _check_container_path(v)

    def to_api(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "source": self.source,
            "containerPath": self.container_path,
            "mode": "READ_WRITE" if self.mode == "rw" else "READ_ONLY",
        }
        if self.source_path:
            body["sourcePath"] = self.source_path
        return body

    def to_cli(self) -> str:
        return f"{self.source}:{self.container_path}:{self.mode}"


class RegistryCredentials(BaseModel):
    """Credentials for a private registry. Not needed for Nebius Container Registry in the
    same project or for public images."""

    username: str | None = None
    password: SecretStr | None = None
    secret_version_id: str | None = Field(
        default=None,
        description="SecretStash version with REGISTRY_USERNAME and REGISTRY_PASSWORD keys",
    )

    @model_validator(mode="after")
    def _one_way(self) -> RegistryCredentials:
        inline = self.username is not None or self.password is not None
        if inline and self.secret_version_id:
            raise ValueError("use either username/password or secret_version_id, not both")
        if inline and not (self.username and self.password):
            raise ValueError("registry username and password must be set together")
        if not inline and not self.secret_version_id:
            raise ValueError("registry credentials need username/password or secret_version_id")
        return self

    def to_api(self) -> dict[str, str]:
        if self.secret_version_id:
            return {"mysteryboxSecretVersion": self.secret_version_id}
        assert self.username and self.password
        return {"username": self.username, "password": self.password.get_secret_value()}


class InjectedFile(BaseModel):
    """A small read-only file written into the container before start (<= 64 KiB)."""

    container_path: str
    content: bytes = Field(min_length=1, max_length=MAX_INJECTED_FILE_BYTES)

    @field_validator("container_path")
    @classmethod
    def _absolute(cls, v: str) -> str:
        return _check_container_path(v)

    def to_api(self) -> dict[str, str]:
        return {
            "containerPath": self.container_path,
            "content": base64.b64encode(self.content).decode("ascii"),
        }


class WorkloadSpec(BaseModel):
    """Settings shared by jobs and endpoints."""

    model_config = ConfigDict(extra="forbid")

    name: str
    image: str = Field(min_length=1, description="registry/path:tag or registry/path@digest")
    container_command: str | None = Field(default=None, description="Overrides the image ENTRYPOINT")
    args: str | None = Field(default=None, description="Arguments passed to the entrypoint")
    env: dict[str, str] = Field(default_factory=dict)
    env_secrets: dict[str, SecretRef] = Field(
        default_factory=dict,
        description="Env var -> SecretStash secret; the payload key should match the variable name",
    )
    working_dir: str | None = None
    platform: str = "cpu-d3"
    preset: str = "2vcpu-8gb"
    subnet_id: str | None = Field(default=None, description="None: project default subnet")
    disk_size_gib: int | None = Field(default=None, ge=1, description="None: 100 GiB GPU / 30 GiB CPU")
    disk_type: DiskType = "NETWORK_SSD"
    shm_size_bytes: int | None = Field(default=None, ge=1)
    volumes: list[VolumeMount] = Field(default_factory=list)
    injected_files: list[InjectedFile] = Field(default_factory=list)
    preemptible: bool = False
    spot_pricing_policy_id: str | None = Field(
        default=None, description="Max-price policy for preemptible VMs; None follows the spot price"
    )
    registry: RegistryCredentials | None = None
    ssh_authorized_keys: list[str] = Field(default_factory=list)
    public_ip: bool = Field(
        default=False,
        description="Public IPv4 for the VM; not needed for the managed HTTPS URL, costs IP quota",
    )
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(f"{v!r} must be lowercase letters, digits and hyphens (<= 63 chars)")
        return v

    @field_validator("env_secrets", mode="before")
    @classmethod
    def _parse_selectors(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {key: SecretRef.parse(sel) if isinstance(sel, str) else sel for key, sel in v.items()}
        return v

    @field_validator("working_dir")
    @classmethod
    def _working_dir(cls, v: str | None) -> str | None:
        return _check_container_path(v) if v is not None else None

    @model_validator(mode="after")
    def _check(self) -> WorkloadSpec:
        for key in [*self.env, *self.env_secrets]:
            if not ENV_NAME_RE.match(key):
                raise ValueError(f"invalid environment variable name {key!r}")
        both = set(self.env) & set(self.env_secrets)
        if both:
            raise ValueError(f"variables set in both env and env_secrets: {sorted(both)}")
        minimum = MIN_GPU_DISK_GIB if self.is_gpu else MIN_CPU_DISK_GIB
        if self.disk_size_gib is not None and self.disk_size_gib < minimum:
            raise ValueError(f"disk_size_gib must be >= {minimum} on {self.platform}")
        if self.preemptible and not self.is_gpu:
            raise ValueError("only GPU platforms offer preemptible VMs")
        if self.spot_pricing_policy_id and not self.preemptible:
            raise ValueError("spot_pricing_policy_id requires preemptible=True")
        paths = [f.container_path for f in self.injected_files] + [v.container_path for v in self.volumes]
        if len(paths) != len(set(paths)):
            raise ValueError("container paths of volumes and injected files must be unique")
        return self

    @property
    def is_gpu(self) -> bool:
        return self.platform.startswith("gpu-")

    @property
    def effective_disk_gib(self) -> int:
        if self.disk_size_gib is not None:
            return self.disk_size_gib
        return DEFAULT_GPU_DISK_GIB if self.is_gpu else DEFAULT_CPU_DISK_GIB

    def with_defaults(self, *, subnet_id: str | None) -> Self:
        """Copy with a default subnet filled in when the spec has none."""
        if self.subnet_id or not subnet_id:
            return self
        return self.model_copy(update={"subnet_id": subnet_id})

    def to_api(self) -> dict[str, Any]:
        """The ``spec`` object of the create request (camelCase, int64 as strings)."""
        spec: dict[str, Any] = {
            "image": self.image,
            "platform": self.platform,
            "preset": self.preset,
            "disk": {"type": self.disk_type, "sizeBytes": str(self.effective_disk_gib * GIB)},
        }
        if self.container_command:
            spec["containerCommand"] = self.container_command
        if self.args:
            spec["args"] = self.args
        env = [{"name": k, "value": v} for k, v in self.env.items()]
        env += [{"name": k, "mysteryboxSecret": ref.to_api()} for k, ref in self.env_secrets.items()]
        if env:
            spec["environmentVariables"] = env
        if self.working_dir:
            spec["workingDir"] = self.working_dir
        if self.subnet_id:
            spec["subnetId"] = self.subnet_id
        if self.shm_size_bytes:
            spec["shmSizeBytes"] = str(self.shm_size_bytes)
        if self.volumes:
            spec["volumes"] = [v.to_api() for v in self.volumes]
        if self.injected_files:
            spec["injectedFiles"] = [f.to_api() for f in self.injected_files]
        if self.preemptible:
            spec["preemptible"] = True
            spec["pricingModel"] = (
                {"spotPricingPolicy": {"id": self.spot_pricing_policy_id}}
                if self.spot_pricing_policy_id
                else {"followsSpotPrice": {}}
            )
        if self.registry:
            spec["registryCredentials"] = self.registry.to_api()
        if self.ssh_authorized_keys:
            spec["sshAuthorizedKeys"] = list(self.ssh_authorized_keys)
        if self.public_ip:
            spec["publicIp"] = True
        return spec

    def to_request(self, project_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Full create-request body."""
        metadata: dict[str, Any] = {"parentId": project_id, "name": self.name}
        if self.labels:
            metadata["labels"] = dict(self.labels)
        body: dict[str, Any] = {"metadata": metadata, "spec": self.to_api()}
        if dry_run:
            body["dryRun"] = True
        return body

    def _cli_common(self) -> list[str]:
        args = ["--name", self.name, "--image", self.image]
        if self.container_command:
            args += ["--container-command", self.container_command]
        if self.args:
            args += ["--args", self.args]
        # Documented form: one flag, "key=value" pairs separated by commas.
        if self.env:
            args += ["--env", ",".join(f"{k}={v}" for k, v in self.env.items())]
        if self.env_secrets:
            args += ["--env-secret", ",".join(f"{k}={ref.selector}" for k, ref in self.env_secrets.items())]
        if self.working_dir:
            args += ["--working-dir", self.working_dir]
        args += ["--platform", self.platform, "--preset", self.preset]
        args += ["--disk-size", f"{self.effective_disk_gib}Gi"]
        if self.subnet_id:
            args += ["--subnet-id", self.subnet_id]
        args += [x for v in self.volumes for x in ("--volume", v.to_cli())]
        if self.registry and self.registry.secret_version_id:
            args += ["--registry-secret", self.registry.secret_version_id]
        elif self.registry:
            args += ["--registry-username", self.registry.username or "", "--registry-password", "<password>"]
        args += [x for key in self.ssh_authorized_keys for x in ("--ssh-key", key)]
        if self.preemptible:
            args.append("--preemptible")
            if self.spot_pricing_policy_id:
                args += ["--spot-pricing-policy-id", self.spot_pricing_policy_id]
            else:
                args.append("--follows-spot-price")
        return args


class JobSpec(WorkloadSpec):
    """A run-to-completion container (no public URL, cannot be stopped, per-second billing)."""

    platform: str = "gpu-l40s-a"
    preset: str = "1gpu-8vcpu-32gb"
    timeout_s: int = Field(default=MIN_JOB_TIMEOUT_S, ge=MIN_JOB_TIMEOUT_S, le=MAX_JOB_TIMEOUT_S)
    restart_on_failure: bool = Field(
        default=False, description="Restart the container on non-zero exit (restartAttempts=-1)"
    )

    def to_api(self) -> dict[str, Any]:
        spec = super().to_api()
        spec["timeout"] = f"{self.timeout_s}s"
        if self.restart_on_failure:
            spec["restartAttempts"] = "-1"
        return spec

    def to_cli_args(self) -> list[str]:
        """``nebius ai job create ...`` equivalent (injected files are not rendered)."""
        args = ["nebius", "ai", "job", "create", *self._cli_common(), "--timeout", f"{self.timeout_s}s"]
        if self.restart_on_failure:
            args += ["--restart-policy", "on-failure"]
        return args


class EndpointSpec(WorkloadSpec):
    """A long-lived container behind a managed HTTPS URL; can be stopped and started."""

    container_ports: list[int] = Field(default_factory=lambda: [8000], min_length=1)
    auth_token: SecretStr | None = Field(
        default=None, description="Clients must send 'Authorization: Bearer <token>'"
    )
    auth_token_secret: SecretRef | None = Field(
        default=None, description="SecretStash secret with an AUTH_TOKEN payload key"
    )

    @field_validator("auth_token_secret", mode="before")
    @classmethod
    def _parse_token_secret(cls, v: Any) -> Any:
        return SecretRef.parse(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check_endpoint(self) -> EndpointSpec:
        if any(not 1 <= port <= 65535 for port in self.container_ports):
            raise ValueError("container ports must be within 1..65535")
        if len(set(self.container_ports)) != len(self.container_ports):
            raise ValueError("container ports must be unique")
        if self.auth_token and self.auth_token_secret:
            raise ValueError("auth_token and auth_token_secret are mutually exclusive")
        if self.auth_enabled and len(self.container_ports) != 1:
            raise ValueError("token auth needs exactly one HTTP port (API constraint)")
        return self

    @property
    def auth_enabled(self) -> bool:
        return self.auth_token is not None or self.auth_token_secret is not None

    def to_api(self) -> dict[str, Any]:
        spec = super().to_api()
        spec["ports"] = [{"containerPort": port, "protocol": "HTTP"} for port in self.container_ports]
        if self.auth_token:
            spec["authToken"] = self.auth_token.get_secret_value()
        elif self.auth_token_secret:
            spec["authTokenMysteryboxSecret"] = self.auth_token_secret.to_api()
        return spec

    def to_cli_args(self) -> list[str]:
        """``nebius ai endpoint create ...`` equivalent (the token value is masked)."""
        args = ["nebius", "ai", "endpoint", "create", *self._cli_common()]
        args += [x for port in self.container_ports for x in ("--container-port", str(port))]
        if self.auth_token:
            args += ["--auth", "token", "--token", "<token>"]
        elif self.auth_token_secret:
            args += ["--auth", "token", "--token-secret", self.auth_token_secret.selector]
        if self.public_ip:
            args.append("--public")
        return args


def _check_container_path(v: str) -> str:
    parts = v.split("/")
    if not v.startswith("/") or v == "/" or v.endswith("/") or any(p in ("", ".", "..") for p in parts[1:]):
        raise ValueError(f"{v!r} must be a clean absolute path")
    return v
