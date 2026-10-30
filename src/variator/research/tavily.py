"""Tavily REST client over httpx: search and extract with retries, typed errors,
an optional response cache and per-call credit accounting.

API reference (checked 2026-10-25):
- POST {base}/search  https://docs.tavily.com/documentation/api-reference/endpoint/search
- POST {base}/extract https://docs.tavily.com/documentation/api-reference/endpoint/extract
Auth is ``Authorization: Bearer tvly-...``; errors are ``{"detail": {"error": "..."}}``
(422 uses a ``detail`` list); 429 carries ``Retry-After`` in seconds.
"""

from __future__ import annotations

import email.utils
import json
import logging
import math
import random
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from variator.config import Settings
from variator.research.cache import CacheEntry, TavilyCache
from variator.research.credits import (
    CreditCharge,
    CreditLedger,
    Endpoint,
    estimate_extract_credits,
    estimate_search_credits,
)
from variator.research.errors import (
    OfflineCacheMissError,
    TavilyAuthError,
    TavilyBadRequestError,
    TavilyCreditsExhaustedError,
    TavilyError,
    TavilyRateLimitError,
    TavilyServerError,
    TavilyTransportError,
)
from variator.research.models import (
    AnswerMode,
    ExtractDepth,
    ExtractResponse,
    RawContentMode,
    SearchDepth,
    SearchResponse,
    TavilyResponse,
    TimeRange,
    Topic,
)

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_SEARCH_RESULTS = 20
MAX_EXTRACT_URLS = 20
MAX_INCLUDE_DOMAINS = 300
MAX_EXCLUDE_DOMAINS = 150


