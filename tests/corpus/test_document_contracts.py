"""Regression tests for structured document, meeting, and attachment contracts."""

from __future__ import annotations

from variator.corpus.source_events import materialize_source_events


def _document_chains() -> dict[str, list[dict[str, object]]]:
    return {
        "accounts": [
            {"account_id": "A001", "account_name_synthetic": "NorthRiver", "status": "customer"},
            {"account_id": "A002", "account_name_synthetic": "Atlas", "status": "customer"},
        ],
        "employees": [
            {"employee_id": "E001", "department": "finance"},
            {"employee_id": "E002", "department": "engineering"},
            {"employee_id": "E003", "department": "support"},
        ],
        "account_health_daily": [
            {
                "account_health_id": "AH-A001-2026-01-05",
                "account_id": "A001",
                "timestamp": "2026-01-05T09:00:00Z",
                "health_score": 0.82,
            },
            {
                "account_health_id": "AH-A002-2026-01-05",
                "account_id": "A002",
                "timestamp": "2026-01-05T09:00:00Z",
                "health_score": 0.64,
            },
        ],
        "meetings": [
            {
                "meeting_id": "MT001",
                "held_at": "2026-01-06T11:00:00Z",
                "linked_account_id": "A001",
                "linked_incident_id": "INC001",
                "linked_project_id": "P001",
                "notes_document_id": "DOC002",
                "owner_employee_id": "E002",
                "meeting_type": "incident_review",
                "title": "Workflow API incident review",
            }
        ],
        "documents": [
            {
                "document_id": "DOC002",
                "document_type": "postmortem",
                "created_at": "2026-01-06T12:00:00Z",
                "updated_at": "2026-01-06T12:00:00Z",
                "title": "INC001 postmortem",
            }
        ],
    }


def _customer_email_chains() -> dict[str, list[dict[str, object]]]:
    chains = _document_chains()
    chains.update(
        {
            "opportunities": [
                {
                    "opportunity_id": "O001",
                    "account_id": "A001",
                    "owner_employee_id": "E001",
                    "stage": "closed_won",
                    "expected_close_date": "2026-01-10",
                    "contract_value_usd": "115000",
                    "win_probability": "0.98",
                }
            ],
            "tasks": [
                {
                    "task_id": "TASK001",
                    "created_at": "2026-01-07T09:00:00Z",
                    "linked_account_id": "A001",
                    "summary": "Complete NorthRiver implementation",
                    "assignee_employee_id": "E002",
                }
            ],
            "support_tickets": [
                {"ticket_id": "TKT001", "account_id": "A001", "opened_at": "2026-01-07T08:30:00Z"}
            ],
        }
    )
    return chains


def test_north_star_report_has_structured_reconciled_contract() -> None:
    reports = [
        event
        for event in materialize_source_events(_document_chains(), seed=42)
        if event.external_id.startswith("north-star-")
    ]

    assert reports
    payload = reports[0].payload or {}
    assert {"reportPeriod", "owner", "sourceRecords", "metrics", "financials", "reconciliation"} <= set(
        payload
    )
    assert payload["reportPeriod"]["start"] <= payload["reportPeriod"]["end"]
    assert payload["owner"]["employeeId"]
    assert payload["sourceRecords"]
    assert payload["metrics"]
    assert payload["financials"]["mrr_usd"] * 12 == payload["financials"]["arr_usd"]
    assert payload["reconciliation"]["financials"] is True


def test_meeting_protocol_has_structured_sections_and_readable_text() -> None:
    meetings = [
        event
        for event in materialize_source_events(_document_chains(), seed=42)
        if event.source == "fireflies-redwood" and event.object_type == "transcript"
    ]

    assert meetings
    protocol = (meetings[0].payload or {}).get("protocol")
    assert isinstance(protocol, dict)
    assert {"attendees", "agenda", "decisions", "owners", "actions", "timestamps"} <= set(protocol)
    assert protocol["attendees"]
    assert protocol["agenda"]
    assert protocol["decisions"]
    assert protocol["owners"]
    assert protocol["actions"]
    assert protocol["timestamps"]
    assert isinstance((meetings[0].payload or {}).get("transcript"), str)
    assert not isinstance((meetings[0].payload or {}).get("body"), str)


def test_attachments_have_document_meta_and_resolve_to_parent_event() -> None:
    events = materialize_source_events(_customer_email_chains(), seed=42)
    messages = [event for event in events if event.source == "mail-redwood" and event.payload]

    attachments = [attachment for event in messages for attachment in event.payload.get("attachments", [])]
    assert attachments
    for event in messages:
        for attachment in event.payload.get("attachments", []):
            metadata = attachment["documentMeta"]
            assert metadata["attachmentId"] == attachment["id"]
            assert metadata["filename"] == attachment["name"]
            assert metadata["mimeType"] == attachment["contentType"]
            assert metadata["sourceDocument"]
            assert metadata["checksum"]
            assert metadata["parentEventId"] == event.event_id
