"""Deterministic assembly of the Redwood training corpus."""

from variator.corpus.canonical import canonical_json, immutable_id, sha256_hex
from variator.corpus.factors import FACTORS_VERSION, factor_catalog_records
from variator.corpus.manifest import file_manifest_entry

__all__ = [
    "FACTORS_VERSION",
    "canonical_json",
    "factor_catalog_records",
    "file_manifest_entry",
    "immutable_id",
    "sha256_hex",
]
