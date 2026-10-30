"""ConvoKit-style Reddit loading for the fixed technical subreddits."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXED_SUBREDDITS: tuple[str, ...] = (
    "LocalLLaMA",
    "MachineLearning",
    "MLOps",
    "selfhosted",
    "llama",
)


def load_reddit_posts(path: Path) -> list[dict[str, Any]]:
    """Load Reddit posts from a JSON file or directory of JSON files."""
    if path.is_dir():
        posts: list[dict[str, Any]] = []
        for file in sorted(path.glob("*.json")) + sorted(path.glob("*.jsonl")):
            posts.extend(load_reddit_posts(file))
        return posts
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "posts" in data:
        return list(data["posts"])
    return [data]


def load_fixed_reddit_posts(path: Path) -> list[dict[str, Any]]:
    """Load only the five technical ConvoKit subreddits in the Redwood scope."""
    return [post for post in load_reddit_posts(path) if str(post.get("subreddit", "")) in FIXED_SUBREDDITS]
