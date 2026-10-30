"""Source-specific deterministic loaders for external corpus records."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from variator.corpus.models import CorpusDocument
from variator.corpus.normalization import (
    normalize_author,
    normalize_enterprise_rag,
    normalize_external_record,
    normalize_timestamp,
)


def _text(*parts: Any) -> str:
    return "\n\n".join(str(part).strip() for part in parts if part is not None and str(part).strip())


def load_enterprise_rag_directory(path: Path) -> list[CorpusDocument]:
    """Load the text-shaped EnterpriseRAG fixture or release directory."""
    if not path.exists():
        raise FileNotFoundError(path)
    files = sorted(path.rglob("*.txt")) if path.is_dir() else [path]
    return [normalize_enterprise_rag(path=file, content=file.read_text(encoding="utf-8")) for file in files]


def load_json_records(path: Path) -> list[dict[str, Any]]:
    """Read a JSON array/object or JSONL file without source-specific inference."""
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return [record for record in payload["records"] if isinstance(record, dict)]
    if isinstance(payload, dict):
        return [payload]
    raise ValueError(f"fixture must contain an object or list: {path}")


def load_solstice_csv_directory(path: Path) -> tuple[list[CorpusDocument], dict[str, list[dict[str, Any]]]]:
    """Load the public Solstice CSV tables while retaining table-level provenance."""
    documents: list[CorpusDocument] = []
    tables: dict[str, list[dict[str, Any]]] = {}
    for csv_path in sorted(path.glob("*.csv")):
        table = csv_path.stem.removeprefix("data_").removesuffix("_train")
        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        tables[table] = rows
        if not rows:
            continue
        fieldnames = list(rows[0])
        primary_key = next(
            (field for field in fieldnames if field == "id" or field.endswith("_id")),
            fieldnames[0],
        )
        timestamp_field = next(
            (field for field in ("timestamp", "created_at", "date", "updated_at") if field in fieldnames),
            None,
        )
        author_field = next(
            (field for field in ("author", "created_by", "assignee", "owner_id") if field in fieldnames),
            None,
        )
        content_fields = [
            field
            for field in ("title", "subject", "content", "body", "description", "text", "summary")
            if field in fieldnames
        ]
        for index, row in enumerate(rows, start=1):
            record_id = str(row.get(primary_key) or f"row-{index}")
            content = _text(*(row.get(field) for field in content_fields))
            if not content:
                content = _text(*(f"{field}: {value}" for field, value in row.items() if value))
            documents.append(
                CorpusDocument(
                    doc_id=f"solstice:{table}:{record_id}",
                    layer="chain",
                    source_type=table,
                    timestamp=(
                        normalize_timestamp(row[timestamp_field])
                        if timestamp_field and row.get(timestamp_field)
                        else None
                    ),
                    author=normalize_author(row.get(author_field)) if author_field else None,
                    content=content,
                    chain_refs=[{"table": table, "primary_key": primary_key, "record_id": record_id}],
                    provenance={
                        "source": "solstice",
                        "table": table,
                        "primary_key": primary_key,
                        "locator": csv_path.as_posix(),
                    },
                )
            )
    return documents, tables


def load_cc_news(records: Iterable[dict[str, Any]]) -> list[CorpusDocument]:
    """Normalize CC-News records without altering their source timestamps."""
    documents: list[CorpusDocument] = []
    for record in records:
        locator = str(record.get("url") or record["id"])
        documents.append(
            normalize_external_record(
                source="cc_news",
                source_type="news_article",
                record_id=record.get("id") or locator,
                content=_text(record.get("title"), record.get("text") or record.get("content")),
                timestamp=record.get("published") or record.get("date"),
                author=record.get("author"),
                locator=locator,
                metadata={"domain": record.get("domain")},
            )
        )
    return documents


def load_amazon_reviews(records: Iterable[dict[str, Any]]) -> list[CorpusDocument]:
    """Normalize Amazon Reviews 2023 records using the review identifier."""
    documents: list[CorpusDocument] = []
    for record in records:
        review_id = record.get("review_id") or record.get("asin")
        if review_id is None:
            raise ValueError("Amazon review requires review_id or asin")
        documents.append(
            normalize_external_record(
                source="amazon_reviews_2023",
                source_type="product_review",
                record_id=review_id,
                content=_text(record.get("title"), record.get("text")),
                timestamp=record.get("timestamp"),
                author=record.get("user_id"),
                metadata={"rating": record.get("rating"), "asin": record.get("asin")},
            )
        )
    return documents


def load_reddit(records: Iterable[dict[str, Any]]) -> list[CorpusDocument]:
    """Normalize ConvoKit-style Reddit posts while preserving thread metadata."""
    documents: list[CorpusDocument] = []
    for record in records:
        post_id = record.get("id") or record.get("post_id")
        if post_id is None:
            raise ValueError("Reddit post requires id or post_id")
        documents.append(
            normalize_external_record(
                source="reddit",
                source_type="reddit_post",
                record_id=post_id,
                content=_text(record.get("title"), record.get("body") or record.get("text")),
                timestamp=record.get("created_utc") or record.get("timestamp"),
                author=record.get("author") or record.get("speaker"),
                metadata={
                    "subreddit": record.get("subreddit"),
                    "thread_id": record.get("thread_id") or record.get("conversation_id"),
                    "reply_to": record.get("reply_to"),
                },
            )
        )
    return documents


def load_github_issues(records: Iterable[dict[str, Any]], repository: str) -> list[CorpusDocument]:
    """Normalize GitHub issue API records with repository-native identity."""
    documents: list[CorpusDocument] = []
    for record in records:
        issue_number = record.get("number")
        if issue_number is None:
            raise ValueError("GitHub issue requires number")
        locator = f"{repository}#{issue_number}"
        user = record.get("user") or {}
        documents.append(
            normalize_external_record(
                source="github",
                source_type="github_issue",
                record_id=locator,
                content=_text(record.get("title"), record.get("body")),
                timestamp=record.get("created_at"),
                author=user.get("login") if isinstance(user, dict) else user,
                locator=record.get("html_url") or locator,
                metadata={
                    "repository": repository,
                    "state": record.get("state"),
                    "labels": record.get("labels", []),
                },
            )
        )
    return documents


def load_solstice_rows(
    *,
    table: str,
    primary_key: str,
    rows: Iterable[dict[str, Any]],
    content_field: str = "content",
    timestamp_field: str = "timestamp",
    author_field: str = "author",
) -> list[CorpusDocument]:
    """Materialize one validated Solstice table as traceable chain documents."""
    documents: list[CorpusDocument] = []
    for row in rows:
        if primary_key not in row:
            raise ValueError(f"Solstice {table} row lacks primary key {primary_key}")
        record_id = str(row[primary_key])
        documents.append(
            CorpusDocument(
                doc_id=f"solstice:{table}:{record_id}",
                layer="chain",
                source_type=table,
                timestamp=(
                    normalize_timestamp(row[timestamp_field])
                    if row.get(timestamp_field) is not None
                    else None
                ),
                author=normalize_author(row.get(author_field)),
                content=str(row.get(content_field, "")),
                chain_refs=[{"table": table, "primary_key": primary_key, "record_id": record_id}],
                provenance={"source": "solstice", "table": table, "primary_key": primary_key},
            )
        )
    return documents
