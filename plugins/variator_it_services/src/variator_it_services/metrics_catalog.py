"""Offline SDLC metric catalog: DevLake, DORA, OpenSSF Scorecard and CHAOSS definitions.

Each metric has `doc_url` (human page) and `source_url` (pinned version) so that a person can
check a number against its definition. Rebuild the data: `scripts/build_sdlc_catalog.py`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOG = Path(__file__).parent / "data" / "sdlc_metrics.json"
SHORT = ("id", "name", "source", "group", "unit", "direction", "definition", "doc_url")


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _by_id() -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in load_catalog()["metrics"]}


def metric(metric_id: str) -> dict[str, Any]:
    """Full entry of one metric; KeyError with close IDs when the ID is unknown."""
    try:
        return _by_id()[metric_id]
    except KeyError:
        near = [k for k in _by_id() if metric_id.lower() in k][:5]
        raise KeyError(f"unknown metric {metric_id!r}; close: {near}") from None


def search(
    text: str = "", *, source: str | None = None, group: str | None = None, used_by: str | None = None
) -> list[dict[str, Any]]:
    """Short entries that match all filters; `text` looks in the ID, name and definition."""
    text = text.lower()
    found = []
    for m in load_catalog()["metrics"]:
        if source and m["source"] != source or group and m["group"] != group:
            continue
        if used_by and not any(u.startswith(used_by) for u in m.get("used_by", [])):
            continue
        if text and text not in f"{m['id']} {m['name']} {m['definition']}".lower():
            continue
        found.append({k: m[k] for k in SHORT})
    return found


def cite(metric_id: str) -> dict[str, str]:
    """Reference for a decision or a drill-down: name, source, human page and pinned page."""
    m = metric(metric_id)
    src = load_catalog()["sources"][m["source"]]
    return {
        "metric": m["name"],
        "source": src["name"],
        "doc_url": m["doc_url"],
        "source_url": m["source_url"],
    }


def references(metric_ids: list[str]) -> list[dict[str, str]]:
    """One reference per metric, in the given order, without duplicates."""
    return [cite(k) for k in dict.fromkeys(metric_ids)]
