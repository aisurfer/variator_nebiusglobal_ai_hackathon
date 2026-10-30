import pytest
from pydantic import BaseModel

from variator.config import MissingCredentialsError, Settings
from variator.llm import (
    LIGHTNING,
    ULTRA,
    LLMBadRequestError,
    LLMConnectionError,
    LLMHTTPError,
    StructuredOutputError,
    TokenFactoryClient,
    Tool,
    ToolLoopError,
    image_part,
    split_reasoning,
    user_message,
)


class Answer(BaseModel):
    value: int
    reason: str


def test_model_keys_resolve_to_ids(fake_tf):
    fake_tf.add_reply(content="hi")
    client = fake_tf.client()
    client.chat("lightning", [user_message("hello")])
    assert fake_tf.requests[0]["model"] == LIGHTNING.model_id
    assert fake_tf.paths[0].endswith("/chat/completions")


def test_model_override_from_settings(fake_tf):
    fake_tf.add_reply(content="hi")
    client = fake_tf.client(Settings(model_overrides={"ultra": "nvidia/ultra-v2"}))
    client.chat("ultra", [user_message("x")])
    assert fake_tf.requests[0]["model"] == "nvidia/ultra-v2"


def test_raw_model_id_passes_through(fake_tf):
    fake_tf.add_reply(content="hi")
    fake_tf.client().chat("Qwen/Qwen3-Embedding-8B", [user_message("x")])
    assert fake_tf.requests[0]["model"] == "Qwen/Qwen3-Embedding-8B"


def test_reasoning_toggle_is_sent_as_chat_template_kwargs(fake_tf):
    fake_tf.add_reply(content="a").add_reply(content="b")
    client = fake_tf.client()
    client.chat("super", [user_message("x")], reasoning=False)
    client.chat("super", [user_message("x")])
    assert fake_tf.requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in fake_tf.requests[1]


def test_reasoning_is_extracted_from_field_and_inline(fake_tf):
    fake_tf.add_reply(content="42", reasoning="because")
    fake_tf.add_reply(content="<think>hmm</think>\n42")
    client = fake_tf.client()
    first = client.chat("ultra", [user_message("x")])
    second = client.chat("ultra", [user_message("x")])
    assert (first.content, first.reasoning) == ("42", "because")
    assert (second.content, second.reasoning) == ("42", "hmm")


def test_split_reasoning_without_think_block():
    assert split_reasoning("plain") == ("plain", None)


def test_usage_and_cost_are_recorded(fake_tf):
    fake_tf.add_reply(content="x", prompt_tokens=10_000, cached_tokens=8_000, completion_tokens=3_000)
    client = fake_tf.client()
    result = client.chat("ultra", [user_message("x")])
    assert result.usage.cached_tokens == 8_000
    expected = (2_000 * 1.00 + 8_000 * 0.12 + 3_000 * 3.00) / 1_000_000
    assert client.ledger.by_model()[ULTRA.model_id].cost_usd == pytest.approx(expected)
    assert client.ledger.total_cost() == pytest.approx(expected)


def test_structured_output_sends_schema_and_validates(fake_tf):
    fake_tf.add_json_reply({"value": 7, "reason": "ok"})
    answer = fake_tf.client().structured("lightning", [user_message("q")], Answer)
    assert answer == Answer(value=7, reason="ok")
    request = fake_tf.requests[0]
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["name"] == "Answer"
    assert '"value"' in request["messages"][0]["content"]
    assert request["chat_template_kwargs"] == {"enable_thinking": False}


def test_structured_output_repairs_invalid_json(fake_tf):
    fake_tf.add_reply(content='```json\n{"value": "seven"}\n```')
    fake_tf.add_reply(content='Sure! {"value": 7, "reason": "fixed"}')
    answer = fake_tf.client().structured("lightning", [user_message("q")], Answer)
    assert answer.value == 7
    repair_turn = fake_tf.requests[1]["messages"][-1]["content"]
    assert "invalid" in repair_turn


