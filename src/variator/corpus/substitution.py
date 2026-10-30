"""Seeded, reversible entity substitution dictionary."""

from __future__ import annotations

import random
import re
from typing import Any


def build_entity_map(seed: int, replacements: dict[str, str]) -> dict[str, Any]:
    """Build a deterministic entity replacement map."""
    rng = random.Random(seed)
    ordered = dict(sorted(replacements.items()))
    return {
        "seed": seed,
        "rules_version": "1",
        "replacements": ordered,
        "rng_state": rng.getstate()[1][:4],
    }


def substitute_entities(text: str, replacements: dict[str, str]) -> tuple[str, list[str]]:
    """Apply longest-first case-aware whole-entity replacements."""
    applied: list[str] = []
    result = text
    for source in sorted(replacements, key=lambda value: (-len(value), value.casefold())):
        pattern = re.compile(rf"(?<!\w){re.escape(source)}(?P<suffix>'s|s')?(?!\w)", re.IGNORECASE)

        def replace(
            match: re.Match[str],
            source: str = source,
            replacement: str = replacements[source],
        ) -> str:
            applied.append(source)
            return replacement + (match.group("suffix") or "")

        result = pattern.sub(replace, result)
    return result, applied
