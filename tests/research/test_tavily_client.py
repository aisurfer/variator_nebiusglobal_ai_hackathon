from __future__ import annotations

import json

import httpx
import pytest
import respx

from variator.config import MissingCredentialsError, Settings
from variator.research import (
    TavilyAuthError,
    TavilyBadRequestError,
    TavilyClient,
    TavilyCreditsExhaustedError,
    TavilyError,
    TavilyRateLimitError,
    TavilyServerError,
    TavilyTransportError,
)
from variator.research.testing import error_payload, extract_payload, search_payload, search_result

TEST_KEY = "tvly-test-key"  # same as the conftest `tavily` fixture


def _body(route, index: int = 0) -> dict:
    return json.loads(route.calls[index].request.content)


def test_search_request_matches_api_reference(tavily_api, tavily):
    route = tavily_api.post("/search").mock(
        return_value=httpx.Response(200, json=search_payload("warehouse rent poland", []))
    )
    tavily.search("warehouse rent poland")

    request = route.calls.last.request
    assert request.method == "POST"
    assert str(request.url) == "https://api.tavily.com/search"
    assert request.headers["authorization"] == f"Bearer {TEST_KEY}"
    assert request.headers["content-type"] == "application/json"
    assert _body(route) == {
        "query": "warehouse rent poland",
        "search_depth": "basic",
        "topic": "general",
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": False,
        "include_usage": True,
    }


def test_search_sends_optional_filters(tavily_api, tavily):
    route = tavily_api.post("/search").mock(return_value=httpx.Response(200, json=search_payload("q", [])))
    tavily.search(
        "q",
        search_depth="advanced",
        topic="news",
        max_results=10,
        time_range="month",
        include_domains=["reuters.com"],
        exclude_domains=["reddit.com"],
        include_answer="advanced",
        include_raw_content="text",
        chunks_per_source=2,
        country="germany",
    )
    body = _body(route)
    assert body["search_depth"] == "advanced"
    assert body["topic"] == "news"
    assert body["max_results"] == 10
    assert body["time_range"] == "month"
    assert body["include_domains"] == ["reuters.com"]
    assert body["exclude_domains"] == ["reddit.com"]
    assert body["include_answer"] == "advanced"
    assert body["include_raw_content"] == "text"
    assert body["chunks_per_source"] == 2
    assert body["country"] == "germany"


def test_search_parses_response_and_reported_credits(tavily_api, tavily):
    payload = search_payload(
        "3pl fees",
        [
            search_result(
                "https://a.example/fees",
                title="3PL fees 2026",
                content="Fees rose 12% [...] in 2026.",
                score=0.93,
                published_date="2026-05-01",
            ),
            {"url": "https://b.example/x", "title": "B", "content": "c", "score": 0.5, "id": "r2", "new": 1},
        ],
        credits=1,
        answer="Fees rose.",
    )
    tavily_api.post("/search").mock(return_value=httpx.Response(200, json=payload))
    response = tavily.search("3pl fees")

    assert response.query == "3pl fees"
    assert response.answer == "Fees rose."
    assert response.request_id == "req-fake"
    assert response.response_time == pytest.approx(0.42)
    first = response.results[0]
    assert (first.url, first.title, first.score) == ("https://a.example/fees", "3PL fees 2026", 0.93)
    assert first.published_date == "2026-05-01"
    assert response.results[1].published_date is None
    assert (response.credits, response.credits_estimated, response.from_cache) == (1, False, False)


def test_search_credits_are_estimated_when_usage_missing(tavily_api, tavily):
    tavily_api.post("/search").mock(
        return_value=httpx.Response(200, json=search_payload("q", [], credits=None))
    )
    basic = tavily.search("q")
    advanced = tavily.search("q", search_depth="advanced")
    assert (basic.credits, basic.credits_estimated) == (1, True)
    assert (advanced.credits, advanced.credits_estimated) == (2, True)


