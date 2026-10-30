"""Semantic and source-native contracts for ordinary enterprise work."""

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from variator.corpus.gold_links import materialize_gold_links
from variator.corpus.models import CorpusDocument
from variator.corpus.source_events import materialize_source_events


def _documents(count: int = 20) -> list[CorpusDocument]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        CorpusDocument(
            doc_id=f"enterprise_rag:github:{index:04d}",
            layer="internal",
            source_type="github",
            timestamp=start + timedelta(days=index),
            author="engineer",
            content=(
                "The billing export loses tenant-level reconciliation context when a retry resumes "
                "after a provider timeout."
            ),
            provenance={
                "source": "enterprise_rag",
                "dsid": f"github:{index:04d}",
                "title": f"Billing reconciliation change {index}",
            },
        )
        for index in range(count)
    ]


def _latest(events):
    latest = {}
    for event in events:
        if event.payload is None:
            continue
        previous = latest.get(event.entity_id)
        if previous is None or event.revision > previous.revision:
            latest[event.entity_id] = event
    return list(latest.values())


def _ordinary(events):
    return [
        event
        for event in _latest(events)
        if (event.payload or {}).get("redwoodEnterpriseStage") == "task"
        and str((event.payload or {}).get("redwoodEnterpriseWorkId", "")).startswith(
            "enterprise-ordinary-"
        )
    ]


def test_ordinary_tasks_are_concrete_and_source_native() -> None:
    events = materialize_source_events({}, _documents())
    tasks = _ordinary(events)
    assert tasks

    required_labels = (
        "Problem:",
        "Context:",
        "Expected outcome:",
        "Owner responsibility:",
        "Constraints:",
        "Acceptance criteria:",
    )
    signatures = set()
    for event in tasks:
        payload = event.payload or {}
        description = str(payload["fields"]["description"])
        assert all(label in description for label in required_labels)
        references = payload["redwoodEnterpriseReferences"]
        assert {
            "workId",
            "taskId",
            "projectId",
            "epicId",
            "decisionId",
        }.issubset(references)
        assert references["taskId"] == event.external_id
        signatures.add(re.sub(r"\b(?:ER|ENT|DEP|INC)-[A-Z0-9-]+\b|\b\d+\b", "<id>", description))

    assert len(signatures) / len(tasks) >= 0.80


def test_ordinary_work_has_sequential_native_gold_links_and_ordered_outcomes() -> None:
    events = materialize_source_events({}, _documents(200))
    links = materialize_gold_links(events, {})
    latest = _latest(events)
    by_external = {event.external_id: event for event in latest}
    by_alias = {event.aliases[0]: event for event in latest if event.aliases}
    ordinary_tasks = _ordinary(events)

    for task in ordinary_tasks:
        refs = task.payload["redwoodEnterpriseReferences"]
        mr_ids = [
            event.external_id
            for event in latest
            if event.source == "gitlab-redwood"
            and event.payload
            and event.payload.get("redwoodEnterpriseWorkId") == refs["workId"]
        ]
        if not mr_ids:
            assert refs["workId"].startswith("enterprise-ordinary-task-only-")
            continue
        assert all(
            {
                "workId",
                "taskId",
                "projectId",
                "epicId",
                "decisionId",
            }.issubset(by_external[mr_id].payload["redwoodEnterpriseReferences"])
            for mr_id in mr_ids
        )

        expected = {
            ("part_of", refs["epicId"]),
                ("decided_in", refs["decisionId"]),
            ("part_of", refs["projectId"]),
        }
        actual = {
            (link.predicate, by_alias[link.target_entity_id].external_id)
            for link in links
            if link.source_entity_id == task.aliases[0]
        }
        assert expected.issubset(actual)

    assert any(link.rule_id == "enterprise_ordinary.mr.task.v1" for link in links)
    assert any(link.rule_id == "enterprise_ordinary.deployment.mr.v1" for link in links)
    assert any(link.rule_id == "enterprise_ordinary.incident.deployment.v1" for link in links)

    stages = defaultdict(list)
    for event in latest:
        payload = event.payload or {}
        work_id = str(payload.get("redwoodEnterpriseWorkId") or "")
        stage = str(payload.get("redwoodEnterpriseStage") or "")
        if work_id.startswith("enterprise-ordinary-") and stage:
            stages[work_id].append((stage, event.world_at))
    for _work_id, work_events in stages.items():
        by_stage = defaultdict(list)
        for stage, world_at in work_events:
            by_stage[stage].append(world_at)
        if "merge_request" not in by_stage:
            continue
        assert max(by_stage["task"]) < min(by_stage["merge_request"])
        if "deployment" in by_stage:
            assert max(by_stage["merge_request"]) < min(by_stage["deployment"])
        if "incident" in by_stage:
            assert max(by_stage["deployment"]) < min(by_stage["incident"])


def test_project_and_decision_prose_is_distinct_and_dependencies_are_written() -> None:
    events = materialize_source_events({}, _documents(320))
    pages = [
        event
        for event in _latest(events)
        if event.source == "confluence-redwood"
        and (
            event.external_id.startswith("enterprise-project-")
            or event.external_id.startswith("enterprise-decision-")
            and not event.external_id.startswith("enterprise-decision-record-")
        )
    ]
    project_text = [
        str(event.payload["body"]["atlas_doc_format"]["value"])
        for event in pages
        if event.external_id.startswith("enterprise-project-")
    ]
    decision_text = [
        str(event.payload["body"]["atlas_doc_format"]["value"])
        for event in pages
        if event.external_id.startswith("enterprise-decision-")
    ]
    def normalize(value: str) -> str:
        return re.sub(
            r"\b(?:enterprise|project|decision|epic)-[a-z0-9-]+\b|\b\d+\b",
            "<id>",
            value.casefold(),
        )

    assert len({normalize(value) for value in project_text}) / len(project_text) >= 0.80
    assert len({normalize(value) for value in decision_text}) / len(decision_text) >= 0.80
    project_events = [event for event in pages if event.external_id.startswith("enterprise-project-")]
    dependent_projects = [
        event
        for event in project_events
        if (event.payload.get("redwoodReferences") or {}).get("dependsOnProjectIds")
    ]
    assert dependent_projects
    assert all(
        "depends on project" in str(event.payload["body"]["atlas_doc_format"]["value"]).casefold()
        for event in dependent_projects
    )
