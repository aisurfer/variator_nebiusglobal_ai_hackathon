"""Offline fake of the Token Factory chat API for unit tests.

Usage::

    fake = FakeTokenFactory()
    fake.add_reply(content='{"answer": 42}')
    fake.add_reply(tool_calls=[("lookup", {"q": "rent"})])
    client = fake.client()
    ...
    assert fake.requests[0]["model"] == "nvidia/Nemotron-3_5-Lightning"

Replies are served in order; a request with no queued reply fails the test.
"""

from __future__ import annotations

import itertools
import json
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

from variator.config import Settings
from variator.llm.client import TokenFactoryClient

Responder = Callable[[dict[str, Any]], httpx.Response]


def chat_completion(
    *,
    content: str | None = "",
    tool_calls: list[tuple[str, dict[str, Any]]] | None = None,
    reasoning: str | None = None,
    model: str = "fake-model",
    prompt_tokens: int = 100,
    cached_tokens: int = 0,
    completion_tokens: int = 20,
    finish_reason: str | None = None,
    call_ids: itertools.count | None = None,
) -> dict[str, Any]:
    """Build a ``/v1/chat/completions`` response body."""
    ids = call_ids or itertools.count()
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call_{next(ids)}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for name, args in tool_calls
        ]
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
    }


class FakeTokenFactory:
    """Queue of canned responses behind an ``httpx.MockTransport``."""

    def __init__(self) -> None:
        self._queue: deque[Responder] = deque()
        self._call_ids = itertools.count()
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []

    def add_reply(self, **kwargs: Any) -> FakeTokenFactory:
        """Queue a successful completion; kwargs go to ``chat_completion``."""

        def respond(request_body: dict[str, Any]) -> httpx.Response:
            body = chat_completion(
                model=request_body.get("model", "fake-model"), call_ids=self._call_ids, **kwargs
            )
            return httpx.Response(200, json=body)

        self._queue.append(respond)
        return self

    def add_json_reply(self, payload: Any, **kwargs: Any) -> FakeTokenFactory:
        return self.add_reply(content=json.dumps(payload), **kwargs)

    def add_error(self, status: int, *, retry_after_ms: int | None = 1) -> FakeTokenFactory:
        headers = {"retry-after-ms": str(retry_after_ms)} if retry_after_ms is not None else {}

        def respond(_: dict[str, Any]) -> httpx.Response:
            return httpx.Response(status, json={"detail": f"fake {status}"}, headers=headers)

        self._queue.append(respond)
        return self

    def add_responder(self, responder: Responder) -> FakeTokenFactory:
        """Queue a custom function: request JSON body -> httpx.Response."""
        self._queue.append(responder)
        return self

    @property
    def pending(self) -> int:
        return len(self._queue)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        self.paths.append(request.url.path)
        if not self._queue:
            raise AssertionError(f"FakeTokenFactory: unexpected request #{len(self.requests)}")
        return self._queue.popleft()(body)

    def http_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def client(self, settings: Settings | None = None, **kwargs: Any) -> TokenFactoryClient:
        settings = settings or Settings(max_retries=2)
        return TokenFactoryClient(settings, http_client=self.http_client(), **kwargs)