def test_extract_request_and_response(tavily_api, tavily):
    route = tavily_api.post("/extract").mock(
        return_value=httpx.Response(
            200,
            json=extract_payload(
                [("https://a.example/1", "page one"), ("https://a.example/2", "page two")],
                failed=[("https://bad.example", "Failed to fetch url")],
            ),
        )
    )
    response = tavily.extract(
        ["https://a.example/1", "https://a.example/2", "https://bad.example"],
        extract_depth="advanced",
        query="3pl fees",
        chunks_per_source=3,
        timeout_s=20,
    )
    request = route.calls.last.request
    assert str(request.url) == "https://api.tavily.com/extract"
    assert request.headers["authorization"] == f"Bearer {TEST_KEY}"
    assert _body(route) == {
        "urls": ["https://a.example/1", "https://a.example/2", "https://bad.example"],
        "extract_depth": "advanced",
        "format": "markdown",
        "include_usage": True,
        "query": "3pl fees",
        "chunks_per_source": 3,
        "timeout": 20,
    }
    assert [r.raw_content for r in response.results] == ["page one", "page two"]
    assert response.failed_results[0].url == "https://bad.example"
    # 2 successful advanced URLs at 2 credits per 5 URLs; failures are free.
    assert response.credits == pytest.approx(0.8)
    assert response.credits_estimated


def test_extract_accepts_single_url_and_reported_usage(tavily_api, tavily):
    route = tavily_api.post("/extract").mock(
        return_value=httpx.Response(200, json=extract_payload([("https://a.example", "x")], credits=1))
    )
    response = tavily.extract("https://a.example")
    assert _body(route)["urls"] == ["https://a.example"]
    assert (response.credits, response.credits_estimated) == (1, False)


def test_base_url_comes_from_settings():
    with respx.mock(base_url="https://proxy.example/tavily") as router:
        route = router.post("/search").mock(return_value=httpx.Response(200, json=search_payload("q", [])))
        client = TavilyClient(
            Settings(tavily_api_key=TEST_KEY, tavily_base_url="https://proxy.example/tavily/")
        )
        client.search("q")
        assert str(route.calls.last.request.url) == "https://proxy.example/tavily/search"


def test_retries_on_429_then_succeeds_honouring_retry_after(tavily_api, tavily, sleeps):
    route = tavily_api.post("/search").mock(
        side_effect=[
            httpx.Response(429, json=error_payload("slow down"), headers={"Retry-After": "3"}),
            httpx.Response(200, json=search_payload("q", [search_result("https://a.example")])),
        ]
    )
    response = tavily.search("q")
    assert route.call_count == 2
    assert sleeps == [3.0]
    assert len(response.results) == 1
    assert len(tavily.ledger.charges()) == 1  # a retried call is billed once


def test_retry_after_is_capped(tavily_api, sleeps):
    client = TavilyClient(
        Settings(tavily_api_key=TEST_KEY), max_retries=1, max_retry_after_s=5, sleep=sleeps.append
    )
    tavily_api.post("/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3600"}),
            httpx.Response(200, json=search_payload("q", [])),
        ]
    )
    client.search("q")
    assert sleeps == [5.0]


def test_retry_after_http_date_in_the_past_means_no_wait(tavily_api, tavily, sleeps):
    tavily_api.post("/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
            httpx.Response(200, json=search_payload("q", [])),
        ]
    )
    tavily.search("q")
    assert sleeps == [0.0]


def test_retries_5xx_with_exponential_backoff(tavily_api, tavily, sleeps):
    route = tavily_api.post("/search").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(502, json=error_payload("bad gateway")),
            httpx.Response(200, json=search_payload("q", [])),
        ]
    )
    tavily.search("q")
    assert route.call_count == 3
    # base 0.5s doubling, with jitter in [delay/2, delay]
    assert 0.25 <= sleeps[0] <= 0.5
    assert 0.5 <= sleeps[1] <= 1.0


def test_gives_up_after_max_retries(tavily_api, tavily, sleeps):
    route = tavily_api.post("/search").mock(
        return_value=httpx.Response(429, json=error_payload("Your request has been blocked"))
    )
    with pytest.raises(TavilyRateLimitError) as info:
        tavily.search("q")
    assert route.call_count == 4  # 1 + max_retries
    assert len(sleeps) == 3
    assert info.value.status_code == 429
    assert "blocked" in str(info.value)
    assert tavily.ledger.charges() == []


def test_timeouts_are_retried_then_raised(tavily_api, tavily):
    route = tavily_api.post("/search").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TavilyTransportError, match="ReadTimeout"):
        tavily.search("q")
    assert route.call_count == 4


