"""Explicitly gated smoke checks for configured live corpus source locators."""

import os
from pathlib import Path

import pytest

from variator.corpus.acquisition import download_artifact
from variator.corpus.reddit import FIXED_SUBREDDITS, load_fixed_reddit_posts

LIVE_SOURCES = (
    "enterprise_rag",
    "solstice",
    "cc_news",
    "amazon_reviews_2023",
    "reddit",
    "github",
)


@pytest.mark.parametrize("source", LIVE_SOURCES)
@pytest.mark.live
def test_live_source_artifact_is_reachable_when_configured(tmp_path: Path, source: str):
    """Fetch one configured artifact and verify that the cache publishes it."""
    url = os.getenv(f"VARIATOR_CORPUS_LIVE_{source.upper()}_URL")
    if not url:
        pytest.skip(f"VARIATOR_CORPUS_LIVE_{source.upper()}_URL is not configured")
    path = download_artifact(url=url, cache_dir=tmp_path, filename=f"{source}.artifact")
    assert path.exists() and path.stat().st_size > 0


def test_fixed_reddit_scope_is_explicitly_declared():
    assert FIXED_SUBREDDITS == ("LocalLLaMA", "MachineLearning", "MLOps", "selfhosted", "llama")
    fixture = Path(__file__).parents[1] / "fixtures" / "corpus" / "reddit.jsonl"
    assert len(load_fixed_reddit_posts(fixture)) == 5
