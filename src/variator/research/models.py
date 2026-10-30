"""Data crossing the module boundary: Tavily responses and research bundles.

Field names mirror the Tavily REST API (https://docs.tavily.com/documentation/api-reference)
so raw JSON parses directly; unknown fields are ignored to survive API additions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from variator.domain import Fact

SearchDepth = Literal["basic", "advanced", "fast", "ultra-fast"]
Topic = Literal["general", "news", "finance"]
TimeRange = Literal["day", "week", "month", "year", "d", "w", "m", "y"]
ExtractDepth = Literal["basic", "advanced"]
AnswerMode = bool | Literal["basic", "advanced"]
RawContentMode = bool | Literal["markdown", "text"]


class _TavilyModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TavilyUsage(_TavilyModel):
    """``usage`` object Tavily returns when the request sets ``include_usage``."""

    credits: float | None = None


class SearchResult(_TavilyModel):
    title: str = ""
    url: str
    content: str = Field(default="", description="Query-relevant snippet(s), joined by ' [...] '")
    score: float = 0.0
    raw_content: str | None = None
    published_date: str | None = None
    favicon: str | None = None


class TavilyResponse(_TavilyModel):
    response_time: float | None = None
    request_id: str | None = None
    usage: TavilyUsage | None = Field(default=None, description="As reported by Tavily")
    # Filled in by TavilyClient, not by Tavily:
    credits: float = Field(default=0.0, description="Credits this call cost; 0 when served from cache")
    credits_estimated: bool = Field(
        default=False, description="True when Tavily did not report usage and the docs price was used"
    )
    from_cache: bool = False


class SearchResponse(TavilyResponse):
    query: str
    answer: str | None = None
    results: list[SearchResult] = Field(default_factory=list)


class ExtractResult(_TavilyModel):
    url: str
    raw_content: str | None = None
    images: list[str] = Field(default_factory=list)
    favicon: str | None = None


class FailedExtraction(_TavilyModel):
    url: str
    error: str = ""


class ExtractResponse(TavilyResponse):
    results: list[ExtractResult] = Field(default_factory=list)
    failed_results: list[FailedExtraction] = Field(default_factory=list)


class ResearchQuery(BaseModel):
    """A focused web search proposed by the planner (or pinned by the caller)."""

    query: str = Field(description="Search engine query, under 400 characters")
    purpose: str = Field(default="", description="Which assumption of the decision this checks")
    topic: Topic = "general"


class SourceDocument(BaseModel):
    """A search result after URL dedupe across queries; ``source_id`` is what prompts cite."""

    source_id: str
    url: str
    title: str = ""
    content: str = ""
    score: float = 0.0
    published_date: str | None = None
    queries: list[str] = Field(default_factory=list, description="Queries that surfaced this URL")


class CitedFact(BaseModel):
    """A web fact plus what the UI needs to show where it came from."""

    fact: Fact
    evidence: str = Field(description="Snippet of the source text that supports the statement")
    source_title: str = ""
    published_date: str | None = None
    query: str | None = Field(default=None, description="First search query that found the source")


class DroppedFact(BaseModel):
    """A fact the model proposed that failed post-validation (kept for transparency/debugging)."""

    statement: str
    source_url: str
    reason: str


class ResearchBundle(BaseModel):
    question: str
    queries: list[ResearchQuery] = Field(default_factory=list)
    results: list[SourceDocument] = Field(default_factory=list)
    cited_facts: list[CitedFact] = Field(default_factory=list)
    dropped: list[DroppedFact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list, description="Non-fatal problems, e.g. a failed query")
    tavily_credits: float = Field(default=0.0, description="Tavily credits spent by this run")
    tavily_cache_hits: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def facts(self) -> list[Fact]:
        """The validated facts (origin WEB, source = URL) for the decision model."""
        return [cited.fact for cited in self.cited_facts]
