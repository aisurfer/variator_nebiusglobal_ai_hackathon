"""Fixtures for offline Data Designer runs: no real network, no writes to the home dir."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx

from variator.config import DEFAULT_TF_BASE_URL

TF_CHAT_URL = DEFAULT_TF_BASE_URL.rstrip("/") + "/chat/completions"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any real TCP connection; respx intercepts httpx before sockets are touched."""
    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError(f"unexpected network connection to {address!r} in an offline test")
        real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture
def dd_offline(tmp_path, monkeypatch: pytest.MonkeyPatch, no_network: None) -> None:
    """Keep Data Designer's side effects inside ``tmp_path`` and its tokenizer offline.

    * ``DataDesigner()`` writes default provider YAMLs to ``~/.data-designer``; redirect.
    * Dataset profiling counts tokens with tiktoken, which downloads ``cl100k_base``
      over the network on first use; replace it with a whitespace count.
    """
    pytest.importorskip("data_designer")
    monkeypatch.setenv("NEMO_TELEMETRY_ENABLED", "false")
    import data_designer.config.default_model_settings as defaults
    import data_designer.engine.analysis.utils.column_statistics_calculations as stats
    import data_designer.engine.models.clients.parsing as parsing
    import data_designer.engine.models.facade as facade
    import data_designer.engine.models.telemetry as telemetry
    import data_designer.interface.data_designer as interface

    home = tmp_path / "dd-home"
    monkeypatch.setattr(defaults, "MODEL_CONFIGS_FILE_PATH", home / "model_configs.yaml")
    monkeypatch.setattr(defaults, "MODEL_PROVIDERS_FILE_PATH", home / "model_providers.yaml")
    monkeypatch.setattr(defaults, "MANAGED_ASSETS_PATH", home / "managed-assets")
    monkeypatch.setattr(interface, "MANAGED_ASSETS_PATH", home / "managed-assets")
    monkeypatch.setattr(telemetry, "TELEMETRY_ENABLED", False)
    monkeypatch.setattr(facade, "TELEMETRY_ENABLED", False)

    def count_words(text: str) -> int:
        return len(str(text).split())

    monkeypatch.setattr(stats, "count_text_tokens", count_words)
    monkeypatch.setattr(parsing, "count_text_tokens", count_words)


@dataclass
class FakeTokenFactoryChat:
    """Records chat-completion requests and answers with a canned reply."""

    reply: str = "Synthetic reply."
    requests: list[dict[str, Any]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    auth_headers: list[str | None] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.urls.append(str(request.url))
        self.auth_headers.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            },
        )

    @property
    def user_prompts(self) -> list[str]:
        """Text of the last user message of each request."""
        prompts = []
        for body in self.requests:
            content = body["messages"][-1]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content)
            prompts.append(content)
        return prompts


@pytest.fixture
def fake_tf_chat(dd_offline: None):
    """A respx-mocked Token Factory chat endpoint; any other host fails the test."""
    fake = FakeTokenFactoryChat()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.post(TF_CHAT_URL).mock(side_effect=fake)
        yield fake
