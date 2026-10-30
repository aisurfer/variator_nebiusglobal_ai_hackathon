"""The LLM gateway over plain HTTP (no infra, no network): request shape, retries, tool calls and usage."""
from __future__ import annotations

import json
import time
from email.utils import formatdate

import httpx
import pytest

from kb import settings as S
from kb.llm import client as C


@pytest.fixture
def api(monkeypatch):
    """An LLMClient whose HTTP goes to `replies` (a list of httpx.Response or exceptions, consumed in order)."""
    monkeypatch.setenv("KB_LLM_ENABLED", "true")
    monkeypatch.setenv("KB_LLM_API_KEY", "test-key")
    monkeypatch.delenv("KB_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("KB_LLM_MODEL", raising=False)
    S.get_settings.cache_clear()
    S.llm_api_key.cache_clear()
    requests: list[httpx.Request] = []
    replies: list[httpx.Response | Exception] = []
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    real = httpx.Client
    monkeypatch.setattr(C.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(C.time, "sleep", waits.append)
    try:
        yield C.LLMClient(), requests, replies, waits
    finally:
        monkeypatch.undo()
        S.get_settings.cache_clear()
        S.llm_api_key.cache_clear()


def _ok(message: dict, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                                     "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7}})


def test_disabled_llm_gives_no_client(monkeypatch):
    monkeypatch.setenv("KB_LLM_ENABLED", "false")
    S.get_settings.cache_clear()
    try:
        assert C.get_llm() is None
    finally:
        monkeypatch.undo()
        S.get_settings.cache_clear()


def test_json_call_posts_a_strict_schema_and_counts_usage(api):
    llm, requests, replies, waits = api
    replies.append(_ok({"role": "assistant", "content": '{"answer": "yes"}'}))
    out = llm.json(system="sys", user="q", schema={"type": "object"}, name="r", effort="minimal", max_output_tokens=50)
    assert out == {"answer": "yes"}
    (req,) = requests
    assert str(req.url) == "https://api.tokenfactory.nebius.com/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer test-key"
    body = json.loads(req.content)
    assert body["model"] == "nvidia/Nemotron-3_5-Lightning" and body["max_completion_tokens"] == 50
    assert body["reasoning_effort"] == "minimal"
    assert body["response_format"] == {"type": "json_schema",
                                       "json_schema": {"name": "r", "strict": True, "schema": {"type": "object"}}}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert llm.usage.as_dict() | {"seconds": 0} == {"calls": 1, "input_tokens": 11, "output_tokens": 7, "seconds": 0}
    assert waits == []


def test_rate_limit_waits_the_retry_after_then_succeeds(api):
    llm, requests, replies, waits = api
    replies += [httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "slow down"}}),
                _ok({"role": "assistant", "content": "{}"})]
    assert llm.json(system="s", user="u", schema={}) == {}
    assert len(requests) == 2 and waits == [2.0] and llm.usage.calls == 1


def test_connection_error_is_retried_with_backoff(api):
    llm, requests, replies, waits = api
    replies += [httpx.ConnectError("refused"), _ok({"role": "assistant", "content": "{}"})]
    assert llm.json(system="s", user="u", schema={}) == {}
    assert len(requests) == 2 and len(waits) == 1 and 0.375 <= waits[0] <= 0.5


def test_server_errors_give_up_after_the_retries(api):
    llm, requests, replies, waits = api
    replies += [httpx.Response(503, text="busy"), httpx.Response(502, json={"error": {"message": "bad gateway"}})]
    with pytest.raises(C.LLMError) as e:
        llm.json(system="s", user="u", schema={})
    assert e.value.status == 502 and "bad gateway" in str(e.value)
    assert len(requests) == C.MAX_RETRIES + 1 and len(waits) == C.MAX_RETRIES and llm.usage.calls == 0


def test_a_rejected_request_is_not_retried(api):
    llm, requests, replies, waits = api
    replies.append(httpx.Response(400, json={"error": {"message": "unknown field"}}))
    with pytest.raises(C.LLMError) as e:
        llm.json(system="s", user="u", schema={})
    assert e.value.status == 400 and len(requests) == 1 and waits == []


def test_tool_calls_come_back_as_a_message_that_can_be_sent_again(api):
    llm, requests, replies, waits = api
    call = {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": '{"q": "EXP-7"}'}}
    replies += [_ok({"role": "assistant", "content": None, "tool_calls": [call]}),
                _ok({"role": "assistant", "content": "done"}, {"prompt_tokens": 5, "completion_tokens": 1})]
    tools = [{"type": "function", "function": {"name": "search", "description": "d", "parameters": {}}}]
    msgs: list = [{"role": "user", "content": "why?"}]
    m = llm.chat(messages=msgs, tools=tools, tool_choice="auto")
    assert m.content is None and m.tool_calls and m.tool_calls[0].function.name == "search"
    assert json.loads(m.tool_calls[0].function.arguments) == {"q": "EXP-7"}
    body = json.loads(requests[0].content)
    assert body["tools"] == tools and body["tool_choice"] == "auto"
    msgs += [m, {"role": "tool", "tool_call_id": "call_1", "content": "[]"}]
    m2 = llm.chat(messages=msgs)
    assert m2.content == "done" and m2.tool_calls is None
    body2 = json.loads(requests[1].content)
    assert body2["messages"][1] == {"role": "assistant", "content": None, "tool_calls": [call]}
    assert "tools" not in body2
    assert (llm.usage.calls, llm.usage.input_tokens, llm.usage.output_tokens) == (2, 16, 8)


def test_budget_stops_before_the_request(api):
    llm, requests, replies, waits = api
    llm.budget = C.Budget(max_calls=0)
    with pytest.raises(C.BudgetExceeded):
        llm.json(system="s", user="u", schema={})
    assert requests == []


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after-ms": "1500"}, 1.5),
    ({"retry-after": "3"}, 3.0),
    ({"retry-after": "0"}, None),
    ({"retry-after": "120"}, None),
    ({"retry-after": "soon"}, None),
    ({}, None),
])
def test_retry_after_parsing(headers, expected):
    assert C._retry_after(httpx.Headers(headers)) == expected


def test_retry_after_as_an_http_date():
    wait = C._retry_after(httpx.Headers({"retry-after": formatdate(time.time() + 30, usegmt=True)}))
    assert wait is not None and 28 <= wait <= 30
