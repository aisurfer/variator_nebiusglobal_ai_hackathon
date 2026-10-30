from __future__ import annotations

import httpx
import pytest

from variator.config import Settings
from variator.research import (
    OfflineCacheMissError,
    TavilyBadRequestError,
    TavilyCache,
    TavilyClient,
    cache_key,
)
from variator.research.testing import extract_payload, search_payload, search_result

TEST_KEY = "tvly-test-key"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _client(cache: TavilyCache, *, key: str | None = TEST_KEY) -> TavilyClient:
    return TavilyClient(Settings(tavily_api_key=key, max_retries=0), cache=cache, sleep=lambda _: None)


def _mock_search(router, query: str = "q", credits: float = 1):
    body = search_payload(query, [search_result("https://a.example/x", content="12%")], credits=credits)
    return router.post("/search").mock(return_value=httpx.Response(200, json=body))


def test_cache_key_is_stable_and_order_independent():
    a = cache_key("search", {"query": "q", "max_results": 5})
    b = cache_key("search", {"max_results": 5, "query": "q"})
    assert a == b and len(a) == 64
    assert cache_key("search", {"query": "q", "max_results": 6}) != a
    assert cache_key("extract", {"query": "q", "max_results": 5}) != a


def test_miss_then_hit_saves_credits(tavily_api, tmp_path):
    route = _mock_search(tavily_api, credits=2)
    client = _client(TavilyCache(tmp_path))

    live = client.search("q")
    cached = client.search("q")

    assert route.call_count == 1
    assert (live.from_cache, live.credits) == (False, 2)
    assert (cached.from_cache, cached.credits) == (True, 0)
    assert cached.results == live.results
    summary = client.ledger.summary()
    assert (summary.calls, summary.cache_hits, summary.credits, summary.credits_saved) == (1, 1, 2, 2)
    assert len(list((tmp_path / "search").glob("*.json"))) == 1


def test_different_parameters_are_different_entries(tavily_api, tmp_path):
    route = _mock_search(tavily_api)
    client = _client(TavilyCache(tmp_path))
    client.search("q")
    client.search("q", max_results=3)
    assert route.call_count == 2


def test_extract_is_cached_too(tavily_api, tmp_path):
    route = tavily_api.post("/extract").mock(
        return_value=httpx.Response(200, json=extract_payload([("https://a.example", "text")], credits=1))
    )
    client = _client(TavilyCache(tmp_path))
    client.extract("https://a.example")
    again = client.extract("https://a.example")
    assert route.call_count == 1
    assert again.from_cache and again.results[0].raw_content == "text"


def test_entries_expire_after_ttl(tavily_api, tmp_path):
    route = _mock_search(tavily_api)
    clock = Clock()
    client = _client(TavilyCache(tmp_path, ttl_s=60, clock=clock))
    client.search("q")
    clock.now += 59
    assert client.search("q").from_cache
    clock.now += 2
    assert not client.search("q").from_cache
    assert route.call_count == 2


def test_ttl_none_never_expires(tavily_api, tmp_path):
    route = _mock_search(tavily_api)
    clock = Clock()
    client = _client(TavilyCache(tmp_path, ttl_s=None, clock=clock))
    client.search("q")
    clock.now += 10 * 365 * 24 * 3600
    assert client.search("q").from_cache
    assert route.call_count == 1


def test_offline_serves_stale_entries_without_key_or_network(tavily_api, tmp_path):
    clock = Clock()
    _mock_search(tavily_api)
    _client(TavilyCache(tmp_path, ttl_s=60, clock=clock)).search("q")
    tavily_api.reset()
    clock.now += 30 * 24 * 3600  # recorded a month before judging

    offline = _client(TavilyCache(tmp_path, ttl_s=60, offline=True, clock=clock), key=None)
    response = offline.search("q")

    assert response.from_cache
    assert response.results[0].url == "https://a.example/x"
    assert not tavily_api.calls


def test_offline_miss_raises_and_never_calls_the_api(tavily_api, tmp_path):
    route = _mock_search(tavily_api)
    offline = _client(TavilyCache(tmp_path, offline=True))
    with pytest.raises(OfflineCacheMissError, match="offline mode"):
        offline.search("never cached")
    assert not route.called
    assert offline.ledger.charges() == []


def test_offline_mode_never_writes(tmp_path):
    cache = TavilyCache(tmp_path, offline=True)
    cache.put("search", {"query": "q"}, {"query": "q", "results": []}, credits=1)
    assert not list(tmp_path.rglob("*.json"))


def test_cache_files_do_not_contain_the_api_key(tavily_api, tmp_path):
    _mock_search(tavily_api)
    _client(TavilyCache(tmp_path)).search("q")
    (path,) = tmp_path.rglob("*.json")
    text = path.read_text()
    assert TEST_KEY not in text
    assert '"query": "q"' in text


def test_corrupt_file_counts_as_a_miss(tavily_api, tmp_path):
    route = _mock_search(tavily_api)
    client = _client(TavilyCache(tmp_path))
    client.search("q")
    (payload_path,) = tmp_path.rglob("*.json")
    payload_path.write_text("{not json")
    assert not client.search("q").from_cache
    assert route.call_count == 2


def test_failed_calls_are_not_cached(tavily_api, tmp_path):
    tavily_api.post("/search").mock(return_value=httpx.Response(400, json={"detail": {"error": "bad"}}))
    client = _client(TavilyCache(tmp_path))
    with pytest.raises(TavilyBadRequestError, match="bad"):
        client.search("q")
    assert not list(tmp_path.rglob("*.json"))


def test_clear_removes_entries(tavily_api, tmp_path):
    _mock_search(tavily_api)
    cache = TavilyCache(tmp_path)
    client = _client(cache)
    client.search("q")
    client.search("q2")
    assert cache.clear() == 2
    assert not list(tmp_path.rglob("*.json"))


def test_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VARIATOR_TAVILY_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("VARIATOR_TAVILY_CACHE_TTL_S", "0")
    monkeypatch.setenv("VARIATOR_TAVILY_OFFLINE", "true")
    cache = TavilyCache.from_env()
    assert (cache.directory, cache.ttl_s, cache.offline) == (tmp_path / "c", None, True)

    for name in ("VARIATOR_TAVILY_CACHE_DIR", "VARIATOR_TAVILY_CACHE_TTL_S", "VARIATOR_TAVILY_OFFLINE"):
        monkeypatch.delenv(name)
    default = TavilyCache.from_env()
    assert default.offline is False and default.ttl_s == 7 * 24 * 3600
    assert default.directory.as_posix() == ".cache/tavily"


def test_unparsable_cached_response_is_refetched_online_and_a_miss_offline(tavily_api, tmp_path):
    route = _mock_search(tavily_api)
    payload = {
        "query": "q",
        "search_depth": "basic",
        "topic": "general",
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": False,
        "include_usage": True,
    }
    TavilyCache(tmp_path).put("search", payload, {"results": "old schema"}, credits=1)

    with pytest.raises(OfflineCacheMissError, match="unreadable"):
        _client(TavilyCache(tmp_path, offline=True), key=None).search("q")
    assert not route.called

    assert not _client(TavilyCache(tmp_path)).search("q").from_cache
    assert route.call_count == 1
