"""Thin client over the Nebius Token Factory chat completions API (``/v1/chat/completions``).

Adds what every Nemotron call in the app needs: model keys instead of raw ids,
a reasoning on/off switch, schema-validated structured output with repair
retries, a tool-calling loop, and cost accounting. Calls go over ``httpx``;
408/409/429/5xx answers and transport errors are retried with exponential
backoff, honouring ``Retry-After``.
"""

from __future__ import annotations

import base64
import email.utils
import json
import logging
import math
import random
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from variator.config import Settings
from variator.llm.models import ModelRegistry
from variator.llm.usage import Usage, UsageLedger

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>(.*?)</think>\s*", re.DOTALL)

_RETRY_STATUS = frozenset({408, 409, 429})
_BACKOFF_INITIAL_S = 0.5
_BACKOFF_MAX_S = 8.0
_MAX_RETRY_AFTER_S = 120.0  # a longer server-directed wait is not retried


class LLMHTTPError(RuntimeError):
    """A Token Factory call failed. ``status_code`` is None when no response arrived."""

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class LLMBadRequestError(LLMHTTPError):
    """400: the request itself is wrong; retrying will not help."""


class LLMConnectionError(LLMHTTPError):
    """Timeout or network failure that persisted after all retries."""


class StructuredOutputError(RuntimeError):
    """The model did not return JSON matching the schema after all attempts."""


class ToolLoopError(RuntimeError):
    """The tool-calling loop hit ``max_steps`` without a final answer."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as returned by the model


@dataclass
class ChatResult:
    content: str
    reasoning: str | None
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    finish_reason: str | None


@dataclass
class Tool:
    """A function the model may call. ``fn`` receives a validated ``params`` instance."""

    name: str
    description: str
    params: type[BaseModel]
    fn: Callable[[Any], Any]

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params.model_json_schema(),
            },
        }


@dataclass
class ToolInvocation:
    name: str
    arguments: dict[str, Any] | None
    output: str
    error: bool = False


@dataclass
class ToolLoopResult:
    final: ChatResult
    messages: list[dict[str, Any]]
    steps: list[ToolInvocation] = field(default_factory=list)


def recommended_sampling(*, reasoning: bool, tools: bool = False) -> dict[str, float]:
    """NVIDIA's recommended sampling for Nemotron 3 (model card / NIM docs)."""
    if tools:
        return {"temperature": 0.6, "top_p": 0.95}
    if reasoning:
        return {"temperature": 1.0, "top_p": 1.0}
    return {"temperature": 0.0}


