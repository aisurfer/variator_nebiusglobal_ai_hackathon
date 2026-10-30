"""Normalization helpers for source records."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from variator.corpus.models import CorpusDocument


def normalize_timestamp(value: Any) -> datetime:
    """Parse a source timestamp into a timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def normalize_author(value: Any) -> str | None:
    """Return a trimmed author string or None when absent."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def make_dsid(source: str, locator: str) -> str:
    """Build a deterministic source document id."""
    return f"{source}:{locator}"


def normalize_enterprise_rag(
    *,
    path: Path,
    content: str,
    timestamp: datetime | str | None = None,
    author: Any = None,
) -> CorpusDocument:
    """Normalize one EnterpriseRAG text file while retaining filename identity."""
    source_type = path.parent.name
    raw_stem = path.stem
    dsid = raw_stem.split("__", maxsplit=1)[0] or raw_stem
    lines = content.splitlines()
    title = lines[0].strip() if lines else ""
    return CorpusDocument(
        doc_id=make_dsid("enterprise_rag", dsid),
        layer="internal",
        source_type=source_type,
        timestamp=normalize_timestamp(timestamp) if timestamp is not None else None,
        author=normalize_author(author),
        content=content,
        provenance={
            "source": "enterprise_rag",
            "locator": path.as_posix(),
            "dsid": dsid,
            "filename_stem": raw_stem,
            "title": title,
        },
    )


def normalize_external_record(
    *,
    source: str,
    source_type: str,
    record_id: str | int,
    content: str,
    timestamp: datetime | str | None,
    author: Any,
    locator: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CorpusDocument:
    """Normalize one external source record without inferring missing values."""
    normalized_id = str(record_id)
    return CorpusDocument(
        doc_id=make_dsid(source, normalized_id),
        layer="external",
        source_type=source_type,
        timestamp=normalize_timestamp(timestamp) if timestamp is not None else None,
        author=normalize_author(author),
        content=content,
        provenance={
            "source": source,
            "locator": locator or normalized_id,
            "source_record_id": normalized_id,
            "metadata": metadata or {},
        },
    )
