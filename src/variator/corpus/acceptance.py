"""Compute contract acceptance from emitted Plancy delivery artifacts."""

from __future__ import annotations

import gzip
import json
import re
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

REQUIRED_CASE_FIELDS = {"id", "evidence", "roots", "forbidden", "answer_facts", "rationale"}
CORE_SOURCES = {"jira-redwood", "confluence-redwood", "mail-redwood", "gitlab-redwood", "xwiki-redwood"}
REQUIRED_ADAPTERS = {"slack", "gmail", "linear", "github", "hubspot", "fireflies", "google_drive"}
PRODUCTION_EVENT_THRESHOLD = 10_000
EXPECTED_EXTERNAL_SOURCE_RECORDS = 634
EXPECTED_EXTERNAL_CANONICAL_DOCUMENTS = 443
THIRD_DELIVERY_SNAPSHOT = (
    Path(__file__).parents[3] / "tests" / "corpus" / "fixtures" / "third_delivery_contract.json"
)
THIRD_DELIVERY_MAX_OPEN_TASK_RATIO = 0.783
THIRD_DELIVERY_STATUS_RATE_TARGET = 2.2
_ROUTINE_OBJECT_TYPES = {
    "amendment",
    "contract",
    "deal",
    "file",
    "invoice",
    "issue",
    "merge_request",
    "message",
    "page",
    "payment",
    "quote",
    "ticket",
    "transcript",
}
_ON_CALL_STAGES = {"incident", "mitigation", "emergency_deployment"}

