"""One-invariant regression probes for the raw production contract audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from variator.corpus.acceptance import _provider_contract_audit


def _event(
    external_id: str,
    *,
    source: str = "jira-redwood",
    object_type: str = "issue",
    at: datetime | None = None,
    payload: dict[str, object] | None = None,
    acl: list[str] | None = None,
) -> dict[str, object]:
    when = at or datetime(2026, 1, 5, 9, tzinfo=UTC)
    entity_id = f"redwood:{source}:{object_type}:{external_id}"
    return {
        "event_id": f"{entity_id}:1",
        "entity_id": entity_id,
        "sequence": 1,
        "source": source,
        "object_type": object_type,
        "external_id": external_id,
        "op": "upsert",
        "revision": 1,
        "previous_event_id": None,
        "schema_version": "plancy-event/2",
        "original_occurred_at": when.isoformat(),
        "world_at": when.isoformat(),
        "observed_at": (when + timedelta(minutes=1)).isoformat(),
        "time_transform_id": "test-calendar-v1",
        "container": "ENG",
        "acl": acl or ["grp:engineering"],
        "aliases": [f"jira.{external_id}"],
        "payload": payload or {"id": external_id, "fields": {"key": external_id}},
    }


def _audit(events: list[dict[str, object]], links: list[dict[str, object]] | None = None, cases=None):
    return _provider_contract_audit(events, links or [], cases or [], {"people": [], "contacts": []})


def test_timeline_inversion_is_rejected_without_relaxing_other_checks() -> None:
    first = _event("PROJECT-1", at=datetime(2026, 1, 5, 10, tzinfo=UTC))
    second = _event("PROJECT-2", at=datetime(2026, 1, 5, 9, tzinfo=UTC))
    report = _audit([first, second])
    assert report["checks"]["timeline_sorted"] is False


def test_payload_external_id_mismatch_is_rejected() -> None:
    event = _event("PROJECT-1", payload={"id": "PROJECT-OTHER", "fields": {"key": "PROJECT-1"}})
    report = _audit([event])
    assert report["checks"]["provider_payload_ids"] is False


def test_unresolved_gold_endpoint_is_rejected_by_provider_parser() -> None:
    event = _event("PROJECT-1")
    link = {
        "source_entity_id": "jira.PROJECT-1",
        "target_entity_id": "jira.PROJECT-MISSING",
    }
    report = _audit([event], [link])
    assert report["checks"]["gold_links_resolve"] is False


def test_email_without_body_is_rejected() -> None:
    event = _event(
        "<message-1@redwood.example>",
        source="mail-redwood",
        object_type="message",
        payload={
            "id": "<message-1@redwood.example>",
            "conversationId": "thread-1",
            "subject": "A specific customer handoff",
            "from": {"emailAddress": {"address": "e001@redwood.example"}},
            "toRecipients": [{"emailAddress": {"address": "e002@redwood.example"}}],
        },
        acl=["user:e001", "user:e002"],
    )
    report = _audit([event])
    assert report["checks"]["email_completeness"] is False


def test_broad_email_acl_is_rejected() -> None:
    event = _event(
        "<message-1@redwood.example>",
        source="mail-redwood",
        object_type="message",
        payload={
            "id": "<message-1@redwood.example>",
            "conversationId": "thread-1",
            "subject": "A specific customer handoff",
            "body": {"content": "A specific body with owner and outcome."},
            "from": {"emailAddress": {"address": "e001@redwood.example"}},
            "toRecipients": [{"emailAddress": {"address": "e002@redwood.example"}}],
        },
        acl=["grp:all-staff"],
    )
    report = _audit([event])
    assert report["checks"]["email_acl_not_broad"] is False


def test_weekend_routine_event_is_rejected() -> None:
    event = _event("PROJECT-1", at=datetime(2026, 1, 10, 10, tzinfo=UTC))
    report = _audit([event])
    assert report["checks"]["weekday_distribution"] is False


def test_redwood_internal_reference_is_rejected() -> None:
    event = _event("PROJECT-1")
    link = {"source_entity_id": "redwood:internal", "target_entity_id": "jira.PROJECT-1"}
    report = _audit([event], [link])
    assert report["checks"]["no_redwood_external_references"] is False


def test_on_call_ratio_above_three_percent_is_rejected() -> None:
    routine = [_event(f"PROJECT-{index}") for index in range(100)]
    incidents = [
        _event(
            f"INCIDENT-{index}",
            payload={
                "id": f"INCIDENT-{index}",
                "fields": {"key": f"PROJECT-{index}"},
                "redwoodStage": "incident",
            },
        )
        for index in range(4)
    ]
    report = _audit(routine + incidents)
    assert report["checks"]["on_call_distribution"] is False
