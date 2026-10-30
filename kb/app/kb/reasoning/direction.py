"""Direction words of a question and of a candidate subject (rise / fall).

"Why did incidents rise?" must not be grounded on "Incidents fell in February 2027": a candidate whose direction is
the opposite of the question's ranks below every candidate with the same direction or with none, and is never the
subject on its own (no subject is better than the opposite one). Deterministic word lists, English only.
"""

from __future__ import annotations

import re

from kb.text import norm

UP, DOWN = "up", "down"
_UP = (
    "rise rises rose risen rising increase increases increased increasing grow grows grew grown growing growth "
    "up higher climb climbs climbed climbing jump jumps jumped spike spikes spiked surge surges surged soar soared"
)
_DOWN = (
    "fall falls fell fallen falling drop drops dropped dropping decline declines declined declining decrease "
    "decreases decreased decreasing down lower shrink shrinks shrank shrunk dip dips dipped slump slumped "
    "plunge plunged"
)
_WORDS = {w: UP for w in _UP.split()} | {w: DOWN for w in _DOWN.split()}
# «set up», «follow-up», «slow down»: phrasal verbs, not a direction
_PHRASAL = re.compile(
    r"\b(set|follow|sign|back|pick|look|break|shut|wind|slow|cut|wrap|take|step|turn|write|clean|show|stand|sit)"
    r"[\s-]+(up|down)\b"
)


def direction_of(text: str | None) -> str | None:
    """``up``, ``down``, or None (no direction word, or both)."""
    if not text:
        return None
    found = {_WORDS[w] for w in re.findall(r"[a-z]+", _PHRASAL.sub(" ", norm(text))) if w in _WORDS}
    return found.pop() if len(found) == 1 else None


def opposite(direction: str | None) -> str | None:
    return {UP: DOWN, DOWN: UP}.get(direction or "")


def split_by_direction(question: str | None, candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """(candidates with the question's direction or none, in their order; candidates with the opposite one)."""
    against = opposite(direction_of(question))
    if not against:
        return list(candidates), []
    keep = [c for c in candidates if direction_of(c.get("label")) != against]
    return keep, [c for c in candidates if direction_of(c.get("label")) == against]