_ENTERPRISE_CHAIN_STAGES = (
    "customer_email",
    "ticket",
    "meeting",
    "decision",
    "task",
    "merge_request",
    "deployment",
    "incident",
    "postmortem",
)
_EXTERNAL_CHAIN_STAGES = (
    "action",
    "ticket",
    "meeting",
    "decision",
    "task",
    "merge_request",
    "deployment",
    "incident",
    "postmortem",
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_gzip_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _loads_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _collect_text_values(value: object, output: list[str]) -> None:
    if isinstance(value, str):
        output.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            _collect_text_values(child, output)
    elif isinstance(value, list):
        for child in value:
            _collect_text_values(child, output)


def _has_native_payload(event: dict[str, Any]) -> bool:
    if event.get("op") != "upsert":
        return event.get("payload") is None
    payload = event.get("payload")
    if not isinstance(payload, dict) or "doc_id" in payload:
        return False
    source = event.get("source")
    object_type = event.get("object_type")
    if source == "jira-redwood":
        return object_type == "issue" and isinstance(payload.get("fields"), dict) and "key" in payload
    if source == "confluence-redwood":
        return object_type == "page" and {"id", "space", "version", "body"}.issubset(payload)
    if source == "mail-redwood":
        return object_type == "message" and {"id", "internetMessageId", "from"}.issubset(payload)
    if source == "gitlab-redwood":
        return object_type in {"merge_request", "commit", "release"} and "id" in payload
    if source == "xwiki-redwood":
        return object_type == "page" and {"id", "title", "version"}.issubset(payload)
    if source == "slack-redwood":
        return object_type == "message" and {"type", "channel", "ts", "user", "text"}.issubset(payload)
    if source == "gmail-redwood":
        return object_type == "message" and {"id", "threadId", "payload"}.issubset(payload)
    if source == "linear-redwood":
        return object_type == "issue" and {"id", "title"}.issubset(payload)
    if source == "github-redwood":
        return "id" in payload and ("title" in payload or "body" in payload)
    if source == "hubspot-redwood":
        return object_type in {
            "crm_object",
            "company",
            "deal",
            "ticket",
            "quote",
            "invoice",
            "payment",
            "contract",
            "amendment",
            "supplier_invoice",
            "operating_cost",
        } and {
            "id",
            "properties",
        }.issubset(payload)
    if source == "fireflies-redwood":
        return object_type == "transcript" and {"id", "title"}.issubset(payload)
    if source == "google-drive-redwood":
        return object_type == "file" and {"id", "name"}.issubset(payload)
    return False


def _has_version_history(events: list[dict[str, Any]]) -> bool:
    revisions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        revisions[str(event.get("entity_id"))].append(event)
    valid_chains = 0
    for chain in revisions.values():
        ordered = sorted(chain, key=lambda event: int(event.get("revision", 0)))
        if len(ordered) < 2:
            continue
        if all(
            event.get("previous_event_id") == previous.get("event_id")
            for previous, event in zip(ordered, ordered[1:], strict=False)
        ):
            valid_chains += 1
    if len(events) > 1000:
        merge_requests = [
            chain
            for chain in revisions.values()
            if chain
            and chain[0].get("source") == "gitlab-redwood"
            and chain[0].get("object_type") == "merge_request"
        ]
        merged = sum(
            1
            for chain in merge_requests
            if any(
                event.get("payload", {}).get("state") == "merged"
                and event.get("payload", {}).get("merged_at")
                and event.get("payload", {}).get("merge_commit_sha")
                for event in chain
            )
        )
        return valid_chains >= 2000 and bool(merge_requests) and merged / len(merge_requests) >= 0.95
    return valid_chains > 0


def _has_directory(directory: dict[str, Any]) -> bool:
    people = directory.get("people")
    if not isinstance(people, list) or len(people) < 45:
        return False
    required = {"id", "name", "role", "department", "manager", "email", "principal", "accounts"}
    provider_accounts = {
        "jira-redwood",
        "confluence-redwood",
        "gitlab-redwood",
        "mail-redwood",
        "slack-redwood",
    }
    organizations = directory.get("organizations")
    contacts = directory.get("contacts")
    return (
        all(
            isinstance(person, dict) and required.issubset(person) and person["accounts"] for person in people
        )
        and all(
            provider_accounts.issubset(set(person["accounts"]))
            for person in people
            if isinstance(person, dict) and isinstance(person.get("accounts"), dict)
        )
        and any(person.get("memberships") for person in people if isinstance(person, dict))
        and bool(organizations)
        and bool(contacts)
        and bool(directory.get("availability"))
        and sum(
            isinstance(organization, dict) and organization.get("kind") in {"client", "prospect", "supplier"}
            for organization in organizations
        )
        >= 5
        and len(contacts) >= len(people) + 5
    )


def _has_access_controls(sources: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    configured = sources.get("sources")
    if not isinstance(configured, list) or not configured:
        return False
    has_containers = all(
        isinstance(source, dict)
        and isinstance(source.get("containers"), (dict, list))
        and source["containers"]
        for source in configured
    )
    has_private_event = any(event.get("acl") for event in events)
    has_explicit_events = all(
        event.get("container")
        and (
            event.get("acl")
            or (event.get("op") == "delete" and event.get("tombstone", {}).get("effective_acl"))
        )
        for event in events
    )
    closed: set[str] = set()
    for source in configured:
        containers = source.get("containers", {})
        if isinstance(containers, dict):
            closed.update(map(str, containers))
        elif isinstance(containers, list):
            closed.update(str(container.get("id")) for container in containers if isinstance(container, dict))
    required_closed = {"HR", "FINANCE", "COMMERCIAL", "LEADERSHIP"}
    return has_containers and has_private_event and has_explicit_events and required_closed.issubset(closed)


def _has_single_calendar(events: list[dict[str, Any]]) -> bool:
    world_times: list[datetime] = []
    for event in events:
        value = event.get("world_at")
        if not isinstance(value, str):
            return False
        try:
            world_times.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return False
        if not event.get("original_occurred_at") or not event.get("time_transform_id"):
            return False
    if not world_times:
        return False
    span_days = (max(world_times) - min(world_times)).total_seconds() / 86400
    if span_days > 180:
        return False
    if len(events) <= 1000:
        return True
    if span_days < 170:
        return False
    active_days: dict[str, set[str]] = defaultdict(set)
    for event in events:
        active_days[str(event.get("source"))].add(str(event.get("world_at"))[:10])
        if event.get("op") == "delete":
            continue
        payload = event.get("payload") or {}
        source = event.get("source")
        current: object | None = None
        if source == "jira-redwood":
            current = payload.get("fields", {}).get("updated")
        elif source == "confluence-redwood":
            current = payload.get("version", {}).get("createdAt")
        elif source == "mail-redwood":
            current = payload.get("sentDateTime")
        elif source == "gitlab-redwood":
            current = payload.get("updated_at")
        elif source == "xwiki-redwood":
            current = payload.get("modified")
        elif source == "slack-redwood":
            current = datetime.fromtimestamp(float(payload.get("ts")), tz=world_times[0].tzinfo).isoformat()
        elif source == "gmail-redwood":
            current = datetime.fromtimestamp(
                int(payload.get("internalDate")) / 1000, tz=world_times[0].tzinfo
            ).isoformat()
        elif source in {"hubspot-redwood", "linear-redwood"}:
            current = payload.get("updatedAt")
        elif source == "fireflies-redwood":
            current = payload.get("date")
        elif source == "google-drive-redwood":
            current = payload.get("modifiedTime")
        if not isinstance(current, str):
            return False
        try:
            current_at = datetime.fromisoformat(current.replace("Z", "+00:00"))
            event_at = datetime.fromisoformat(str(event["world_at"]).replace("Z", "+00:00"))
        except ValueError:
            return False
        if current_at.tzinfo is None:
            current_at = current_at.replace(tzinfo=event_at.tzinfo)
        if abs((current_at - event_at).total_seconds()) > 1:
            return False
    return (
        len(active_days["gitlab-redwood"]) >= 120
        and len(active_days["jira-redwood"]) >= 120
        and len(active_days["mail-redwood"]) >= 10
        and len(active_days["hubspot-redwood"]) >= 20
    )


def _has_semantic_links(links: list[dict[str, Any]], events: list[dict[str, Any]]) -> bool:
    entities = {
        reference
        for event in events
        for reference in [str(event.get("entity_id")), *map(str, event.get("aliases", []))]
    }
    structurally_valid = bool(links) and all(
        link.get("source_entity_id") in entities
        and link.get("target_entity_id") in entities
        and link.get("rule_id")
        and link.get("rule_id") != "sequence-adjacency-v1"
        and not str(link.get("rule_id")).startswith("source_field.")
        and link.get("expressed_as")
        for link in links
    )
    if structurally_valid and len(events) > 1_000:
        thematic = [link for link in links if str(link.get("rule_id", "")).startswith("enterprise_task.")]
        if thematic:
            target_counts = Counter(str(link.get("target_entity_id")) for link in thematic)
            if (
                any(
                    "enterprise-engineering-rollout" in str(link.get("target_entity_id")) for link in thematic
                )
                or len(target_counts) < 50
                or max(target_counts.values()) > len(thematic) * 0.2
            ):
                return False
    return structurally_valid and (
        len(events) <= 1000 or any(link.get("explicit") is False for link in links)
    )


def _has_email_chains(events: list[dict[str, Any]], links: list[dict[str, Any]]) -> bool:
    messages = [
        event
        for event in events
        if event.get("source") == "mail-redwood"
        and str(event.get("external_id", "")).startswith("<rw-chain-")
    ]
    if len(messages) < 3_000:
        return False
    conversations = Counter(str(event.get("payload", {}).get("conversationId")) for event in messages)
    if len(conversations) < 600 or len(set(conversations.values())) < 4:
        return False
    if max(conversations.values()) > 12:
        escalation_ids = {
            str(event.get("payload", {}).get("conversationId"))
            for event in messages
            if (event.get("payload", {}).get("redwoodThreadKind") == "escalation")
        }
        if any(
            size > 12 and conversation_id not in escalation_ids
            for conversation_id, size in conversations.items()
        ):
            return False
    unanswered = sum(size == 1 for size in conversations.values())
    if unanswered / len(conversations) < 0.05:
        return False
    if len({str(event.get("world_at"))[:10] for event in messages}) < 120:
        return False
    alias_by_external_id = {
        str(event.get("external_id")): str((event.get("aliases") or [event.get("entity_id")])[0])
        for event in messages
    }
    expected_replies: set[tuple[str, str]] = set()
    delayed_replies = 0
    reply_count = 0
    sent_at_by_external_id = {
        str(event.get("external_id")): str(event.get("payload", {}).get("sentDateTime")) for event in messages
    }
    for event in messages:
        reply_target = event.get("payload", {}).get("inReplyTo")
        if not reply_target:
            continue
        source_alias = str((event.get("aliases") or [event.get("entity_id")])[0])
        target_alias = alias_by_external_id.get(str(reply_target))
        if not target_alias:
            return False
        expected_replies.add((source_alias, target_alias))
        try:
            sent_at = datetime.fromisoformat(
                str(event.get("payload", {}).get("sentDateTime")).replace("Z", "+00:00")
            )
            previous_at = datetime.fromisoformat(
                sent_at_by_external_id[str(reply_target)].replace("Z", "+00:00")
            )
        except (KeyError, ValueError):
            return False
        reply_count += 1
        delayed_replies += (sent_at - previous_at).total_seconds() >= 24 * 3600
    if not reply_count or delayed_replies / reply_count < 0.10:
        return False
    actual_replies = {
        (str(link.get("source_entity_id")), str(link.get("target_entity_id")))
        for link in links
        if link.get("predicate") == "replies_to" and link.get("rule_id") == "graph.in_reply_to.v2"
    }
    return expected_replies == actual_replies


def _has_financial_histories(events: list[dict[str, Any]]) -> bool:
    episode_events = [
        event
        for event in events
        if event.get("source") == "hubspot-redwood"
        and event.get("payload", {}).get("properties", {}).get("redwood_episode_id")
        and event.get("object_type") in {"deal", "quote", "invoice", "payment"}
    ]
    if not episode_events:
        return len(events) <= 1_000

    by_episode: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for event in episode_events:
        properties = event["payload"]["properties"]
        by_episode[str(properties["redwood_episode_id"])][str(event["object_type"])][
            str(event["entity_id"])
        ].append(event)
    if len(by_episode) < 24:
        return False

    accepted_total = 0.0
    invoiced_total = 0.0
    paid_total = 0.0
    receivable_total = 0.0
    quote_statuses_seen: set[str] = set()
    invoice_statuses_seen: set[str] = set()
    for objects in by_episode.values():
        if len(objects["deal"]) != 1 or len(objects["quote"]) != 1:
            return False
        deal = sorted(next(iter(objects["deal"].values())), key=lambda item: item["revision"])
        quote = sorted(next(iter(objects["quote"].values())), key=lambda item: item["revision"])
        deal_statuses = [item["payload"]["properties"]["status"] for item in deal]
        quote_statuses = [item["payload"]["properties"]["status"] for item in quote]
        quote_statuses_seen.update(str(status) for status in quote_statuses)
        if deal_statuses[:-1] != ["new", "qualified", "proposal_sent", "negotiation"]:
            return False
        if quote_statuses[:3] != ["draft", "sent", "revised"]:
            return False
        won = deal_statuses[-1] == "closed_won"
        if won != (quote_statuses[-1] == "accepted") and not (
            not won and quote_statuses[-1] in {"rejected", "revoked"}
        ):
            return False
        if not won:
            if objects["invoice"] or objects["payment"]:
                return False
            continue
        if len(objects["invoice"]) != 1 or len(objects["payment"]) > 1:
            return False
        invoice = sorted(next(iter(objects["invoice"].values())), key=lambda item: item["revision"])
        invoice_statuses_seen.update(str(item["payload"]["properties"]["status"]) for item in invoice)
        payment = (
            sorted(next(iter(objects["payment"].values())), key=lambda item: item["revision"])
            if objects["payment"]
            else []
        )
        final_quote = quote[-1]["payload"]["properties"]
        final_invoice = invoice[-1]["payload"]["properties"]
        amount = float(final_quote["amount"])
        paid = float(payment[-1]["payload"]["properties"]["cumulative_amount"]) if payment else 0.0
        final_invoice_status = str(final_invoice["status"])
        accepted_total += amount
        invoiced_total += float(final_invoice["amount"])
        paid_total += paid
        receivable_total += float(final_invoice["balance"])
        if amount != float(final_invoice["amount"]):
            return False
        if paid != float(final_invoice["paid_amount"]):
            return False
        expected_balance = 0.0 if final_invoice_status == "voided" else round(amount - paid, 2)
        if expected_balance != float(final_invoice["balance"]):
            return False
        if final_invoice_status == "paid" and (not payment or float(final_invoice["balance"]) != 0):
            return False
        if final_invoice_status in {"overdue", "voided"} and payment:
            if any(
                str(item["payload"]["properties"].get("status")) not in {"pending", "voided"}
                or float(item["payload"]["properties"].get("cumulative_amount") or 0) != 0
                for item in payment
            ):
                return False
        for history in (deal, quote, invoice, payment):
            timestamps: list[datetime] = []
            for event in history:
                try:
                    timestamps.append(datetime.fromisoformat(str(event["world_at"]).replace("Z", "+00:00")))
                except (KeyError, ValueError):
                    return False
            if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
                return False
        causal_starts: list[datetime] = []
        for event in (deal[0], quote[0], invoice[0], *(payment[:1])):
            try:
                causal_starts.append(datetime.fromisoformat(str(event["world_at"]).replace("Z", "+00:00")))
            except (KeyError, ValueError):
                return False
        if causal_starts != sorted(causal_starts):
            return False
    reconciled = round(accepted_total, 2) == round(invoiced_total, 2) and round(
        paid_total + receivable_total, 2
    ) <= round(invoiced_total, 2)
    if len(events) <= 1_000:
        return reconciled
    return (
        reconciled
        and bool({"rejected", "revoked"}.intersection(quote_statuses_seen))
        and {"issued", "overdue", "partial", "paid", "voided"}.issubset(invoice_statuses_seen)
        and receivable_total > 0
    )


def _has_causal_episodes(events: list[dict[str, Any]], links: list[dict[str, Any]]) -> bool:
    required = (
        "customer_email",
        "ticket",
        "slack",
        "meeting",
        "task",
        "merge_request",
        "deployment",
        "incident",
        "postmortem",
    )
    latest_by_stage: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for event in events:
        payload = event.get("payload") or {}
        episode_id = payload.get("redwoodEpisodeId")
        stage = payload.get("redwoodStage") or payload.get("redwoodExternalStage")
        if not episode_id or not stage:
            continue
        previous = latest_by_stage[str(episode_id)].get(str(stage))
        if previous is None or int(event.get("revision", 0)) > int(previous.get("revision", 0)):
            latest_by_stage[str(episode_id)][str(stage)] = event
    complete = [stages for stages in latest_by_stage.values() if set(required).issubset(stages)]
    if not complete:
        return len(events) <= 1_000
    if len(complete) < 24:
        return False
    accounts: set[str] = set()
    for stages in complete:
        try:
            ordered_at = [
                datetime.fromisoformat(str(stages[stage]["world_at"]).replace("Z", "+00:00"))
                for stage in required
            ]
            accounts.add(str(stages["ticket"]["payload"]["redwoodReferences"]["accountId"]))
        except (KeyError, ValueError):
            return False
        if ordered_at != sorted(ordered_at):
            return False
        merge_request = stages["merge_request"]["payload"]
        if merge_request.get("state") not in {"merged", "closed"}:
            return False
        if merge_request.get("state") == "merged" and not (
            merge_request.get("merged_at") and merge_request.get("merge_commit_sha")
        ):
            return False
        if (
            merge_request.get("state") == "closed"
            and "because" not in str(merge_request.get("description", "")).casefold()
        ):
            return False
    if len(accounts) < 6:
        return False
    if sum(str(link.get("rule_id")) == "episode.commercial.approval.v1" for link in links) < 24:
        return False
    if any(
        int(stages["meeting"].get("revision", 0)) < 2
        or not stages["task"].get("payload", {}).get("fields", {}).get("comment", {}).get("comments")
        for stages in complete
    ):
        return False

    episode_links = [link for link in links if str(link.get("rule_id", "")).startswith("episode.")]
    if len(episode_links) < len(complete) * 8:
        return False
    if any(str(link.get("rule_id", "")).startswith("source_field.") for link in links):
        return False
    if sum(bool(link.get("explicit")) for link in episode_links) / len(episode_links) < 0.8:
        return False
    event_by_alias: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("op") != "upsert" or not event.get("payload"):
            continue
        for alias in event.get("aliases", []):
            event_by_alias[str(alias)] = event
    for link in episode_links:
        source = event_by_alias.get(str(link.get("source_entity_id")))
        target = event_by_alias.get(str(link.get("target_entity_id")))
        if not source or not target:
            return False
        payload_text = json.dumps(source.get("payload"), ensure_ascii=False)
        quote = str(link.get("evidence_quote") or "")
        if not quote or quote not in payload_text:
            return False
        if link.get("explicit") and str(target.get("external_id")) not in payload_text:
            return False

    latest_merge_requests: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("source") != "gitlab-redwood" or event.get("object_type") != "merge_request":
            continue
        previous = latest_merge_requests.get(str(event.get("entity_id")))
        if previous is None or int(event.get("revision", 0)) > int(previous.get("revision", 0)):
            latest_merge_requests[str(event.get("entity_id"))] = event
    return all(
        event.get("payload", {}).get("state") in {"merged", "closed"}
        for event in latest_merge_requests.values()
    )


def _has_contract_cases(
    cases: list[dict[str, Any]], events: list[dict[str, Any]], links: list[dict[str, Any]]
) -> bool:
    expected_quotas = {
        "lookup": 9,
        "why": 11,
        "history": 10,
        "as_of": 8,
        "conflict": 6,
        "stale": 6,
        "impact": 3,
        "multi_hop": 6,
        "acl": 8,
        "negative": 6,
        "implicit_ref": 10,
        "numeric": 10,
        "people": 7,
    }
    if len(cases) < 100 or not all(REQUIRED_CASE_FIELDS.issubset(case) for case in cases):
        return False
    strict = len(events) > 10_000 and any(
        (event.get("payload") or {}).get("redwoodEpisodeId") for event in events
    )
    if strict and Counter(str(case.get("category")) for case in cases) != expected_quotas:
        return False
    events_by_entity: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for event in events:
        references = [str(event.get("entity_id")), *map(str, event.get("aliases", []))]
        for entity_id in references:
            events_by_entity[entity_id][int(event.get("revision", 0))] = event
    causal_pairs = {(str(link.get("source_entity_id")), str(link.get("target_entity_id"))) for link in links}
    for case in cases:
        category = case.get("category")
        evidence = case.get("evidence")
        if not isinstance(evidence, list):
            return False
        if category == "negative":
            if evidence or not case.get("expect_not_found") or case.get("answer_facts"):
                return False
            continue
        if not evidence:
            return False
        evidence_events: list[dict[str, Any]] = []
        for item in evidence:
            if not isinstance(item, dict):
                return False
            entity_id = str(item.get("entity_id"))
            revision = item.get("version")
            if not isinstance(revision, int) or revision not in events_by_entity[entity_id]:
                return False
            evidence_events.append(events_by_entity[entity_id][revision])
        text_values: list[str] = []

        _collect_text_values(evidence_events, text_values)
        evidence_text = (
            " ".join(text_values).casefold().replace("\\n", " ").replace("\\t", " ").replace("\\", "")
            + " "
            + json.dumps(evidence_events, ensure_ascii=False).casefold()
        )
        evidence_text = re.sub(r"\s+", " ", evidence_text)
        for fact in case.get("answer_facts", []):
            options = fact.get("any") if isinstance(fact, dict) else None
            fact_key = fact.get("key") if isinstance(fact, dict) else None
            if (
                not isinstance(options, list)
                or not options
                or strict
                and (fact.get("key") == "source_excerpt" or any(len(str(option)) > 120 for option in options))
            ):
                return False
            if (
                strict
                and category == "numeric"
                and fact_key
                in {
                    "invoice_total",
                    "paid_total",
                    "outstanding_total",
                }
            ):
                continue
            def evidence_contains(option: object, *, text: str = evidence_text) -> bool:
                value = re.sub(
                    r"\s+",
                    " ",
                    str(option).casefold().replace("\\n", " ").replace("\\t", " ").replace("\\", ""),
                )
                if value in text:
                    return True
                number = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", str(option))
                if number is None:
                    return False
                compact_value = number.group(0).replace(",", "")
                compact_evidence = text.replace(",", "")
                return compact_value in compact_evidence

            if not any(evidence_contains(option) for option in options):
                return False
        if category == "why":
            roots = case.get("roots")
            if not isinstance(roots, list) or not roots:
                return False
            if strict:
                if not 2 <= len(roots) <= 5:
                    return False
                path = [str(evidence[0].get("entity_id"))] + [
                    str(root.get("entity_id")) for root in roots if isinstance(root, dict)
                ]
                if len(path) != len(roots) + 1 or not all(
                    pair in causal_pairs for pair in zip(path, path[1:], strict=False)
                ):
                    return False
            elif not any(
                (str(item.get("entity_id")), str(root.get("entity_id"))) in causal_pairs
                for item in evidence
                for root in roots
                if isinstance(root, dict)
            ):
                return False
        if strict and category == "history":
            if len(evidence) < 3 or len({str(item.get("entity_id")) for item in evidence}) != 1:
                return False
        if strict and category == "as_of":
            if len(evidence) < 2 or not case.get("known_at"):
                return False
            item = evidence[0]
            reference = str(item.get("entity_id"))
            version = int(item.get("version", 0))
            current_event = events_by_entity[reference].get(version)
            next_event = events_by_entity[reference].get(version + 1)
            if current_event is None or next_event is None:
                return False
            try:
                known_at = datetime.fromisoformat(str(case["known_at"]).replace("Z", "+00:00"))
                observed_at = datetime.fromisoformat(str(current_event["observed_at"]).replace("Z", "+00:00"))
                next_at = datetime.fromisoformat(str(next_event["world_at"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                return False
            if not observed_at <= known_at < next_at:
                return False
        if strict and category == "multi_hop":
            if len({str(event.get("source")) for event in evidence_events}) < 3:
                return False
        if strict and category == "numeric":
            if not all(
                event.get("object_type") in {"deal", "quote", "invoice", "payment"}
                for event in evidence_events
            ):
                return False
            if sum(event.get("object_type") == "deal" for event in evidence_events) < 2:
                return False
            if sum(event.get("object_type") == "invoice" for event in evidence_events) < 2:
                return False
            numeric_facts = {
                str(fact.get("key")): float(fact.get("any", [0])[0])
                for fact in case.get("answer_facts", [])
                if fact.get("key") in {"invoice_total", "paid_total", "outstanding_total"}
            }
            invoice_total = sum(
                float((event.get("payload") or {}).get("properties", {}).get("amount") or 0)
                for event in evidence_events
                if event.get("object_type") == "invoice"
                and str((event.get("payload") or {}).get("properties", {}).get("status")) != "voided"
            )
            paid_total = sum(
                float((event.get("payload") or {}).get("properties", {}).get("cumulative_amount") or 0)
                for event in evidence_events
                if event.get("object_type") == "payment"
            )
            outstanding_total = invoice_total - paid_total
            if any(
                abs(numeric_facts.get(key, float("nan")) - expected) > 0.01
                for key, expected in {
                    "invoice_total": invoice_total,
                    "paid_total": paid_total,
                    "outstanding_total": outstanding_total,
                }.items()
            ):
                return False
        if strict and category == "acl":
            forbidden = case.get("forbidden")
            if not isinstance(forbidden, list):
                return False
            for item in forbidden:
                entity_id = str(item.get("entity_id")) if isinstance(item, dict) else ""
                revisions = events_by_entity.get(entity_id, {})
                if not revisions:
                    return False
                forbidden_event = revisions[max(revisions)]
                evidence_ids = {str(value.get("entity_id")) for value in evidence}
                aliases = {str(alias) for alias in forbidden_event.get("aliases", [])}
                if not evidence_ids.intersection({entity_id, *aliases}):
                    return False
        elif category == "acl" and case.get("forbidden") is None:
            return False
    if strict:
        if sum(str(case.get("principal")) == "user:admin" for case in cases) / len(cases) >= 0.25:
            return False
        normalized_questions = [
            re.sub(
                r"\b[a-z]+[-_]?[a-z]*\d+[a-z0-9-]*\b|\b\d+(?:\.\d+)?\b",
                "id",
                str(case.get("question", "")).casefold(),
            )
            for case in cases
        ]
        if len(set(normalized_questions)) / len(normalized_questions) <= 0.8:
            return False
    return True


def _has_executable_adapters(root: Path, events: list[dict[str, Any]]) -> bool:
    adapters = root / "adapters"
    return all(
        (adapters / provider / "adapter.py").exists()
        and (adapters / provider / "README.md").exists()
        and any((adapters / provider / "fixtures").glob("*.json"))
        and any((adapters / provider / "tests").glob("test_*.py"))
        for provider in REQUIRED_ADAPTERS
    )


def _has_dashboard(root: Path, events: list[dict[str, Any]] | None = None) -> bool:
    path = root / "dashboard.json"
    if not path.exists():
        return False
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    roles = dashboard.get("roles")
    required = {"ceo", "sales", "engineering"}
    metrics = dashboard.get("metrics")
    required_metric_ids = {
        "closed_won_revenue_usd",
        "weighted_pipeline_usd",
        "accepted_proposals_usd",
        "accounts_receivable_usd",
        "overdue_receivables_usd",
        "paid_amount_usd",
        "arr_usd",
        "mrr_usd",
        "churn_rate",
        "expansion_usd",
        "dso_days",
        "discount_usd",
        "gross_margin_pct",
        "forecast_revenue_usd",
        "actual_revenue_usd",
        "supplier_cost_usd",
        "operating_cost_usd",
        "at_risk_arr_usd",
        "active_customers",
        "weekly_active_customers_reports",
    }
    timeline = events if events is not None else _read_gzip_jsonl(root / "fixtures" / "timeline.jsonl.gz")
    production = any(
        (event.get("payload") or {}).get("properties", {}).get("redwood_episode_id") for event in timeline
    )
    reconciled = True
    if production:
        latest: dict[str, dict[str, Any]] = {}
        for event in timeline:
            if event.get("source") != "hubspot-redwood":
                continue
            previous = latest.get(str(event.get("entity_id")))
            if previous is None or int(event.get("revision", 0)) > int(previous.get("revision", 0)):
                latest[str(event.get("entity_id"))] = event
        episode_events = [
            event
            for event in latest.values()
            if event.get("payload", {}).get("properties", {}).get("redwood_episode_id")
        ]
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in episode_events:
            by_type[str(event.get("object_type"))].append(event)

        def props(event: dict[str, Any]) -> dict[str, Any]:
            return event.get("payload", {}).get("properties", {})

        won = [event for event in by_type["deal"] if props(event).get("status") == "closed_won"]
        open_deals = [
            event
            for event in by_type["deal"]
            if props(event).get("status") not in {"closed_won", "closed_lost"}
        ]
        accepted = [event for event in by_type["quote"] if props(event).get("status") == "accepted"]
        overdue = [event for event in by_type["invoice"] if props(event).get("status") == "overdue"]
        expected_values = {
            "closed_won_revenue_usd": sum(float(props(event).get("amount") or 0) for event in won),
            "weighted_pipeline_usd": sum(float(props(event).get("amount") or 0) for event in open_deals),
            "accepted_proposals_usd": sum(float(props(event).get("amount") or 0) for event in accepted),
            "accounts_receivable_usd": sum(
                float(props(event).get("balance") or 0) for event in by_type["invoice"]
            ),
            "overdue_receivables_usd": sum(float(props(event).get("balance") or 0) for event in overdue),
            "paid_amount_usd": sum(
                float(props(event).get("cumulative_amount") or 0) for event in by_type["payment"]
            ),
            "active_customers": len({str(props(event).get("account_id")) for event in by_type["deal"]}),
        }
        active_contracts = [event for event in by_type["contract"] if props(event).get("status") == "active"]
        churned_contracts = [
            event for event in by_type["contract"] if props(event).get("status") == "churned"
        ]
        approved_amendments = [
            event for event in by_type["amendment"] if props(event).get("status") == "approved"
        ]
        supplier_costs = [
            event for event in by_type["supplier_invoice"] if props(event).get("status") == "paid"
        ]
        operating_costs = [
            event for event in by_type["operating_cost"] if props(event).get("status") == "actual"
        ]
        supplier_total = sum(float(props(event).get("amount") or 0) for event in supplier_costs)
        operating_total = sum(float(props(event).get("amount") or 0) for event in operating_costs)
        dso_values: list[float] = []
        for entity_id in {
            str(event.get("entity_id"))
            for event in timeline
            if event.get("object_type") == "invoice"
            and event.get("payload", {}).get("properties", {}).get("redwood_episode_id")
        }:
            history = sorted(
                [event for event in timeline if str(event.get("entity_id")) == entity_id],
                key=lambda event: int(event.get("revision", 0)),
            )
            issued = next((event for event in history if props(event).get("status") == "issued"), None)
            settled = next(
                (event for event in history if props(event).get("status") in {"paid", "voided"}),
                None,
            )
            if issued and settled:
                dso_values.append(
                    (
                        datetime.fromisoformat(str(settled["world_at"]).replace("Z", "+00:00"))
                        - datetime.fromisoformat(str(issued["world_at"]).replace("Z", "+00:00"))
                    ).total_seconds()
                    / 86400
                )
        expected_values.update(
            {
                "arr_usd": sum(float(props(event).get("arr") or 0) for event in active_contracts),
                "mrr_usd": sum(float(props(event).get("mrr") or 0) for event in active_contracts),
                "churn_rate": len(churned_contracts) / max(1, len(active_contracts) + len(churned_contracts)),
                "expansion_usd": sum(
                    float(props(event).get("delta_arr") or 0) for event in approved_amendments
                ),
                "dso_days": sum(dso_values) / max(1, len(dso_values)),
                "discount_usd": sum(float(props(event).get("discount") or 0) for event in won),
                "gross_margin_pct": sum(float(props(event).get("margin") or 0) for event in active_contracts)
                / max(1, len(active_contracts)),
                "forecast_revenue_usd": sum(
                    float(props(event).get("forecast_amount") or 0) for event in by_type["deal"]
                ),
                "actual_revenue_usd": sum(
                    float(props(event).get("amount") or 0)
                    for event in by_type["invoice"]
                    if props(event).get("status") == "paid"
                ),
                "supplier_cost_usd": supplier_total,
                "operating_cost_usd": operating_total,
            }
        )
        metric_by_id = {str(metric.get("id")): metric for metric in metrics if isinstance(metric, dict)}
        reconciled = all(
            metric_id in metric_by_id and float(metric_by_id[metric_id].get("value", -1)) == value
            for metric_id, value in expected_values.items()
        )
    return (
        isinstance(roles, list)
        and required.issubset({role.get("id") for role in roles if isinstance(role, dict)})
        and isinstance(dashboard.get("northStar"), dict)
        and isinstance(metrics, list)
        and (
            not production
            or (
                required_metric_ids.issubset(
                    {metric.get("id") for metric in metrics if isinstance(metric, dict)}
                )
                and any(
                    metric.get("id") == "weekly_active_customers_reports" and metric.get("value", 0) >= 26
                    for metric in metrics
                    if isinstance(metric, dict)
                )
                and reconciled
            )
        )
        and required.issubset({metric.get("roleId") for metric in metrics if isinstance(metric, dict)})
        and all(
            isinstance(metric, dict)
            and isinstance(metric.get("value"), (int, float))
            and metric.get("evidence")
            for metric in metrics
        )
    )


def _has_connected_sample(
    root: Path, links: list[dict[str, Any]], events: list[dict[str, Any]]
) -> bool:
    full_timeline = events
    if not any(
        (event.get("payload") or {}).get("redwoodEpisodeId")
        or (event.get("payload") or {}).get("properties", {}).get("redwood_episode_id")
        for event in full_timeline
    ):
        return False
    sample_events = _read_gzip_jsonl(root / "sample" / "timeline.jsonl.gz")
    sample_cases = _read_jsonl(root / "sample" / "cases.jsonl")
    if len(sample_events) < 200 or not 10 <= len(sample_cases) <= 20:
        return False
    entities = {(event.get("aliases") or [str(event.get("entity_id"))])[0] for event in sample_events}
    adjacency: dict[str, set[str]] = defaultdict(set)
    for link in links:
        source = str(link.get("source_entity_id"))
        target = str(link.get("target_entity_id"))
        if source in entities and target in entities:
            adjacency[source].add(target)
            adjacency[target].add(source)
    if not entities:
        return False
    seen = {min(entities)}
    queue = deque([min(entities)])
    while queue:
        current = queue.popleft()
        for target in sorted(adjacency[current] - seen):
            seen.add(target)
            queue.append(target)
    return seen == entities


def _has_external_context(events: list[dict[str, Any]], links: list[dict[str, Any]]) -> bool:
    """Require every external layer to participate in an explicit operational action."""
    payloads = [event.get("payload") if isinstance(event.get("payload"), dict) else {} for event in events]
    external_documents = {
        str(payload.get("redwood_metadata", {}).get("canonical_document_id"))
        for payload in payloads
        if payload.get("redwood_metadata", {}).get("provenance", {}).get("source")
        in {"cc_news", "reddit", "amazon_reviews", "amazon_reviews_2023"}
    }
    action_events = [
        event
        for event, payload in zip(events, payloads, strict=False)
        if payload.get("redwoodExternalAction") and event.get("aliases")
    ]
    actions = {str((event.get("payload") or {}).get("canonicalDocumentId")) for event in action_events}
    linked_pairs = {
        (str(link.get("source_entity_id")), str(link.get("target_entity_id")))
        for link in links
        if link.get("rule_id") == "external_context.source_document.v1"
    }
    target_by_document = {
        str(payload.get("redwood_metadata", {}).get("canonical_document_id")): str(
            (event.get("aliases") or [event.get("entity_id")])[0]
        )
        for event, payload in zip(events, payloads, strict=False)
        if payload.get("redwood_metadata", {}).get("canonical_document_id")
    }
    all_actions_linked = all(
        (
            str(event["aliases"][0]),
            target_by_document.get(str((event.get("payload") or {}).get("canonicalDocumentId")), ""),
        )
        in linked_pairs
        for event in action_events
    )
    return bool(external_documents) and external_documents.issubset(actions) and all_actions_linked


def _has_mutations(events: list[dict[str, Any]]) -> bool:
    """Verify that production contains real edits, reopenings, and source deletions."""
    if len(events) <= 1_000:
        return True
    payloads = [event.get("payload") if isinstance(event.get("payload"), dict) else {} for event in events]
    has_edit = any(
        event.get("source") == "slack-redwood" and event.get("revision", 0) >= 2 and payload.get("edited")
        for event, payload in zip(events, payloads, strict=False)
    )
    has_delete = any(
        event.get("op") == "delete"
        and event.get("previous_event_id")
        and event.get("tombstone", {}).get("effective_acl")
        for event in events
    )
    has_reopen = any(
        payload.get("fields", {}).get("status", {}).get("name") == "Reopened" for payload in payloads
    )
    has_revoked = any(
        payload.get("properties", {}).get("status") in {"revoked", "voided"} for payload in payloads
    )
    return has_edit and has_delete and has_reopen and has_revoked


def _has_operational_rhythm(events: list[dict[str, Any]]) -> bool:
    """Reject uniformly round-the-clock production activity."""
    if len(events) <= 1_000:
        return True
    times: list[datetime] = []
    for event in events:
        try:
            times.append(datetime.fromisoformat(str(event["world_at"]).replace("Z", "+00:00")))
        except (KeyError, ValueError):
            return False
    weekend_ratio = sum(value.weekday() >= 5 for value in times) / len(times)
    off_hours_ratio = sum(not 8 <= value.hour < 18 for value in times) / len(times)
    by_source: dict[str, list[datetime]] = defaultdict(list)
    for event, value in zip(events, times, strict=False):
        by_source[str(event.get("source"))].append(value)
    realistic_sources = True
    for source_times in by_source.values():
        if len(source_times) < 100:
            continue
        source_weekend = sum(value.weekday() >= 5 for value in source_times) / len(source_times)
        source_off_hours = sum(not 8 <= value.hour < 18 for value in source_times) / len(source_times)
        busiest_hour = max(Counter(value.hour for value in source_times).values()) / len(source_times)
        if source_weekend >= 0.08 or source_off_hours >= 0.12 or busiest_hour >= 0.35:
            realistic_sources = False
            break
    return weekend_ratio < 0.08 and off_hours_ratio < 0.12 and realistic_sources


def _has_source_time_semantics(events: list[dict[str, Any]]) -> bool:
    """Reject ordinary source activity emitted outside the allowed on-call window."""
    if not _is_production_delivery(events):
        return True
    latest = _latest_upserts(events)
    allowed_stages = {"incident", "mitigation", "emergency_deployment"}
    for event in latest.values():
        at = _event_time(event)
        if at is None or 8 <= at.hour < 18:
            continue
        payload = event.get("payload") or {}
        stage = str(payload.get("redwoodStage") or payload.get("redwoodExternalStage") or "")
        source_kind = str(payload.get("redwoodOnCallKind") or "")
        if stage not in allowed_stages and source_kind not in allowed_stages:
            return False
    return True


def _has_email_source_semantics(events: list[dict[str, Any]]) -> bool:
    """Validate Graph-like message identity, thread topology, and thread mix."""
    if not _is_production_delivery(events):
        return True
    messages = [
        event
        for event in _latest_upserts(events).values()
        if event.get("source") == "mail-redwood" and event.get("op") == "upsert"
    ]
    if not messages:
        return False
    by_id = {str(event.get("external_id")): event for event in messages if event.get("external_id")}
    by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    subjects: list[str] = []
    reply_count = 0
    delayed = 0
    forwarded = 0
    cc_count = 0
    for event in messages:
        payload = event.get("payload") or {}
        conversation_id = payload.get("conversationId")
        if not conversation_id:
            return False
        by_conversation[str(conversation_id)].append(event)
        subject = str(payload.get("subject") or "")
        subjects.append(subject)
        forwarded += subject.casefold().startswith(("fw:", "fwd:"))
        cc_count += bool(payload.get("ccRecipients"))
        reply_id = payload.get("inReplyTo")
        if not reply_id:
            continue
        parent = by_id.get(str(reply_id))
        if parent is None:
            return False
        reply_count += 1
        reply_at = _event_time(event)
        parent_at = _event_time(parent)
        if reply_at is None or parent_at is None or reply_at <= parent_at:
            return False
        delayed += (reply_at - parent_at).total_seconds() >= 24 * 3600

    def normalized(value: str) -> str:
        return re.sub(
            r"\b(?:[A-Z]{1,8}[-_])?[A-Z0-9]{4,}\b|\b\d+(?:\.\d+)?\b|\S+@\S+",
            "<id>",
            re.sub(r"\s+", " ", value.casefold()),
        ).strip()

    normalized_subjects = Counter(normalized(subject) for subject in subjects)
    if normalized_subjects and max(normalized_subjects.values()) > len(subjects) * 0.05:
        return False
    if any(
        length > 12
        and not any(
            str((event.get("payload") or {}).get("redwoodThreadKind")) == "escalation" for event in items
        )
        for items, length in ((items, len(items)) for items in by_conversation.values())
    ):
        return False
    if not 0.05 <= forwarded / len(messages) <= 0.10:
        return False
    if not 0.10 <= cc_count / len(messages) <= 0.40:
        return False
    if reply_count and delayed / reply_count < 0.05:
        return False
    return True


def _has_mr_source_semantics(events: list[dict[str, Any]]) -> bool:
    """Validate MR workflow states and pipeline chronology independently."""
    if not _is_production_delivery(events):
        return True
    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if (
            event.get("source") == "gitlab-redwood"
            and event.get("object_type") == "merge_request"
            and str(event.get("external_id", "")).startswith("dsid_")
        ):
            histories[str(event.get("entity_id"))].append(event)
    if not histories:
        return False
    features = Counter()
    failed_entities: set[str] = set()
    for entity_id, history in histories.items():
        ordered_history = sorted(history, key=lambda event: int(event.get("revision", 0)))
        payloads = [event.get("payload") or {} for event in ordered_history]
        if any(payload.get("draft") or payload.get("state") == "draft" for payload in payloads):
            features["draft"] += 1
        if any(
            payload.get("has_conflicts")
            or payload.get("merge_conflicts")
            or payload.get("merge_status") in {"conflict", "conflicts"}
            for payload in payloads
        ):
            features["conflict"] += 1
        if any(
            any(
                str(approval.get("status", "")).casefold() in {"rejected", "changes_requested"}
                for approval in payload.get("approvals", [])
            )
            or payload.get("approval_state") in {"rejected", "changes_requested"}
            or payload.get("requested_changes")
            for payload in payloads
        ):
            features["rejected_approval"] += 1
        final_payload = payloads[-1]
        files = final_payload.get("changed_files") or []
        paths = [str(file.get("path") if isinstance(file, dict) else file) for file in files]
        if paths and all(
            path.startswith(("docs/", "documentation/")) or path.endswith(".md") for path in paths
        ):
            features["docs_only"] += 1
        if paths and all(path.startswith(("test/", "tests/")) for path in paths):
            features["test_only"] += 1
        if (final_payload.get("sourceLifecycle") or {}).get("changeKind") == "rollback":
            features["rollback"] += 1
        for payload in payloads:
            pipelines = payload.get("pipelines") or []
            attempts: list[tuple[int, datetime]] = []
            for pipeline in pipelines:
                try:
                    attempt = int(str(pipeline.get("id", "")).rsplit("-", 1)[-1])
                    finished = datetime.fromisoformat(str(pipeline["finished_at"]).replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError):
                    return False
                attempts.append((attempt, finished))
                if pipeline.get("status") == "failed" or pipeline.get("failed_checks"):
                    failed_entities.add(entity_id)
            attempts.sort()
            if any(left[1] >= right[1] for left, right in zip(attempts, attempts[1:], strict=False)):
                return False
    total = len(histories)
    if not 0.02 <= features["draft"] / total <= 0.08:
        return False
    if not 0.02 <= features["conflict"] / total <= 0.08:
        return False
    if not 0.05 <= features["rejected_approval"] / total <= 0.15:
        return False
    if features["docs_only"] / total < 0.03 or features["test_only"] / total < 0.03:
        return False
    if features["rollback"] / total < 0.03:
        return False
    return 0.10 <= len(failed_entities) / total <= 0.25


def _has_ordinary_work_diversity(events: list[dict[str, Any]]) -> bool:
    """Keep the mandatory benchmark chains from becoming the whole company."""
    if not _is_production_delivery(events):
        return True
    ordinary: dict[str, set[str]] = defaultdict(set)
    mr_by_work: Counter[str] = Counter()
    for event in _latest_upserts(events).values():
        payload = event.get("payload") or {}
        work_id = str(payload.get("redwoodEnterpriseWorkId") or "")
        stage = str(payload.get("redwoodEnterpriseStage") or payload.get("redwoodStage") or "")
        if work_id.startswith("enterprise-ordinary-") and stage:
            ordinary[work_id].add(stage)
            if stage == "merge_request":
                mr_by_work[work_id] += 1
    if len(ordinary) < 100:
        return False
    mr_counts = Counter(mr_by_work.get(work_id, 0) for work_id in ordinary)
    if len(mr_counts) < 3 or mr_counts[max(mr_counts, key=mr_counts.get)] > len(ordinary) * 0.60:
        return False
    deployed = [stages for stages in ordinary.values() if "deployment" in stages]
    incident_deployments = sum("incident" in stages or "postmortem" in stages for stages in deployed)
    if deployed and incident_deployments / len(deployed) > 0.40:
        return False
    return True


def _has_directory_source_identities(events: list[dict[str, Any]], directory: dict[str, Any]) -> bool:
    """Source payload actors must resolve to directory names, not bare employee IDs."""
    if not _is_production_delivery(events):
        return True
    known_ids = {
        str(person.get("id", "")).removeprefix("RW-").replace("RW-", "")
        for person in directory.get("people", [])
        if isinstance(person, dict)
    }
    names = {
        str(person.get("name"))
        for person in directory.get("people", [])
        if isinstance(person, dict) and person.get("name")
    }
    for event in _latest_upserts(events).values():
        payload_text = json.dumps(event.get("payload") or {}, ensure_ascii=False)
        for employee_id in re.findall(r'"displayName"\s*:\s*"(E\d{3})"', payload_text):
            if employee_id in known_ids and employee_id not in names:
                return False
    return True


def _has_case_question_diversity(cases: list[dict[str, Any]], events: list[dict[str, Any]]) -> bool:
    if not _is_production_delivery(events):
        return True
    prefixes = Counter(" ".join(str(case.get("question", "")).casefold().split()[:5]) for case in cases)
    return not prefixes or max(prefixes.values()) <= len(cases) * 0.10


def _quality_text(value: object) -> str:
    return re.sub(
        r"\b(?:EP|ER|ENT|EXT|MR|TASK|TKT|MTG|DEC|DEP|INC|PM)[-_A-Z0-9]+\b|\b\d+(?:\.\d+)?\b|<[^>]+>",
        "<id>",
        re.sub(r"\s+", " ", str(value).casefold()),
    ).strip()


_LATEST_UPSERT_CACHE: dict[int, dict[str, dict[str, Any]]] = {}


def _latest_upserts(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cache_key = id(events)
    cached = _LATEST_UPSERT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("op") != "upsert" or not event.get("payload"):
            continue
        previous = latest.get(str(event.get("entity_id")))
        if previous is None or int(event.get("revision", 0)) > int(previous.get("revision", 0)):
            latest[str(event.get("entity_id"))] = event
    _LATEST_UPSERT_CACHE[cache_key] = latest
    return latest


def _event_time(event: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(event["world_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None


def _is_production_delivery(events: list[dict[str, Any]]) -> bool:
    return len(events) > PRODUCTION_EVENT_THRESHOLD


def _latest_chain_stages(events: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    chains: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for event in _latest_upserts(events).values():
        payload = event.get("payload") or {}
        chain_id = payload.get("redwoodCausalChainId") or payload.get("redwoodExternalChainId")
        stage = payload.get("redwoodStage") or payload.get("redwoodExternalStage")
        if chain_id and stage:
            chains[str(chain_id)][str(stage)].append(event)
    return chains


def _chain_times(chain: dict[str, list[dict[str, Any]]], stages: tuple[str, ...]) -> list[datetime] | None:
    values: list[datetime] = []
    for stage in stages:
        stage_times = [_event_time(event) for event in chain.get(stage, [])]
        if not stage_times or any(value is None for value in stage_times):
            return None
        values.append(min(value for value in stage_times if value is not None))
    return values


def _has_chain_temporal_diversity(events: list[dict[str, Any]]) -> bool:
    """Reject production chains that only differ by IDs over a fixed clock template."""
    if not _is_production_delivery(events):
        return True
    chains = _latest_chain_stages(events)
    enterprise = [
        _chain_times(chain, _ENTERPRISE_CHAIN_STAGES)
        for chain_id, chain in chains.items()
        if chain_id.startswith("enterprise-causal-batch-")
    ]
    enterprise_times = [times for times in enterprise if times is not None]
    if len(enterprise_times) != 1_000:
        return False
    durations = [int((times[-1] - times[0]).total_seconds()) for times in enterprise_times]
    gaps = [
        int((right - left).total_seconds())
        for times in enterprise_times
        for left, right in zip(times, times[1:], strict=False)
    ]
    if len(set(durations)) < 50 or max(Counter(durations).values()) > len(durations) * 0.20:
        return False
    if len(set(gaps)) < 12 or max(Counter(gaps).values()) > len(gaps) * 0.35:
        return False

    external_durations: list[int] = []
    for chain_id, chain in chains.items():
        if not chain_id.startswith("external-chain-"):
            continue
        available = [stage for stage in _EXTERNAL_CHAIN_STAGES if stage in chain]
        if len(available) < 2:
            continue
        times = _chain_times(chain, tuple(available))
        if times is not None:
            external_durations.append(int((times[-1] - times[0]).total_seconds()))
    return not external_durations or len(set(external_durations)) >= 10


def _external_signal_ids(events: list[dict[str, Any]]) -> set[str]:
    return {
        str((event.get("payload") or {}).get("canonicalDocumentId"))
        for event in _latest_upserts(events).values()
        if (event.get("payload") or {}).get("redwoodExternalAction")
        and (event.get("payload") or {}).get("canonicalDocumentId")
    }


def _has_external_denominator(root: Path, events: list[dict[str, Any]]) -> bool:
    """Recompute the external 634-record to 443-document reconciliation from raw files."""
    if not _is_production_delivery(events):
        return True
    mapping_path = root.parent / "external_signal_mapping.jsonl"
    mapping = _read_jsonl(mapping_path)
    required_mapping_fields = {
        "record_id",
        "source",
        "canonical_document_id",
        "original_record_id",
        "original_timestamp",
        "dedup_reason",
        "dedup_cluster_id",
        "content_hash",
    }
    if len(mapping) != EXPECTED_EXTERNAL_SOURCE_RECORDS or any(
        not required_mapping_fields.issubset(record)
        or not record.get("record_id")
        or not record.get("canonical_document_id")
        for record in mapping
    ):
        return False
    documents = _read_jsonl(root.parent / "documents.jsonl")
    external_documents = {
        str(document.get("doc_id")) for document in documents if document.get("layer") == "external"
    }
    canonical_ids = {str(record.get("canonical_document_id")) for record in mapping}
    record_ids = [str(record.get("record_id")) for record in mapping]
    if (
        len(external_documents) != EXPECTED_EXTERNAL_CANONICAL_DOCUMENTS
        or len(canonical_ids) != EXPECTED_EXTERNAL_CANONICAL_DOCUMENTS
        or canonical_ids != external_documents
        or len(record_ids) != len(set(record_ids))
        or sum(str(record.get("dedup_reason")) == "canonical_source_record" for record in mapping)
        != EXPECTED_EXTERNAL_CANONICAL_DOCUMENTS
    ):
        return False
    return _external_signal_ids(events) == external_documents


def _external_outcome_family(stages: set[str]) -> str | None:
    if "incident" in stages:
        return "incident"
    if "deployment" in stages:
        return "deployment"
    if "merge_request" in stages:
        return "experiment"
    if "decision" in stages:
        return "decision"
    if "task" in stages:
        return "task"
    if "ticket" in stages:
        return "ticket"
    return None


def _has_external_outcome_diversity(events: list[dict[str, Any]]) -> bool:
    """Require downstream outcomes to represent more than one incident template."""
    if not _is_production_delivery(events):
        return True
    chains = _latest_chain_stages(events)
    families: Counter[str] = Counter()
    stage_order = {stage: index for index, stage in enumerate(_EXTERNAL_CHAIN_STAGES)}
    for chain_id, chain in chains.items():
        if not chain_id.startswith("external-chain-"):
            continue
        terminal_stage = max(chain, key=lambda stage: stage_order.get(stage, -1))
        terminal = chain[terminal_stage][0]
        payload = terminal.get("payload") or {}
        family = payload.get("redwoodExternalOutcomeFamily")
        if not family:
            if terminal.get("source") == "hubspot-redwood":
                family = "commercial"
            elif terminal.get("source") == "mail-redwood":
                family = "customer_email"
            elif terminal_stage == "incident":
                family = "incident"
            elif terminal_stage == "postmortem":
                family = "postmortem"
            elif terminal_stage == "deployment":
                family = "deployment"
            elif terminal_stage == "merge_request":
                family = "experiment"
            elif terminal_stage == "decision":
                family = "decision"
            elif terminal_stage == "task":
                family = "task"
            elif terminal_stage == "ticket":
                family = "ticket"
        if family:
            families[str(family)] += 1
    if len(families) < 6 or any(value < 10 for value in families.values()):
        return False
    total = sum(families.values())
    incident_total = families["incident"] + families["postmortem"]
    return total > 0 and incident_total / total <= 0.30


def _semantic_signature(value: object) -> str:
    text = _quality_text(value)
    text = re.sub(r"\b(?:reviewer|author|blocker|context|evidence|posture|action)\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _human_payload_text(payload: object) -> str:
    """Inspect source-visible prose without counting generator metadata as text."""
    values: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.casefold().startswith("redwood") or key in {
                    "id",
                    "external_id",
                    "entity_id",
                    "aliases",
                    "references",
                }:
                    continue
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
        elif isinstance(value, str) and value.strip():
            values.append(value)

    collect(payload)
    return " ".join(values)


def _has_semantic_diversity(events: list[dict[str, Any]]) -> bool:
    """Reject lexical variation that still has one operational writing template."""
    if not _is_production_delivery(events):
        return True
    latest = _latest_upserts(events)
    task_events = [
        event
        for event in latest.values()
        if event.get("source") == "jira-redwood"
        and (event.get("payload") or {}).get("fields", {}).get("issuetype", {}).get("name") == "Task"
        and "-TKT-" not in str(event.get("external_id", ""))
        and not str(event.get("external_id", "")).startswith(("INC", "external-action-"))
    ]
    comments_by_task = [
        (event.get("payload") or {}).get("fields", {}).get("comment", {}).get("comments") or []
        for event in task_events
    ]
    comments = [comment.get("body") for items in comments_by_task for comment in items if comment.get("body")]
    comment_signatures = {_semantic_signature(value) for value in comments}
    if comments and len(comment_signatures) / len(comments) < 0.60:
        return False
    comment_counts = Counter(len(items) for items in comments_by_task if items)
    if (
        len(comment_counts) < 4
        or max(comment_counts.values(), default=0) > len(comments_by_task) * 0.85
        or any(count > 8 for items in comments_by_task for count in [len(items)])
    ):
        return False
    comment_kinds = Counter(
        str(comment.get("body", "")).split(":", 1)[0].casefold()
        for items in comments_by_task
        for comment in items
        if comment.get("body")
    )
    recognized_kinds = {
        kind
        for kind in comment_kinds
        if any(
            marker in kind
            for marker in (
                "reviewer",
                "author",
                "blocker",
                "scope",
                "evidence",
                "approval",
                "reopen",
                "close",
                "requested",
                "reproduced",
            )
        )
    }
    if len(recognized_kinds) < 5:
        return False

    meetings = [
        event
        for event in latest.values()
        if event.get("source") == "fireflies-redwood" and event.get("object_type") == "transcript"
    ]
    transcripts = [(event.get("payload") or {}).get("transcript") for event in meetings]
    if transcripts and len({_semantic_signature(value) for value in transcripts}) / len(transcripts) < 0.60:
        return False
    transcript_signatures = Counter(_semantic_signature(value) for value in transcripts)
    if transcript_signatures and max(transcript_signatures.values()) > len(transcripts) * 0.10:
        return False
    summaries = [str((event.get("payload") or {}).get("summary", "")).casefold() for event in meetings]
    participant_counts = Counter(
        len((event.get("payload") or {}).get("participants", [])) for event in meetings
    )
    if len(set(summaries)) < 5 or max(participant_counts.values(), default=0) > len(meetings) * 0.90:
        return False
    meeting_markers = (
        ("alternative", "option", "trade-off"),
        ("owner", "follow-up", "due"),
        ("risk", "blocker", "concern"),
        ("decided", "agreed", "approved"),
        ("revised", "reopen", "changed"),
    )
    transcript_text = " ".join(str(value) for value in transcripts).casefold()
    marker_coverage = sum(any(marker in transcript_text for marker in markers) for markers in meeting_markers)
    if marker_coverage < 4:
        return False

    subjects = [
        str((event.get("payload") or {}).get("subject", ""))
        for event in latest.values()
        if event.get("source") == "mail-redwood"
    ]
    if subjects:
        lengths = sorted(len(subject) for subject in subjects)
        median = lengths[len(lengths) // 2]
        p95 = lengths[min(len(lengths) - 1, int(len(lengths) * 0.95))]
        normalized_subjects = {_semantic_signature(subject) for subject in subjects}
        subject_counts = Counter(_semantic_signature(subject) for subject in subjects)
        if (
            median < 25
            or median > 80
            or p95 > 120
            or len(normalized_subjects) / len(subjects) < 0.35
            or max(subject_counts.values(), default=0) > len(subjects) * 0.05
        ):
            return False
        if any(re.search(r"\b(?:alpha|bravo|charlie)\b", subject.casefold()) for subject in subjects):
            return False

    bodies = [
        _quality_text((event.get("payload") or {}).get("body", {}).get("content"))
        for event in latest.values()
        if event.get("source") == "mail-redwood"
    ]
    return not bodies or len(set(bodies)) / len(bodies) >= 0.80


def _collection_size(value: object) -> int | None:
    if isinstance(value, (list, dict, tuple, set)):
        return len(value)
    return None


def _changed_file_pattern(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("path") or value.get("new_path") or value.get("old_path") or value
    return re.sub(r"\b(?:[a-f0-9]{7,40}|\d+)\b", "<id>", str(value).casefold())


def _has_mr_diversity(events: list[dict[str, Any]]) -> bool:
    """Require source-native MR histories to vary in shape and file surface."""
    if not _is_production_delivery(events):
        return True
    merge_requests = [
        event
        for event in _latest_upserts(events).values()
        if event.get("source") == "gitlab-redwood"
        and event.get("object_type") == "merge_request"
        and str(event.get("external_id", "")).startswith("dsid_")
    ]
    if len(merge_requests) < 5:
        return False
    fields = ("reviewers", "approvals", "pipelines", "changed_files", "commits", "review_comments")
    for field in fields:
        cardinalities = [
            _collection_size((event.get("payload") or {}).get(field)) for event in merge_requests
        ]
        if any(value is None for value in cardinalities):
            return False
        counts = Counter(value for value in cardinalities if value is not None)
        if len(counts) < 2 or max(counts.values()) > len(cardinalities) * 0.95:
            return False
    changed_file_patterns = {
        _changed_file_pattern(file)
        for event in merge_requests
        for file in ((event.get("payload") or {}).get("changed_files") or [])
    }
    return len(changed_file_patterns) >= 500


def _has_true_implicit_links(events: list[dict[str, Any]], links: list[dict[str, Any]]) -> bool:
    """Require implicit links to be recoverable without exposing the target ID."""
    if not _is_production_delivery(events):
        return True
    latest = _latest_upserts(events)
    by_reference = {
        reference: event
        for event in latest.values()
        for reference in (
            str(event.get("entity_id")),
            str(event.get("external_id")),
            *map(str, event.get("aliases", [])),
        )
    }
    implicit = [link for link in links if link.get("explicit") is False]
    if len(implicit) < 500:
        return False
    quotes: set[str] = set()
    for link in implicit:
        source = by_reference.get(str(link.get("source_entity_id")))
        target = by_reference.get(str(link.get("target_entity_id")))
        quote = str(link.get("evidence_quote") or "")
        if not source or not target or not quote:
            return False
        source_text = _human_payload_text(source.get("payload") or {}).casefold()
        if quote.casefold() not in source_text:
            return False
        target_ids = {
            str(target.get("entity_id")),
            str(target.get("external_id")),
            *map(str, target.get("aliases", [])),
        }
        if any(target_id and target_id.casefold() in source_text for target_id in target_ids):
            return False
        quotes.add(_semantic_signature(quote))
    return len(quotes) >= 100


def _case_account_ids(
    case: dict[str, Any], events_by_entity: dict[str, dict[int, dict[str, Any]]]
) -> set[str]:
    accounts: set[str] = set()
    for item in case.get("evidence", []):
        event = events_by_entity.get(str(item.get("entity_id")), {}).get(int(item.get("version", 0)))
        payload = event.get("payload", {}) if event else {}
        references = payload.get("redwoodReferences", {})
        properties = payload.get("properties", {})
        for value in (references.get("accountId"), properties.get("account_id")):
            if value:
                accounts.add(str(value))
    return accounts


def _case_scenario(case: dict[str, Any], evidence_events: list[dict[str, Any]]) -> str:
    explicit = case.get("scenario") or case.get("metadata", {}).get("scenario")
    if explicit:
        return str(explicit)
    payloads = [(event.get("payload") or {}) for event in evidence_events]
    episode_ids = {
        str(payload.get("redwoodEpisodeId") or "") for payload in payloads if payload.get("redwoodEpisodeId")
    }
    if any(payload.get("redwoodExternalChainId") for payload in payloads):
        return "external"
    if any(
        event.get("source") == "hubspot-redwood"
        or "-COM-" in str((event.get("payload") or {}).get("redwoodEpisodeId") or "")
        for event in evidence_events
    ):
        return "commercial"
    if any("-SUP-" in episode_id for episode_id in episode_ids):
        return "support"
    if any(str(payload.get("redwoodCausalChainId", "")).startswith("enterprise-") for payload in payloads):
        return "operational"
    text = json.dumps(
        {
            "question": case.get("question"),
            "evidence": evidence_events,
        },
        ensure_ascii=False,
    ).casefold()
    if any(token in text for token in ("invoice", "quote", "deal", "renewal", "commercial")):
        return "commercial"
    if any(token in text for token in ("reddit", "review", "news", "competitive")):
        return "external"
    if any(token in text for token in ("employee", "manager", "people", "absence")):
        return "people"
    if any(token in text for token in ("deployment", "merge request", "incident", "postmortem")):
        return "operational"
    return "support"


def _has_case_quality(cases: list[dict[str, Any]], events: list[dict[str, Any]]) -> bool:
    """Require non-trivial numeric and scenario coverage in production evaluation cases."""
    if not _is_production_delivery(events):
        return True
    latest = _latest_upserts(events)
    events_by_entity: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for event in events:
        for reference in [str(event.get("entity_id")), *map(str, event.get("aliases", []))]:
            events_by_entity[reference][int(event.get("revision", 0))] = event

    numeric_cases = [case for case in cases if case.get("category") == "numeric"]
    non_zero_ar = 0
    numeric_statuses: set[str] = set()
    for case in numeric_cases:
        evidence_events = [
            events_by_entity.get(str(item.get("entity_id")), {}).get(int(item.get("version", 0)))
            for item in case.get("evidence", [])
        ]
        invoice_events = [
            event for event in evidence_events if event and event.get("object_type") == "invoice"
        ]
        balances = [
            float((event.get("payload") or {}).get("properties", {}).get("balance") or 0)
            for event in invoice_events
        ]
        non_zero_ar += bool(any(balance > 0 for balance in balances))
        numeric_statuses.update(
            str((event.get("payload") or {}).get("properties", {}).get("status")) for event in invoice_events
        )
    if len(numeric_cases) < 10 or non_zero_ar < 6 or len(numeric_statuses) < 3:
        return False

    why_cases = [case for case in cases if case.get("category") == "why"]
    latest_by_reference = {
        reference: event
        for event in latest.values()
        for reference in [
            str(event.get("entity_id")),
            str(event.get("external_id")),
            *map(str, event.get("aliases", [])),
        ]
    }
    why_accounts = (
        set().union(*(_case_account_ids(case, events_by_entity) for case in why_cases))
        if why_cases
        else set()
    )
    why_scenarios = {
        _case_scenario(
            case,
            [latest_by_reference.get(str(item.get("entity_id")), {}) for item in case.get("evidence", [])],
        )
        for case in why_cases
    }
    if len(why_accounts) < 6 or len(why_scenarios) < 4:
        return False

    as_of_cases = [case for case in cases if case.get("category") == "as_of"]
    as_of_case_types: Counter[str] = Counter()
    revised_decision_cases = 0
    for case in as_of_cases:
        case_events = [
            events_by_entity.get(str(item.get("entity_id")), {}).get(int(item.get("version", 0)))
            for item in case.get("evidence", [])
        ]
        case_events = [event for event in case_events if event]
        if not case_events:
            continue
        as_of_case_types[str(case_events[0].get("object_type"))] += 1
        if (
            sum((event.get("payload") or {}).get("redwoodStage") == "decision" for event in case_events) >= 1
            and len({int(item.get("version", 0)) for item in case.get("evidence", [])}) >= 2
        ):
            revised_decision_cases += 1
    return (
        as_of_case_types["issue"] >= 2
        and as_of_case_types["deal"] >= 2
        and as_of_case_types["invoice"] >= 2
        and revised_decision_cases >= 2
    )


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _build_event_reference_index(
    events: list[dict[str, Any]], *, aliases_only: bool = False
) -> dict[str, dict[int, dict[str, Any]]]:
    index: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for event in events:
        try:
            revision = int(event.get("revision", 0))
        except (TypeError, ValueError):
            continue
        references = list(map(str, event.get("aliases", [])))
        if not aliases_only:
            references.extend([str(event.get("entity_id")), str(event.get("external_id"))])
        for reference in references:
            if reference:
                index[reference][revision] = event
    return index


def _case_evidence_events(
    case: dict[str, Any],
    events: list[dict[str, Any]],
    index: dict[str, dict[int, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]] | None:
    events_by_entity = index or _build_event_reference_index(events)
    resolved: list[dict[str, Any]] = []
    for item in case.get("evidence", []):
        if not isinstance(item, dict):
            return None
        try:
            version = int(item.get("version", 0))
        except (TypeError, ValueError):
            return None
        event = events_by_entity.get(str(item.get("entity_id")), {}).get(version)
        if event is None:
            return None
        resolved.append(event)
    return resolved


def _has_atomic_answer_facts(cases: list[dict[str, Any]], events: list[dict[str, Any]]) -> bool:
    """Require one scalar answer per fact and verify non-derived facts in evidence text."""
    numeric_derived = {"invoice_total", "paid_total", "outstanding_total"}
    events_by_entity = _build_event_reference_index(events)
    for case in cases:
        facts = case.get("answer_facts", [])
        if not isinstance(facts, list):
            return False
        keys: set[str] = set()
        evidence_events = _case_evidence_events(case, events, events_by_entity)
        if case.get("evidence") and evidence_events is None:
            return False
        evidence_text = ""
        if evidence_events:
            text_values: list[str] = []
            _collect_text_values(evidence_events, text_values)
            evidence_text = _quality_text(
                " ".join(text_values) + " " + json.dumps(evidence_events, ensure_ascii=False)
            )
        for fact in facts:
            if not isinstance(fact, dict):
                return False
            key = str(fact.get("key") or "")
            options = fact.get("any")
            if not key or key in keys or not isinstance(options, list) or len(options) != 1:
                return False
            value = options[0]
            if isinstance(value, (dict, list, tuple, set)) or value is None or not str(value).strip():
                return False
            keys.add(key)
            raw_evidence_text = json.dumps(evidence_events, ensure_ascii=False)
            value_in_evidence = _quality_text(value) in evidence_text
            number = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", str(value))
            if number is not None:
                value_in_evidence = value_in_evidence or (
                    number.group(0).replace(",", "") in raw_evidence_text.replace(",", "")
                )
            if key not in numeric_derived and evidence_text and not value_in_evidence:
                if not key.endswith("_date"):
                    return False
                raw_dates = {
                    str(event.get(field, ""))[:10]
                    for event in evidence_events or []
                    for field in ("world_at", "observed_at", "original_occurred_at", "modified_at")
                }
                if str(value)[:10] not in raw_dates:
                    return False
    return True


def _has_numeric_case_arithmetic(
    cases: list[dict[str, Any]], events: list[dict[str, Any]], *, strict: bool = False
) -> bool:
    """Recompute numeric answers from revision-specific invoice/payment evidence."""
    if not strict and not _is_production_delivery(events):
        return True
    events_by_entity: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for event in events:
        for reference in [str(event.get("entity_id")), *map(str, event.get("aliases", []))]:
            events_by_entity[reference][int(event.get("revision", 0))] = event
    numeric_cases = [case for case in cases if case.get("category") == "numeric"]
    if len(numeric_cases) != 10:
        return False
    non_zero = 0
    statuses: set[str] = set()
    for case in numeric_cases:
        evidence_events = [
            events_by_entity.get(str(item.get("entity_id")), {}).get(int(item.get("version", 0)))
            for item in case.get("evidence", [])
        ]
        evidence_events = [event for event in evidence_events if event]
        invoices = [event for event in evidence_events if event.get("object_type") == "invoice"]
        payments = [event for event in evidence_events if event.get("object_type") == "payment"]
        if len(invoices) < 2 or not payments:
            return False
        effective_invoices = [
            event
            for event in invoices
            if str((event.get("payload") or {}).get("properties", {}).get("status")) != "voided"
        ]
        invoice_values = [
            _decimal((event.get("payload") or {}).get("properties", {}).get("amount") or 0)
            for event in effective_invoices
        ]
        payment_values = [
            _decimal((event.get("payload") or {}).get("properties", {}).get("cumulative_amount") or 0)
            for event in payments
        ]
        if any(value is None for value in [*invoice_values, *payment_values]):
            return False
        invoice_total = sum((value for value in invoice_values if value is not None), Decimal("0.00"))
        paid_total = sum((value for value in payment_values if value is not None), Decimal("0.00"))
        outstanding_total = (invoice_total - paid_total).quantize(Decimal("0.01"))
        facts = {
            str(fact.get("key")): (fact.get("any") or [None])[0]
            for fact in case.get("answer_facts", [])
            if isinstance(fact, dict)
        }
        try:
            if any(
                not isinstance(fact.get("any"), list) or len(fact.get("any", [])) != 1
                for fact in case.get("answer_facts", [])
                if isinstance(fact, dict)
                and fact.get("key") in {"invoice_total", "paid_total", "outstanding_total"}
            ):
                return False
            invoice_fact = _decimal(facts["invoice_total"])
            paid_fact = _decimal(facts["paid_total"])
            outstanding_fact = _decimal(facts["outstanding_total"])
            if invoice_fact is None or paid_fact is None or outstanding_fact is None:
                return False
            if abs(invoice_fact - invoice_total) > Decimal("0.01"):
                return False
            if abs(paid_fact - paid_total) > Decimal("0.01"):
                return False
            if abs(outstanding_fact - outstanding_total) > Decimal("0.01"):
                return False
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return False
        non_zero += outstanding_total > 0
        statuses.update(
            str((event.get("payload") or {}).get("properties", {}).get("status")) for event in invoices
        )
    return non_zero >= 6 and {"paid", "partial", "overdue"}.issubset(statuses)


def _has_evidence_backed_case_families(cases: list[dict[str, Any]], events: list[dict[str, Any]]) -> bool:
    """Require why-case families to come from evidence objects, not question wording."""
    if not _is_production_delivery(events):
        return True
    latest = _latest_upserts(events)
    by_reference = {
        reference: event
        for event in latest.values()
        for reference in [
            str(event.get("entity_id")),
            str(event.get("external_id")),
            *map(str, event.get("aliases", [])),
        ]
    }
    families: Counter[str] = Counter()
    accounts: set[str] = set()
    for case in [case for case in cases if case.get("category") == "why"]:
        evidence = [by_reference.get(str(item.get("entity_id"))) for item in case.get("evidence", [])]
        evidence = [event for event in evidence if event]
        if not any(
            (event.get("payload") or {}).get("redwoodStage") == "decision"
            or (event.get("payload") or {}).get("redwoodExternalStage") == "decision"
            for event in evidence
        ):
            return False
        if any((event.get("payload") or {}).get("redwoodExternalChainId") for event in evidence):
            family = "external"
        elif any(event.get("source") == "hubspot-redwood" for event in evidence):
            family = "commercial"
        elif any(
            str((event.get("payload") or {}).get("redwoodEpisodeId", "")).endswith("-COM-01")
            or "-COM-" in str((event.get("payload") or {}).get("redwoodEpisodeId", ""))
            for event in evidence
        ):
            family = "commercial"
        elif any(
            str((event.get("payload") or {}).get("redwoodCausalChainId", "")).startswith("enterprise-")
            for event in evidence
        ):
            family = "operational"
        else:
            family = "support"
        families[family] += 1
        for event in evidence:
            references = (event.get("payload") or {}).get("redwoodReferences") or {}
            if references.get("accountId"):
                accounts.add(str(references["accountId"]))
    return len(families) >= 4 and max(families.values(), default=0) <= 4 and len(accounts) >= 6


def _has_acl_case_semantics(cases: list[dict[str, Any]], events: list[dict[str, Any]]) -> bool:
    """Require ACL cases to discuss the protected object they actually evidence."""
    if not _is_production_delivery(events):
        return True
    latest = _latest_upserts(events)
    by_reference = {
        reference: event
        for event in latest.values()
        for reference in [
            str(event.get("entity_id")),
            str(event.get("external_id")),
            *map(str, event.get("aliases", [])),
        ]
    }
    acl_cases = [case for case in cases if case.get("category") == "acl"]
    if len(acl_cases) != 8:
        return False
    modes: set[str] = set()
    departments: set[str] = set()
    principals: set[str] = set()
    scenarios: set[str] = set()
    required_scenarios = {
        "finance_full",
        "sales_margin_redacted",
        "engineering_commercial_redacted",
        "unrelated_not_found",
        "manager_inherited",
        "membership_change",
        "historical_as_of",
        "finance_secret_denied",
    }
    for case in acl_cases:
        scenario = str(case.get("acl_scenario") or "")
        if not scenario:
            return False
        scenarios.add(scenario)
        if not case.get("acl_context"):
            return False
        forbidden = case.get("forbidden") or []
        forbidden_ids = {str(item.get("entity_id")) for item in forbidden}
        object_ids = {
            str(item.get("entity_id"))
            for item in [*case.get("evidence", []), *forbidden]
            if isinstance(item, dict) and item.get("entity_id")
        }
        object_events = [by_reference.get(event_id) for event_id in object_ids]
        object_events = [event for event in object_events if event]
        if not object_events or not any(
            str(event.get("external_id")) in case.get("question", "") for event in object_events
        ):
            return False
        forbidden_events = [by_reference.get(entity_id) for entity_id in forbidden_ids]
        forbidden_events = [event for event in forbidden_events if event]
        evidence_events = [by_reference.get(str(item.get("entity_id"))) for item in case.get("evidence", [])]
        evidence_events = [event for event in evidence_events if event]
        if not evidence_events:
            return False
        if forbidden_ids and not forbidden_ids.intersection(
            {str(item.get("entity_id")) for item in case.get("evidence", [])}
        ):
            return False
        forbidden_accounts = {
            str(
                ((event.get("payload") or {}).get("redwoodReferences") or {}).get("accountId")
                or ((event.get("payload") or {}).get("properties") or {}).get("account_id")
            )
            for event in forbidden_events
        }
        evidence_accounts = {
            str(
                ((event.get("payload") or {}).get("redwoodReferences") or {}).get("accountId")
                or ((event.get("payload") or {}).get("properties") or {}).get("account_id")
            )
            for event in evidence_events
        }
        if forbidden_events and not forbidden_accounts.intersection(evidence_accounts):
            return False
        text = json.dumps(case, ensure_ascii=False).casefold()
        mode = (
            "not_found"
            if case.get("expect_not_found")
            else "redaction"
            if "redacted" in text or "redaction" in text
            else "allow"
        )
        modes.add(mode)
        principal = str(case.get("principal"))
        principals.add(principal)
        departments.update(
            str((event.get("payload") or {}).get("redwoodAclDepartment"))
            for event in evidence_events
            if (event.get("payload") or {}).get("redwoodAclDepartment")
        )
    return (
        required_scenarios.issubset(scenarios)
        and {"allow", "redaction", "not_found"}.issubset(modes)
        and len(principals) >= 6
        and len(departments) >= 4
    )


def _stage(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    return str(payload.get("redwoodStage") or payload.get("redwoodExternalStage") or "")


def _event_lookup(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        reference: event
        for event in _latest_upserts(events).values()
        for reference in [
            str(event.get("entity_id")),
            str(event.get("external_id")),
            *map(str, event.get("aliases", [])),
        ]
    }


def _has_enterprise_chronology(events: list[dict[str, Any]]) -> bool:
    """Check complete enterprise chains in raw event time, not metadata order."""
    if not _is_production_delivery(events):
        return True
    chains = _latest_chain_stages(events)
    chain_count = 0
    for chain_id, chain in chains.items():
        if not chain_id.startswith("enterprise-causal-batch-"):
            continue
        chain_count += 1
        times = _chain_times(chain, _ENTERPRISE_CHAIN_STAGES)
        if times is None or times != sorted(times) or len(set(times)) != len(times):
            return False
    return chain_count == 1_000


def _has_ordinary_chronology(events: list[dict[str, Any]]) -> bool:
    """Check ordinary work stage order, retaining support for either Redwood stage field."""
    if not _is_production_delivery(events):
        return True
    by_work: dict[str, dict[str, list[datetime]]] = defaultdict(lambda: defaultdict(list))
    for event in _latest_upserts(events).values():
        payload = event.get("payload") or {}
        work_id = str(payload.get("redwoodEnterpriseWorkId") or "")
        stage = str(payload.get("redwoodEnterpriseStage") or _stage(event))
        at = _event_time(event)
        if work_id.startswith("enterprise-ordinary-") and stage and at is not None:
            by_work[work_id][stage].append(at)
    if len(by_work) < 100:
        return False
    complete = 0
    ordered = 0
    for stages in by_work.values():
        if not {"task", "merge_request", "deployment"}.issubset(stages):
            continue
        complete += 1
        if max(stages["task"]) < min(stages["merge_request"]) < min(stages["deployment"]):
            ordered += 1
    return complete == 0 or ordered / complete >= 0.95


def _has_ordinary_decision_coverage(events: list[dict[str, Any]]) -> bool:
    """Require decisions to cover ordinary work rather than only benchmark chains."""
    ordinary: dict[str, set[str]] = defaultdict(set)
    for event in _latest_upserts(events).values():
        payload = event.get("payload") or {}
        work_id = str(payload.get("redwoodEnterpriseWorkId") or "")
        stage = str(payload.get("redwoodEnterpriseStage") or _stage(event))
        if work_id.startswith("enterprise-ordinary-") and stage:
            ordinary[work_id].add(stage)
    if not ordinary:
        return True
    if len(ordinary) < 100:
        return False
    covered = sum("decision" in stages for stages in ordinary.values())
    return covered / len(ordinary) >= 0.50


def _has_project_decision_diversity(events: list[dict[str, Any]]) -> bool:
    """Recompute project and decision semantic diversity from source-visible prose."""
    if not _is_production_delivery(events):
        return True
    latest = _latest_upserts(events).values()
    projects = [
        event
        for event in latest
        if event.get("source") == "confluence-redwood"
        and "project" in str((event.get("payload") or {}).get("labels", {})).casefold()
        and "enterprise-project-" in str(event.get("external_id", ""))
    ]
    decisions = [
        event
        for event in _latest_upserts(events).values()
        if event.get("source") == "confluence-redwood"
        and "decision" in str((event.get("payload") or {}).get("labels", {})).casefold()
        and "enterprise-decision-" in str(event.get("external_id", ""))
    ]
    if len(projects) < 10 or len(decisions) < 10:
        return False
    project_signatures = Counter(
        _semantic_signature(_human_payload_text(event.get("payload"))) for event in projects
    )
    decision_signatures = Counter(
        _semantic_signature(_human_payload_text(event.get("payload"))) for event in decisions
    )
    return (
        len(project_signatures) >= 6
        and len(decision_signatures) >= 6
        and max(project_signatures.values()) <= len(projects) * 0.30
        and max(decision_signatures.values()) <= len(decisions) * 0.30
    )


def _has_normalized_task_metric(events: list[dict[str, Any]]) -> bool:
    """Recompute normalized task-description diversity from raw Jira payloads."""
    if not _is_production_delivery(events):
        return True
    tasks = [
        event
        for event in _latest_upserts(events).values()
        if event.get("source") == "jira-redwood"
        and (event.get("payload") or {}).get("fields", {}).get("issuetype", {}).get("name") == "Task"
        and "-TKT-" not in str(event.get("external_id", ""))
        and not str(event.get("external_id", "")).startswith(("INC", "external-action-"))
    ]
    descriptions = [
        _semantic_signature((event.get("payload") or {}).get("fields", {}).get("description"))
        for event in tasks
    ]
    if not descriptions:
        return False
    signatures = Counter(descriptions)
    return (
        len(set(descriptions)) / len(descriptions) >= 0.85
        and max(signatures.values(), default=0) / len(descriptions) <= 0.05
    )


def _has_routine_rhythm(events: list[dict[str, Any]]) -> bool:
    """Check weekend and off-hours rates independently for each object type."""
    if not _is_production_delivery(events):
        return True
    latest = _latest_upserts(events)
    by_type: dict[str, list[datetime]] = defaultdict(list)
    for event in latest.values():
        at = _event_time(event)
        if at is not None:
            by_type[str(event.get("object_type"))].append(at)
    exceptional = {"incident", "mitigation", "emergency_deployment"}
    for object_type, times in by_type.items():
        if len(times) < 50:
            continue
        weekend = sum(at.weekday() >= 5 for at in times) / len(times)
        off_hours = sum(not 8 <= at.hour < 18 for at in times) / len(times)
        if object_type not in {"incident", "deployment"} and (weekend >= 0.08 or off_hours >= 0.12):
            return False
        for event in latest.values():
            if str(event.get("object_type")) != object_type:
                continue
            at = _event_time(event)
            if at is None or 8 <= at.hour < 18 or at.weekday() < 5:
                continue
            if _stage(event) not in exceptional:
                return False
    return True


def _has_vacation_conflicts(events: list[dict[str, Any]], directory: dict[str, Any]) -> bool:
    """Reject source activity that names a person while that person is on vacation."""
    windows: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    people_by_id = {
        str(person.get("id")): person
        for person in directory.get("people", [])
        if isinstance(person, dict) and person.get("id")
    }
    for item in directory.get("availability", []):
        if not isinstance(item, dict) or item.get("kind") != "vacation":
            continue
        try:
            start = datetime.fromisoformat(str(item["valid_from"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(item["valid_to"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            return False
        windows[str(item.get("person_id"))].append((start, end))
    for event in events:
        at = _event_time(event)
        if at is None:
            return False
        payload_text = json.dumps(event.get("payload") or {}, ensure_ascii=False)
        for employee_id in set(re.findall(r"\bE\d{3}\b", payload_text)):
            person_id = f"RW-{employee_id}"
            if person_id not in people_by_id:
                continue
            if any(start <= at < end for start, end in windows.get(person_id, [])):
                return False
    return True


def _has_changed_file_distribution(events: list[dict[str, Any]]) -> bool:
    """Require an MR changed-file distribution with a non-trivial tail."""
    merge_requests = [
        event
        for event in _latest_upserts(events).values()
        if event.get("source") == "gitlab-redwood"
        and event.get("object_type") == "merge_request"
        and str(event.get("external_id", "")).startswith("dsid_")
    ]
    if not merge_requests:
        return True
    sizes = [len((event.get("payload") or {}).get("changed_files") or []) for event in merge_requests]
    if len(sizes) < 5:
        return False
    counts = Counter(sizes)
    tail = sum(size >= 3 for size in sizes) / len(sizes)
    return len(counts) >= 3 and max(counts.values()) <= len(sizes) * 0.90 and max(sizes) >= 4 and tail >= 0.05


def _has_cancelled_mr_downstream(events: list[dict[str, Any]], links: list[dict[str, Any]]) -> bool:
    """Cancelled merge requests must not causally drive deployments or incidents."""
    latest = _latest_upserts(events)
    cancelled_aliases = {
        alias
        for event in latest.values()
        if event.get("source") == "gitlab-redwood"
        and event.get("object_type") == "merge_request"
        and str(event.get("external_id", "")).startswith("dsid_")
        and (event.get("payload") or {}).get("state") == "closed"
        for alias in [str(event.get("entity_id")), *map(str, event.get("aliases", []))]
    }
    return not any(
        str(link.get("source_entity_id")) in cancelled_aliases
        and any(token in str(link.get("rule_id")) for token in ("deployment", "incident", "postmortem"))
        for link in links
    )


def _has_causal_edge_direction(events: list[dict[str, Any]], links: list[dict[str, Any]]) -> bool:
    """Require causal edges to point from the later consequence to the earlier cause."""
    lookup = _event_lookup(events)
    relevant_links = [
        link
        for link in links
        if str(link.get("rule_id", "")).startswith(
            (
                "enterprise_batch.",
                "external_ticket.",
                "external_meeting.",
                "external_decision.",
                "external_task.",
                "external_mr.",
                "external_deployment.",
                "external_incident.",
                "external_postmortem.",
                "external_chain.",
            )
        )
    ]
    for link in relevant_links:
        source = lookup.get(str(link.get("source_entity_id")))
        target = lookup.get(str(link.get("target_entity_id")))
        if source is None or target is None:
            return False
        source_at = _event_time(source)
        target_at = _event_time(target)
        if source_at is None or target_at is None or source_at <= target_at:
            return False
        if not _stage(source) or not _stage(target):
            return False
    return True


def _has_source_configs(sources: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    configured = {
        str(source.get("id")): source
        for source in sources.get("sources", [])
        if isinstance(source, dict) and source.get("id")
    }
    if not configured:
        return False
    for event in events:
        source = configured.get(str(event.get("source")))
        if source is None or not event.get("container"):
            return False
        containers = source.get("containers")
        if isinstance(containers, dict):
            if str(event.get("container")) not in containers:
                return False
        elif isinstance(containers, list):
            ids = {str(item.get("id")) for item in containers if isinstance(item, dict)}
            if str(event.get("container")) not in ids:
                return False
        else:
            return False
    return True


def _principal_groups(directory: dict[str, Any], principal: str, at: datetime) -> set[str]:
    groups: set[str] = set()
    for person in directory.get("people", []):
        if not isinstance(person, dict) or str(person.get("principal")) != principal:
            continue
        for membership in person.get("memberships", []):
            if not isinstance(membership, dict):
                continue
            try:
                start = datetime.fromisoformat(str(membership["valid_from"]).replace("Z", "+00:00"))
                end = (
                    datetime.fromisoformat(str(membership["valid_to"]).replace("Z", "+00:00"))
                    if membership.get("valid_to")
                    else None
                )
            except (KeyError, TypeError, ValueError):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=at.tzinfo)
            if end is not None and end.tzinfo is None:
                end = end.replace(tzinfo=at.tzinfo)
            if start <= at and (end is None or at <= end):
                groups.add(str(membership.get("group")))
    return groups


def _event_acl_rules(event: dict[str, Any], sources: dict[str, Any]) -> tuple[set[str], set[str]]:
    acl = event.get("acl")
    if isinstance(acl, list):
        return {str(value) for value in acl}, set()
    if isinstance(acl, dict):
        return (
            {
                str(rule.get("principal")) if isinstance(rule, dict) else str(rule)
                for rule in acl.get("allow", [])
            },
            {
                str(rule.get("principal")) if isinstance(rule, dict) else str(rule)
                for rule in acl.get("deny", [])
            },
        )
    tombstone = event.get("tombstone") or {}
    effective = tombstone.get("effective_acl") if isinstance(tombstone, dict) else None
    if isinstance(effective, dict):
        return _event_acl_rules({"acl": effective}, sources)
    for source in sources.get("sources", []):
        if not isinstance(source, dict) or str(source.get("id")) != str(event.get("source")):
            continue
        containers = source.get("containers", {})
        container = containers.get(str(event.get("container"))) if isinstance(containers, dict) else None
        if isinstance(container, dict):
            return {str(value) for value in container.get("acl", [])}, set()
    return set(), set()


def _effective_access(
    event: dict[str, Any],
    principal: str,
    at: datetime,
    directory: dict[str, Any],
    sources: dict[str, Any],
) -> bool:
    if principal in {str(value) for value in directory.get("admins", [])}:
        return True
    allow, deny = _event_acl_rules(event, sources)
    groups = _principal_groups(directory, principal, at)
    tokens = {principal, *groups, *(f"group:{group}" for group in groups)}
    if deny.intersection(tokens):
        return False
    return bool(allow.intersection(tokens))


def _has_acl_recomputation(
    cases: list[dict[str, Any]],
    events: list[dict[str, Any]],
    directory: dict[str, Any],
    sources: dict[str, Any],
) -> bool:
    """Recompute effective access from directory memberships, source ACLs, and event ACLs."""
    lookup = _event_lookup(events)
    known_principals = {str(value) for value in directory.get("admins", [])}
    known_groups = {str(value) for value in directory.get("groups", {})}
    for person in directory.get("people", []):
        if isinstance(person, dict):
            if person.get("principal"):
                known_principals.add(str(person["principal"]))
            known_groups.update(map(str, person.get("groups", [])))
    for contact in directory.get("contacts", []):
        if isinstance(contact, dict) and contact.get("principal"):
            known_principals.add(str(contact["principal"]))
    for event in events:
        allow, deny = _event_acl_rules(event, sources)
        if not allow and not deny:
            return False
        if not (allow | deny).issubset(known_principals | known_groups):
            return False
    acl_cases = [case for case in cases if case.get("category") == "acl"]
    if not acl_cases:
        return True
    for case in acl_cases:
        principal = str(case.get("principal") or "")
        if not principal:
            return False
        scenario = str(case.get("acl_scenario") or "")
        context = case.get("acl_context")
        if not isinstance(context, list) or not context:
            return False
        for snapshot in context:
            if not isinstance(snapshot, dict) or str(snapshot.get("principal")) != principal:
                return False
            if scenario == "historical_as_of":
                try:
                    cutoff = datetime.fromisoformat(str(snapshot["cutoff"]).replace("Z", "+00:00"))
                    valid_from = datetime.fromisoformat(str(snapshot["valid_from"]).replace("Z", "+00:00"))
                    valid_to = (
                        datetime.fromisoformat(str(snapshot["valid_to"]).replace("Z", "+00:00"))
                        if snapshot.get("valid_to")
                        else None
                    )
                except (KeyError, TypeError, ValueError):
                    return False
                if cutoff < valid_from or valid_to is not None and cutoff > valid_to:
                    return False
        for item in case.get("forbidden", []):
            if not isinstance(item, dict):
                return False
            event = lookup.get(str(item.get("entity_id")))
            if event is None:
                return False
            known_at = _event_time(event)
            if case.get("known_at"):
                try:
                    known_at = datetime.fromisoformat(str(case["known_at"]).replace("Z", "+00:00"))
                except ValueError:
                    return False
                if known_at.tzinfo is None and _event_time(event) is not None:
                    known_at = known_at.replace(tzinfo=_event_time(event).tzinfo)
            effective = known_at is not None and _effective_access(
                event, principal, known_at, directory, sources
            )
            if scenario in {"unrelated_not_found", "membership_change", "finance_secret_denied"}:
                if effective:
                    return False
            elif (
                scenario
                in {
                    "sales_margin_redacted",
                    "engineering_commercial_redacted",
                }
                and not effective
            ):
                return False
        if scenario in {"finance_full", "manager_inherited", "historical_as_of"}:
            for item in case.get("evidence", []):
                event = lookup.get(str(item.get("entity_id")))
                if event is None:
                    return False
                known_at = _event_time(event)
                if case.get("known_at"):
                    try:
                        known_at = datetime.fromisoformat(str(case["known_at"]).replace("Z", "+00:00"))
                    except ValueError:
                        return False
                if known_at is None or not _effective_access(event, principal, known_at, directory, sources):
                    return False
        for item in case.get("evidence", []):
            if not isinstance(item, dict):
                return False
            event = lookup.get(str(item.get("entity_id")))
            if event is None:
                return False
    return True


def _has_mutation_deletion_counts(events: list[dict[str, Any]]) -> bool:
    """Require edits, reopened states, revoked financial states, and tombstones."""
    if not _is_production_delivery(events):
        return True
    revisions = sum(event.get("op") == "upsert" and int(event.get("revision", 0)) >= 2 for event in events)
    deletions = sum(event.get("op") == "delete" for event in events)
    edits = sum(
        event.get("source") == "slack-redwood"
        and int(event.get("revision", 0)) >= 2
        and bool((event.get("payload") or {}).get("edited"))
        for event in events
    )
    reopened = sum(
        (event.get("payload") or {}).get("fields", {}).get("status", {}).get("name") == "Reopened"
        for event in events
    )
    revoked = sum(
        (event.get("payload") or {}).get("properties", {}).get("status") in {"revoked", "voided"}
        for event in events
    )
    return revisions > 0 and deletions > 0 and edits > 0 and reopened > 0 and revoked > 0


def _has_case_family_account_principal(cases: list[dict[str, Any]], events: list[dict[str, Any]]) -> bool:
    """Derive case family, account, and principal diversity from evidence."""
    if not _is_production_delivery(events):
        return True
    lookup = _event_lookup(events)
    families: Counter[str] = Counter()
    accounts: set[str] = set()
    principals = {str(case.get("principal")) for case in cases if case.get("principal")}
    for case in [case for case in cases if case.get("category") == "why"]:
        evidence = [lookup.get(str(item.get("entity_id"))) for item in case.get("evidence", [])]
        evidence = [event for event in evidence if event]
        if not evidence:
            return False
        payloads = [(event.get("payload") or {}) for event in evidence]
        if any(payload.get("redwoodExternalChainId") for payload in payloads):
            family = "external"
        elif any(event.get("source") == "hubspot-redwood" for event in evidence):
            family = "commercial"
        elif any(
            str(payload.get("redwoodCausalChainId", "")).startswith("enterprise-") for payload in payloads
        ):
            family = "operational"
        else:
            family = "support"
        families[family] += 1
        for payload in payloads:
            references = payload.get("redwoodReferences") or {}
            if references.get("accountId"):
                accounts.add(str(references["accountId"]))
    return len(families) >= 4 and len(accounts) >= 6 and len(principals) >= 6


def _has_deterministic_ordering(
    events: list[dict[str, Any]], links: list[dict[str, Any]], cases: list[dict[str, Any]]
) -> bool:
    """Catch ordering drift that makes deterministic-sensitive samples unstable."""
    sequences = [event.get("sequence") for event in events]
    if sequences and sequences != list(range(1, len(sequences) + 1)):
        return False
    by_entity: dict[str, list[int]] = defaultdict(list)
    for event in events:
        try:
            by_entity[str(event.get("entity_id"))].append(int(event.get("revision", 0)))
        except (TypeError, ValueError):
            return False
    if any(revisions != sorted(set(revisions)) for revisions in by_entity.values()):
        return False
    identifiers = [str(link.get("link_id", "")) for link in links]
    if identifiers:

        def link_key(value: str) -> int | str:
            suffix = value.rsplit("-", 1)[-1]
            return int(suffix) if suffix.isdigit() else value

        if identifiers != sorted(identifiers, key=link_key):
            return False
    case_ids = [str(case.get("id", "")) for case in cases]
    if not case_ids:
        return True

    def case_key(value: str) -> int | str:
        suffix = value[1:]
        return int(suffix) if suffix.isdigit() else value

    return case_ids == sorted(case_ids, key=case_key)


def _provider_contract_audit(
    events: list[dict[str, Any]],
    links: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    directory: dict[str, Any],
) -> dict[str, Any]:
    """Audit the raw provider-facing contract independently of internal IDs."""
    timeline_sort_violations = 0
    sequence_world_at_mismatches = 0
    previous_key: tuple[datetime, str] | None = None
    for index, event in enumerate(events, start=1):
        when = _event_time(event)
        event_key = str(event.get("event_id") or "")
        if when is None:
            timeline_sort_violations += 1
        else:
            key = (when, event_key)
            if previous_key is not None and key < previous_key:
                timeline_sort_violations += 1
            previous_key = key
        if event.get("sequence") != index:
            sequence_world_at_mismatches += 1

    provider_history_inversions = 0
    by_provider_object: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_provider_object[
            (str(event.get("source")), str(event.get("object_type")), str(event.get("external_id")))
        ].append(event)
    for history in by_provider_object.values():
        history.sort(key=lambda item: (int(item.get("revision", 0)), str(item.get("event_id", ""))))
        times = [_event_time(item) for item in history]
        if any(
            left is None or right is None or right < left
            for left, right in zip(times, times[1:], strict=False)
        ):
            provider_history_inversions += 1

    chain_order = _ENTERPRISE_CHAIN_STAGES
    causal_chronology_violations = 0
    for stages in _latest_chain_stages(events).values():
        ordered = [stages[stage] for stage in chain_order if stage in stages]
        times = [
            _event_time(item)
            for items in ordered
            for item in [max(items, key=lambda x: x.get("revision", 0))]
        ]
        if any(
            left is None or right is None or right <= left
            for left, right in zip(times, times[1:], strict=False)
        ):
            causal_chronology_violations += 1
    for stages in _latest_chain_stages(events).values():
        ordered = [stages[stage] for stage in _EXTERNAL_CHAIN_STAGES if stage in stages]
        times = [
            _event_time(item)
            for items in ordered
            for item in [max(items, key=lambda x: x.get("revision", 0))]
        ]
        if any(
            left is None or right is None or right <= left
            for left, right in zip(times, times[1:], strict=False)
        ):
            causal_chronology_violations += 1

    payload_id_mismatches = 0
    jira_key_violations = 0
    for event in events:
        if event.get("op") != "upsert" or not isinstance(event.get("payload"), dict):
            continue
        payload = event["payload"]
        external_id = str(event.get("external_id", ""))
        if str(payload.get("id", "")) != external_id:
            payload_id_mismatches += 1
        if event.get("source") == "jira-redwood":
            key = str((payload.get("fields") or {}).get("key", ""))
            if not re.fullmatch(r"PROJECT-\d+", key):
                jira_key_violations += 1
        if (
            event.get("source") == "gitlab-redwood"
            and event.get("object_type") == "merge_request"
            and str(payload.get("iid", "")) != external_id
        ):
            payload_id_mismatches += 1
        if event.get("source") == "mail-redwood" and str(payload.get("internetMessageId", "")) != external_id:
            payload_id_mismatches += 1

    alias_index = {str(alias) for event in events for alias in event.get("aliases", []) if alias}
    unresolved_gold = [
        link.get("link_id")
        for link in links
        if str(link.get("source_entity_id")) not in alias_index
        or str(link.get("target_entity_id")) not in alias_index
        or any(str(value) not in alias_index for value in link.get("evidence_entity_ids", []))
    ]
    unresolved_cases = [
        case.get("id")
        for case in cases
        if any(
            str(item.get("entity_id")) not in alias_index
            for field in ("evidence", "roots", "forbidden")
            for item in case.get(field, [])
            if isinstance(item, dict) and item.get("entity_id")
        )
    ]
    redwood_external_references = sum(
        1
        for link in links
        for value in (
            link.get("source_entity_id"),
            link.get("target_entity_id"),
            *link.get("evidence_entity_ids", []),
        )
        if str(value).startswith("redwood:")
    ) + sum(
        1
        for case in cases
        for field in ("evidence", "roots", "forbidden")
        for item in case.get(field, [])
        if isinstance(item, dict) and str(item.get("entity_id", "")).startswith("redwood:")
    )
    target_by_alias = {
        str(alias): event
        for event in events
        for alias in event.get("aliases", [])
        if alias
    }
    implicit_target_ids = sum(
        1
        for link in links
        if not link.get("explicit")
        and str(link.get("evidence_quote", "")).casefold()
        == str(
            (target_by_alias.get(str(link.get("target_entity_id"))) or {}).get("external_id", "")
        ).casefold()
    )

    email_participants: dict[str, str] = {}
    for person in directory.get("people", []):
        if isinstance(person, dict) and person.get("email"):
            email_participants[str(person["email"]).casefold()] = str(person.get("principal", ""))
    for contact in directory.get("contacts", []):
        if isinstance(contact, dict) and contact.get("email"):
            email_participants[str(contact["email"]).casefold()] = str(contact.get("principal", ""))
    emails_without_body = 0
    emails_without_acl = 0
    broad_email_acl = 0
    unknown_participants: list[str] = []
    missing_email_acl_participants = 0
    for event in events:
        if event.get("source") != "mail-redwood" or event.get("op") != "upsert":
            continue
        payload = event.get("payload") or {}
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        if not str(payload.get("subject") or "").strip() or not str(body.get("content") or "").strip():
            emails_without_body += 1
        acl = {str(value) for value in event.get("acl", [])}
        if not acl:
            emails_without_acl += 1
        if "grp:all-staff" in acl:
            broad_email_acl += 1
        participants = []
        sender = payload.get("from", {}).get("emailAddress", {}).get("address")
        if sender:
            participants.append(sender)
        for field in ("toRecipients", "ccRecipients", "bccRecipients"):
            participants.extend(
                item.get("emailAddress", {}).get("address")
                for item in payload.get(field, [])
                if isinstance(item, dict)
            )
        expected = {email_participants.get(str(value).casefold(), "") for value in participants}
        unknown = {str(value) for value in participants if str(value).casefold() not in email_participants}
        unknown_participants.extend(sorted(unknown))
        missing_email_acl_participants += sum(
            bool(principal) and f"{principal}" not in acl for principal in expected
        )

    tasks = []
    for event in _latest_upserts(events).values():
        fields = (event.get("payload") or {}).get("fields") or {}
        if event.get("source") == "jira-redwood" and fields.get("issuetype", {}).get("name") == "Task":
            tasks.append(event)
    final_states = Counter(
        str(((event.get("payload") or {}).get("fields") or {}).get("status", {}).get("name") or "unknown")
        for event in tasks
    )
    closed_tasks = [
        event
        for event in tasks
        if str(((event.get("payload") or {}).get("fields") or {}).get("status", {}).get("name") or "")
        in {"Done", "Closed"}
    ]
    total_transitions = 0
    in_progress_closed = 0
    reopened_tasks = 0
    employee_days: set[tuple[str, str]] = set()
    for event in tasks:
        histories = ((event.get("payload") or {}).get("changelog") or {}).get("histories") or []
        status_names = [
            str(item.get("toString") or "") for history in histories for item in history.get("items", [])
        ]
        if "Reopened" in status_names:
            reopened_tasks += 1
        if str(((event.get("payload") or {}).get("fields") or {}).get("status", {}).get("name")) in {
            "Done",
            "Closed",
        }:
            in_progress_closed += int("In Progress" in status_names)
        for history in histories:
            for item in history.get("items", []):
                if item.get("field") != "status":
                    continue
                total_transitions += 1
                author = str((history.get("author") or {}).get("accountId") or "unknown")
                created = str(history.get("created") or "")[:10]
                employee_days.add((author, created))
    open_tasks = sum(state not in {"Done", "Closed", "Cancelled"} for state in final_states.elements())
    transition_rate = total_transitions / max(1, 45 * 180)

    routine_weekdays: Counter[str] = Counter()
    routine_by_source_object: dict[str, Counter[str]] = defaultdict(Counter)
    routine_weekend = 0
    routine_off_hours = 0
    on_call = 0
    routine_count = 0
    for event in _latest_upserts(events).values():
        when = _event_time(event)
        if when is None:
            continue
        payload = event.get("payload") or {}
        stage = str(payload.get("redwoodStage") or payload.get("redwoodExternalStage") or "")
        if stage in _ON_CALL_STAGES:
            on_call += 1
        if event.get("object_type") not in _ROUTINE_OBJECT_TYPES or stage in _ON_CALL_STAGES:
            continue
        routine_count += 1
        routine_weekdays[when.strftime("%A")] += 1
        routine_by_source_object[f"{event.get('source')}:{event.get('object_type')}"][
            when.strftime("%A")
        ] += 1
        routine_weekend += int(when.weekday() >= 5)
        routine_off_hours += int(not 8 <= when.hour < 18)

    on_call_ratio = on_call / max(1, routine_count + on_call)

    vacation_conflicts = 0
    vacations = [item for item in directory.get("availability", []) if item.get("status") == "vacation"]
    for event in _latest_upserts(events).values():
        when = _event_time(event)
        if when is None:
            continue
        payload_text = json.dumps(event.get("payload") or {}, ensure_ascii=False)
        for vacation in vacations:
            start = _event_time({"world_at": vacation.get("valid_from")})
            end = _event_time({"world_at": vacation.get("valid_to")})
            if start and end and start <= when < end and str(vacation.get("person_id")) in payload_text:
                stage = str((event.get("payload") or {}).get("redwoodStage") or "")
                if stage not in _ON_CALL_STAGES:
                    vacation_conflicts += 1

    north_star_violations = 0
    meeting_protocol_violations = 0
    attachments_without_meta = 0
    for event in events:
        payload = event.get("payload") or {}
        if event.get("op") == "upsert" and str(event.get("external_id", "")).startswith("north-star-"):
            required = {"reportPeriod", "owner", "sourceRecords", "metrics", "financials", "reconciliation"}
            financials = payload.get("financials") or {}
            if (
                not required.issubset(payload)
                or not payload.get("sourceRecords")
                or not payload.get("metrics")
            ):
                north_star_violations += 1
            elif abs(float(financials.get("arr_usd", 0)) - float(financials.get("mrr_usd", 0)) * 12) > 0.01:
                north_star_violations += 1
        if event.get("op") == "upsert" and event.get("object_type") == "transcript":
            protocol = payload.get("protocol")
            required = {"attendees", "agenda", "decisions", "owners", "actions", "timestamps"}
            if (
                not isinstance(protocol, dict)
                or not required.issubset(protocol)
                or isinstance(payload.get("body"), str)
            ):
                meeting_protocol_violations += 1

        def visit(value: object) -> None:
            nonlocal attachments_without_meta
            if isinstance(value, dict):
                for attachment in value.get("attachments", []):
                    if isinstance(attachment, dict):
                        meta = attachment.get("documentMeta")
                        if not isinstance(meta, dict) or not {
                            "attachmentId",
                            "filename",
                            "mimeType",
                            "sourceDocument",
                            "checksum",
                        }.issubset(meta):
                            attachments_without_meta += 1
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)

    weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    weekday_values = [routine_weekdays.get(day, 0) for day in weekdays]
    nonzero_weekdays = [value for value in weekday_values if value]
    weekday_balanced = (
        not routine_count
        or bool(nonzero_weekdays)
        and max(weekday_values) <= routine_count * 0.25
        and max(nonzero_weekdays) / min(nonzero_weekdays) <= 1.5
    )
    by_source_object_balanced = True
    for counts in routine_by_source_object.values():
        values = [counts.get(day, 0) for day in weekdays]
        nonzero = [value for value in values if value]
        if sum(values) >= 100 and len(nonzero) >= 5 and (
            max(values) > sum(values) * 0.25 or max(nonzero) / min(nonzero) > 1.5
        ):
            by_source_object_balanced = False

    return {
        "checks": {
            "timeline_sorted": timeline_sort_violations == 0,
            "sequence_world_at": sequence_world_at_mismatches == 0,
            "provider_histories_chronological": provider_history_inversions == 0,
            "causal_chronology": causal_chronology_violations == 0,
            "provider_payload_ids": payload_id_mismatches == 0 and jira_key_violations == 0,
            "gold_links_resolve": not unresolved_gold,
            "case_references_resolve": not unresolved_cases,
            "no_redwood_external_references": redwood_external_references == 0,
            "implicit_targets_absent": implicit_target_ids == 0,
            "email_completeness": emails_without_body == 0,
            "email_acl": emails_without_acl == 0 and missing_email_acl_participants == 0,
            "email_acl_not_broad": broad_email_acl == 0,
            "email_participants_known": not unknown_participants,
            "task_lifecycle": all(
                (
                    "In Progress" in statuses
                    and bool({"Review", "Validation"}.intersection(statuses))
                )
                for event in closed_tasks
                for statuses in [
                    {
                        str(item.get("toString") or "")
                        for history in (
                            ((event.get("payload") or {}).get("changelog") or {}).get("histories") or []
                        )
                        for item in history.get("items", [])
                    }
                ]
            ),
            "task_status_rhythm": (
                not _is_production_delivery(events)
                or transition_rate >= THIRD_DELIVERY_STATUS_RATE_TARGET
            ),
            "reopened_tasks_present": not _is_production_delivery(events) or reopened_tasks > 0,
            "open_task_ratio": open_tasks / max(1, len(tasks)) <= THIRD_DELIVERY_MAX_OPEN_TASK_RATIO,
            "weekday_distribution": routine_count == 0
            or (
                routine_weekend == 0
                and routine_off_hours == 0
                and weekday_balanced
                and by_source_object_balanced
            ),
            "on_call_distribution": (
                not _is_production_delivery(events) and on_call == 0
            ) or 0.01 <= on_call_ratio <= 0.03,
            "vacation_conflicts": vacation_conflicts == 0,
            "north_star_schema": north_star_violations == 0,
            "meeting_protocol_schema": meeting_protocol_violations == 0,
            "attachment_document_meta": attachments_without_meta == 0,
        },
        "metrics": {
            "timeline_sort_violations": timeline_sort_violations,
            "sequence_world_at_mismatches": sequence_world_at_mismatches,
            "provider_history_inversions": provider_history_inversions,
            "causal_chronology_violations": causal_chronology_violations,
            "payload_external_id_mismatches": payload_id_mismatches,
            "jira_key_violations": jira_key_violations,
            "unresolved_gold_link_endpoints": len(unresolved_gold),
            "unresolved_case_references": len(unresolved_cases),
            "redwood_external_references": redwood_external_references,
            "implicit_target_ids": implicit_target_ids,
            "emails_without_body": emails_without_body,
            "emails_without_acl": emails_without_acl,
            "broad_email_acl_count": broad_email_acl,
            "unknown_email_participants": len(unknown_participants),
            "missing_email_acl_participants": missing_email_acl_participants,
            "task_lifecycle": {
                "task_count": len(tasks),
                "closed_count": len(closed_tasks),
                "closed_through_in_progress": in_progress_closed,
                "reopened_task_count": reopened_tasks,
                "final_state_distribution": dict(final_states),
                "status_transition_count": total_transitions,
                "status_transition_rate_per_employee_day": round(transition_rate, 4),
            },
            "open_task_ratio": round(open_tasks / max(1, len(tasks)), 6),
            "weekday_distribution": dict(routine_weekdays),
            "weekday_distribution_by_source_object": {
                key: dict(value) for key, value in sorted(routine_by_source_object.items())
            },
            "routine_weekend_events": routine_weekend,
            "routine_off_hours_events": routine_off_hours,
            "allowed_on_call_ratio": round(on_call_ratio, 6),
            "vacation_conflicts": vacation_conflicts,
            "north_star_schema_violations": north_star_violations,
            "meeting_protocol_schema_violations": meeting_protocol_violations,
            "attachments_without_document_meta": attachments_without_meta,
        },
    }


def _has_materialized_data(root: Path, events: list[dict[str, Any]]) -> bool:
    """Validate canonical materialized documents without consulting manifest counts."""
    documents = _read_jsonl(root.parent / "documents.jsonl")
    if not documents:
        return False
    document_ids = {str(document.get("doc_id")) for document in documents}
    if any(
        not document.get("doc_id") or document.get("layer") not in {"internal", "chain", "external"}
        for document in documents
    ):
        return False
    external_ids = {
        str((event.get("payload") or {}).get("canonicalDocumentId"))
        for event in _latest_upserts(events).values()
        if (event.get("payload") or {}).get("redwoodExternalAction")
    }
    return not external_ids or external_ids.issubset(document_ids)


def _has_project_dag_shape(events: list[dict[str, Any]], links: list[dict[str, Any]]) -> bool:
    """Require a branching project DAG rather than independent identical ladders."""
    if not _is_production_delivery(events):
        return True
    project_links = [link for link in links if link.get("rule_id") == "enterprise_project.depends_on.v1"]
    if not project_links:
        return False
    graph: dict[str, set[str]] = defaultdict(set)
    incoming: Counter[str] = Counter()
    outgoing: Counter[str] = Counter()
    by_reference = {
        reference: event
        for event in _latest_upserts(events).values()
        for reference in [str(event.get("entity_id")), *map(str, event.get("aliases", []))]
    }
    for link in project_links:
        source = str(link.get("source_entity_id"))
        target = str(link.get("target_entity_id"))
        source_event = by_reference.get(source)
        if not source_event or not link.get("evidence_quote"):
            return False
        source_text = json.dumps(source_event.get("payload") or {}, ensure_ascii=False)
        if str(link["evidence_quote"]) not in source_text:
            return False
        graph[source].add(target)
        incoming[target] += 1
        outgoing[source] += 1
    nodes = set(graph) | set(incoming)
    roots = nodes - set(incoming)
    if len(roots) != 12 or any(value > 3 for value in incoming.values()):
        return False
    if not any(value >= 2 for value in incoming.values()) or not any(
        value >= 2 for value in outgoing.values()
    ):
        return False
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return False
        if node in visited:
            return True
        visiting.add(node)
        if any(not visit(child) for child in graph.get(node, ())):
            return False
        visiting.remove(node)
        visited.add(node)
        return True

    if not all(visit(node) for node in nodes):
        return False

    depths: dict[str, int] = {}

    def depth(node: str) -> int:
        if node in depths:
            return depths[node]
        children = graph.get(node, set())
        depths[node] = 1 + max((depth(child) for child in children), default=0)
        return depths[node]

    return 2 <= max((depth(node) for node in nodes), default=0) <= 6


def _coverage_breakdown(events: list[dict[str, Any]]) -> dict[str, int]:
    latest = _latest_upserts(events)
    enterprise = 0
    external = 0
    support = 0
    for event in latest.values():
        payload = event.get("payload") or {}
        chain_id = str(
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or ""
        )
        if chain_id.startswith("enterprise-causal-batch-"):
            enterprise += 1
        elif chain_id.startswith("external-chain-"):
            external += 1
        elif chain_id.startswith("EP-"):
            support += 1
    return {
        "enterprise-causal-batch-events": enterprise,
        "external-chain-events": external,
        "support-episode-events": support,
    }


def _has_causal_quality(
    events: list[dict[str, Any]], links: list[dict[str, Any]], cases: list[dict[str, Any]]
) -> bool:
    """Enforce production causal semantics instead of structural presence only."""
    if len(events) <= 10_000:
        return True
    latest = _latest_upserts(events)
    aliases = {alias: event for event in latest.values() for alias in event.get("aliases", [])}
    rules = Counter(str(link.get("rule_id")) for link in links)
    batch_rules = {
        "enterprise_batch.ticket.email.v1",
        "enterprise_batch.meeting.ticket.v1",
        "enterprise_batch.decision.meeting.v1",
        "enterprise_batch.task.decision.v1",
        "enterprise_batch.mr.task.v1",
        "enterprise_batch.deployment.mr.v1",
        "enterprise_batch.incident.deployment.v1",
        "enterprise_batch.postmortem.incident.v1",
    }
    if any(rules[rule] < 1_000 for rule in batch_rules):
        return False

    chains: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for event in latest.values():
        payload = event.get("payload") or {}
        chain_id = payload.get("redwoodCausalChainId") or payload.get("redwoodExternalChainId")
        stage = payload.get("redwoodStage") or payload.get("redwoodExternalStage")
        if chain_id and stage:
            chains[str(chain_id)][str(stage)].append(event)

    def ordered(chain: dict[str, list[dict[str, Any]]], stages: list[str]) -> bool:
        if not all(stage in chain for stage in stages):
            return False
        stage_times = []
        for stage in stages:
            times = [_event_time(event) for event in chain[stage]]
            if not times or any(value is None for value in times):
                return False
            stage_times.append(sorted(value for value in times if value is not None))
        return all(max(left) < min(right) for left, right in zip(stage_times, stage_times[1:], strict=False))

    enterprise_ids = [chain_id for chain_id in chains if chain_id.startswith("enterprise-causal-batch-")]
    external_ids = [chain_id for chain_id in chains if chain_id.startswith("external-chain-")]
    enterprise_stages = [
        "customer_email",
        "ticket",
        "meeting",
        "decision",
        "task",
        "merge_request",
        "deployment",
        "incident",
        "postmortem",
    ]
    external_stages = [
        "action",
        "ticket",
        "meeting",
        "decision",
        "task",
        "merge_request",
        "deployment",
        "incident",
        "postmortem",
    ]
    if len(enterprise_ids) != 1_000 or not all(
        ordered(chains[chain_id], enterprise_stages) for chain_id in enterprise_ids
    ):
        return False
    external_signal_ids = {
        str((event.get("payload") or {}).get("canonicalDocumentId"))
        for event in latest.values()
        if (event.get("payload") or {}).get("redwoodExternalAction")
    }
    external_chain_signal_ids = {
        str((event.get("payload") or {}).get("canonicalDocumentId"))
        for chain_id in external_ids
        for event in chains[chain_id].get("action", [])
    }
    if not external_signal_ids or len(external_chain_signal_ids) / len(external_signal_ids) < 0.75:
        return False

    def ordered_partial(chain: dict[str, list[dict[str, Any]]]) -> bool:
        present = [stage for stage in external_stages if stage in chain]
        return len(present) >= 2 and ordered(chain, present)

    if not external_ids or not all(ordered_partial(chains[chain_id]) for chain_id in external_ids):
        return False
    if any(
        (event.get("payload") or {}).get("redwoodExternalStage") != "meeting"
        for chain_id in external_ids
        for event in chains[chain_id].get("meeting", [])
    ):
        return False

    direction_rules = {
        *batch_rules,
        "external_ticket.action.v1",
        "external_meeting.ticket.v1",
        "external_decision.meeting.v1",
        "external_task.decision.v1",
        "external_mr.task.v1",
        "external_deployment.mr.v1",
        "external_incident.deployment.v1",
        "external_postmortem.incident.v1",
    }
    for link in links:
        if str(link.get("rule_id")) not in direction_rules:
            continue
        source = aliases.get(str(link.get("source_entity_id")))
        target = aliases.get(str(link.get("target_entity_id")))
        source_at = _event_time(source or {})
        target_at = _event_time(target or {})
        if source_at is None or target_at is None or source_at <= target_at:
            return False

    closed_aliases = {
        alias
        for event in latest.values()
        if event.get("source") == "gitlab-redwood"
        and str(event.get("external_id", "")).startswith("dsid_")
        and (event.get("payload") or {}).get("state") == "closed"
        for alias in event.get("aliases", [])
    }
    if any(
        str(link.get("source_entity_id")) in closed_aliases
        and any(token in str(link.get("rule_id")) for token in ("deployment", "incident", "postmortem"))
        for link in links
    ):
        return False

    project_edges = [
        (str(link.get("source_entity_id")), str(link.get("target_entity_id")))
        for link in links
        if link.get("rule_id") == "enterprise_project.depends_on.v1"
    ]
    graph: dict[str, set[str]] = defaultdict(set)
    for source, target in project_edges:
        graph[source].add(target)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return False
        if node in visited:
            return True
        visiting.add(node)
        if any(not visit(child) for child in graph.get(node, ())):
            return False
        visiting.remove(node)
        visited.add(node)
        return True

    if not all(visit(node) for node in graph):
        return False

    close_cases = sum(
        any(fact.get("key") == "close_reason" for fact in case.get("answer_facts", [])) for case in cases
    )
    if close_cases > 15 or any(str(case.get("principal")) == "user:admin" for case in cases):
        return False
    why_cases = [case for case in cases if case.get("category") == "why"]
    if len(why_cases) != 11:
        return False
    for case in why_cases:
        if not any(
            (
                ((aliases.get(str(item.get("entity_id"))) or {}).get("payload") or {}).get("redwoodStage")
                or ((aliases.get(str(item.get("entity_id"))) or {}).get("payload") or {}).get(
                    "redwoodExternalStage"
                )
            )
            == "decision"
            for item in case.get("evidence", [])
        ):
            return False

    task_texts = [
        _quality_text((event.get("payload") or {}).get("fields", {}).get("description"))
        for event in latest.values()
        if event.get("source") == "jira-redwood"
        and (event.get("payload") or {}).get("fields", {}).get("issuetype", {}).get("name") == "Task"
        and "-TKT-" not in str(event.get("external_id", ""))
        and not str(event.get("external_id", "")).startswith(("INC", "external-action-"))
    ]
    comments = [
        str(comment.get("body"))
        for event in latest.values()
        for comment in (
            ((event.get("payload") or {}).get("fields", {}).get("comment") or {}).get("comments", [])
        )
    ]
    meetings = [
        str((event.get("payload") or {}).get("transcript"))
        for event in latest.values()
        if event.get("source") == "fireflies-redwood"
    ]
    subjects = [
        str((event.get("payload") or {}).get("subject"))
        for event in latest.values()
        if event.get("source") == "mail-redwood"
    ]
    bodies = [
        _quality_text((event.get("payload") or {}).get("body", {}).get("content"))
        for event in latest.values()
        if event.get("source") == "mail-redwood"
    ]
    diversity = (
        (task_texts, 0.85),
        (comments, 0.60),
        (meetings, 0.60),
        (subjects, 0.35),
        (bodies, 0.80),
    )
    for values, threshold in diversity:
        normalized = {_quality_text(value) for value in values if value}
        if values and len(normalized) / len(values) < threshold:
            return False

    mr_required = {
        "reviewers",
        "approvals",
        "pipelines",
        "changed_files",
        "diff_summary",
        "commits",
        "review_comments",
    }
    if any(
        not mr_required.issubset(event.get("payload") or {})
        for event in latest.values()
        if event.get("source") == "gitlab-redwood" and str(event.get("external_id", "")).startswith("dsid_")
    ):
        return False
    for event in latest.values():
        event_at = _event_time(event)
        if event_at is None:
            return False
        employee_ids = set(re.findall(r"\bE\d{1,3}\b", json.dumps(event.get("payload") or {})))
        for employee_id in employee_ids:
            number = int(employee_id[1:])
            if number < 1 or (number - 1) % 9 != 0:
                continue
            vacation_start = datetime(2026, 1, 1, tzinfo=event_at.tzinfo) + timedelta(days=30 + number - 1)
            vacation_end = vacation_start + timedelta(days=7)
            if vacation_start <= event_at < vacation_end:
                return False
    return True


def _has_quality_coverage(
    events: list[dict[str, Any]], links: list[dict[str, Any]], cases: list[dict[str, Any]]
) -> bool:
    """Reject production deliveries that are accepted structurally but remain templated."""
    if len(events) <= 10_000:
        return True
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("op") != "upsert" or not event.get("payload"):
            continue
        previous = latest.get(str(event.get("entity_id")))
        if previous is None or int(event.get("revision", 0)) > int(previous.get("revision", 0)):
            latest[str(event.get("entity_id"))] = event

    chain_stages: dict[str, set[str]] = defaultdict(set)
    for event in latest.values():
        payload = event.get("payload") or {}
        chain_id = (
            payload.get("redwoodEpisodeId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodCausalChainId")
            or payload.get("properties", {}).get("redwood_episode_id")
        )
        stage = payload.get("redwoodStage") or payload.get("redwoodExternalStage")
        if chain_id and stage:
            chain_stages[str(chain_id)].add(str(stage))
    complete_chains = sum(
        chain_id.startswith("enterprise-causal-batch-")
        and {"ticket", "meeting", "task", "merge_request", "deployment", "incident"}.issubset(stages)
        and "customer_email" in stages
        for chain_id, stages in chain_stages.items()
    )
    merge_requests = [
        event
        for event in latest.values()
        if event.get("source") == "gitlab-redwood"
        and event.get("object_type") == "merge_request"
        and event.get("external_id", "").startswith("dsid_")
    ]
    if complete_chains < 1_000:
        return False

    external_chain_ids = {
        str(chain_id)
        for chain_id, stages in chain_stages.items()
        if str(chain_id).startswith("external-chain-")
        and len(
            stages.intersection(
                {"ticket", "meeting", "decision", "task", "merge_request", "deployment", "incident"}
            )
        )
        >= 1
    }
    external_actions = {
        str((event.get("payload") or {}).get("canonicalDocumentId"))
        for event in latest.values()
        if (event.get("payload") or {}).get("redwoodExternalAction")
    }
    if len(external_chain_ids) / max(1, len(external_actions)) < 0.75:
        return False

    task_events = [
        event
        for event in latest.values()
        if event.get("source") == "jira-redwood"
        and (event.get("payload") or {}).get("fields", {}).get("issuetype", {}).get("name") == "Task"
        and "-TKT-" not in str(event.get("external_id", ""))
        and not str(event.get("external_id", "")).startswith(("INC", "external-action-"))
    ]
    descriptions = [
        _quality_text((event.get("payload") or {}).get("fields", {}).get("description"))
        for event in task_events
    ]
    if descriptions and len(set(descriptions)) / len(descriptions) < 0.75:
        return False
    commented = sum(
        bool(((event.get("payload") or {}).get("fields", {}).get("comment") or {}).get("comments"))
        for event in task_events
    )
    if task_events and commented / len(task_events) < 0.8:
        return False

    implicit_quotes = {
        str(link.get("evidence_quote"))
        for link in links
        if not link.get("explicit") and link.get("evidence_quote")
    }
    if len(implicit_quotes) < 8:
        return False

    cancelled = {
        reference
        for event in merge_requests
        if (event.get("payload") or {}).get("state") == "closed"
        for reference in [str(event.get("entity_id")), *map(str, event.get("aliases", []))]
    }
    cancelled_cases = sum(
        any(str(item.get("entity_id")) in cancelled for item in case.get("evidence", []))
        and any(fact.get("key") == "close_reason" for fact in case.get("answer_facts", []))
        for case in cases
    )
    return 10 <= cancelled_cases <= 15


def _audit_metrics(
    root: Path,
    events: list[dict[str, Any]],
    links: list[dict[str, Any]],
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = _latest_upserts(events)
    tasks = [
        event
        for event in latest.values()
        if event.get("source") == "jira-redwood"
        and (event.get("payload") or {}).get("fields", {}).get("issuetype", {}).get("name") == "Task"
    ]
    task_signatures = {
        _semantic_signature((event.get("payload") or {}).get("fields", {}).get("description"))
        for event in tasks
    }
    task_signature_counts = Counter(
        _semantic_signature((event.get("payload") or {}).get("fields", {}).get("description"))
        for event in tasks
    )
    mrs = [
        event
        for event in latest.values()
        if event.get("source") == "gitlab-redwood"
        and event.get("object_type") == "merge_request"
        and str(event.get("external_id", "")).startswith("dsid_")
    ]
    changed_file_sizes = [len((event.get("payload") or {}).get("changed_files") or []) for event in mrs]
    by_object_type: dict[str, dict[str, float | int]] = {}
    for object_type in sorted({str(event.get("object_type")) for event in latest.values()}):
        times = [
            _event_time(event) for event in latest.values() if str(event.get("object_type")) == object_type
        ]
        values = [value for value in times if value is not None]
        if not values:
            continue
        by_object_type[object_type] = {
            "count": len(values),
            "weekend_ratio": round(sum(value.weekday() >= 5 for value in values) / len(values), 6),
            "off_hours_ratio": round(sum(not 8 <= value.hour < 18 for value in values) / len(values), 6),
        }
    return {
        "event_count": len(events),
        "latest_entity_count": len(latest),
        "link_count": len(links),
        "case_count": len(cases),
        "normalized_task_metric": round(len(task_signatures) / len(tasks), 6) if tasks else 0.0,
        "task_description_count": len(tasks),
        "task_description_unique_count": len(task_signatures),
        "task_description_dominant_skeleton_ratio": round(
            max(task_signature_counts.values(), default=0) / len(tasks), 6
        )
        if tasks
        else 0.0,
        "merge_request_count": len(mrs),
        "changed_files": {
            "min": min(changed_file_sizes) if changed_file_sizes else 0,
            "max": max(changed_file_sizes) if changed_file_sizes else 0,
            "tail_ge_3_ratio": round(
                sum(size >= 3 for size in changed_file_sizes) / len(changed_file_sizes), 6
            )
            if changed_file_sizes
            else 0.0,
        },
        "by_object_type": by_object_type,
        "external_signal_ids": len(_external_signal_ids(events)),
        "production_threshold": PRODUCTION_EVENT_THRESHOLD,
        "manifest_consulted": False,
        "root": str(root),
    }


def audit_delivery(root: Path) -> dict[str, object]:
    """Run the canonical raw-evidence audit without trusting manifest acceptance fields."""
    events = _read_gzip_jsonl(root / "fixtures" / "timeline.jsonl.gz")
    cases = _read_jsonl(root / "cases.jsonl")
    links = _read_jsonl(root / "gold_links.jsonl")
    sources = _loads_yaml(root / "config" / "sources.yaml")
    directory = _loads_yaml(root / "config" / "people.yaml")
    financial_reconciliation = _has_financial_histories(events) and _has_dashboard(root, events)
    semantic_diversity = _has_semantic_diversity(events)
    mr_diversity = _has_mr_diversity(events)
    changed_file_distribution = _has_changed_file_distribution(events)
    routine_rhythm = _has_routine_rhythm(events)
    sample_connectivity = _has_connected_sample(root, links, events)
    numeric_recomputation = _has_numeric_case_arithmetic(cases, events)
    contract_audit = _provider_contract_audit(events, links, cases, directory)
    contract_checks = dict(contract_audit["checks"])
    baseline_contract_ok = False
    if THIRD_DELIVERY_SNAPSHOT.exists():
        try:
            baseline = json.loads(THIRD_DELIVERY_SNAPSHOT.read_text(encoding="utf-8"))
            baseline_contract_ok = (
                baseline.get("artifact", {}).get("found_read_only") is True
                and baseline.get("schema", {}).get("event", {}).get("schema_version") == "plancy-event/2"
                and baseline.get("baseline_observations", {}).get("cases") == 100
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            baseline_contract_ok = False
    checks: dict[str, bool] = {
        "required_files": all(
            (root / relative).exists()
            for relative in (
                "config/sources.yaml",
                "config/people.yaml",
                "fixtures/timeline.jsonl.gz",
                "cases.jsonl",
                "gold_links.jsonl",
                "dashboard.json",
                "manifest.json",
            )
        ),
        "source_configs": _has_source_configs(sources, events),
        "materialized_data": _has_materialized_data(root, events),
        "source_native_payloads": (
            bool(events)
            and len(events) >= len(_read_jsonl(root.parent / "documents.jsonl"))
            and all(_has_native_payload(event) for event in events)
        ),
        "version_history": _has_version_history(events),
        "access_controls": _has_access_controls(sources, events),
        "directory": _has_directory(directory),
        "directory_source_identities": _has_directory_source_identities(events, directory),
        "single_calendar": _has_single_calendar(events),
        "semantic_links": _has_semantic_links(links, events),
        "email_chains": len(events) <= 1000 or _has_email_chains(events, links),
        "financial_histories": financial_reconciliation,
        "financial_reconciliation": financial_reconciliation,
        "causal_episodes": _has_causal_episodes(events, links),
        "enterprise_chronology": _has_enterprise_chronology(events),
        "ordinary_chronology": _has_ordinary_chronology(events),
        "causal_edge_direction": _has_causal_edge_direction(events, links),
        "cancelled_mr_downstream": _has_cancelled_mr_downstream(events, links),
        "external_context": _has_external_context(events, links),
        "external_denominator": _has_external_denominator(root, events),
        "external_outcomes": _has_external_outcome_diversity(events),
        "mutations": _has_mutations(events),
        "mutation_deletion_counts": _has_mutation_deletion_counts(events),
        "operational_rhythm": _has_operational_rhythm(events),
        "routine_rhythm": routine_rhythm,
        "weekend_off_hours_by_object_type": routine_rhythm,
        "source_time_semantics": _has_source_time_semantics(events),
        "email_source_semantics": _has_email_source_semantics(events),
        "mr_source_semantics": _has_mr_source_semantics(events),
        "ordinary_work_diversity": _has_ordinary_work_diversity(events),
        "ordinary_decision_coverage": _has_ordinary_decision_coverage(events),
        "quality_coverage": _has_quality_coverage(events, links, cases),
        "causal_quality": _has_causal_quality(events, links, cases),
        "chain_temporal_diversity": _has_chain_temporal_diversity(events),
        "external_denominator_reconciliation": _has_external_denominator(root, events),
        "external_outcome_diversity": _has_external_outcome_diversity(events),
        "semantic_diversity": semantic_diversity,
        "normalized_task_metric": _has_normalized_task_metric(events),
        "project_decision_diversity": _has_project_decision_diversity(events),
        "comment_meeting_subject_body_diversity": semantic_diversity,
        "merge_request_diversity": mr_diversity,
        "mr_distributions": mr_diversity and changed_file_distribution,
        "changed_files_tail": changed_file_distribution,
        "true_implicit_links": _has_true_implicit_links(events, links),
        "case_quality": _has_case_quality(cases, events),
        "numeric_case_arithmetic": numeric_recomputation,
        "numeric_recomputation": numeric_recomputation,
        "atomic_answer_facts": _has_atomic_answer_facts(cases, events),
        "evidence_backed_case_families": _has_evidence_backed_case_families(cases, events),
        "case_family_account_principal": _has_case_family_account_principal(cases, events),
        "acl_case_semantics": _has_acl_case_semantics(cases, events),
        "acl_recomputation": _has_acl_recomputation(cases, events, directory, sources),
        "case_question_diversity": _has_case_question_diversity(cases, events),
        "project_dag_shape": _has_project_dag_shape(events, links),
        "project_dag": _has_project_dag_shape(events, links),
        "evaluation_cases": _has_contract_cases(cases, events, links),
        "provider_adapters": _has_executable_adapters(root, events),
        "dashboard": _has_dashboard(root, events),
        "sample": sample_connectivity,
        "sample_connectivity": sample_connectivity,
        "deterministic_ordering": _has_deterministic_ordering(events, links, cases),
        "third_delivery_contract": baseline_contract_ok,
        "provider_contract_compatibility": all(contract_checks.values()),
    }
    checks.update(contract_checks)
    failed = sorted(name for name, passed in checks.items() if not passed)
    metrics = _audit_metrics(root, events, links, cases)
    metrics.update(contract_audit["metrics"])
    metrics["third_delivery_contract"] = {
        "snapshot_path": str(THIRD_DELIVERY_SNAPSHOT),
        "found_read_only": baseline_contract_ok,
    }
    return {
        "accepted": not failed,
        "checks": checks,
        "failed": failed,
        "quality_coverage": _coverage_breakdown(events),
        "metrics": metrics,
    }


def validate_delivery(root: Path) -> dict[str, object]:
    """Return the canonical raw-evidence acceptance report for one delivery."""
    return audit_delivery(root)


def require_accepted_delivery(report: dict[str, object]) -> None:
    """Refuse a requested accepted delivery when computed checks are not green."""
    if report.get("accepted") is not True:
        failed = report.get("failed", [])
        raise ValueError(f"Plancy delivery acceptance failed: {', '.join(map(str, failed))}")
