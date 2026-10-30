"""Runtime settings read from environment variables.

Every key is optional at import time: modules raise ``MissingCredentialsError``
only when they are about to make a real network call. Tests inject fake
transports and never need keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_TF_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
DEFAULT_SANDBOXES_BASE_URL = "https://api.tokenfactory.nebius.com/sandboxes"
DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com"


class MissingCredentialsError(RuntimeError):
    """Raised when a real API call is attempted without the required key."""


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    nebius_api_key: str | None = None
    tf_base_url: str = DEFAULT_TF_BASE_URL
    nebius_project_id: str | None = None
    sandboxes_base_url: str = DEFAULT_SANDBOXES_BASE_URL

    nebius_iam_token: str | None = None
    nebius_cloud_project_id: str | None = None
    nebius_subnet_id: str | None = None

    tavily_api_key: str | None = None
    tavily_base_url: str = DEFAULT_TAVILY_BASE_URL

    # model key ("lightning", "super", ...) -> model id override
    model_overrides: dict[str, str] = field(default_factory=dict)

    request_timeout_s: float = 120.0
    max_retries: int = 4
    # "json_schema" or "json_object"; the client also falls back automatically
    structured_mode: str = "json_schema"

    @classmethod
    def from_env(cls) -> Settings:
        overrides = {
            key.removeprefix("VARIATOR_MODEL_").lower(): value
            for key, value in os.environ.items()
            if key.startswith("VARIATOR_MODEL_") and value.strip()
        }
        return cls(
            nebius_api_key=_env("NEBIUS_API_KEY"),
            tf_base_url=_env("NEBIUS_TF_BASE_URL") or DEFAULT_TF_BASE_URL,
            nebius_project_id=_env("NEBIUS_PROJECT_ID"),
            sandboxes_base_url=_env("NEBIUS_SANDBOXES_BASE_URL") or DEFAULT_SANDBOXES_BASE_URL,
            nebius_iam_token=_env("NEBIUS_IAM_TOKEN"),
            nebius_cloud_project_id=_env("NEBIUS_CLOUD_PROJECT_ID"),
            nebius_subnet_id=_env("NEBIUS_SUBNET_ID"),
            tavily_api_key=_env("TAVILY_API_KEY"),
            tavily_base_url=_env("TAVILY_BASE_URL") or DEFAULT_TAVILY_BASE_URL,
            model_overrides=overrides,
            request_timeout_s=float(_env("VARIATOR_TIMEOUT_S") or 120.0),
            max_retries=int(_env("VARIATOR_MAX_RETRIES") or 4),
            structured_mode=_env("VARIATOR_STRUCTURED_MODE") or "json_schema",
        )

    def require(self, attr: str) -> str:
        """Return a credential or raise ``MissingCredentialsError`` naming the env var."""
        value = getattr(self, attr)
        if not value:
            raise MissingCredentialsError(f"{attr.upper()} is not set (see .env.example)")
        return value
