"""Offline fixture coverage for all Redwood corpus source layers."""

import gzip
import json
from pathlib import Path

import pytest
import yaml

from variator.corpus.cli import assemble
from variator.corpus.factors import FACTOR_GROUPS, factor_group
from variator.corpus.loaders import (
    load_amazon_reviews,
    load_cc_news,
    load_github_issues,
    load_json_records,
    load_reddit,
)
from variator.corpus.normalization import normalize_enterprise_rag
from variator.corpus.schemas import validate_record

FIXTURES = Path(__file__).parents[1] / "fixtures" / "corpus"


def test_every_source_fixture_loads_with_normalized_identity():
    assert len(list((FIXTURES / "enterprise_rag").rglob("*.txt"))) == 7
    assert len(load_cc_news(load_json_records(FIXTURES / "cc_news.jsonl"))) == 1
    assert len(load_amazon_reviews(load_json_records(FIXTURES / "amazon_reviews.jsonl"))) == 1
    assert len(load_reddit(load_json_records(FIXTURES / "reddit.jsonl"))) == 5
    github = load_json_records(FIXTURES / "github_issues.jsonl")
    assert len(load_github_issues(github[:1], "vllm-project/engine")) == 1
    assert {record["subreddit"] for record in load_json_records(FIXTURES / "reddit.jsonl")} == {
        "LocalLLaMA",
        "MachineLearning",
        "MLOps",
        "selfhosted",
        "llama",
    }


def test_enterprise_rag_normalization_preserves_title_dsid_and_timestamp():
    path = FIXTURES / "enterprise_rag" / "finance" / "er-finance-001.txt"
    document = normalize_enterprise_rag(
        path=path,
        content=path.read_text(encoding="utf-8"),
        timestamp="2026-01-01T12:00:00+02:00",
        author=" Maya Chen ",
    )
    assert document.doc_id == "enterprise_rag:er-finance-001"
    assert document.source_type == "finance"
    assert document.provenance["dsid"] == "er-finance-001"
    assert (
        document.provenance["title"] == "Revenue is $120000 and growth is 12% for the first Redwood quarter."
    )
    assert document.author == "Maya Chen"
    assert document.timestamp is not None and document.timestamp.isoformat() == "2026-01-01T10:00:00+00:00"


def test_factor_catalog_covers_all_required_groups():
    assert set(FACTOR_GROUPS) == {
        "financial",
        "commercial_customer",
        "product_technology",
        "operations_infrastructure",
        "people",
        "market_competition",
        "risk_governance",
        "decision_lever",
    }
    assert all(factor_group(factor_id) for factors in FACTOR_GROUPS.values() for factor_id in factors)


def test_fixture_assembly_emits_contract_files_and_valid_records(tmp_path):
    manifest = assemble(output=tmp_path, fixtures=FIXTURES, seed=42, validation_fraction=0.2)
    assert manifest["counts"]["documents"] == 24
    assert manifest["counts"]["case_total"] == 100
    assert manifest["counts"]["case_dev"] == 70
    assert manifest["counts"]["case_holdout"] == 30
    assert manifest["leakage"]["leakage_rate"] == 0.0
    report = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
    assert report["document_counts_by_layer"] == {"chain": 8, "external": 9, "internal": 7}
    assert report["case_counts"] == {"dev": 70, "holdout": 30, "total": 100}
    factor_ids = {
        item["factor_id"]
        for item in (
            json.loads(line) for line in (tmp_path / "factor_observations.jsonl").read_text().splitlines()
        )
    }
    assert {factor_group(factor_id) for factor_id in factor_ids} == {
        "financial",
        "commercial_customer",
        "product_technology",
        "operations_infrastructure",
        "people",
        "market_competition",
        "risk_governance",
        "decision_lever",
    }
    assert (tmp_path / "plancy" / "fixtures" / "timeline.jsonl.gz").exists()
    assert len((tmp_path / "plancy" / "adapters").iterdir().__iter__().__next__().name) > 0

    first_document = json.loads((tmp_path / "documents.jsonl").read_text(encoding="utf-8").splitlines()[0])
    validate_record("CorpusDocument", first_document)
    first_event = json.loads(
        gzip.open(tmp_path / "plancy" / "fixtures" / "timeline.jsonl.gz", "rt", encoding="utf-8").readline()
    )
    validate_record("PlancyEvent", first_event)
    first_case = json.loads((tmp_path / "plancy" / "cases.jsonl").read_text(encoding="utf-8").splitlines()[0])
    validate_record("EvaluationCase", first_case)


def test_placeholder_delivery_reports_failed_acceptance_checks(tmp_path):
    """An incomplete delivery must not claim Plancy acceptance."""
    manifest = assemble(output=tmp_path, fixtures=FIXTURES, seed=42, validation_fraction=0.2)

    assert manifest["acceptance"]["accepted"] is False
    assert "sample" in manifest["acceptance"]["failed"]
    assert manifest["acceptance"]["checks"]["required_files"] is True


def test_delivery_dashboard_has_three_contract_roles(tmp_path):
    """Dashboard roles are structured principals backed by the Redwood directory."""
    manifest = assemble(output=tmp_path, fixtures=FIXTURES, seed=42, validation_fraction=0.2)

    assert manifest["acceptance"]["checks"]["dashboard"] is True


