"""Shared fixtures: the generated history (v2 when present, else v1); tests skip without it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

CANDIDATES = [
    os.environ.get("VARIATOR_ACTORS_HISTORY", ""),
    "/tmp/variator-1000/mvp-history-v2",
    "/tmp/variator-1000/mvp-history",
]


def history_dir() -> Path | None:
    for c in CANDIDATES:
        if c and (Path(c) / "datasets").is_dir():
            return Path(c)
    return None


@pytest.fixture(scope="session")
def history() -> Path:
    d = history_dir()
    if d is None:
        pytest.skip("no generated history directory")
    return d


@pytest.fixture(scope="session")
def learned(history):
    from variator.actors.learn import learn

    return learn(history)