class TavilyClient:
    """Synchronous Tavily client.

    The key is read lazily: a client without ``TAVILY_API_KEY`` still serves cache hits
    (offline demos) and raises ``MissingCredentialsError`` only before a live call.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        api_key: str | None = None,
        http_client: httpx.Client | None = None,
        cache: TavilyCache | None = None,
        ledger: CreditLedger | None = None,
        timeout_s: float = 60.0,
        max_retries: int | None = None,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 20.0,
        max_retry_after_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.cache = cache
        self.ledger = ledger or CreditLedger()
        self.timeout_s = timeout_s
        self.max_retries = self.settings.max_retries if max_retries is None else max_retries
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.max_retry_after_s = max_retry_after_s
        self._api_key = api_key or self.settings.tavily_api_key
        self._base_url = self.settings.tavily_base_url.rstrip("/")
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout_s)
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> TavilyClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def search(
        self,
        query: str,
        *,
        search_depth: SearchDepth = "basic",
        topic: Topic = "general",
        max_results: int = 5,
        time_range: TimeRange | None = None,
        include_domains: Sequence[str] | None = None,
        exclude_domains: Sequence[str] | None = None,
        include_answer: AnswerMode = False,
        include_raw_content: RawContentMode = False,
        chunks_per_source: int | None = None,
        country: str | None = None,
    ) -> SearchResponse:
        """Run one web search (1 credit; 2 for ``search_depth="advanced"``).

        ``chunks_per_source`` (1-3) applies to basic/fast/advanced depths; ``country``
        boosts one country and only works with ``topic="general"`` (per the docs).
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 0 <= max_results <= MAX_SEARCH_RESULTS:
            raise ValueError(f"max_results must be 0..{MAX_SEARCH_RESULTS}")
        if chunks_per_source is not None and not 1 <= chunks_per_source <= 3:
            raise ValueError("chunks_per_source must be 1..3")
        if include_domains and len(include_domains) > MAX_INCLUDE_DOMAINS:
            raise ValueError(f"at most {MAX_INCLUDE_DOMAINS} include_domains")
        if exclude_domains and len(exclude_domains) > MAX_EXCLUDE_DOMAINS:
            raise ValueError(f"at most {MAX_EXCLUDE_DOMAINS} exclude_domains")

        payload: dict[str, Any] = {
            "query": query,
            "search_depth": search_depth,
            "topic": topic,
            "max_results": max_results,
            "include_answer": include_answer,
            "include_raw_content": include_raw_content,
            # Ask Tavily to report the real charge instead of relying on our price table.
            "include_usage": True,
        }
        optional = {
            "time_range": time_range,
            "include_domains": list(include_domains) if include_domains else None,
            "exclude_domains": list(exclude_domains) if exclude_domains else None,
            "chunks_per_source": chunks_per_source,
            "country": country,
        }
        payload.update({k: v for k, v in optional.items() if v is not None})
        return self._call(
            "search",
            payload,
            label=query,
            parse=SearchResponse.model_validate,
            estimate=lambda _: estimate_search_credits(search_depth),
        )

    def extract(
        self,
        urls: Sequence[str] | str,
        *,
        extract_depth: ExtractDepth = "basic",
        query: str | None = None,
        chunks_per_source: int | None = None,
        format: Literal["markdown", "text"] = "markdown",
        timeout_s: float | None = None,
    ) -> ExtractResponse:
        """Fetch page content for 1-20 URLs (1 credit per 5 successful URLs; 2 if advanced).

        With ``query`` Tavily returns only the top ``chunks_per_source`` (1-5) relevant
        chunks per page, which keeps downstream LLM prompts small. Per-URL failures come
        back in ``failed_results`` with HTTP 200 and are not billed.
        """
        url_list = [urls] if isinstance(urls, str) else list(urls)
        if not 1 <= len(url_list) <= MAX_EXTRACT_URLS:
            raise ValueError(f"extract takes 1..{MAX_EXTRACT_URLS} urls")
        if chunks_per_source is not None:
            if query is None:
                raise ValueError("chunks_per_source requires query")
            if not 1 <= chunks_per_source <= 5:
                raise ValueError("chunks_per_source must be 1..5")
        if timeout_s is not None and not 1.0 <= timeout_s <= 60.0:
            raise ValueError("timeout_s must be 1..60 seconds")

        payload: dict[str, Any] = {
            "urls": url_list,
            "extract_depth": extract_depth,
            "format": format,
            "include_usage": True,
        }
        optional = {"query": query, "chunks_per_source": chunks_per_source, "timeout": timeout_s}
        payload.update({k: v for k, v in optional.items() if v is not None})
        label = url_list[0] + (f" (+{len(url_list) - 1} more)" if len(url_list) > 1 else "")
        # The server-side timeout must expire before ours, or we would retry a call that succeeded.
        http_timeout = max(self.timeout_s, timeout_s + 10) if timeout_s else None
        return self._call(
            "extract",
            payload,
            label=label,
            parse=ExtractResponse.model_validate,
            estimate=lambda r: estimate_extract_credits(len(r.results), extract_depth),
            http_timeout=http_timeout,
        )

    def _call[M: TavilyResponse](
        self,
        endpoint: Endpoint,
        payload: dict[str, Any],
        *,
        label: str,
        parse: Callable[[Any], M],
        estimate: Callable[[M], float],
        http_timeout: float | None = None,
    ) -> M:
        if self.cache is not None:
            entry = self.cache.get(endpoint, payload)  # raises OfflineCacheMissError when offline
            response = self._parse_cached(entry, parse) if entry is not None else None
            if entry is not None and response is not None:
                response.credits, response.credits_estimated, response.from_cache = 0.0, False, True
                self.ledger.record(
                    CreditCharge(
                        endpoint=endpoint,
                        label=label,
                        credits=0.0,
                        estimated=False,
                        from_cache=True,
                        credits_saved=entry.credits,
                    )
                )
                return response

        data = self._post(endpoint, payload, timeout=http_timeout)
        try:
            response = parse(data)
        except ValidationError as exc:
            raise TavilyServerError(f"unexpected Tavily {endpoint} response: {exc}") from exc
        reported = response.usage.credits if response.usage else None
        response.credits = reported if reported is not None else estimate(response)
        response.credits_estimated = reported is None
        self.ledger.record(
            CreditCharge(
                endpoint=endpoint,
                label=label,
                credits=response.credits,
                estimated=response.credits_estimated,
                from_cache=False,
            )
        )
        if self.cache is not None:
            self.cache.put(endpoint, payload, data, response.credits)
        return response

    def _parse_cached[M: TavilyResponse](self, entry: CacheEntry, parse: Callable[[Any], M]) -> M | None:
        try:
            return parse(entry.response)
        except ValidationError as exc:
            if self.cache is not None and self.cache.offline:
                raise OfflineCacheMissError(f"offline mode: unreadable cached {entry.endpoint}") from exc
            log.warning("ignoring unparsable cached Tavily %s response: %s", entry.endpoint, exc)
            return None

    def _require_key(self) -> str:
        return self._api_key or self.settings.require("tavily_api_key")

    def _post(self, endpoint: str, payload: dict[str, Any], *, timeout: float | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._require_key()}", "Content-Type": "application/json"}
        url = f"{self._base_url}/{endpoint}"
        attempt = 0
        while True:
            try:
                response = self._http.post(
                    url, json=payload, headers=headers, timeout=timeout or self.timeout_s
                )
            except httpx.TransportError as exc:  # includes timeouts
                if attempt >= self.max_retries:
                    raise TavilyTransportError(
                        f"Tavily {endpoint} failed after {attempt + 1} attempts: {type(exc).__name__}: {exc}"
                    ) from exc
                delay = self._backoff(attempt)
                log.warning("Tavily %s: %s, retrying in %.1fs", endpoint, type(exc).__name__, delay)
            else:
                if response.is_success:
                    try:
                        return response.json()
                    except json.JSONDecodeError as exc:
                        raise TavilyServerError(
                            f"Tavily {endpoint} returned non-JSON body", status_code=response.status_code
                        ) from exc
                if response.status_code not in RETRYABLE_STATUS or attempt >= self.max_retries:
                    raise error_from_response(response)
                delay = self._retry_after(response)
                if delay is None:
                    delay = self._backoff(attempt)
                log.warning("Tavily %s: HTTP %s, retrying in %.1fs", endpoint, response.status_code, delay)
            self._sleep(delay)
            attempt += 1

    def _backoff(self, attempt: int) -> float:
        capped = min(self.backoff_max_s, self.backoff_base_s * 2**attempt)
        return capped / 2 + random.uniform(0, capped / 2)

    def _retry_after(self, response: httpx.Response) -> float | None:
        """Seconds from ``Retry-After`` (delta-seconds or HTTP-date), capped; None if absent."""
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            seconds = float(raw)
            if not math.isfinite(seconds):
                return None
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - datetime.now(UTC)).total_seconds()
        return min(max(seconds, 0.0), self.max_retry_after_s)


