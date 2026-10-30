"""Anti-hallucination guard: every number a fact claims must exist in its source.

Policy (documented in README):

* ``Fact.value`` must match a number from the source (table profile, text chunk
  or chart reading) within ``rel_tol`` (1% by default). Matching is on absolute
  values (``"fell 12%"`` stored as ``12``) and honours scale words in the unit
  (``value=1.2, unit="million USD"``). Unmatched facts are **dropped** by default
  (``policy="drop"``); ``policy="downgrade"`` keeps them with low confidence.
* Numbers inside ``Fact.statement`` are checked too, allowing for the rounding
  the text shows (``"1.2M"`` matches 1,234,567). Unmatched statement numbers only
  **downgrade** confidence: the extractor cannot tell a hallucination from a
  harmless number such as a store count, so dropping would lose real facts.
  Years (1900-2100) and small integers (<= 31: months, quarters, days) are skipped.
"""

from __future__ import annotations

import logging
import math
import re
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from variator.domain import Fact
from variator.ingestion.parsing import decimals_in, parse_body

log = logging.getLogger(__name__)

UnverifiedPolicy = Literal["drop", "downgrade"]
DEFAULT_REL_TOL = 0.01
UNVERIFIED_CONFIDENCE = 0.3

_SCALE_WORDS: dict[str, float] = {
    "k": 1e3, "thousand": 1e3, "thousands": 1e3, "тыс": 1e3,
    "m": 1e6, "mm": 1e6, "mn": 1e6, "mln": 1e6, "mio": 1e6, "million": 1e6, "millions": 1e6, "млн": 1e6,
    "b": 1e9, "bn": 1e9, "bln": 1e9, "mrd": 1e9, "billion": 1e9, "billions": 1e9, "млрд": 1e9,
    "tn": 1e12, "trillion": 1e12, "trillions": 1e12,
}
_PERCENT_WORDS = {"%", "percent", "per cent", "pct", "pp"}
_NUMBER_RE = re.compile(
    r"(?<![\w.,])"
    r"([-+\u2212]?)"
    r"(\d{1,3}(?:[,. \u00a0\u202f'\u2019]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"(?:\s?(%|percent\b|per cent\b|pct\b|pp\b|k\b|thousands?\b|тыс\b|mm\b|mn\b|mln\b|mio\b|"
    r"millions?\b|млн\b|m\b|bn\b|bln\b|mrd\b|billions?\b|млрд\b|b\b|tn\b|trillions?\b))?",
    re.IGNORECASE,
)
_PIECE_RE = re.compile(r"\d+(?:[.,]\d+)?")
_UNIT_SCALE_RE = re.compile(
    r"(?:^|[\s$\u20ac\u00a3(/])(k|thousands?|'000|тыс|mm|mn|mln|mio|m|millions?|млн|b|bn|bln|mrd|"
    r"billions?|млрд|tn|trillions?)"
    r"(?=$|[\s.)/]|[A-Z]{3}\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TextNumber:
    """A number written in text, with the rounding its digits imply."""

    value: float
    half_unit: float  # 0.5 of the last written digit, times the scale
    percent: bool
    scale: float
    plain_integer: bool  # no decimals, no scale, no percent, no grouping


def _clean(token: str) -> str:
    return re.sub(r"[ \u00a0\u202f'\u2019]", "", token)


def _interpretations(token: str) -> list[tuple[float, int]]:
    """(value, decimals) for every plausible decimal convention, "." first."""
    body = _clean(token)
    out: list[tuple[float, int]] = []
    for decimal in (".", ","):
        value = parse_body(body, decimal)
        if value is not None and all(abs(value - v) > 1e-12 for v, _ in out):
            out.append((value, decimals_in(body, decimal)))
    return out


def _suffix_scale(suffix: str | None) -> tuple[float, bool]:
    if not suffix:
        return 1.0, False
    key = suffix.lower()
    if key in _PERCENT_WORDS:
        return 1.0, True
    return _SCALE_WORDS.get(key, 1.0), False


def numbers_in_text(text: str, *, all_interpretations: bool = False) -> list[TextNumber]:
    """Extract numbers from prose: ``1,234.5``, ``1.234,5``, ``12 %``, ``$1.2bn``, ``3.4k``.

    With ``all_interpretations`` every reading of an ambiguous token is returned
    (``1,234`` as 1234 and 1.234), which is what a *source* index wants.
    """
    found: list[TextNumber] = []
    for match in _NUMBER_RE.finditer(text):
        sign, token, suffix = match.groups()
        scale, percent = _suffix_scale(suffix)
        readings = _interpretations(token)
        if not all_interpretations:
            readings = readings[:1]
        negative = sign in ("-", "\u2212")
        grouped = bool(re.search(r"[,. \u00a0\u202f'\u2019]\d{3}(?!\d)", token)) and len(token) > 4
        for value, decimals in readings:
            found.append(
                TextNumber(
                    value=(-value if negative else value) * scale,
                    half_unit=0.5 * 10.0 ** (-decimals) * scale,
                    percent=percent,
                    scale=scale,
                    plain_integer=decimals == 0 and scale == 1.0 and not percent and not grouped,
                )
            )
    return found


def source_numbers(text: str) -> list[float]:
    """Every number a source text could be claiming, for a ``NumberIndex``."""
    numbers: list[float] = []
    for n in numbers_in_text(text, all_interpretations=True):
        numbers.append(n.value)
        if n.scale != 1.0:
            numbers.append(n.value / n.scale)  # the bare digits, e.g. "1.2" of "1.2bn"
        if n.percent:
            numbers.append(n.value / 100)
    for piece in _PIECE_RE.findall(text):  # digits split differently than the main regex chose
        numbers.extend(v for v, _ in _interpretations(piece))
    return numbers


class NumberIndex:
    """Sorted absolute values with tolerant membership tests."""

    def __init__(self, numbers: Iterable[float | int | None] = ()) -> None:
        self._values = sorted(
            {abs(float(x)) for x in numbers if x is not None and math.isfinite(float(x))}
        )

    def __len__(self) -> int:
        return len(self._values)

    def contains(self, x: float, *, rel_tol: float = DEFAULT_REL_TOL, abs_tol: float = 0.0) -> bool:
        x = abs(x)
        tol = max(rel_tol * x, abs_tol, 1e-12)
        i = bisect_left(self._values, x - tol)
        return i < len(self._values) and self._values[i] <= x + tol


def unit_scale(unit: str | None) -> float:
    """Scale implied by a unit such as ``"million USD"``, ``"kEUR"`` or ``"$bn"``."""
    if not unit:
        return 1.0
    m = _UNIT_SCALE_RE.search(unit.strip())
    if not m:
        return 1.0
    word = m.group(1).lower()
    return 1e3 if word == "'000" else _SCALE_WORDS.get(word, 1.0)


def value_is_grounded(fact: Fact, index: NumberIndex, *, rel_tol: float = DEFAULT_REL_TOL) -> bool | None:
    """True/False for a fact with a value; None when the fact has no value."""
    if fact.value is None:
        return None
    scale = unit_scale(fact.unit)
    candidates = {fact.value, fact.value * scale}
    return any(index.contains(c, rel_tol=rel_tol) for c in candidates)


def unverified_statement_numbers(
    statement: str, index: NumberIndex, *, rel_tol: float = DEFAULT_REL_TOL
) -> list[float]:
    """Numbers in a statement that match nothing in the source (rounding-aware)."""
    missing: list[float] = []
    for n in numbers_in_text(statement):
        if n.plain_integer and (abs(n.value) <= 31 or 1900 <= abs(n.value) <= 2100):
            continue
        # "4.2 million" also matches a source that states 4.2 with the scale in a unit or axis label
        readings = [(n.value, n.half_unit)]
        if n.scale != 1.0:
            readings.append((n.value / n.scale, n.half_unit / n.scale))
        if not any(index.contains(v, rel_tol=rel_tol, abs_tol=h) for v, h in readings):
            missing.append(n.value)
    return missing


class GroundingReport(BaseModel):
    kept: list[Fact] = Field(default_factory=list)
    rejected: list[Fact] = Field(default_factory=list)
    downgraded: int = 0


def ground_facts(
    facts: Sequence[Fact],
    index: NumberIndex,
    *,
    policy: UnverifiedPolicy = "drop",
    rel_tol: float = DEFAULT_REL_TOL,
    unverified_confidence: float = UNVERIFIED_CONFIDENCE,
) -> GroundingReport:
    """Apply the guard described in the module docstring."""
    if policy not in ("drop", "downgrade"):
        raise ValueError(f"unknown policy {policy!r}")
    report = GroundingReport()
    for fact in facts:
        grounded = value_is_grounded(fact, index, rel_tol=rel_tol)
        if grounded is False and policy == "drop":
            log.info("dropping ungrounded fact: %s (value=%s)", fact.statement, fact.value)
            report.rejected.append(fact)
            continue
        loose = unverified_statement_numbers(fact.statement, index, rel_tol=rel_tol)
        if grounded is False or loose:
            if loose:
                log.info("statement numbers not in source %s: %s", loose, fact.statement)
            fact = fact.model_copy(update={"confidence": min(fact.confidence, unverified_confidence)})
            report.downgraded += 1
        report.kept.append(fact)
    return report
