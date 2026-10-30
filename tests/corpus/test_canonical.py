"""Tests for deterministic corpus identifiers and content hashing."""

from datetime import UTC, datetime, timedelta

import pytest

from variator.corpus.canonical import canonical_json, immutable_id, sha256_hex
from variator.corpus.cli import build_parser
from variator.corpus.leakage import enforce_leakage_threshold, scan_leakage
from variator.corpus.linking import ranked_links
from variator.corpus.output import write_jsonl
from variator.corpus.redwood_profile import (
    PROFILE_DAYS,
    PROFILE_PEOPLE,
    validate_world_time,
    world_time_transform,
)
from variator.corpus.relationships import materialize_relationships
from variator.corpus.solstice import materialize_chain_events
from variator.corpus.substitution import substitute_entities


def test_equivalent_mapping_key_order_has_one_canonical_hash():
    """Object-key order never changes the canonical bytes or content hash."""
    assert canonical_json({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert sha256_hex({"b": 2, "a": 1}) == sha256_hex({"a": 1, "b": 2})
    assert immutable_id("redwood", "jira", "issue", "ENG-241") == "redwood:jira:issue:ENG-241"


def test_substitution_is_longest_first_reversible_and_boundary_aware():
    replaced, applied = substitute_entities(
        "Together's vLLM integration is unrelated to NotTogether.",
        {"Together": "Redwood", "vLLM": "Inference API"},
    )
    assert replaced == "Redwood's Inference API integration is unrelated to NotTogether."
    assert applied == ["Together", "vLLM"]
    assert scan_leakage(replaced, ["Together", "vLLM"]) == []
    assert enforce_leakage_threshold([replaced], ["Together"], 0.0)["leakage_rate"] == 0.0
    with pytest.raises(ValueError, match="exceeds threshold"):
        enforce_leakage_threshold(["Together"], ["Together"], 0.0)


def test_solstice_materialization_rejects_broken_foreign_keys():
    rows = [{"id": "msg-1", "ticket_id": "T-1", "content": "Investigating."}]
    docs = materialize_chain_events(
        table="slack_messages", primary_key="id", rows=rows, foreign_keys={"ticket_id": {"T-1"}}
    )
    assert docs[0].chain_refs[0]["record_id"] == "msg-1"
    with pytest.raises(ValueError, match="invalid Solstice foreign keys"):
        materialize_chain_events(
            table="slack_messages", primary_key="id", rows=rows, foreign_keys={"ticket_id": set()}
        )


def test_ranked_links_are_stable_and_time_bounded():
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    docs = [
        {"doc_id": "a", "timestamp": now, "entities": ["redwood"]},
        {"doc_id": "b", "timestamp": now + timedelta(hours=1), "entities": ["redwood"]},
        {"doc_id": "c", "timestamp": now + timedelta(days=3), "entities": ["redwood"]},
    ]
    assert ranked_links(docs, 7200, 1) == {"a": ["b"], "b": ["a"], "c": []}


def test_cli_exposes_fetch_and_assemble_commands():
    assert build_parser().parse_args(["fetch"]).command == "fetch"
    assert build_parser().parse_args(["assemble"]).command == "assemble"


def test_jsonl_writer_is_stable(tmp_path):
    output = tmp_path / "documents.jsonl"
    assert write_jsonl(output, [{"b": 2, "a": 1}]) == 1
    assert output.read_bytes() == b'{"a":1,"b":2}\n'


def test_relationships_require_an_explicit_rule():
    observations = [
        {"entity_id": "x", "factor_id": "operations.incident_rate"},
        {"entity_id": "x", "factor_id": "commercial.sla_risk"},
    ]
    assert materialize_relationships(observations, {}) == []
    assert (
        materialize_relationships(
            observations, {("operations.incident_rate", "commercial.sla_risk"): ("causes", "incident-sla-v1")}
        )[0]["predicate"]
        == "causes"
    )


def test_redwood_profile_constants_and_time_transform_are_stable():
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    assert (PROFILE_PEOPLE, PROFILE_DAYS) == (45, 180)
    assert validate_world_time(now, world_time_transform(now, 12), 12)
