"""Fact extraction with Nemotron 3.5 Lightning, guarded against invented numbers.

* ``facts_from_profile``: a deterministic ``TableProfile`` -> facts (origin DATA).
* ``facts_from_text``: report text, chunked -> facts (origin DOCUMENT), deduplicated.

Every fact passes ``grounding.ground_facts`` against the numbers in its own source.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field, field_validator

from variator.domain import Fact, FactOrigin
from variator.ingestion import prompts
from variator.ingestion.grounding import (
    DEFAULT_REL_TOL,
    NumberIndex,
    UnverifiedPolicy,
    ground_facts,
    source_numbers,
)
from variator.ingestion.tables import TableProfile
from variator.llm import TokenFactoryClient, user_message
from variator.llm.models import ROLE_MODEL

log = logging.getLogger(__name__)

DATA_MAX_CONFIDENCE = 0.95  # values are deterministic statistics, checked against the profile
DATA_QUALITATIVE_MAX_CONFIDENCE = 0.7  # interpretation without a checkable number
DOCUMENT_MAX_CONFIDENCE = 0.85  # documents can be stale, rounded or forward-looking


class FactDraft(BaseModel):
    """What the LLM returns for one fact; origin and source are filled in by code."""

    statement: str = Field(description="One self-contained sentence")
    value: float | None = Field(default=None, description="Key number, copied exactly from the input")
    unit: str | None = Field(default=None, description='e.g. "USD", "million EUR", "%", "orders"')
    period: str | None = Field(default=None, description='e.g. "2025-Q3", "FY2025", "2025-01..2025-12"')
    confidence: float = Field(default=0.7, description="0..1")

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v: object) -> float:
        try:
            return min(max(float(v), 0.0), 1.0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.7


class DataFactDraft(FactDraft):
    column: str | None = Field(default=None, description="Profile column the fact is about, or null")


class FactList(BaseModel):
    facts: list[FactDraft] = Field(default_factory=list)


class DataFactList(BaseModel):
    facts: list[DataFactDraft] = Field(default_factory=list)


# ---------------------------------------------------------------- tables


def facts_from_profile(
    client: TokenFactoryClient,
    profile: TableProfile,
    *,
    model: str = ROLE_MODEL["extraction"],
    max_facts: int = 25,
    on_unverified: UnverifiedPolicy = "drop",
    rel_tol: float = DEFAULT_REL_TOL,
) -> list[Fact]:
    """Turn a table profile into self-contained facts (origin DATA, source ``table:column``).

    One Lightning call with reasoning off. Facts whose value is not in the profile
    (within ``rel_tol``) are dropped, or kept with confidence 0.3 when
    ``on_unverified="downgrade"``. Raises ``StructuredOutputError`` if the model
    never returns valid JSON. An empty profile returns ``[]`` without a call.
    """
    if not profile.columns or profile.row_count == 0:
        return []
    content = (
        prompts.PROFILE_FACTS_INSTRUCTIONS
        + "\n\n"
        + prompts.PROFILE_FACTS_TEMPLATE.format(max_facts=max_facts, profile=profile.to_prompt())
    )
    drafts = client.structured(model, [user_message(content)], DataFactList, reasoning=False).facts
    facts = []
    for draft in drafts:
        if not draft.statement.strip():
            continue
        column = draft.column if draft.column and profile.column(draft.column) else None
        cap = DATA_MAX_CONFIDENCE if draft.value is not None else DATA_QUALITATIVE_MAX_CONFIDENCE
        confidence = min(draft.confidence, cap)
        facts.append(
            Fact(
                statement=draft.statement.strip(),
                value=draft.value,
                unit=draft.unit,
                period=draft.period,
                origin=FactOrigin.DATA,
                source=f"{profile.name}:{column}" if column else profile.name,
                confidence=confidence,
            )
        )
    report = ground_facts(facts, NumberIndex(profile.grounding_numbers()), policy=on_unverified,
                          rel_tol=rel_tol)
    if report.rejected:
        log.warning("%s: dropped %d fact(s) with numbers not in the profile", profile.name,
                    len(report.rejected))
    return dedupe_facts(report.kept)[:max_facts]


# ---------------------------------------------------------------- text

_SENTENCE_END_RE = re.compile(r"(?<=[.!?;])\s+")


def _split_long(piece: str, limit: int) -> list[str]:
    """Split an oversize paragraph at sentence ends, then hard-cut what is still too long."""
    out: list[str] = []
    current = ""
    for sentence in _SENTENCE_END_RE.split(piece):
        while len(sentence) > limit:
            if current:
                out.append(current)
                current = ""
            out.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + 1 + len(sentence) > limit:
            out.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        out.append(current)
    return out


def chunk_text(text: str, *, chunk_chars: int = 12_000, overlap_chars: int | None = None) -> list[str]:
    """Split text into chunks of at most ``chunk_chars`` on paragraph, then sentence, boundaries.

    Each chunk starts with up to ``overlap_chars`` of trailing paragraphs from the
    previous one (default ``min(500, chunk_chars // 10)``) so facts that straddle a
    boundary are still seen whole; duplicates are removed later by ``dedupe_facts``.
    """
    if chunk_chars < 200:
        raise ValueError("chunk_chars must be >= 200")
    overlap = min(500, chunk_chars // 10) if overlap_chars is None else overlap_chars
    if not 0 <= overlap < chunk_chars // 2:
        raise ValueError("overlap_chars must be in [0, chunk_chars / 2)")
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if paragraph:
            fits = len(paragraph) <= chunk_chars
            pieces.extend([paragraph] if fits else _split_long(paragraph, chunk_chars))

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for piece in pieces:
        if current and size + 2 + len(piece) > chunk_chars:
            chunks.append("\n\n".join(current))
            carried: list[str] = []
            carried_size = 0
            for prev in reversed(current):
                if carried_size + len(prev) + 2 > overlap:
                    break
                carried.insert(0, prev)
                carried_size += len(prev) + 2
            if carried_size + len(piece) > chunk_chars:
                carried, carried_size = [], 0
            current, size = carried, carried_size
        current.append(piece)
        size += len(piece) + (2 if len(current) > 1 else 0)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


_WORD_RE = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)?")


def _words(statement: str) -> set[str]:
    return set(_WORD_RE.findall(statement.lower()))


def _same_value(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 0.005 * max(abs(a), abs(b), 1e-12)


def _is_duplicate(a: Fact, b: Fact) -> bool:
    wa, wb = _words(a.statement), _words(b.statement)
    if wa == wb:
        return True
    if not wa or not wb or not _same_value(a.value, b.value):
        return False
    if {w for w in wa if w[0].isdigit()} != {w for w in wb if w[0].isdigit()}:
        return False  # different years or figures: different facts
    if (a.period or "").lower() != (b.period or "").lower() and a.period and b.period:
        return False
    jaccard = len(wa & wb) / len(wa | wb)
    return jaccard >= (0.6 if a.value is not None else 0.8)


def dedupe_facts(facts: Sequence[Fact]) -> list[Fact]:
    """Remove near-duplicate facts (same wording, or same value and similar wording).

    Keeps the first position and the most confident version of each fact.
    """
    kept: list[Fact] = []
    for fact in facts:
        for i, existing in enumerate(kept):
            if _is_duplicate(existing, fact):
                if fact.confidence > existing.confidence:
                    kept[i] = fact
                break
        else:
            kept.append(fact)
    return kept


def _facts_from_chunk(
    client: TokenFactoryClient,
    chunk: str,
    *,
    source: str,
    part: int,
    parts: int,
    model: str,
    max_facts: int,
    on_unverified: UnverifiedPolicy,
    rel_tol: float,
) -> list[Fact]:
    content = (
        prompts.TEXT_FACTS_INSTRUCTIONS
        + "\n\n"
        + prompts.TEXT_FACTS_TEMPLATE.format(
            source=source, part=part, parts=parts, max_facts=max_facts, text=chunk
        )
    )
    drafts = client.structured(model, [user_message(content)], FactList, reasoning=False).facts
    facts = [
        Fact(
            statement=d.statement.strip(),
            value=d.value,
            unit=d.unit,
            period=d.period,
            origin=FactOrigin.DOCUMENT,
            source=source,
            confidence=min(d.confidence, DOCUMENT_MAX_CONFIDENCE),
        )
        for d in drafts
        if d.statement.strip()
    ]
    report = ground_facts(facts, NumberIndex(source_numbers(chunk)), policy=on_unverified, rel_tol=rel_tol)
    if report.rejected:
        log.warning("%s part %d: dropped %d fact(s) with numbers not in the text", source, part,
                    len(report.rejected))
    return report.kept


def facts_from_text(
    client: TokenFactoryClient,
    text: str,
    *,
    source: str,
    model: str = ROLE_MODEL["extraction"],
    chunk_chars: int = 12_000,
    overlap_chars: int | None = None,
    max_facts_per_chunk: int = 20,
    on_unverified: UnverifiedPolicy = "drop",
    rel_tol: float = DEFAULT_REL_TOL,
    max_workers: int = 1,
) -> list[Fact]:
    """Extract facts (origin DOCUMENT) from report text, one Lightning call per chunk.

    Fact values must appear in their own chunk (see ``grounding``). Results are
    deduplicated across chunks, in document order. ``max_workers > 1`` runs chunk
    calls concurrently (mind Token Factory rate limits). Raises
    ``StructuredOutputError`` if a chunk never yields valid JSON.
    """
    chunks = chunk_text(text, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
    if not chunks:
        return []

    def run(item: tuple[int, str]) -> list[Fact]:
        part, chunk = item
        return _facts_from_chunk(
            client, chunk, source=source, part=part, parts=len(chunks), model=model,
            max_facts=max_facts_per_chunk, on_unverified=on_unverified, rel_tol=rel_tol,
        )

    items = list(enumerate(chunks, start=1))
    if max_workers > 1 and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            per_chunk = list(pool.map(run, items))
    else:
        per_chunk = [run(item) for item in items]
    return dedupe_facts([fact for facts in per_chunk for fact in facts])
