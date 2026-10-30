"""Contract coverage for deterministic source-native customer email chains."""

import json
import re
from collections import Counter
from hashlib import sha256

from variator.corpus import acceptance
from variator.corpus.email_events import materialize_email_specs
from variator.corpus.episodes import plan_business_episodes
from variator.corpus.gold_links import materialize_gold_links
from variator.corpus.models import PlancyEvent
from variator.corpus.redwood_delivery import build_directory


def _customer_chains():
    return {
        "accounts": [
            {
                "account_id": f"A{index:03d}",
                "account_name_synthetic": f"Customer {index}",
                "status": "customer",
            }
            for index in range(1, 7)
        ],
        "employees": [{"employee_id": f"E{index:03d}", "department": "sales"} for index in range(1, 7)],
        "opportunities": [
            {
                "opportunity_id": "O001",
                "account_id": "A001",
                "owner_employee_id": "E001",
                "stage": "closed_won",
                "expected_close_date": "2025-02-01",
                "contract_value_usd": "115000",
                "win_probability": "0.98",
            }
        ],
        "tasks": [
            {
                "task_id": "TASK001",
                "created_at": "2025-01-08T09:00:00Z",
                "linked_account_id": "A001",
                "summary": "Complete NorthRiver implementation",
                "assignee_employee_id": "E002",
            }
        ],
        "support_tickets": [
            {
                "ticket_id": "TKT001",
                "account_id": "A001",
                "opened_at": "2025-01-08T08:30:00Z",
            }
        ],
    }


def _email_specs():
    chains = _customer_chains()
    episodes = plan_business_episodes(chains, seed=42)
    return chains, materialize_email_specs(chains, episodes)


def _email_events():
    chains, specs = _email_specs()
    events = []
    for sequence, spec in enumerate(specs, start=1):
        payload = spec["payload"]
        entity_id = str(spec["entity_id"])
        content_sha = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        events.append(
            PlancyEvent(
                event_id=f"{entity_id}:1",
                entity_id=entity_id,
                sequence=sequence,
                source=spec["source"],
                object_type=spec["object_type"],
                external_id=spec["external_id"],
                op="upsert",
                revision=1,
                content_sha256=content_sha,
                original_occurred_at=spec["original"],
                world_at=spec["original"],
                observed_at=spec["original"],
                at=spec["original"],
                modified_at=spec["original"],
                time_transform_id="test-causal-to-redwood",
                container=spec["container"],
                acl=spec["acl"],
                aliases=[f"mail.{payload['id']}"],
                payload=payload,
            )
        )
    return chains, events


def _normalized_subject(value: str) -> str:
    return re.sub(
        r"\b(?:[A-Z]{1,8}[-_])?[A-Z0-9]{4,}\b|\b\d+(?:\.\d+)?\b|\S+@\S+",
        "<id>",
        re.sub(r"\s+", " ", value.casefold()),
    ).strip()


def test_customer_email_chains_vary_and_preserve_replies():
    _chains, events = _email_events()
    messages = [
        event
        for event in events
        if event.source == "mail-redwood" and event.external_id.startswith("<rw-chain-")
    ]

    assert len(messages) >= 3_000
    assert len({event.original_occurred_at.date() for event in messages}) >= 120
    assert all(event.external_id == event.payload["internetMessageId"] for event in messages)
    assert all(event.payload["redwoodEpisodeId"] in event.payload["body"]["content"] for event in messages)
    assert all(event.acl and event.acl.allow for event in messages)
    assert all(event.payload["conversationId"] for event in messages)

    conversation_sizes = Counter(event.payload["conversationId"] for event in messages)
    assert len(conversation_sizes) >= 600
    assert set(conversation_sizes.values()) == set(range(1, 11))
    assert sum(size == 1 for size in conversation_sizes.values()) >= len(conversation_sizes) * 0.05
    assert len(set(conversation_sizes.values())) > 1
    assert len(set(conversation_sizes.values())) == 10
    assert len(set(conversation_sizes.values())) != len(conversation_sizes)
    assert any(event.payload["attachments"] for event in messages)
    assert 0.10 <= sum(bool(event.payload["ccRecipients"]) for event in messages) / len(messages) <= 0.40
    assert (
        0.05
        <= sum(event.payload["subject"].casefold().startswith(("fw:", "fwd:")) for event in messages)
        / len(messages)
        <= 0.10
    )
    assert any(event.payload["redwoodMessageIntent"] == "approval" for event in messages)
    assert any(event.payload["redwoodMessageIntent"] == "rejection" for event in messages)
    assert any(event.payload["redwoodMessageIntent"] == "escalation" for event in messages)
    normalized_subjects = Counter(_normalized_subject(event.payload["subject"]) for event in messages)
    assert max(normalized_subjects.values()) <= len(messages) * 0.05
    by_id = {event.external_id: event for event in messages}
    for conversation_id in conversation_sizes:
        thread = sorted(
            (event for event in messages if event.payload["conversationId"] == conversation_id),
            key=lambda event: event.original_occurred_at,
        )
        assert thread[0].payload["inReplyTo"] is None
        assert [event.payload["inReplyTo"] for event in thread[1:]] == [
            event.payload["internetMessageId"] for event in thread[:-1]
        ]
        for event in thread:
            seen = set()
            parent_id = event.payload["inReplyTo"]
            while parent_id:
                assert parent_id in by_id
                assert parent_id not in seen
                seen.add(parent_id)
                parent = by_id[parent_id]
                assert parent.original_occurred_at < event.original_occurred_at
                parent_id = parent.payload["inReplyTo"]

    reply_delays = [
        later.original_occurred_at - earlier.original_occurred_at
        for conversation_id in conversation_sizes
        for earlier, later in zip(
            sorted(
                (event for event in messages if event.payload["conversationId"] == conversation_id),
                key=lambda event: event.original_occurred_at,
            ),
            sorted(
                (event for event in messages if event.payload["conversationId"] == conversation_id),
                key=lambda event: event.original_occurred_at,
            )[1:],
            strict=False,
        )
    ]
    assert min(reply_delays).total_seconds() <= 15 * 60
    assert max(reply_delays).days >= 3