def image_part(data: bytes, mime: str = "image/png") -> dict[str, Any]:
    """Message content part with an inline base64 image (for Nemotron Super VL)."""
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def user_message(text: str, images: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    if not images:
        return {"role": "user", "content": text}
    return {"role": "user", "content": [{"type": "text", "text": text}, *images]}


def split_reasoning(content: str) -> tuple[str, str | None]:
    """Strip inline ``<think>...</think>`` blocks; return (answer, reasoning)."""
    thoughts = _THINK_RE.findall(content)
    if not thoughts:
        return content, None
    return _THINK_RE.sub("", content).strip(), "\n".join(t.strip() for t in thoughts)


def _rejects_json_schema(exc: LLMBadRequestError) -> bool:
    text = str(exc).lower()
    return "json_schema" in text or "response_format" in text


def _error_from_response(response: httpx.Response) -> LLMHTTPError:
    """Map an error status to ``LLMBadRequestError`` (400) or ``LLMHTTPError``."""
    text = response.text.strip()
    try:
        body: Any = json.loads(text)
    except ValueError:
        body = text
    cls = LLMBadRequestError if response.status_code == 400 else LLMHTTPError
    message = f"Token Factory HTTP {response.status_code}: {text[:2000] or response.reason_phrase}"
    return cls(message, status_code=response.status_code, body=body)


def _retry_after_s(headers: httpx.Headers) -> float | None:
    """Server-directed wait from ``retry-after-ms`` or ``Retry-After`` (seconds or HTTP date)."""
    for name, divisor in (("retry-after-ms", 1000), ("retry-after", 1)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value) / divisor
        except ValueError:
            continue
        return seconds if math.isfinite(seconds) else None
    try:
        parsed = email.utils.parsedate_tz(headers.get("retry-after") or "")
        return None if parsed is None else email.utils.mktime_tz(parsed) - time.time()
    except (TypeError, ValueError, OverflowError):
        return None


def _retry_delay_s(retries_done: int, retry_after: float | None) -> float:
    """``Retry-After`` when sane, else 0.5 s doubling up to 8 s, minus up to 25% jitter."""
    if retry_after is not None and 0 < retry_after <= _MAX_RETRY_AFTER_S:
        return retry_after
    backoff = min(_BACKOFF_INITIAL_S * 2**retries_done, _BACKOFF_MAX_S)
    return backoff * (1 - 0.25 * random.random())


def extract_json(text: str) -> str:
    """Pull a JSON object out of a reply that may be wrapped in prose or code fences."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


class TokenFactoryClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http_client: httpx.Client | None = None,
        ledger: UsageLedger | None = None,
        api_key: str | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.models = ModelRegistry(self.settings.model_overrides)
        self.ledger = ledger or UsageLedger(self.models)
        if api_key is None:
            # A fake transport in tests needs no key; a real call does.
            api_key = (
                self.settings.nebius_api_key
                if http_client is not None
                else self.settings.require("nebius_api_key")
            )
        self.structured_mode = self.settings.structured_mode
        self._base_url = self.settings.tf_base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key or 'test-key'}", "Accept": "application/json"}
        self._http = http_client or httpx.Client(
            limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100),
            follow_redirects=True,
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        """One API call with retries; returns the JSON body or raises ``LLMHTTPError``."""
        url = f"{self._base_url}/{path}"
        attempt = 0
        while True:
            retry_after: float | None = None
            try:
                response = self._http.request(
                    method, url, json=payload, headers=self._headers, timeout=self.settings.request_timeout_s
                )
            except httpx.RequestError as exc:  # includes timeouts
                if attempt >= self.settings.max_retries:
                    raise LLMConnectionError(
                        f"Token Factory {path} failed after {attempt + 1} attempts: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                reason = type(exc).__name__
            else:
                if response.is_success:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise LLMHTTPError(
                            f"Token Factory {path} returned a non-JSON body",
                            status_code=response.status_code,
                            body=response.text,
                        ) from exc
                retry_after = _retry_after_s(response.headers)
                retryable = response.status_code in _RETRY_STATUS or response.status_code >= 500
                if retry_after is not None and retry_after > _MAX_RETRY_AFTER_S:
                    retryable = False
                if not retryable or attempt >= self.settings.max_retries:
                    raise _error_from_response(response)
                reason = f"HTTP {response.status_code}"
            delay = _retry_delay_s(attempt, retry_after)
            log.info("Token Factory %s: %s, retrying in %.2fs", path, reason, delay)
            time.sleep(delay)
            attempt += 1

    def list_models(self) -> list[str]:
        """Model ids the endpoint serves (``GET /v1/models``)."""
        return [item["id"] for item in self._request("GET", "models").get("data") or []]

    def chat(
        self,
        model: str,
        messages: Sequence[dict[str, Any]],
        *,
        reasoning: bool | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> ChatResult:
        """One chat completion. ``model`` is a registry key ("ultra") or a raw model id.

        ``reasoning=None`` keeps the model default (Nemotron 3 thinks by default);
        True/False is sent as ``chat_template_kwargs.enable_thinking``.
        """
        model_id = self.models.model_id(model)
        body: dict[str, Any] = dict(extra_body or {})
        if reasoning is not None:
            body.setdefault("chat_template_kwargs", {})["enable_thinking"] = reasoning
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": list(messages),
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "tools": list(tools) if tools else None,
            "tool_choice": tool_choice,
            "response_format": response_format,
        }
        # ``extra_body`` fields go to the top level of the JSON body and win on clashes.
        payload = {**{k: v for k, v in kwargs.items() if v is not None}, **body}

        started = time.perf_counter()
        response = self._request("POST", "chat/completions", payload)
        duration_s = time.perf_counter() - started
        choice = response["choices"][0]
        message = choice.get("message") or {}
        content, inline_reasoning = split_reasoning(message.get("content") or "")
        reasoning_text = message.get("reasoning_content") or message.get("reasoning") or inline_reasoning

        usage = Usage()
        raw_usage = response.get("usage")
        if raw_usage is not None:
            details = raw_usage.get("prompt_tokens_details") or {}
            usage = Usage(
                prompt_tokens=raw_usage.get("prompt_tokens") or 0,
                cached_tokens=details.get("cached_tokens") or 0,
                completion_tokens=raw_usage.get("completion_tokens") or 0,
            )
        self.ledger.record(model_id, usage, duration_s=duration_s)

        return ChatResult(
            content=content,
            reasoning=reasoning_text,
            tool_calls=[
                ToolCall(
                    id=tc.get("id") or "",
                    name=tc["function"]["name"],
                    arguments=tc["function"].get("arguments") or "{}",
                )
                for tc in message.get("tool_calls") or []
            ],
            usage=usage,
            model=response.get("model") or model_id,
            finish_reason=choice.get("finish_reason"),
        )

    def structured(
        self,
        model: str,
        messages: Sequence[dict[str, Any]],
        schema: type[T],
        *,
        reasoning: bool | None = False,
        max_attempts: int = 2,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> T:
        """Ask for JSON matching ``schema`` and validate it, repairing on failure.

        The schema goes both into the system prompt and ``response_format`` (the
        Token Factory docs recommend doing both). ``system`` is prepended to that
        single system message, so callers never need a second one (some chat
        templates reject two). If the API rejects ``json_schema`` (its reference
        is contradictory on support), the client switches to ``json_object`` for
        the rest of its life and relies on the prompt plus validation.
        """
        json_schema = schema.model_json_schema()
        instruction = (
            "Reply with a single JSON object that matches this JSON Schema. "
            "No prose, no code fences.\n" + json.dumps(json_schema, ensure_ascii=False)
        )
        if system:
            instruction = system.rstrip() + "\n\n" + instruction
        convo: list[dict[str, Any]] = [{"role": "system", "content": instruction}, *messages]
        last_error = ""
        for _ in range(max_attempts):
            result = self._structured_call(
                model,
                convo,
                schema.__name__,
                json_schema,
                reasoning=reasoning,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
            )
            try:
                return schema.model_validate_json(extract_json(result.content))
            except ValidationError as exc:
                last_error = str(exc)
                convo = [
                    *convo,
                    {"role": "assistant", "content": result.content},
                    {
                        "role": "user",
                        "content": "The JSON is invalid:\n" + last_error[:2000]
                        + "\nReturn the corrected JSON object only.",
                    },
                ]
        raise StructuredOutputError(
            f"{schema.__name__}: no valid JSON after {max_attempts} attempts: {last_error[:500]}"
        )

    def _structured_call(
        self,
        model: str,
        convo: list[dict[str, Any]],
        name: str,
        json_schema: dict[str, Any],
        **kwargs: Any,
    ) -> ChatResult:
        if self.structured_mode == "json_schema":
            response_format = {"type": "json_schema", "json_schema": {"name": name, "schema": json_schema}}
            try:
                return self.chat(model, convo, response_format=response_format, **kwargs)
            except LLMBadRequestError as exc:
                if not _rejects_json_schema(exc):
                    raise
                self.structured_mode = "json_object"
        return self.chat(model, convo, response_format={"type": "json_object"}, **kwargs)

    def run_tools(
        self,
        model: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Tool],
        *,
        max_steps: int = 8,
        reasoning: bool | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ToolLoopResult:
        """Let the model call ``tools`` until it answers without tool calls.

        Tool errors are fed back to the model as tool output instead of raising,
        so it can recover.
        """
        by_name = {tool.name: tool for tool in tools}
        specs = [tool.spec() for tool in tools]
        convo: list[dict[str, Any]] = list(messages)
        steps: list[ToolInvocation] = []
        sampling = recommended_sampling(reasoning=bool(reasoning), tools=True)
        for _ in range(max_steps):
            result = self.chat(
                model,
                convo,
                reasoning=reasoning,
                temperature=temperature if temperature is not None else sampling["temperature"],
                top_p=None if temperature is not None else sampling["top_p"],
                max_tokens=max_tokens,
                tools=specs,
            )
            if not result.tool_calls:
                return ToolLoopResult(final=result, messages=convo, steps=steps)
            convo.append(
                {
                    "role": "assistant",
                    "content": result.content or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for call in result.tool_calls
                    ],
                }
            )
            for call in result.tool_calls:
                invocation = self._invoke(by_name, call)
                steps.append(invocation)
                convo.append({"role": "tool", "tool_call_id": call.id, "content": invocation.output})
        raise ToolLoopError(f"no final answer after {max_steps} steps")

    @staticmethod
    def _invoke(by_name: dict[str, Tool], call: ToolCall) -> ToolInvocation:
        tool = by_name.get(call.name)
        if tool is None:
            return ToolInvocation(call.name, None, f"error: unknown tool {call.name!r}", error=True)
        try:
            args = json.loads(call.arguments or "{}")
            output = tool.fn(tool.params.model_validate(args))
        except Exception as exc:  # fed back to the model so it can retry
            return ToolInvocation(call.name, None, f"error: {type(exc).__name__}: {exc}", error=True)
        if isinstance(output, BaseModel):
            text = output.model_dump_json()
        elif isinstance(output, str):
            text = output
        else:
            text = json.dumps(output, ensure_ascii=False, default=str)
        return ToolInvocation(call.name, args, text)
