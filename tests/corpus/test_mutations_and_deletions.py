"""Revision, tombstone, and adapter deletion semantics."""

from datetime import UTC, datetime

import pytest

from variator.corpus.adapters import (
    fireflies,
    github,
    gmail,
    google_drive,
    hubspot,
    linear,
    slack,
)
from variator.corpus.models import CorpusDocument
from variator.corpus.source_events import materialize_source_events


def _chains() -> dict[str, list[dict[str, object]]]:
    return {
        "accounts": [
            {"account_id": f"A{index:03d}", "account_name_synthetic": f"Customer {index}"}
            for index in range(1, 7)
        ],
        "employees": [
            {"employee_id": f"E{index:03d}", "department": "engineering"} for index in range(1, 13)
        ],
        "opportunities": [
            {
                "opportunity_id": f"O{index:03d}",
                "account_id": f"A{index:03d}",
                "contract_value_usd": 100_000,
            }
            for index in range(1, 7)
        ],
    }


def test_revisions_reopens_and_tombstones_preserve_immutable_history() -> None:
    document = CorpusDocument(
        doc_id="enterprise_rag:confluence:delete-me",
        layer="internal",
        source_type="confluence",
        timestamp=datetime(2026, 1, 3, tzinfo=UTC),
        author="E001",
        content="Document scheduled for deletion after retention review.",
        provenance={"source": "enterprise_rag", "dsid": "confluence:delete-me", "title": "Delete me"},
    )
    events = materialize_source_events(_chains(), [document], seed=42)
    deleted = [event for event in events if event.op == "delete"]
    assert deleted
    assert all(event.tombstone and event.payload is None for event in deleted)
    assert all(event.previous_event_id == f"{event.entity_id}:{event.revision - 1}" for event in deleted)
    assert all(event.tombstone.effective_acl.allow for event in deleted)

    slack = [event for event in events if event.payload and event.payload.get("redwoodStage") == "slack"]
    assert any(event.revision >= 2 and event.payload.get("edited") for event in slack)
    task_statuses = {
        event.payload["fields"]["status"]["name"]
        for event in events
        if event.payload and event.payload.get("redwoodStage") == "task"
    }
    assert "Reopened" in task_statuses


@pytest.mark.parametrize(
    ("adapter", "source", "key_object_type", "external_id", "payload", "entity_id"),
    [
        (
            slack,
            "slack-redwood",
            "message",
            "slack-event-1",
            {"channel": "C001", "ts": "1700000000.000001", "user": "E001", "text": "Hi"},
            "slack.message.slack-event-1",
        ),
        (
            gmail,
            "gmail-redwood",
            "message",
            "gmail-event-1",
            {"id": "gmail-payload-1", "payload": {"headers": []}},
            "gmail.message.gmail-event-1",
        ),
        (
            linear,
            "linear-redwood",
            "issue",
            "linear-event-1",
            {"id": "linear-payload-1", "title": "Issue"},
            "linear.issue.linear-event-1",
        ),
        (
            github,
            "github-redwood",
            "record",
            "github-event-1",
            {"id": "github-payload-1", "title": "Pull request"},
            "github.record.github-event-1",
        ),
        (
            hubspot,
            "hubspot-redwood",
            "crm_object",
            "hubspot-event-1",
            {"id": "hubspot-payload-1", "properties": {"name": "Deal"}},
            "hubspot.crm_object.hubspot-event-1",
        ),
        (
            fireflies,
            "fireflies-redwood",
            "transcript",
            "fireflies-event-1",
            {"id": "fireflies-payload-1", "title": "Meeting"},
            "fireflies.transcript.fireflies-event-1",
        ),
        (
            google_drive,
            "google-drive-redwood",
            "file",
            "drive-event-1",
            {"id": "drive-payload-1", "name": "Document"},
            "google_drive.file.drive-event-1",
        ),
    ],
)
def test_provider_adapter_delete_returns_stable_tombstone_record(
    adapter: object,
    source: str,
    key_object_type: str,
    external_id: str,
    payload: dict[str, object],
    entity_id: str,
) -> None:
    world_at = "2026-01-01T00:00:00+00:00"
    effective_acl = {
        "allow": [{"principal": "grp:engineering"}],
        "deny": [],
        "inherit": False,
    }
    base_event = {
        "source": source,
        "object_type": key_object_type,
        "external_id": external_id,
        "world_at": world_at,
    }

    live = adapter.adapt({**base_event, "op": "upsert", "payload": payload})[0]
    deleted = adapter.adapt(
        {
            **base_event,
            "op": "delete",
            "payload": None,
            "tombstone": {"effective_acl": effective_acl, "reason": "retention_policy"},
        }
    )

    assert len(deleted) == 1
    record = deleted[0]
    expected_key = {
        "source_id": source,
        "object_type": key_object_type,
        "external_id": external_id,
    }
    assert live["entity_id"] == record["entity_id"] == entity_id
    assert live["key"] == record["key"] == expected_key
    assert record["deleted"] is True
    assert record["valid_from"] == world_at
    assert record["acl"] == effective_acl


def test_hubspot_archived_upsert_remains_deleted() -> None:
    archived = {
        "source": "hubspot-redwood",
        "object_type": "deal",
        "external_id": "D-1",
        "world_at": "2026-01-01T00:00:00+00:00",
        "payload": {"id": "D-1", "properties": {"name": "Archived"}, "archived": True},
    }
    assert hubspot.adapt(archived)[0]["deleted"] is True
