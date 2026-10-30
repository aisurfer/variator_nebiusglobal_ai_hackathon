"""Targeted contracts for source-native merge requests and operational time."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from variator.corpus.models import CorpusDocument
from variator.corpus.source_events import materialize_source_events


def _documents(count: int = 240) -> list[CorpusDocument]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        CorpusDocument(
            doc_id=f"enterprise_rag:github:{index:04d}",
            layer="internal",
            source_type="github",
            timestamp=start + timedelta(days=index % 180),
            author="engineer",
            content=f"Implement the themed change for work item {index}.",
            provenance={
                "source": "enterprise_rag",
                "dsid": f"github:{index:04d}",
                "title": f"Work item {index}",
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
    return latest


def _merge_request_history(events):
    histories = defaultdict(list)
    for event in events:
        if event.source == "gitlab-redwood" and event.object_type == "merge_request":
            histories[event.entity_id].append(event)
    return histories


def _normalized_path(value: object) -> str:
    return re.sub(r"\b(?:[a-f0-9]{7,40}|\d+)\b", "<id>", str(value).casefold())


def test_merge_request_paths_have_realistic_distribution_and_source_references() -> None:
    events = materialize_source_events({}, _documents())
    merge_requests = [
        event
        for event in _latest(events).values()
        if event.source == "gitlab-redwood"
        and event.object_type == "merge_request"
        and event.external_id.startswith("github-")
    ]
    file_counts = [len(event.payload["changed_files"]) for event in merge_requests]
    paths = {
        _normalized_path(path)
        for event in merge_requests
        for path in event.payload["changed_files"]
    }

    assert merge_requests
    assert min(file_counts) >= 1
    assert max(file_counts) <= 20
    assert sum(count > 4 for count in file_counts) / len(file_counts) >= 0.10
    assert sum(count >= 10 for count in file_counts) / len(file_counts) >= 0.02
    assert len(paths) >= 500

    for event in merge_requests:
        payload = event.payload
        changed_files = set(payload["changed_files"])
        summary = payload["diff_summary"]
        assert summary["files_changed"] == len(changed_files)
        assert set(summary["paths"]) == changed_files
        for commit in payload["commits"]:
            assert set(commit["paths"]).issubset(changed_files)
            assert any(path in commit["message"] for path in commit["paths"])
        for comment in payload["review_comments"]:
            assert any(path in comment["body"] for path in changed_files)

        change_kind = payload["sourceLifecycle"]["changeKind"]
        if change_kind == "docs-only":
            assert all(path.startswith("docs/") for path in changed_files)
        if change_kind == "test-only":
            assert all(path.startswith("tests/") for path in changed_files)
        if change_kind == "rollback":
            lifecycle = payload["sourceLifecycle"]
            assert set(lifecycle["previousDeploymentPaths"]) == changed_files


def test_merge_request_lifecycle_and_routine_events_keep_causal_rhythm() -> None:
    events = materialize_source_events({}, _documents())
    latest = _latest(events)
    allowed_off_hours = {"incident", "mitigation", "emergency_deployment"}

    for event in latest.values():
        payload = event.payload or {}
        stage = str(
            payload.get("redwoodStage")
            or payload.get("redwoodExternalStage")
            or payload.get("redwoodEnterpriseStage")
            or ""
        )
        on_call_kind = str(payload.get("redwoodOnCallKind") or "")
        if event.world_at.weekday() >= 5 or not 8 <= event.world_at.hour < 18:
            assert stage in allowed_off_hours or on_call_kind in allowed_off_hours

    histories = _merge_request_history(events)
    assert histories
    cancelled = set()
    failed = set()
    for entity_id, history in histories.items():
        ordered = sorted(history, key=lambda event: event.revision)
        payloads = [event.payload for event in ordered]
        final = payloads[-1]
        if final["state"] == "closed":
            cancelled.add(entity_id)
            work_id = final["sourceLifecycle"]["workId"]
            assert not any(
                event.payload
                and event.payload.get("redwoodEnterpriseWorkId") == work_id
                and event.payload.get("redwoodEnterpriseStage") == "deployment"
                for event in events
            )
        if any(
            pipeline.get("status") == "failed" or pipeline.get("failed_checks")
            for payload in payloads
            for pipeline in payload["pipelines"]
        ):
            failed.add(entity_id)
        for payload in payloads:
            attempts = [
                (
                    datetime.fromisoformat(pipeline["started_at"]),
                    datetime.fromisoformat(pipeline["finished_at"]),
                )
                for pipeline in payload["pipelines"]
            ]
            assert attempts == sorted(attempts)
            assert all(left[1] < right[0] for left, right in zip(attempts, attempts[1:], strict=False))

    total = len(histories)
    assert cancelled or total
    assert 0.10 <= len(failed) / total <= 0.25
