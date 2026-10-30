"""Tavily credit prices and a running ledger, so the UI can show what research cost.

Prices from https://docs.tavily.com/documentation/api-credits (checked 2026-10-25):
search basic/fast/ultra-fast = 1 credit, advanced = 2; extract = 1 credit (basic) or
2 credits (advanced) per 5 *successful* URLs, failed URLs are free.
"""

from __future__ import annotations

import threading
from typing import Literal

from pydantic import BaseModel

from variator.research.models import ExtractDepth, SearchDepth

SEARCH_CREDITS: dict[str, float] = {"basic": 1.0, "fast": 1.0, "ultra-fast": 1.0, "advanced": 2.0}
EXTRACT_CREDITS_PER_5_URLS: dict[str, float] = {"basic": 1.0, "advanced": 2.0}

# ASSUMPTION: pay-as-you-go price per credit; monthly plans are cheaper ($0.005-0.0075).
# Only used for a rough USD figure in the UI. (https://www.tavily.com/pricing)
USD_PER_CREDIT = 0.008

Endpoint = Literal["search", "extract"]


def estimate_search_credits(search_depth: SearchDepth) -> float:
    """Docs price of one search request."""
    return SEARCH_CREDITS.get(search_depth, 1.0)


def estimate_extract_credits(successful_urls: int, extract_depth: ExtractDepth) -> float:
    """Docs price of an extract request, pro rata.

    ASSUMPTION: Tavily bills per 5 successful URLs across the account, so a 2-URL call
    is shown as 0.4 credits instead of rounding up
    (https://docs.tavily.com/documentation/api-credits does not say how partial batches round).
    """
    return EXTRACT_CREDITS_PER_5_URLS.get(extract_depth, 1.0) * max(successful_urls, 0) / 5


class CreditCharge(BaseModel):
    """One Tavily call as seen by the user."""

    endpoint: Endpoint
    label: str  # the query, or the first URL for extract
    credits: float  # 0 for cache hits
    estimated: bool  # True when Tavily did not report usage
    from_cache: bool
    credits_saved: float = 0.0  # what a cache hit would have cost live


class CreditSummary(BaseModel):
    calls: int  # live network calls
    cache_hits: int
    credits: float
    credits_saved: float
    usd_estimate: float


class CreditLedger:
    """Thread-safe log of Tavily charges; share one per session or per research run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._charges: list[CreditCharge] = []

    def record(self, charge: CreditCharge) -> None:
        with self._lock:
            self._charges.append(charge)

    def charges(self) -> list[CreditCharge]:
        with self._lock:
            return list(self._charges)

    @property
    def total_credits(self) -> float:
        return sum(c.credits for c in self.charges())

    def summary(self, usd_per_credit: float = USD_PER_CREDIT) -> CreditSummary:
        """Totals for the UI. ``usd_estimate`` is approximate (see ``USD_PER_CREDIT``)."""
        charges = self.charges()
        credits = sum(c.credits for c in charges)
        return CreditSummary(
            calls=sum(not c.from_cache for c in charges),
            cache_hits=sum(c.from_cache for c in charges),
            credits=credits,
            credits_saved=sum(c.credits_saved for c in charges),
            usd_estimate=round(credits * usd_per_credit, 6),
        )
