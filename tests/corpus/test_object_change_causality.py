"""Object-change causality from communication to operational state."""

from collections import defaultdict

from variator.corpus.gold_links import materialize_gold_links
from variator.corpus.source_events import materialize_source_events


def _chains() -> dict[str, list[dict[str, object]]]:
    return {
        "accounts": [
            {"account_id": f"A{index:03d}", "account_name_synthetic": f"Customer {index}"}
            for index in range(1, 7)
        ],
        "employees": [{"employee_id": f"E{index:03d}", "department": "sales"} for index in range(1, 13)],
        "opportunities": [
            {
                "opportunity_id": f"O{index:03d}",
                "account_id": f"A{index:03d}",
                "contract_value_usd": 100_000,
            }
            for index in range(1, 7)
        ],
    }


def _stages(events):
    grouped = defaultdict(dict)
    for event in events:
        payload = event.payload or {}
        episode_id = payload.get("redwoodEpisodeId") or payload.get("properties", {}).get(
            "redwood_episode_id"
        )
        stage = payload.get("redwoodStage")
        if not episode_id or not stage:
            continue
        previous = grouped[str(episode_id)].get(str(stage))
        if previous is None or event.revision > previous.revision:
            grouped[str(episode_id)][str(stage)] = event
    return grouped


def test_communication_changes_are_source_native_and_ordered() -> None:
    chains = _chains()
    events = materialize_source_events(chains, seed=42)
    grouped = _stages(events)
    support = next(stages for stages in grouped.values() if "postmortem" in stages)

    email = support["customer_email"]
    ticket = support["ticket"]
    meeting = support["meeting"]
    task = support["task"]
    merge_request = support["merge_request"]
    deployment = support["deployment"]
    incident = support["incident"]

    assert email.external_id in ticket.payload["fields"]["description"]
    assert meeting.revision >= 2
    assert "protocol" in meeting.payload
    assert meeting.payload["protocol"]["decision"]
    assert task.external_id in merge_request.payload["description"]
    assert merge_request.external_id in deployment.payload["body"]["atlas_doc_format"]["value"]
    assert deployment.external_id in incident.payload["fields"]["description"]
    ordered = [email, ticket, meeting, task, merge_request, deployment, incident]
    assert [event.world_at for event in ordered] == sorted(event.world_at for event in ordered)
    assert task.payload["fields"]["comment"]["comments"]


def test_approval_email_changes_deal_and_finance_has_all_terminal_paths() -> None:
    chains = _chains()
    events = materialize_source_events(chains, seed=42)
    links = materialize_gold_links(events, chains)
    episode_deals = [
        event
        for event in events
        if event.object_type == "deal"
        and event.revision == 5
        and event.payload.get("properties", {}).get("redwood_episode_id")
    ]
    assert episode_deals
    assert all(event.payload["properties"].get("approval_email_id") for event in episode_deals)
    assert any(link.rule_id == "episode.commercial.approval.v1" for link in links)

    quote_statuses = {
        event.payload["properties"]["status"]
        for event in events
        if event.object_type == "quote" and event.payload.get("properties", {}).get("redwood_episode_id")
    }
    invoice_statuses = {
        event.payload["properties"]["status"]
        for event in events
        if event.object_type == "invoice" and event.payload.get("properties", {}).get("redwood_episode_id")
    }
    assert "revoked" in quote_statuses
    assert "voided" in invoice_statuses
    assert {"issued", "paid", "partial"}.issubset(invoice_statuses)
