"""Prompts and LLM output schemas for the research pipeline.

Every prompt starts with a static instruction block and appends the dynamic part
(question, facts, sources) at the end, so Token Factory prefix caching can reuse the
static tokens. Instructions go in the user turn because ``TokenFactoryClient.structured``
already prepends its own system message with the JSON schema.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from pydantic import BaseModel, Field

from variator.domain import Fact
from variator.research.models import ResearchQuery, SourceDocument

QUERY_PLANNER_INSTRUCTIONS = """\
You are the research planner of VARIATOR, a decision simulator for business leaders.
Given a business decision and what is already known about the company, propose web
search queries that find EXTERNAL, preferably quantitative, market signals needed to set
the assumptions of a Monte Carlo model: price and cost trends, market size and growth,
demand indicators, competitor moves, regulation, macro data (inflation, interest rates,
FX, wages) and industry benchmarks.

Rules:
- One assumption per query; queries must not overlap.
- Be specific: name the industry, geography, metric and year when relevant.
- Prefer queries likely to return numbers (%, prices, rates) from reputable sources.
- Never search for the company's internal data: it is already known.
- Plain keywords, under 20 words; no boolean operators, quotes or site: filters.
- topic is "news" for recent events, "finance" for market or financial data, else "general".
- purpose names the model assumption the query informs.
- Return no more than the maximum number of queries given below.
"""

FACT_EXTRACTOR_INSTRUCTIONS = """\
You extract evidence for VARIATOR, a decision simulator for business leaders. From the
SOURCES below, extract facts relevant to the decision question that could inform the
assumptions of a simulation: numbers with units and periods, trends, benchmarks, prices,
risks and notable events.

Rules:
- Use ONLY the text of the SOURCES. Never add outside knowledge. If a source contains
  nothing relevant, extract nothing from it. Fewer, well-supported facts beat many.
- statement: one self-contained sentence saying what, where and when, e.g.
  "Average 3PL fulfilment fees in Germany rose 12% year over year in 2026."
  Every number in the statement must appear in the source text.
- source_url: copy the URL line of the source the fact comes from, exactly.
- evidence: a short verbatim quote (at most 40 words) from that source supporting the fact.
- value: the main number as a plain number (12% -> 12 with unit "%"); null if qualitative.
- unit: e.g. "%", "USD/m2/month", "EUR", "days"; null if none.
- period: what the number refers to (e.g. "2026", "2026-Q2", "FY2025"); null if unknown.
- confidence: 0.9 official statistics or primary data; 0.7 reputable press or research;
  0.5 blogs, vendor marketing or forecasts.
- Return {"facts": []} when nothing is relevant.
"""


class PlannedQueries(BaseModel):
    """LLM output of the query planner."""

    queries: list[ResearchQuery]


class ExtractedFact(BaseModel):
    """One fact as the LLM proposes it, before post-validation.

    No range constraints here: a schema violation triggers a costly repair call,
    so the pipeline clamps values afterwards instead.
    """

    statement: str
    source_url: str = Field(description="Exact URL of the source, copied from its URL line")
    evidence: str = Field(description="Short verbatim quote from that source")
    value: float | None = None
    unit: str | None = None
    period: str | None = None
    confidence: float = 0.7


class ExtractedFacts(BaseModel):
    """LLM output of one extraction batch."""

    facts: list[ExtractedFact]


def _format_fact(fact: Fact, limit: int) -> str:
    details = [str(part) for part in (fact.value, fact.unit, fact.period) if part not in (None, "")]
    line = fact.statement.strip()
    if details:
        line += f" ({' '.join(details)})"
    return "- " + line[:limit]


def query_planner_prompt(
    question: str,
    context_facts: Sequence[Fact],
    *,
    max_queries: int,
    today: date,
    max_context_facts: int = 15,
    max_fact_chars: int = 240,
) -> str:
    """User message for the query planner: static instructions, then the dynamic part."""
    known = [_format_fact(f, max_fact_chars) for f in list(context_facts)[:max_context_facts]]
    return (
        f"{QUERY_PLANNER_INSTRUCTIONS}\n"
        f"Today: {today.isoformat()}\n"
        f"Maximum number of queries: {max_queries}\n\n"
        f"Decision question:\n{question.strip()}\n\n"
        "Known company facts:\n" + ("\n".join(known) if known else "- none provided")
    )


def format_source(doc: SourceDocument) -> str:
    """One source block as the extractor sees it."""
    lines = [f"[{doc.source_id}] {doc.title.strip() or 'Untitled'}", f"URL: {doc.url}"]
    if doc.published_date:
        lines.append(f"Published: {doc.published_date}")
    lines.append(f"Text: {doc.content.strip()}")
    return "\n".join(lines)


def extraction_prompt(question: str, sources: Sequence[SourceDocument]) -> str:
    """User message for one extraction batch. The question precedes the sources so the
    prefix is shared by every batch of the same run."""
    blocks = "\n\n".join(format_source(doc) for doc in sources)
    return f"{FACT_EXTRACTOR_INSTRUCTIONS}\nDecision question:\n{question.strip()}\n\nSOURCES:\n\n{blocks}"
