"""Facts: evidence gathered from company data, documents and the web."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class FactOrigin(StrEnum):
    DATA = "data"  # extracted from uploaded tables
    DOCUMENT = "document"  # extracted from PDFs, slides, screenshots
    WEB = "web"  # found by web search (Tavily)
    USER = "user"  # stated by the user


class Fact(BaseModel):
    """A single piece of evidence the decision model can rely on."""

    statement: str = Field(description="One self-contained sentence")
    value: float | None = None
    unit: str | None = None
    period: str | None = Field(default=None, description="e.g. 2026-Q3, FY2025, monthly")
    origin: FactOrigin
    source: str | None = Field(default=None, description="File name, page or URL")
    confidence: float = Field(default=0.7, ge=0, le=1)
