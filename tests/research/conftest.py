from __future__ import annotations

import pytest
import respx

from variator.config import Settings
from variator.research import TavilyClient
from variator.research.testing import FakeTavily

TAVILY_BASE = "https://api.tavily.com"
TEST_KEY = "tvly-test-key"


@pytest.fixture
def tavily_api():
    """respx router for the real Tavily base URL; unmatched requests fail the test."""
    with respx.mock(base_url=TAVILY_BASE, assert_all_called=False) as router:
        yield router


@pytest.fixture
def sleeps() -> list[float]:
    """Delays the client would have slept for (no real sleeping in tests)."""
    return []


@pytest.fixture
def tavily(sleeps: list[float]):
    client = TavilyClient(Settings(tavily_api_key=TEST_KEY, max_retries=3), sleep=sleeps.append)
    yield client
    client.close()


@pytest.fixture
def fake_tavily() -> FakeTavily:
    return FakeTavily()
