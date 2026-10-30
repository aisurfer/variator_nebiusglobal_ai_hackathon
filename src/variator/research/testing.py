"""Offline fake of the Tavily API for unit tests (here and in modules that use research).

Usage::

    fake = FakeTavily()
    fake.add_search("3pl fees germany 2026", [search_result("https://a.example/x", content="...")])
    tavily = fake.client()
    ...
    assert fake.requests[0] == ("search", {...request body...})

Unknown queries return no results; ``fail()`` makes a query or URL return an HTTP error.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from variator.config import Settings
from variator.research.tavily import TavilyClient


def search_result(
    url: str,
    *,
    title: str = "",
    content: str = "",
    score: float = 0.8,
    published_date: str | None = None,
) -> dict[str, Any]:
    """One item of a Tavily ``/search`` ``results`` array."""
    item: dict[str, Any] = {"title": title or url, "url": url, "content": content, "score": score}
    if published_date is not None:
        item["published_date"] = published_date
    return item


def search_payload(
    query: str,
    results: Sequence[dict[str, Any]],
    *,
    credits: float | None = 1,
    answer: str | None = None,
) -> dict[str, Any]:
    """A Tavily ``/search`` 200 body; ``credits=None`` omits ``usage``."""
    body: dict[str, Any] = {
        "query": query,
        "answer": answer,
        "images": [],
        "results": list(results),
        "response_time": 0.42,
        "request_id": "req-fake",
    }
    if credits is not None:
        body["usage"] = {"credits": credits}
    return body


def extract_payload(
    results: Sequence[tuple[str, str]],
    failed: Sequence[tuple[str, str]] = (),
    *,
    credits: float | None = None,
) -> dict[str, Any]:
    """A Tavily ``/extract`` 200 body from (url, raw_content) and (url, error) pairs."""
    body: dict[str, Any] = {
        "results": [{"url": url, "raw_content": text, "images": []} for url, text in results],
        "failed_results": [{"url": url, "error": error} for url, error in failed],
        "response_time": 0.9,
        "request_id": "req-fake",
    }
    if credits is not None:
        body["usage"] = {"credits": credits}
    return body


def error_payload(message: str) -> dict[str, Any]:
    """Tavily error body shape: ``{"detail": {"error": ...}}``."""
    return {"detail": {"error": message}}


class FakeTavily:
    """Canned Tavily search/extract behind an ``httpx.MockTransport``."""

    def __init__(self) -> None:
        self._search: dict[str, tuple[list[dict[str, Any]], float | None]] = {}
        self._extract: dict[str, str] = {}
        self._errors: dict[str, int] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def add_search(
        self, query: str, results: Sequence[dict[str, Any]], *, credits: float | None = 1
    ) -> FakeTavily:
        self._search[query] = (list(results), credits)
        return self

    def add_extract(self, url: str, raw_content: str) -> FakeTavily:
        self._extract[url] = raw_content
        return self

    def fail(self, query_or_url: str, status: int) -> FakeTavily:
        self._errors[query_or_url] = status
        return self

    def endpoint_requests(self, endpoint: str) -> list[dict[str, Any]]:
        return [body for name, body in self.requests if name == endpoint]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.requests.append((endpoint, body))
        if endpoint == "search":
            query = body["query"]
            if query in self._errors:
                return httpx.Response(self._errors[query], json=error_payload(f"fake error for {query}"))
            results, credits = self._search.get(query, ([], 1))
            return httpx.Response(200, json=search_payload(query, results, credits=credits))
        if endpoint == "extract":
            urls = body["urls"] if isinstance(body["urls"], list) else [body["urls"]]
            for url in urls:
                if url in self._errors:
                    return httpx.Response(self._errors[url], json=error_payload(f"fake error for {url}"))
            ok = [(u, self._extract[u]) for u in urls if u in self._extract]
            failed = [(u, "Failed to fetch url") for u in urls if u not in self._extract]
            return httpx.Response(200, json=extract_payload(ok, failed))
        return httpx.Response(404, json=error_payload(f"unknown endpoint {endpoint}"))

    def http_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def client(self, settings: Settings | None = None, **kwargs: Any) -> TavilyClient:
        """A ``TavilyClient`` wired to this fake (no retries, no sleeping, fake key)."""
        settings = settings or Settings(tavily_api_key="tvly-fake", max_retries=0)
        kwargs.setdefault("sleep", lambda _: None)
        return TavilyClient(settings, http_client=self.http_client(), **kwargs)
