"""Solstice foreign-key validation helpers."""

from __future__ import annotations

from variator.corpus.loaders import load_solstice_rows
from variator.corpus.models import CorpusDocument


def validate_foreign_keys(rows: list[dict], key: str, valid_ids: set) -> list[str]:
    """Return list of invalid foreign key values."""
    errors: list[str] = []
    for row in rows:
        value = row.get(key)
        if value is not None and value not in valid_ids:
            errors.append(str(value))
    return errors


def materialize_chain_events(
    *,
    table: str,
    primary_key: str,
    rows: list[dict],
    foreign_keys: dict[str, set],
) -> list[CorpusDocument]:
    """Reject broken references then materialize traceable chain documents."""
    failures = {
        field: validate_foreign_keys(rows, field, valid_ids) for field, valid_ids in foreign_keys.items()
    }
    invalid = {field: values for field, values in failures.items() if values}
    if invalid:
        raise ValueError(f"invalid Solstice foreign keys: {invalid}")
    return load_solstice_rows(table=table, primary_key=primary_key, rows=rows)
