"""Smoke tests against the real Tavily and Token Factory APIs (run with ``--live``).

Cost per run: about 3 Tavily credits and a few cents of Nemotron Lightning tokens.
"""

from __future__ import annotations

import pytest

from variator.config import Settings
from variator.domain import FactOrigin
from variator.llm import TokenFactoryClient
from variator.research import TavilyClient, gather_external_facts, normalize_url


@pytest.mark.live
def test_live_basic_search():
    with TavilyClient(Settings.from_env()) as tavily:
        response = tavily.search("warehouse rent growth Poland 2026", max_results=3)
    assert response.results, "expected at least one result"
    assert all(r.url.startswith("http") for r in response.results)
    assert response.credits >= 1
    assert not response.credits_estimated, "include_usage should make Tavily report credits"


@pytest.mark.live
def test_live_pipeline():
    settings = Settings.from_env()
    with TavilyClient(settings) as tavily:
        bundle = gather_external_facts(
            "Should a mid-size e-commerce company open a second fulfilment warehouse in Poland in 2027?",
            tf_client=TokenFactoryClient(settings),
            tavily=tavily,
            max_queries=2,
            results_per_query=3,
        )
    assert bundle.queries and bundle.results, bundle.warnings
    result_urls = {normalize_url(d.url) for d in bundle.results}
    for fact in bundle.facts:
        assert fact.origin is FactOrigin.WEB
        assert normalize_url(fact.source or "") in result_urls
    assert bundle.tavily_credits >= 1
