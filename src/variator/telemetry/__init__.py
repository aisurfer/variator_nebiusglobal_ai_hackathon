"""Run statistics: every stage of every run (tools, agent, LLM calls) in SQLite."""

import os
from pathlib import Path

from variator.telemetry.middleware import StatsMiddleware
from variator.telemetry.store import CURRENT, DEFAULT_STATS_PATH, ENV_STATS, StageRef, StatsStore


def stats_from_env() -> StatsStore | None:
    """``VARIATOR_STATS`` = path (default ``.variator/stats.sqlite``) or ``off``."""
    value = os.environ.get(ENV_STATS, str(DEFAULT_STATS_PATH))
    if value.lower() in ("off", "0", "none", ""):
        return None
    return StatsStore(Path(value))


__all__ = [
    "CURRENT",
    "DEFAULT_STATS_PATH",
    "ENV_STATS",
    "StageRef",
    "StatsMiddleware",
    "StatsStore",
    "stats_from_env",
]
