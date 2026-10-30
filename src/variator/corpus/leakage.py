"""Leakage scanning for residual real-world entities."""

from __future__ import annotations

import re
from collections.abc import Iterable


def scan_leakage(text: str, forbidden: Iterable[str]) -> list[str]:
    """Return forbidden terms found in text."""
    found: list[str] = []
    for term in forbidden:
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE):
            found.append(term)
    return found


def scan_regex(text: str, pattern: str) -> list[str]:
    """Return all regex matches in text."""
    return re.findall(pattern, text)


def enforce_leakage_threshold(
    documents: Iterable[str], forbidden: Iterable[str], threshold: float
) -> dict[str, int | float]:
    """Return leakage statistics or reject a corpus above its configured rate."""
    texts = list(documents)
    leaked = sum(1 for text in texts if scan_leakage(text, forbidden))
    rate = leaked / len(texts) if texts else 0.0
    if rate > threshold:
        raise ValueError(f"leakage rate {rate:.6f} exceeds threshold {threshold:.6f}")
    return {"document_count": len(texts), "leaked_document_count": leaked, "leakage_rate": rate}
