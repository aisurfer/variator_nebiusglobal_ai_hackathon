"""Shared fixtures of the future engine: the learned actors and the history; tests skip without them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ACTORS = Path(os.environ.get("VARIATOR_FUTURE_ACTORS", "/tmp/variator-1000/mvp-artifacts/actors"))
HISTORY = Path(os.environ.get("VARIATOR_FUTURE_HISTORY", "/tmp/variator-1000/mvp-history-mgmt"))


@pytest.fixture(scope="session")
def company():
    if not (ACTORS / "habits.json").is_file() or not (HISTORY / "datasets").is_dir():
        pytest.skip("no learned actors or no history")
    from variator.future.company import CompanyModel
    from variator.future.forks import case_config
    from variator.future.params import Habits
    from variator.future.state import build_world

    habits = Habits.load(ACTORS)
    cfg = case_config("company_best_year")
    world = build_world(habits, HISTORY)
    return CompanyModel(habits, world, cfg)
