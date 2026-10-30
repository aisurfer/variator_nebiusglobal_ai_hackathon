"""LLM gateway (KB-8): chat completions API (Nebius Token Factory) with strict JSON output and tool calling.

Plain HTTPS through httpx: POST {llm_base_url}/chat/completions with a bearer key. Connection errors, timeouts,
408/409/429 and 5xx replies are retried MAX_RETRIES times, after the server's Retry-After when it gives a sane one,
else after an exponential backoff. The model never sees content the principal may not read: callers pass only
ACL-filtered evidence. Usage is accumulated per client so callers can enforce budgets (B2/B3/B5). The API key is
never logged.
"""
from __future__ import annotations

import email.utils
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from kb.settings import get_settings, llm_api_key, llm_available

log = logging.getLogger(__name__)

MAX_RETRIES = 1  # attempts after the first one
BACKOFF_S = 0.5  # first wait without a Retry-After; doubles per retry up to BACKOFF_MAX_S, minus up to 25 % jitter
BACKOFF_MAX_S = 8.0
RETRY_AFTER_MAX_S = 60.0  # a longer (or absent, or unparseable) Retry-After falls back to the backoff
RETRY_STATUS = frozenset({408, 409, 429})  # and every 5xx


class LLMUnavailable(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class LLMError(Exception):
    """The API refused the request, or still failed after the retries. `status` is None without an HTTP reply."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class ToolFunction:
    name: str
    arguments: str  # JSON text as the model wrote it; the caller parses and validates it


@dataclass
class ToolCall:
    id: str
    function: ToolFunction
    type: str = "function"


@dataclass
class ChatMessage:
    """The assistant message of a reply: `.content`, or `.tool_calls` for a tool-calling turn."""

    role: str = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None

    @classmethod
    def from_api(cls, m: dict[str, Any]) -> ChatMessage:
        calls = [ToolCall(id=c.get("id") or "", type=c.get("type") or "function",
                          function=ToolFunction(name=(c.get("function") or {}).get("name") or "",
                                                arguments=(c.get("function") or {}).get("arguments") or "{}"))
                 for c in m.get("tool_calls") or []]
        return cls(role=m.get("role") or "assistant", content=m.get("content"), tool_calls=calls or None)

    def as_dict(self) -> dict[str, Any]:
        """The message as the API takes it back in the `messages` of the next turn."""
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [{"id": c.id, "type": c.type,
                                  "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                 for c in self.tool_calls]
        return out


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "seconds": round(self.seconds, 2)}


@dataclass
class Budget:
    max_calls: int = 4
    max_input_tokens: int = 32_000
    max_output_tokens: int = 4_000

    def check(self, u: Usage) -> None:
        if u.calls >= self.max_calls or u.input_tokens >= self.max_input_tokens or u.output_tokens >= self.max_output_tokens:
            raise BudgetExceeded(f"LLM budget exhausted: {u.as_dict()}")


def _retry_after(headers: httpx.Headers) -> float | None:
    """Seconds the server asks to wait (retry-after-ms, or Retry-After in seconds or as an HTTP date), when sane."""
    try:
        if (ms := headers.get("retry-after-ms")) is not None:
            wait = float(ms) / 1000
        elif (v := headers.get("retry-after")) is not None:
            try:
                wait = float(v)
            except ValueError:
                wait = email.utils.parsedate_to_datetime(v).timestamp() - time.time()
        else:
            return None
    except (TypeError, ValueError):
        return None
    return wait if 0 < wait <= RETRY_AFTER_MAX_S else None


def _backoff(attempt: int) -> float:
    return min(BACKOFF_S * 2 ** attempt, BACKOFF_MAX_S) * (1 - 0.25 * random.random())


def _error_text(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:300]
    err = body.get("error") if isinstance(body, dict) else None
    msg = err.get("message") if isinstance(err, dict) else (body.get("detail") if isinstance(body, dict) else None)
    return str(msg or body)[:300]


def _message(resp: dict[str, Any]) -> ChatMessage:
    choices = resp.get("choices") or []
    if not choices:
        raise LLMError("chat completions reply without choices")
    return ChatMessage.from_api(choices[0].get("message") or {})


@dataclass
class LLMClient:
    model: str = field(default_factory=lambda: get_settings().llm_model)
    budget: Budget | None = None
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self) -> None:
        if not llm_available():
            raise LLMUnavailable("LLM disabled or no API key")
        s = get_settings()
        self._http = httpx.Client(base_url=s.llm_base_url, timeout=s.llm_timeout_s,
                                  headers={"Authorization": f"Bearer {llm_api_key()}", "Accept": "application/json"})

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """One request with retries; the parsed JSON reply, or LLMError."""
        for attempt in range(MAX_RETRIES + 1):
            last = attempt == MAX_RETRIES
            try:
                r = self._http.post("chat/completions", json=body)
            except httpx.TransportError as exc:  # refused or reset connection, DNS, timeouts
                if last:
                    raise LLMError(f"chat completions unreachable: {type(exc).__name__}") from exc
                wait = _backoff(attempt)
                log.warning("LLM call failed (%s), retry in %.1fs", type(exc).__name__, wait)
            else:
                if r.is_success:
                    try:
                        return r.json()
                    except ValueError as exc:
                        raise LLMError("chat completions reply is not JSON", r.status_code) from exc
                if last or not (r.status_code in RETRY_STATUS or r.status_code >= 500):
                    raise LLMError(f"chat completions HTTP {r.status_code}: {_error_text(r)}", r.status_code)
                wait = _retry_after(r.headers) or _backoff(attempt)
                log.warning("LLM call failed (HTTP %s), retry in %.1fs", r.status_code, wait)
            time.sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover - the last attempt returns or raises

    def _call(self, **kwargs: Any) -> dict[str, Any]:
        if self.budget:
            self.budget.check(self.usage)
        t0 = time.monotonic()
        resp = self._post({"model": self.model, **kwargs})
        self.usage.calls += 1
        self.usage.seconds += time.monotonic() - t0
        usage = resp.get("usage") or {}
        self.usage.input_tokens += usage.get("prompt_tokens") or 0
        self.usage.output_tokens += usage.get("completion_tokens") or 0
        return resp

    def json(self, *, system: str, user: str, schema: dict[str, Any], name: str = "result", effort: str = "low",
             max_output_tokens: int = 2000) -> dict[str, Any]:
        """One structured call; the reply must validate against the strict JSON schema."""
        resp = self._call(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
            reasoning_effort=effort,
            max_completion_tokens=max_output_tokens,
        )
        content = _message(resp).content or "{}"
        return json.loads(content)

    def chat(self, *, messages: list[dict[str, Any] | ChatMessage], tools: list[dict[str, Any]] | None = None,
             effort: str = "low", max_output_tokens: int = 2000, tool_choice: str | None = None) -> ChatMessage:
        """One tool-calling turn; returns the assistant message (with .tool_calls or .content). A returned message
        may go back into `messages` as is."""
        kwargs: dict[str, Any] = {"messages": [m.as_dict() if isinstance(m, ChatMessage) else m for m in messages],
                                  "reasoning_effort": effort, "max_completion_tokens": max_output_tokens}
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        return _message(self._call(**kwargs))


def get_llm(budget: Budget | None = None) -> LLMClient | None:
    try:
        return LLMClient(budget=budget)
    except LLMUnavailable:
        return None