def test_connect_error_then_success(tavily_api, tavily, sleeps):
    tavily_api.post("/search").mock(
        side_effect=[httpx.ConnectError("down"), httpx.Response(200, json=search_payload("q", []))]
    )
    assert tavily.search("q").query == "q"
    assert len(sleeps) == 1


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, TavilyBadRequestError),
        (401, TavilyAuthError),
        (432, TavilyCreditsExhaustedError),
        (433, TavilyCreditsExhaustedError),
    ],
)
def test_non_retryable_errors_map_to_types(tavily_api, tavily, sleeps, status, error_type):
    route = tavily_api.post("/search").mock(
        return_value=httpx.Response(status, json={"detail": {"error": f"msg {status}"}, "request_id": "r-9"})
    )
    with pytest.raises(error_type) as info:
        tavily.search("q")
    assert route.call_count == 1
    assert sleeps == []
    assert info.value.status_code == status
    assert info.value.request_id == "r-9"
    assert f"msg {status}" in info.value.message
    assert isinstance(info.value, TavilyError)


def test_validation_error_list_is_readable(tavily_api, tavily):
    tavily_api.post("/search").mock(
        return_value=httpx.Response(
            422, json={"detail": [{"type": "missing", "loc": ["body", "query"], "msg": "Field required"}]}
        )
    )
    with pytest.raises(TavilyBadRequestError, match="body.query: Field required"):
        tavily.search("q")


def test_persistent_500_becomes_server_error(tavily_api, sleeps):
    client = TavilyClient(Settings(tavily_api_key=TEST_KEY), max_retries=1, sleep=sleeps.append)
    tavily_api.post("/extract").mock(
        return_value=httpx.Response(500, json=error_payload("Internal Server Error"))
    )
    with pytest.raises(TavilyServerError, match="Internal Server Error"):
        client.extract("https://a.example")


def test_non_json_or_malformed_success_body_is_a_server_error(tavily_api, tavily):
    tavily_api.post("/search").mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(TavilyServerError, match="non-JSON"):
        tavily.search("q")
    tavily_api.post("/search").mock(return_value=httpx.Response(200, json={"results": "nope"}))
    with pytest.raises(TavilyServerError, match="unexpected"):
        tavily.search("q")


def test_missing_key_raises_before_any_request(tavily_api):
    route = tavily_api.post("/search")
    client = TavilyClient(Settings())  # construction itself needs no key
    with pytest.raises(MissingCredentialsError, match="TAVILY_API_KEY"):
        client.search("q")
    assert not route.called


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.search("   "),
        lambda c: c.search("q", max_results=21),
        lambda c: c.search("q", chunks_per_source=4),
        lambda c: c.extract([]),
        lambda c: c.extract([f"https://a.example/{i}" for i in range(21)]),
        lambda c: c.extract("https://a.example", chunks_per_source=2),
        lambda c: c.extract("https://a.example", timeout_s=90),
    ],
)
def test_invalid_arguments_fail_locally(tavily_api, tavily, call):
    with pytest.raises(ValueError):
        call(tavily)
    assert not tavily_api.calls


def test_ledger_accounts_credits_per_call(tavily_api, tavily):
    tavily_api.post("/search").mock(
        side_effect=[
            httpx.Response(200, json=search_payload("a", [], credits=1)),
            httpx.Response(200, json=search_payload("b", [], credits=None)),
        ]
    )
    tavily_api.post("/extract").mock(
        return_value=httpx.Response(200, json=extract_payload([("https://a.example", "x")]))
    )
    tavily.search("a")
    tavily.search("b", search_depth="advanced")
    tavily.extract(["https://a.example", "https://b.example"])

    charges = tavily.ledger.charges()
    assert [(c.endpoint, c.label, c.credits, c.estimated) for c in charges] == [
        ("search", "a", 1, False),
        ("search", "b", 2, True),
        ("extract", "https://a.example (+1 more)", pytest.approx(0.2), True),
    ]
    summary = tavily.ledger.summary(usd_per_credit=0.01)
    assert summary.calls == 3
    assert summary.cache_hits == 0
    assert summary.credits == pytest.approx(3.2)
    assert summary.usd_estimate == pytest.approx(0.032)
