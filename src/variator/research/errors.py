"""Typed errors for the Tavily integration.

Status codes follow the Tavily API reference
(https://docs.tavily.com/documentation/api-reference/endpoint/search):
400 bad request, 401 bad key, 422 validation, 429 rate limit,
432 plan/key credit limit, 433 pay-as-you-go limit, 5xx server errors.
"""

from __future__ import annotations


class TavilyError(RuntimeError):
    """Base class for every Tavily failure. ``status_code`` is None for non-HTTP failures."""

    def __init__(self, message: str, *, status_code: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.request_id = request_id


class TavilyAuthError(TavilyError):
    """401: the API key is missing or invalid."""


class TavilyCreditsExhaustedError(TavilyError):
    """432 (plan or key limit) or 433 (pay-as-you-go limit): no credits left, do not retry."""


class TavilyBadRequestError(TavilyError):
    """400 or 422: the request itself is wrong; retrying will not help."""


class TavilyRateLimitError(TavilyError):
    """429 that persisted after all retries."""


class TavilyServerError(TavilyError):
    """5xx (or an unexpected status) that persisted after all retries."""


class TavilyTransportError(TavilyError):
    """Timeout or network failure that persisted after all retries."""


class OfflineCacheMissError(TavilyError):
    """Offline mode is on and the request is not in the cache, so no live call is made."""