def test_structured_system_is_merged_into_one_system_message(fake_tf):
    fake_tf.add_json_reply({"value": 1, "reason": "r"})
    fake_tf.client().structured(
        "super", [user_message("q")], Answer, system="You are a CFO.", top_p=0.9
    )
    messages = fake_tf.requests[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"].startswith("You are a CFO.")
    assert fake_tf.requests[0]["top_p"] == 0.9


def test_structured_falls_back_to_json_object(fake_tf):
    def reject(_body):
        import httpx

        return httpx.Response(
            400, json={"detail": "Only {'type': 'json_object'} or {'type': 'text'} response_format"}
        )

    fake_tf.add_responder(reject)
    fake_tf.add_json_reply({"value": 2, "reason": "r"})
    fake_tf.add_json_reply({"value": 3, "reason": "r"})
    client = fake_tf.client()
    assert client.structured("lightning", [user_message("q")], Answer).value == 2
    assert client.structured("lightning", [user_message("q")], Answer).value == 3
    formats = [r["response_format"]["type"] for r in fake_tf.requests]
    assert formats == ["json_schema", "json_object", "json_object"]
    assert client.structured_mode == "json_object"


def test_unrelated_bad_request_is_not_swallowed(fake_tf):
    import httpx

    fake_tf.add_responder(lambda _b: httpx.Response(400, json={"detail": "context length exceeded"}))
    with pytest.raises(LLMBadRequestError) as info:
        fake_tf.client().structured("lightning", [user_message("q")], Answer)
    assert info.value.status_code == 400
    assert info.value.body == {"detail": "context length exceeded"}
    assert len(fake_tf.requests) == 1  # a 400 is not retried


def test_structured_output_gives_up(fake_tf):
    fake_tf.add_reply(content="nope").add_reply(content="still nope")
    with pytest.raises(StructuredOutputError):
        fake_tf.client().structured("lightning", [user_message("q")], Answer, max_attempts=2)


class Lookup(BaseModel):
    city: str


def test_tool_loop_executes_tools_and_returns_final(fake_tf):
    fake_tf.add_reply(tool_calls=[("rent", {"city": "Warsaw"}), ("missing_tool", {})])
    fake_tf.add_reply(content="Rent is 20")
    calls = []

    def rent(args: Lookup) -> dict:
        calls.append(args.city)
        return {"usd_per_m2": 20}

    tool = Tool("rent", "Warehouse rent by city", Lookup, rent)
    result = fake_tf.client().run_tools("lightning", [user_message("rent?")], [tool])
    assert result.final.content == "Rent is 20"
    assert calls == ["Warsaw"]
    assert [s.error for s in result.steps] == [False, True]
    second = fake_tf.requests[1]
    assert second["tools"][0]["function"]["name"] == "rent"
    tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == '{"usd_per_m2": 20}'
    assert "unknown tool" in tool_msgs[1]["content"]


def test_tool_errors_are_fed_back(fake_tf):
    fake_tf.add_reply(tool_calls=[("rent", {"wrong": 1})]).add_reply(content="done")
    tool = Tool("rent", "d", Lookup, lambda a: 1)
    result = fake_tf.client().run_tools("lightning", [user_message("x")], [tool])
    assert result.steps[0].error and "ValidationError" in result.steps[0].output


def test_tool_loop_max_steps(fake_tf):
    for _ in range(2):
        fake_tf.add_reply(tool_calls=[("rent", {"city": "A"})])
    tool = Tool("rent", "d", Lookup, lambda a: "x")
    with pytest.raises(ToolLoopError):
        fake_tf.client().run_tools("lightning", [user_message("x")], [tool], max_steps=2)


def test_retries_on_rate_limit(fake_tf):
    fake_tf.add_error(429).add_reply(content="ok")
    assert fake_tf.client().chat("lightning", [user_message("x")]).content == "ok"
    assert len(fake_tf.requests) == 2


def test_server_errors_give_up_after_max_retries(fake_tf, monkeypatch):
    delays = []
    monkeypatch.setattr("variator.llm.client.time.sleep", delays.append)
    for _ in range(3):
        fake_tf.add_error(503, retry_after_ms=1500)
    with pytest.raises(LLMHTTPError) as info:
        fake_tf.client(Settings(max_retries=2)).chat("lightning", [user_message("x")])
    assert info.value.status_code == 503 and not isinstance(info.value, LLMBadRequestError)
    assert len(fake_tf.requests) == 3
    assert delays == [1.5, 1.5]  # retry-after-ms honoured


def test_transport_errors_back_off_then_raise(fake_tf, monkeypatch):
    import httpx

    delays = []
    monkeypatch.setattr("variator.llm.client.time.sleep", delays.append)

    def down(_body):
        raise httpx.ConnectError("connection refused")

    for _ in range(3):
        fake_tf.add_responder(down)
    with pytest.raises(LLMConnectionError, match="ConnectError"):
        fake_tf.client(Settings(max_retries=2)).chat("lightning", [user_message("x")])
    assert len(fake_tf.requests) == 3
    assert 0.375 <= delays[0] <= 0.5 and 0.75 <= delays[1] <= 1.0


def test_extra_body_goes_to_the_top_level(fake_tf):
    fake_tf.add_reply(content="x")
    fake_tf.client().chat(
        "super", [user_message("x")], reasoning=False, extra_body={"min_p": 0.1, "top_p": 0.5}, top_p=0.9
    )
    request = fake_tf.requests[0]
    assert request["min_p"] == 0.1 and request["top_p"] == 0.5
    assert request["chat_template_kwargs"] == {"enable_thinking": False}


def test_list_models(fake_tf):
    import httpx

    fake_tf.add_responder(lambda _b: httpx.Response(200, json={"data": [{"id": "a/b"}, {"id": "c/d"}]}))
    assert fake_tf.client().list_models() == ["a/b", "c/d"]
    assert fake_tf.paths[0].endswith("/v1/models")


def test_vision_message_shape(fake_tf):
    fake_tf.add_reply(content="a chart")
    msg = user_message("describe", images=[image_part(b"\x89PNG", "image/png")])
    fake_tf.client().chat("super_vl", [msg])
    content = fake_tf.requests[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_real_client_requires_key():
    with pytest.raises(MissingCredentialsError, match="NEBIUS_API_KEY"):
        TokenFactoryClient(Settings())


@pytest.mark.live
def test_live_lightning_smoke():
    client = TokenFactoryClient(Settings.from_env())
    result = client.chat("lightning", [user_message("Reply with the word OK.")], reasoning=False)
    assert "ok" in result.content.lower()
    assert result.usage.completion_tokens > 0


@pytest.mark.live
def test_live_model_ids_exist():
    """Catalog ids drift; this verifies the registry against GET /v1/models."""
    client = TokenFactoryClient(Settings.from_env())
    available = set(client.list_models())
    missing = [spec.model_id for spec in client.models if spec.model_id not in available]
    assert not missing, f"update ids in variator/llm/models.py: {missing}"
