"""Query planning for a business-specific external research profile."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from variator.research.models import ResearchQuery, Topic

_LENSES: tuple[tuple[str, str, Topic], ...] = (
    ("business model", "business model revenue streams pricing unit economics", "finance"),
    ("customers", "typical customers buying criteria use cases procurement", "general"),
    ("competitors", "competitors market structure differentiation market shares", "finance"),
    ("regulation", "regulation licensing compliance reporting legal obligations", "general"),
    ("operations", "operating model delivery workflow suppliers dependencies", "general"),
    ("labour", "roles skills hiring salaries staffing shortages", "general"),
    ("risks", "failure modes operational risks incidents insurance", "general"),
    ("benchmarks", "industry benchmarks costs margins productivity service levels", "finance"),
    ("market", "market size growth demand outlook trends", "finance"),
    ("technology", "technology stack adoption infrastructure constraints", "general"),
    ("customers pain", "customer complaints churn switching reasons", "general"),
    ("economics", "inflation interest rates wages currency input costs", "finance"),
)

_MODIFIERS: tuple[str, ...] = (
    "statistics 2026",
    "industry report",
    "official guidance",
    "benchmark data",
    "case study",
    "common challenges",
    "recent developments",
    "small business",
    "enterprise",
    "Europe",
)


def business_profile_queries(
    business: str,
    *,
    geography: str = "",
    max_queries: int = 24,
    year: int | None = None,
) -> list[ResearchQuery]:
    """Create a broad, reproducible query set for a business research profile.

    The first pass covers every lens once. Larger budgets add orthogonal modifiers,
    so a caller can scale from a quick profile to hundreds of Tavily searches without
    changing the meaning of the research. The hard cap prevents accidental credit burns.
    """
    if not business.strip():
        raise ValueError("business must not be empty")
    if not 1 <= max_queries <= 1000:
        raise ValueError("max_queries must be 1..1000")
    subject = " ".join(business.split())
    place = " ".join(geography.split())
    suffix = f" {place}" if place else ""
    year_suffix = f" {year or date.today().year}" if year is not None else ""

    queries: list[ResearchQuery] = []
    seen: set[str] = set()

    def add(lens: str, terms: str, topic: Topic, modifier: str = "") -> None:
        parts = (subject, suffix.strip(), terms, modifier, year_suffix.strip())
        query = " ".join(part for part in parts if part)
        key = query.casefold()
        if key in seen:
            return
        seen.add(key)
        queries.append(ResearchQuery(query=query[:400], purpose=lens, topic=topic))

    for lens, terms, topic in _LENSES:
        add(lens, terms, topic)
        if len(queries) >= max_queries:
            return queries

    for modifier in _MODIFIERS:
        for lens, terms, topic in _LENSES:
            add(lens, terms, topic, modifier)
            if len(queries) >= max_queries:
                return queries
    return queries


def profile_lenses() -> Sequence[str]:
    """Names of the research lenses, useful for UI labels and telemetry."""
    return tuple(lens for lens, _, _ in _LENSES)