def test_customer_email_participants_resolve_to_named_directory_contacts_and_acl():
    chains, events = _email_events()
    directory = build_directory(chains)
    people_by_email = {
        str(person["email"]): str(person["principal"])
        for person in directory["people"]
    }
    contacts_by_email = {
        str(contact["email"]): str(contact["principal"])
        for contact in directory["contacts"]
        if contact.get("email") and contact.get("principal")
    }
    principals_by_email = people_by_email | contacts_by_email

    messages = [event for event in events if event.source == "mail-redwood"]
    assert messages
    for event in messages:
        payload = event.payload
        assert payload["subject"].strip()
        assert payload["conversationId"].strip()
        assert payload["body"]["content"].strip()

        addresses = [payload["from"]["emailAddress"]["address"]]
        addresses.extend(
            recipient["emailAddress"]["address"] for recipient in payload["toRecipients"]
        )
        addresses.extend(
            recipient["emailAddress"]["address"] for recipient in payload["ccRecipients"]
        )
        expected_principals = {principals_by_email[address] for address in addresses}
        actual_principals = {rule.principal for rule in event.acl.allow}
        assert actual_principals == expected_principals
        assert "grp:all-staff" not in actual_principals
        assert any(
            address in contacts_by_email and "@" in address
            for address in addresses
        )


def test_customer_email_replies_and_forwards_keep_source_thread_integrity():
    _chains, events = _email_events()
    messages = [event for event in events if event.source == "mail-redwood"]
    by_id = {event.external_id: event for event in messages}
    for event in messages:
        payload = event.payload
        reply_id = payload["inReplyTo"]
        headers = {header["name"]: header["value"] for header in payload["internetMessageHeaders"]}
        if reply_id:
            assert payload["conversationId"] == by_id[reply_id].payload["conversationId"]
            assert headers["In-Reply-To"] == reply_id
            assert headers["References"] == reply_id
        if payload["redwoodForwarded"]:
            assert payload["forwardedFrom"]
            assert "X-Redwood-Forwarded-From" in headers
            assert payload["body"]["content"].strip()

def test_customer_email_replies_are_materialized_as_gold_links():
    chains, events = _email_events()

    links = materialize_gold_links(events, chains)
    reply_links = [
        link for link in links if link.predicate == "replies_to" and link.source_entity_id.startswith("mail.")
    ]

    expected_replies = sum(
        bool(event.payload.get("inReplyTo"))
        for event in events
        if event.source == "mail-redwood" and event.external_id.startswith("<rw-chain-")
    )
    assert len(reply_links) == expected_replies
    assert all(link.expressed_as == "reply" for link in reply_links)
    assert all(link.evidence_quote and link.evidence_quote.startswith("<rw-chain-") for link in reply_links)


def test_email_chain_acceptance_rejects_incomplete_reply_coverage():
    chains, events = _email_events()
    links = materialize_gold_links(events, chains)
    event_records = [event.model_dump(mode="json") for event in events]
    link_records = [link.model_dump(mode="json") for link in links]

    assert acceptance._has_email_chains(event_records, link_records)
    missing_reply = list(link_records)
    reply_index = next(
        index for index, link in enumerate(missing_reply) if link["rule_id"] == "graph.in_reply_to.v2"
    )
    missing_reply.pop(reply_index)
    assert not acceptance._has_email_chains(event_records, missing_reply)