def _error_message(response: httpx.Response) -> tuple[str, str | None]:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return (response.text.strip()[:300] or response.reason_phrase), None
    request_id = body.get("request_id") if isinstance(body, dict) else None
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        message = detail.get("error") or json.dumps(detail)[:300]
    elif isinstance(detail, list):  # 422 validation errors
        message = "; ".join(
            f"{'.'.join(str(p) for p in item.get('loc', []))}: {item.get('msg', '')}"
            if isinstance(item, dict)
            else str(item)
            for item in detail
        )
    else:
        message = str(detail)
    return message, request_id


def error_from_response(response: httpx.Response) -> TavilyError:
    """Map a failed Tavily response to a typed error."""
    status = response.status_code
    message, request_id = _error_message(response)
    text = f"Tavily HTTP {status}: {message}"
    cls: type[TavilyError]
    if status == 401:
        cls = TavilyAuthError
    elif status == 403:
        # Not in the API reference; a forbidden key is still a credentials problem.
        cls = TavilyAuthError
    elif status in (432, 433):
        cls = TavilyCreditsExhaustedError
    elif status in (400, 422):
        cls = TavilyBadRequestError
    elif status == 429:
        cls = TavilyRateLimitError
    else:
        cls = TavilyServerError
    return cls(text, status_code=status, request_id=request_id)
