"""External market signals: Tavily web search + Nemotron fact extraction with verified citations."""

from variator.research.cache import CacheEntry, TavilyCache, cache_key
from variator.research.credits import (
    USD_PER_CREDIT,
    CreditCharge,
    CreditLedger,
    CreditSummary,
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
    CitedFact,
    DroppedFact,
    ExtractResponse,
    ExtractResult,
    FailedExtraction,
    ResearchBundle,
    ResearchQuery,
    SearchResponse,
    SearchResult,
    SourceDocument,
)
from variator.research.pipeline import (
    dedupe_facts,
    gather_external_facts,
    merge_results,
    normalize_url,
    plan_queries,
    validate_fact,
)
from variator.research.profile import business_profile_queries, profile_lenses
from variator.research.tavily import TavilyClient

__all__ = [
    "USD_PER_CREDIT",
    "CacheEntry",
    "CitedFact",
    "CreditCharge",
    "CreditLedger",
    "CreditSummary",
    "DroppedFact",
    "ExtractResponse",
    "ExtractResult",
    "FailedExtraction",
    "OfflineCacheMissError",
    "ResearchBundle",
    "ResearchQuery",
    "SearchResponse",
    "SearchResult",
    "SourceDocument",
    "TavilyAuthError",
    "TavilyBadRequestError",
    "TavilyCache",
    "TavilyClient",
    "TavilyCreditsExhaustedError",
    "TavilyError",
    "TavilyRateLimitError",
    "TavilyServerError",
    "TavilyTransportError",
    "cache_key",
    "business_profile_queries",
    "dedupe_facts",
    "estimate_extract_credits",
    "estimate_search_credits",
    "gather_external_facts",
    "merge_results",
    "normalize_url",
    "plan_queries",
    "profile_lenses",
    "validate_fact",
]