def test_require_acceptance_rejects_a_placeholder_delivery(tmp_path):
    """Callers requesting an accepted delivery cannot receive a known skeleton."""
    with pytest.raises(ValueError, match="Plancy delivery acceptance failed"):
        assemble(
            output=tmp_path,
            fixtures=FIXTURES,
            seed=42,
            validation_fraction=0.2,
            require_acceptance=True,
        )


def test_fixture_delivery_has_contract_directory_and_container_acls(tmp_path):
    """The emitted directory and sources must be usable for access-aware ingestion."""
    assemble(output=tmp_path, fixtures=FIXTURES, seed=42, validation_fraction=0.2)

    directory = yaml.safe_load((tmp_path / "plancy" / "config" / "people.yaml").read_text())
    person = directory["people"][0]
    assert {"id", "name", "role", "department", "manager", "email", "principal", "accounts"}.issubset(person)
    assert person["accounts"]

    sources = yaml.safe_load((tmp_path / "plancy" / "config" / "sources.yaml").read_text())
    assert all(source["containers"] for source in sources["sources"])
    assert all(container["acl"] for source in sources["sources"] for container in source["containers"])


def test_operational_task_history_emits_native_jira_revisions(tmp_path):
    """A source task and its status history become full Jira snapshots, not corpus documents."""
    assemble(output=tmp_path, fixtures=FIXTURES, seed=42, validation_fraction=0.2)

    with gzip.open(tmp_path / "plancy" / "fixtures" / "timeline.jsonl.gz", "rt", encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle]
    task_events = [
        event for event in events if event["source"] == "jira-redwood" and event["external_id"] == "task-1"
    ]

    assert [event["revision"] for event in task_events] == [1, 2, 3, 4]
    assert task_events[-1]["previous_event_id"] == task_events[-2]["event_id"]
    assert task_events[-1]["payload"]["key"].startswith("PROJECT-")
    assert task_events[-1]["payload"]["fields"]["status"]["name"] == "Done"
    assert len(task_events[-1]["payload"]["changelog"]["histories"]) == 3


def test_operational_document_emits_native_confluence_page(tmp_path):
    """An operational document is delivered as a Confluence API snapshot."""
    assemble(output=tmp_path, fixtures=FIXTURES, seed=42, validation_fraction=0.2)

    with gzip.open(tmp_path / "plancy" / "fixtures" / "timeline.jsonl.gz", "rt", encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle]
    document = next(
        event
        for event in events
        if event["source"] == "confluence-redwood" and event["external_id"] == "documents-doc-1"
    )

    assert document["payload"]["title"] == "Deployment incident review"
    assert document["payload"]["body"]["atlas_doc_format"]["value"]


def test_operational_chain_emits_source_expressed_incident_cause(tmp_path):
    """The incident-to-PR link comes from the source foreign key, never event position."""
    assemble(output=tmp_path, fixtures=FIXTURES, seed=42, validation_fraction=0.2)

    links = [
        json.loads(line)
        for line in (tmp_path / "plancy" / "gold_links.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    cause = next(
        link
        for link in links
        if link["source_entity_id"] == "jira.incident-1"
        and link["target_entity_id"] == "gitlab.platform-inference.mr-pr-1"
    )

    assert cause["predicate"] == "caused_by"
    assert cause["rule_id"] == "incident.pull_request_id.v1"

    document_link = next(
        link
        for link in links
        if link["source_entity_id"] == "confluence.page-documents-doc-1"
        and link["target_entity_id"] == "jira.incident-1"
    )
    assert document_link["predicate"] == "informed_by"
    assert document_link["rule_id"] == "source_field.linked_incident_id.v1"


def test_slack_delivery_adapter_contains_executable_mapping_code(tmp_path):
    """A provider present in the timeline ships code, not only a metadata declaration."""
    assemble(output=tmp_path, fixtures=FIXTURES, seed=42, validation_fraction=0.2)

    adapter = tmp_path / "plancy" / "adapters" / "slack" / "adapter.py"
    assert adapter.exists()
    assert "def adapt(" in adapter.read_text(encoding="utf-8")
    assert (adapter.parent / "fixtures" / "message.json").exists()
    assert (adapter.parent / "tests" / "test_adapter.py").exists()


def test_operational_delivery_recognizes_native_core_and_slack_payloads(tmp_path):
    """A native Jira, GitLab, and Slack payload is not rejected as a corpus wrapper."""
    manifest = assemble(output=tmp_path, fixtures=FIXTURES, seed=42, validation_fraction=0.2)

    assert manifest["acceptance"]["checks"]["source_native_payloads"] is True


def test_delivery_why_case_has_real_evidence_root_and_answer_fact(tmp_path):
    """A why case points at an expressed causal chain and facts in its source evidence."""
    assemble(output=tmp_path, fixtures=FIXTURES, seed=42, validation_fraction=0.2)

    cases = [
        json.loads(line)
        for line in (tmp_path / "plancy" / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    why_case = next(case for case in cases if case["category"] == "why")

    assert {"id", "evidence", "roots", "forbidden", "answer_facts", "rationale"}.issubset(why_case)
    assert why_case["roots"]
    assert why_case["answer_facts"]
    assert "record " not in why_case["question"].casefold()


def test_delivery_cases_pass_evidence_and_causal_path_validation(tmp_path):
    """The acceptance validator resolves case evidence, facts, roots, and ACL negatives."""
    manifest = assemble(output=tmp_path, fixtures=FIXTURES, seed=42, validation_fraction=0.2)

    assert manifest["acceptance"]["checks"]["evaluation_cases"] is True
