"""Regression tests for the canonical raw-evidence Redwood audit."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from gzip import open as gzip_open
from pathlib import Path

import yaml

from variator.corpus import acceptance

_SCRIPT = Path(__file__).parents[2] / "scripts" / "audit_redwood.py"
_SPEC = importlib.util.spec_from_file_location("audit_redwood", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
audit_redwood = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit_redwood)


def _event(
    entity_id: str,
    at: datetime,
    *,
    source: str = "jira-redwood",
    object_type: str = "issue",
    payload: dict[str, object] | None = None,
    acl: list[str] | None = None,
    container: str = "ENG",
    external_id: str | None = None,
    revision: int = 1,
) -> dict[str, object]:
    value = payload or {"fields": {"key": external_id or entity_id}}
    return {
        "entity_id": entity_id,
        "aliases": [entity_id],
        "external_id": external_id or entity_id,
        "source": source,
        "object_type": object_type,
        "op": "upsert",
        "revision": revision,
        "sequence": revision,
        "world_at": at.isoformat(),
        "observed_at": (at + timedelta(minutes=1)).isoformat(),
        "original_occurred_at": at.isoformat(),
        "time_transform_id": "test-v1",
        "container": container,
        "acl": acl or ["grp:engineering"],
        "payload": value,
    }


def _write_delivery(
    root: Path,
    events: list[dict[str, object]],
    cases: list[dict[str, object]] | None = None,
) -> None:
    (root / "fixtures").mkdir(parents=True)
    (root / "config").mkdir()
    with gzip_open(root / "fixtures" / "timeline.jsonl.gz", "wt", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    (root / "gold_links.jsonl").write_text("", encoding="utf-8")
    (root / "cases.jsonl").write_text(
        "\n".join(json.dumps(case) for case in cases or []) + ("\n" if cases else ""),
        encoding="utf-8",
    )
    (root / "dashboard.json").write_text(json.dumps({"roles": [], "metrics": []}), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({"acceptance": {"accepted": True}}), encoding="utf-8")
    (root / "config" / "people.yaml").write_text(
        yaml.safe_dump(
            {
                "admins": ["user:admin"],
                "groups": {"grp:engineering": {"name": "Engineering"}},
                "people": [
                    {
                        "id": "RW-E001",
                        "name": "Redwood Employee",
                        "principal": "user:redwood-employee",
                        "groups": ["grp:engineering"],
                        "memberships": [
                            {
                                "group": "grp:engineering",
                                "valid_from": "2026-01-01T00:00:00+00:00",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "config" / "sources.yaml").write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {
                        "id": "jira-redwood",
                        "containers": {
                            "ENG": {"acl": ["grp:engineering"]},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_canonical_audit_rejects_raw_corruption_even_when_manifest_claims_acceptance(tmp_path: Path) -> None:
    event = _event("jira.issue-1", datetime(2026, 1, 1, 9, tzinfo=UTC))
    _write_delivery(tmp_path, [event])

    report = acceptance.audit_delivery(tmp_path)

    assert report["accepted"] is False
    assert report["checks"]["source_native_payloads"] is False


def test_causal_direction_uses_redwood_external_stage_and_rejects_reversed_edge() -> None:
    old = _event(
        "external-action-1",
        datetime(2026, 1, 1, 9, tzinfo=UTC),
        payload={"redwoodExternalChainId": "external-chain-1", "redwoodExternalStage": "action"},
    )
    new = _event(
        "external-ticket-1",
        datetime(2026, 1, 1, 10, tzinfo=UTC),
        payload={"redwoodExternalChainId": "external-chain-1", "redwoodExternalStage": "ticket"},
    )
    reversed_link = {
        "source_entity_id": old["entity_id"],
        "target_entity_id": new["entity_id"],
        "rule_id": "external_ticket.action.v1",
    }

    assert not acceptance._has_causal_edge_direction([old, new], [reversed_link])


def test_numeric_recomputation_rejects_tampered_answer_fact() -> None:
    at = datetime(2026, 1, 1, 9, tzinfo=UTC)
    events = [
        _event(
            "invoice-1",
            at,
            object_type="invoice",
            source="hubspot-redwood",
            container="FINANCE",
            acl=["grp:finance"],
            payload={"properties": {"status": "partial", "amount": 100, "balance": 40}},
        ),
        _event(
            "invoice-2",
            at + timedelta(minutes=1),
            object_type="invoice",
            source="hubspot-redwood",
            container="FINANCE",
            acl=["grp:finance"],
            payload={"properties": {"status": "paid", "amount": 100, "balance": 0}},
        ),
        _event(
            "payment-1",
            at + timedelta(minutes=2),
            object_type="payment",
            source="hubspot-redwood",
            container="FINANCE",
            acl=["grp:finance"],
            payload={"properties": {"status": "completed", "cumulative_amount": 60}},
        ),
    ]
    case = {
        "category": "numeric",
        "evidence": [
            {"entity_id": "invoice-1", "version": 1},
            {"entity_id": "invoice-2", "version": 1},
            {"entity_id": "payment-1", "version": 1},
        ],
        "answer_facts": [
            {"key": "invoice_total", "any": ["999"]},
            {"key": "paid_total", "any": ["60"]},
            {"key": "outstanding_total", "any": ["140"]},
        ],
    }

    assert not acceptance._has_numeric_case_arithmetic([case], events, strict=True)


def test_atomic_answer_facts_reject_multiple_alternatives() -> None:
    case = {
        "category": "lookup",
        "answer_facts": [{"key": "status", "any": ["Open", "Done"]}],
    }

    assert not acceptance._has_atomic_answer_facts([case], [])


def test_acl_recomputation_rejects_case_that_is_effectively_allowed() -> None:
    at = datetime(2026, 1, 1, 9, tzinfo=UTC)
    event = _event(
        "finance-1",
        at,
        source="hubspot-redwood",
        object_type="invoice",
        container="FINANCE",
        acl=["grp:finance"],
        payload={"properties": {"status": "paid", "account_id": "A001"}},
    )
    directory = {
        "admins": [],
        "people": [
            {
                "principal": "user:finance",
                "groups": ["grp:finance"],
                "memberships": [
                    {"group": "grp:finance", "valid_from": "2026-01-01T00:00:00+00:00"}
                ],
            }
        ],
    }
    sources = {"sources": [{"id": "hubspot-redwood", "containers": {"FINANCE": {"acl": ["grp:finance"]}}}]}
    case = {
        "category": "acl",
        "principal": "user:finance",
        "known_at": at.isoformat(),
        "evidence": [],
        "forbidden": [{"entity_id": "finance-1"}],
        "expect_not_found": True,
    }

    assert not acceptance._has_acl_recomputation([case], [event], directory, sources)


def test_changed_file_distribution_rejects_missing_long_tail() -> None:
    at = datetime(2026, 1, 1, 9, tzinfo=UTC)
    events = [
        _event(
            f"mr-{index}",
            at + timedelta(minutes=index),
            source="gitlab-redwood",
            object_type="merge_request",
            external_id=f"dsid_{index}",
            payload={
                "state": "merged",
                "reviewers": ["E001"],
                "approvals": ["E001"],
                "pipelines": [
                    {"id": f"pipeline-{index}", "status": "success", "finished_at": at.isoformat()}
                ],
                "changed_files": ["src/shared.py"],
                "commits": [f"commit-{index}"],
                "review_comments": [{"body": "review"}],
            },
        )
        for index in range(100)
    ]

    assert not acceptance._has_changed_file_distribution(events)


def test_ordinary_decision_coverage_rejects_work_without_decisions() -> None:
    events = [
        _event(
            f"task-{index}",
            datetime(2026, 1, 1, 9, tzinfo=UTC) + timedelta(minutes=index),
            payload={
                "redwoodEnterpriseWorkId": f"enterprise-ordinary-{index}",
                "redwoodEnterpriseStage": "task",
            },
        )
        for index in range(100)
    ]

    assert not acceptance._has_ordinary_decision_coverage(events)


def test_cancelled_merge_request_cannot_have_downstream_causal_edge() -> None:
    at = datetime(2026, 1, 1, 9, tzinfo=UTC)
    mr = _event(
        "mr-1",
        at,
        source="gitlab-redwood",
        object_type="merge_request",
        external_id="dsid_1",
        payload={"state": "closed"},
    )
    deployment = _event("deployment-1", at + timedelta(hours=1), payload={"redwoodStage": "deployment"})
    link = {
        "source_entity_id": mr["entity_id"],
        "target_entity_id": deployment["entity_id"],
        "rule_id": "enterprise_batch.deployment.mr.v1",
    }

    assert not acceptance._has_cancelled_mr_downstream([mr, deployment], [link])


def test_vacation_conflict_rejects_event_for_unavailable_principal() -> None:
    at = datetime(2026, 2, 5, 9, tzinfo=UTC)
    event = _event(
        "task-1",
        at,
        payload={"assignee": "E001", "redwoodStage": "task"},
    )
    directory = {
        "people": [{"id": "RW-E001", "principal": "user:e001", "accounts": {}}],
        "availability": [
            {
                "person_id": "RW-E001",
                "kind": "vacation",
                "status": "vacation",
                "valid_from": "2026-02-01T00:00:00+00:00",
                "valid_to": "2026-02-10T00:00:00+00:00",
            }
        ],
    }

    assert not acceptance._has_vacation_conflicts([event], directory)


def test_audit_cli_emits_human_and_json_and_returns_nonzero(tmp_path: Path, capsys) -> None:
    _write_delivery(tmp_path, [_event("jira.issue-1", datetime(2026, 1, 1, 9, tzinfo=UTC))])

    result = audit_redwood.main([str(tmp_path), "--format", "both"])
    output = capsys.readouterr().out

    assert result == 1
    assert "Redwood canonical audit: FAIL" in output
    assert '"manifest_consulted": false' in output
