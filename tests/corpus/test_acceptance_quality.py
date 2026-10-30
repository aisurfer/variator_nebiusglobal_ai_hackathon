"""Regression tests for strict production-quality acceptance checks."""

from __future__ import annotations

from pathlib import Path

from variator.corpus import acceptance


def _production(events: list[dict[str, object]]) -> list[dict[str, object]]:
    padding = 10_001 - len(events)
    return events + [
        {
            "entity_id": f"padding-{index}",
            "op": "delete",
            "payload": None,
            "revision": 1,
        }
        for index in range(max(0, padding))
    ]


def _chain_event(chain_id: str, stage: str, at: str) -> dict[str, object]:
    return {
        "entity_id": f"{chain_id}:{stage}",
        "aliases": [f"{chain_id}:{stage}"],
        "op": "upsert",
        "revision": 1,
        "world_at": at,
        "payload": {
            "redwoodCausalChainId": chain_id,
            "redwoodStage": stage,
        },
    }


def test_temporal_acceptance_rejects_fixed_chain_duration_and_gap() -> None:
    stages = acceptance._ENTERPRISE_CHAIN_STAGES
    events = [
        _chain_event(
            f"enterprise-causal-batch-{index:04d}",
            stage,
            f"2026-01-01T{8 + stage_index // 3:02d}:{(stage_index % 3) * 20:02d}:00+00:00",
        )
        for index in range(1_000)
        for stage_index, stage in enumerate(stages)
    ]

    assert not acceptance._has_chain_temporal_diversity(_production(events))


def test_external_outcome_acceptance_rejects_one_incident_template() -> None:
    events = [
        _chain_event(f"external-chain-{index:04d}", "action", "2026-01-01T08:00:00+00:00")
        for index in range(5)
    ] + [
        _chain_event(f"external-chain-{index:04d}", "incident", "2026-01-01T09:00:00+00:00")
        for index in range(5)
    ]

    assert not acceptance._has_external_outcome_diversity(_production(events))


def test_external_denominator_acceptance_rejects_silent_443_substitution(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        '{"document_counts_by_layer":{"external":443}}',
        encoding="utf-8",
    )

    assert not acceptance._has_external_denominator(tmp_path, _production([]))


def test_semantic_acceptance_rejects_fixed_comment_sequence() -> None:
    events = []
    for index in range(100):
        comments = [
            {"body": "Reviewer question: confirm the evidence."},
            {"body": "Author response: the evidence is attached."},
            {"body": "Blocker found: owner action is required."},
        ]
        events.append(
            {
                "entity_id": f"task-{index}",
                "op": "upsert",
                "revision": 1,
                "source": "jira-redwood",
                "payload": {
                    "fields": {
                        "issuetype": {"name": "Task"},
                        "comment": {"comments": comments},
                    }
                },
            }
        )

    assert not acceptance._has_semantic_diversity(_production(events))


def test_merge_request_acceptance_rejects_fixed_cardinalities_and_missing_patterns() -> None:
    events = [
        {
            "entity_id": f"mr-{index}",
            "op": "upsert",
            "revision": 1,
            "source": "gitlab-redwood",
            "object_type": "merge_request",
            "external_id": f"dsid_{index}",
            "payload": {
                "reviewers": ["E001", "E002"],
                "approvals": ["E001"],
                "pipelines": [{"status": "success"}],
                "changed_files": ["src/service.py", "tests/test_service.py"],
                "commits": ["abc1234"],
                "review_comments": [{"body": "Looks good."}],
            },
        }
        for index in range(5)
    ]

    assert not acceptance._has_mr_diversity(_production(events))


def test_implicit_acceptance_rejects_target_id_in_source_payload() -> None:
    source = {
        "entity_id": "source",
        "aliases": ["source"],
        "external_id": "SRC",
        "op": "upsert",
        "revision": 1,
        "payload": {"text": "The target TGT was approved."},
    }
    target = {
        "entity_id": "target",
        "aliases": ["target"],
        "external_id": "TGT",
        "op": "upsert",
        "revision": 1,
        "payload": {"text": "approval"},
    }
    links = [
        {
            "source_entity_id": "source",
            "target_entity_id": "target",
            "explicit": False,
            "evidence_quote": "approved",
        }
        for _ in range(500)
    ]

    assert not acceptance._has_true_implicit_links(_production([source, target]), links)


def test_case_acceptance_rejects_all_numeric_cases_with_zero_ar() -> None:
    events = [
        {
            "entity_id": f"invoice-{index}",
            "aliases": [f"invoice-{index}"],
            "op": "upsert",
            "revision": 1,
            "object_type": "invoice",
            "payload": {"properties": {"balance": 0, "status": "paid"}},
        }
        for index in range(10)
    ]
    cases = [
        {
            "category": "numeric",
            "evidence": [{"entity_id": f"invoice-{index}", "version": 1}],
        }
        for index in range(10)
    ]

    assert not acceptance._has_case_quality(cases, _production(events))


def test_project_acceptance_rejects_uniform_ladders_without_branching() -> None:
    events = [
        {
            "entity_id": f"project-{index}",
            "aliases": [f"project-{index}"],
            "external_id": f"project-{index}",
            "op": "upsert",
            "revision": 1,
            "payload": {"description": f"Depends on project-{index - 1}"},
        }
        for index in range(64)
    ]
    links = [
        {
            "source_entity_id": f"project-{index}",
            "target_entity_id": f"project-{index - 1}",
            "evidence_quote": f"project-{index - 1}",
            "rule_id": "enterprise_project.depends_on.v1",
        }
        for index in range(1, 64)
    ]

    assert not acceptance._has_project_dag_shape(_production(events), links)
