"""Deterministic JSONL output writers for corpus delivery."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from variator.corpus.canonical import canonical_json


def write_jsonl(path: Path, records: Iterable[dict]) -> int:
    """Write records in caller-supplied deterministic order and return their count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
            count += 1
    return count


def write_gzip_jsonl(path: Path, records: Iterable[dict]) -> int:
    """Write deterministic UTF-8 JSONL compressed with a stable gzip header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("wb") as raw:
        handle = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0)
        try:
            for record in records:
                line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write(line.encode("utf-8"))
                handle.write(b"\n")
                count += 1
        finally:
            handle.close()
    return count


def write_json(path: Path, value: object) -> None:
    """Write one deterministic JSON object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value))


def assign_time_split(
    documents: Iterable[dict],
    *,
    validation_fraction: float = 0.2,
    null_policy: str = "train",
) -> tuple[list[str], list[str], dict[str, object]]:
    """Assign timestamped records to a chronological train/validation split."""
    if not 0 < validation_fraction < 1:
        raise ValueError("validation_fraction must be between 0 and 1")
    records = list(documents)
    dated = [record for record in records if isinstance(record.get("timestamp"), datetime)]
    null_ids = [
        str(record["doc_id"]) for record in records if not isinstance(record.get("timestamp"), datetime)
    ]
    if null_policy not in {"train", "validation", "drop"}:
        raise ValueError("null_policy must be train, validation, or drop")
    dated.sort(key=lambda record: (record["timestamp"], str(record["doc_id"])))
    cut = max(1, min(len(dated), int(round(len(dated) * (1 - validation_fraction))))) if dated else 0
    train = [str(record["doc_id"]) for record in dated[:cut]]
    validation = [str(record["doc_id"]) for record in dated[cut:]]
    if null_policy == "train":
        train.extend(sorted(null_ids))
    elif null_policy == "validation":
        validation.extend(sorted(null_ids))
    return (
        train,
        validation,
        {
            "boundary": dated[cut]["timestamp"].isoformat() if cut < len(dated) else None,
            "null_timestamp_policy": null_policy,
            "train_count": len(train),
            "validation_count": len(validation),
        },
    )


def write_splits(path: Path, train_ids: list[str], validation_ids: list[str]) -> None:
    """Write a stable time-split index without changing caller ordering."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"train": train_ids, "validation": validation_ids}, sort_keys=True),
        encoding="utf-8",
    )
