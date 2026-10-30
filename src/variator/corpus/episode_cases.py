"""Evaluation cases grounded in emitted causal business episodes."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from variator.corpus.models import EvaluationCase, GoldLink, PlancyEvent

CASE_QUOTAS = {
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

_CANCELLATION_CASE_CATEGORIES = {
    "lookup",
    "history",
    "as_of",
    "conflict",
    "stale",
    "impact",
    "multi_hop",
    "implicit_ref",
    "people",
}

_CANCELLATION_CASE_SLOTS = {
    ("lookup", 0),
    ("lookup", 1),
    ("history", 0),
    ("history", 1),
    ("conflict", 0),
    ("stale", 0),
    ("impact", 0),
    ("multi_hop", 0),
    ("multi_hop", 1),
    ("implicit_ref", 0),
    ("people", 0),
    ("people", 1),
}

_WHY_QUESTION_FORMS = (
    "which customer support signal and release decision led to incident {external_id}",
    "how did the operational account escalation become the deployment behind incident {external_id}",
    "what external customer evidence connects the reported symptom to incident {external_id}",
    "which commercial commitment and mitigation decision explain the failure recorded as {external_id}",
    "how did the approved follow-up change cause the outcome in {external_id}",
    "what support context, task, and deployment preceded {external_id}",
    "which owner decision and merged change account for {external_id}",
    "how was the customer impact carried from intake to {external_id}",
    "what sequence of review and implementation produced {external_id}",
    "which source-native decision links the customer report with {external_id}",
    "how did the agreed work item become the incident observed in {external_id}",
)

_EXTERNAL_WHY_QUESTION_FORMS = {
    "external_competitive": (
        "which market signal and recorded decision explain {external_id}",
        "how did the competitive discussion become the outcome in {external_id}",
    ),
    "external_experiment": (
        "which community signal led to the experiment behind {external_id}",
        "what evidence connected the Reddit discussion with {external_id}",
    ),
    "external_customer_risk": (
        "how did the customer review become the account action recorded in {external_id}",
        "which review evidence explains the customer outcome {external_id}",
    ),
    "external_operational": (
        "which external signal and decision preceded the operational result {external_id}",
        "how did the reported issue lead to the remediation recorded in {external_id}",
    ),
}

_AS_OF_QUESTION_FORMS = {
    "task": (
        "what status did task {external_id} have before its next workflow revision",
        "which owner and status were recorded for task {external_id} at the cutoff",
    ),
    "deal": (
        "what sales stage was recorded for deal {external_id} before the next revision",
        "which forecast state did deal {external_id} have at the evaluation cutoff",
    ),
    "invoice": (
        "what invoice state and balance were valid for {external_id} before the next revision",
        "which amount remained due on {external_id} at the historical cutoff",
    ),
    "decision": (
        "what decision wording was valid before the revision of {external_id}",
        "which earlier resolution did the revised decision {external_id} replace",
    ),
}

_NUMERIC_QUESTION_FORMS = (
    "what billed, collected, and outstanding totals should be reported for deals {deal_a} and {deal_b}",
    "reconcile the invoice amounts, payments, and remaining balances for {deal_a} and {deal_b}",
    "how much was invoiced, paid, and still due across {deal_a} and {deal_b}",
    "what does the accounts-receivable record show for {deal_a} and {deal_b}",
    "compare the billed amount with collections and open balance for {deal_a} and {deal_b}",
    "which combined totals are supported by the deal and payment snapshots for {deal_a} and {deal_b}",
    "what amount was issued, collected, and left outstanding for {deal_a} and {deal_b}",
    "summarize the financial position across the two commercial records {deal_a} and {deal_b}",
    "what portfolio totals follow from the invoice histories for {deal_a} and {deal_b}",
    "which totals reconcile the customer billing records for {deal_a} and {deal_b}",
)

_ACL_QUESTION_FORMS = (
    "what support status may this principal see while the protected {kind} remains redacted",
    "can this principal retrieve the protected {kind}, or should its financial facts be not found",
    "which visible support facts can be returned without disclosing the restricted {kind}",
    "what should the requester receive when access to the protected {kind} is denied",
    "which customer-facing status is safe to show beside the restricted {kind}",
    "should the protected {kind} be redacted for this principal, and what public status remains",
    "what evidence is visible even though the protected {kind} is outside this principal's ACL",
    "which support result is allowed without exposing the commercial {kind}",
)

_IMPLICIT_REFERENCE_PROMPTS = (
    "which action did the meeting approve after reviewing the customer's reported issue",
    "what resolution followed the support escalation discussed in",
    "which mitigation was agreed after the client-impact update in",
    "what did the team decide after the account's operational signal in",
    "which follow-up did the meeting record after the service complaint in",
    "what engineering decision followed the customer-impact discussion in",
    "which next step was agreed after the support handoff in",
    "what action was recorded after the incident context was reviewed in",
    "which mitigation did the participants approve in response to the reported problem in",
    "what decision connected the customer context to the follow-up work in",
)

_CANCELLATION_QUESTION_FORMS = (
    "what final status and close reason were recorded for cancelled merge request {external_id}",
    "which reason explains the closure of merge request {external_id}",
    "when and why was merge request {external_id} cancelled",
    "what lifecycle outcome was recorded for merge request {external_id} after scope review",
    "which final state did cancelled merge request {external_id} reach",
    "what cancellation decision is documented for merge request {external_id}",
    "how was the work on merge request {external_id} concluded",
    "which status and date describe the cancelled change {external_id}",
    "what happened at the end of the review for merge request {external_id}",
    "what close reason and final date are recorded for {external_id}",
    "how does the source audit trail characterize cancelled merge request {external_id}",
    "which recorded outcome replaced the planned deployment for {external_id}",
    "what was the final disposition of merge request {external_id}",
    "which cancellation facts are attached to merge request {external_id}",
    "what state followed the scope review for merge request {external_id}",
    "why does the source record show merge request {external_id} as closed",
)


def _reference(event: PlancyEvent) -> str:
    return event.aliases[0] if event.aliases else event.entity_id


def _evidence(event: PlancyEvent, why: str) -> dict[str, Any]:
    return {"entity_id": _reference(event), "version": event.revision, "why": why}


def _canonical_evidence(event: PlancyEvent, why: str) -> dict[str, Any]:
    return {"entity_id": _reference(event), "version": event.revision, "why": why}


def _fact(key: str, value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        value = (
            value.get("displayName")
            or value.get("name")
            or value.get("value")
            or value.get("id")
            or value.get("status")
        )
    if isinstance(value, (list, tuple, set)):
        value = ", ".join(str(item) for item in value)
    text = " ".join(str(value).split())
    if len(text) > 120:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        text = next((sentence for sentence in sentences if len(sentence) <= 120), "recorded")
    return {"key": key, "any": [text]}


def _status(event: PlancyEvent) -> str:
    payload = event.payload or {}
    if event.source == "jira-redwood":
        return str(payload.get("fields", {}).get("status", {}).get("name") or "unknown")
    if event.source == "hubspot-redwood":
        return str(payload.get("properties", {}).get("status") or "unknown")
    return str(payload.get("state") or payload.get("status") or "recorded")


def _event_date(event: PlancyEvent) -> str:
    return event.world_at.date().isoformat()


def _cancellation_question(index: int, event: PlancyEvent) -> str:
    clause = _CANCELLATION_QUESTION_FORMS[index % len(_CANCELLATION_QUESTION_FORMS)]
    return _question(index, clause.format(external_id=event.external_id))


def _jira_owner(event: PlancyEvent) -> str:
    return str((event.payload or {}).get("fields", {}).get("assignee", {}).get("displayName") or "unassigned")


def _question(index: int, clause: str) -> str:
    leads = (
        "Using only the cited source records",
        "From the source-native audit trail",
        "At the stated evaluation cutoff",
        "After reconciling the linked systems",
        "Based on the authoritative snapshots",
        "Within the evidence visible to the assigned principal",
        "Using the recorded object history",
        "From the customer-facing record and history",
        "After checking the linked source snapshots",
        "Using the principal-scoped evidence trail",
        "From the accountable owner's source records",
        "After comparing the relevant revisions",
        "Using the records visible at cutoff",
        "From the linked operational snapshots",
        "After reviewing the accountable source trail",
        "Using the customer and system records",
        "According to the referenced source evidence",
        "Considering the linked operational context",
        "From the accountable business record",
        "After tracing the recorded ownership path",
        "Using the relevant source-native snapshots",
        "Based on the linked business evidence",
        "Within the recorded operational history",
        "After comparing the cited source records",
    )
    lead_index = (index + int(sha256(clause.encode()).hexdigest()[:4], 16)) % len(leads)
    suffix = "" if index < len(leads) else " without relying on superseded evidence"
    return f"{leads[lead_index]}, {clause.rstrip('?')}{suffix}?"


def _episode_account_id(episode_id: str, stages: dict[str, PlancyEvent]) -> str:
    for event in stages.values():
        payload = event.payload or {}
        references = payload.get("redwoodReferences")
        if isinstance(references, dict) and references.get("accountId"):
            return str(references["accountId"])
        properties = payload.get("properties")
        if isinstance(properties, dict) and properties.get("account_id"):
            return str(properties["account_id"])
    parts = episode_id.split("-")
    return next((part for part in parts if part.startswith("A")), episode_id)


def _spread_episode_ids(
    episode_ids: list[str], latest: dict[str, dict[str, PlancyEvent]], count: int
) -> list[str]:
    """Choose episodes round-robin by account so cases do not cluster on A001."""
    buckets: dict[str, list[str]] = defaultdict(list)
    for episode_id in sorted(episode_ids):
        buckets[_episode_account_id(episode_id, latest[episode_id])].append(episode_id)
    ordered_accounts = sorted(buckets)
    selected: list[str] = []
    cursor = 0
    while len(selected) < count and ordered_accounts:
        account = ordered_accounts[cursor % len(ordered_accounts)]
        position = cursor // len(ordered_accounts)
        if position < len(buckets[account]):
            selected.append(buckets[account][position])
        cursor += 1
        if cursor >= len(ordered_accounts) * (max(len(items) for items in buckets.values()) + 1):
            break
    return selected


def _external_why_records(
    events: list[PlancyEvent],
) -> list[tuple[str, dict[str, PlancyEvent]]]:
    """Return external chains whose source-native records contain a decision.

    The family is derived from provider, stage coverage, and terminal object
    types.  It is deliberately independent of the generated question text.
    """
    grouped: dict[str, dict[str, PlancyEvent]] = defaultdict(dict)
    for event in events:
        payload = event.payload or {}
        chain_id = payload.get("redwoodExternalChainId")
        stage = payload.get("redwoodExternalStage")
        if not chain_id or not stage or event.op != "upsert" or not event.payload:
            continue
        previous = grouped[str(chain_id)].get(str(stage))
        if previous is None or event.revision > previous.revision:
            grouped[str(chain_id)][str(stage)] = event

    records: list[tuple[str, dict[str, PlancyEvent]]] = []
    for stages in grouped.values():
        if "decision" not in stages or "action" not in stages:
            continue
        canonical_id = str((stages["action"].payload or {}).get("canonicalDocumentId") or "")
        if canonical_id:
            canonical_event = next(
                (event for event in events if event.external_id == canonical_id and event.aliases),
                None,
            )
            if canonical_event is not None:
                stages["source_document"] = canonical_event
        source = str((stages["action"].payload or {}).get("externalSource") or "")
        if "incident" in stages:
            family = "external_operational"
        elif source in {"cc_news", "market bulletin"}:
            family = "external_competitive"
        elif source in {"reddit", "community discussion"} and "merge_request" in stages:
            family = "external_experiment"
        elif source in {"amazon_reviews_2023", "customer review archive"} and "deal_action" in stages:
            family = "commercial"
        elif source in {"amazon_reviews_2023", "customer review archive"} and "task" in stages:
            family = "external_customer_risk"
        elif "incident" in stages:
            family = "external_operational"
        else:
            continue
        records.append((family, stages))
    return sorted(
        records,
        key=lambda item: (
            item[0],
            item[1]["action"].external_id,
        ),
    )


def _why_account_id(stages: dict[str, PlancyEvent]) -> str | None:
    for event in stages.values():
        payload = event.payload or {}
        references = payload.get("redwoodReferences")
        if isinstance(references, dict) and references.get("accountId"):
            return str(references["accountId"])
        properties = payload.get("properties")
        if isinstance(properties, dict) and properties.get("account_id"):
            return str(properties["account_id"])
    return None


def _decision_wording(event: PlancyEvent) -> str:
    payload = event.payload or {}
    for key in ("decision", "decision_text"):
        if payload.get(key):
            return str(payload[key])
    protocol = payload.get("protocol")
    if isinstance(protocol, dict) and protocol.get("decision"):
        return str(protocol["decision"])
    return str(payload.get("title") or payload.get("summary") or event.external_id)


def _properties(event: PlancyEvent) -> dict[str, Any]:
    properties = (event.payload or {}).get("properties", {})
    return properties if isinstance(properties, dict) else {}


def _status_or_value(event: PlancyEvent, key: str) -> Any:
    return _properties(event).get(key)


def _visible_status(event: PlancyEvent) -> str:
    status = _status(event)
    if status != "recorded":
        return status
    payload = event.payload or {}
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    return str(
        payload.get("summary")
        or payload.get("title")
        or payload.get("subject")
        or body.get("content")
        or status
    )


def _secret_facts(event: PlancyEvent) -> list[dict[str, Any]]:
    properties = (event.payload or {}).get("properties", {})
    if event.object_type == "deal":
        pairs = (
            ("amount", properties.get("amount")),
            ("discount", properties.get("discount")),
            ("forecast", properties.get("forecast_amount")),
        )
        return [_fact(key, value) for key, value in pairs if value is not None]
    if event.object_type == "quote":
        pairs = (("amount", properties.get("amount")), ("status", properties.get("status")))
        return [_fact(key, value) for key, value in pairs if value is not None]
    if event.object_type == "invoice":
        facts: list[dict[str, Any]] = []
        if properties.get("amount") is not None:
            facts.append(_fact("amount", f"USD {float(properties['amount']):,.0f}"))
        if properties.get("status") is not None:
            facts.append(_fact("status", properties["status"]))
        if properties.get("balance") is not None:
            facts.append(_fact("outstanding_amount", str(properties["balance"])))
        return facts
    if event.object_type == "payment":
        pairs = (("amount", properties.get("cumulative_amount")), ("status", properties.get("status")))
        return [_fact(key, value) for key, value in pairs if value is not None]
    fields = (event.payload or {}).get("fields", {})
    return [
        _fact("state", _status(event)),
        _fact("reason", fields.get("summary") or (event.payload or {}).get("summary") or event.external_id),
    ]


def _acl_membership_context(
    directory: dict[str, Any] | None, principal: str, group: str, known_at: Any
) -> list[dict[str, Any]]:
    """Copy the membership state used by an ACL case into its evidence context."""
    for person in (directory or {}).get("people", []):
        if not isinstance(person, dict) or str(person.get("principal")) != principal:
            continue
        for membership in person.get("memberships", []):
            if isinstance(membership, dict) and str(membership.get("group")) == group:
                return [
                    {
                        "principal": principal,
                        "group": group,
                        "valid_from": membership.get("valid_from"),
                        "valid_to": membership.get("valid_to"),
                        "cutoff": known_at.isoformat() if hasattr(known_at, "isoformat") else str(known_at),
                    }
                ]
    return [
        {
            "principal": principal,
            "group": group,
            "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_to": None,
            "cutoff": known_at.isoformat() if hasattr(known_at, "isoformat") else str(known_at),
        }
    ]


def _episode_groups(
    events: list[PlancyEvent],
) -> tuple[
    dict[str, dict[str, PlancyEvent]],
    dict[str, dict[str, list[PlancyEvent]]],
]:
    latest: dict[str, dict[str, PlancyEvent]] = defaultdict(dict)
    histories: dict[str, dict[str, list[PlancyEvent]]] = defaultdict(lambda: defaultdict(list))
    for event in events:
        payload = event.payload or {}
        episode_id = payload.get("redwoodEpisodeId") or payload.get("properties", {}).get(
            "redwood_episode_id"
        )
        stage = payload.get("redwoodStage")
        if not stage and event.source == "hubspot-redwood":
            stage = event.object_type
        if not episode_id or not stage:
            continue
        histories[str(episode_id)][str(stage)].append(event)
        previous = latest[str(episode_id)].get(str(stage))
        if previous is None or event.revision > previous.revision:
            latest[str(episode_id)][str(stage)] = event
    for episode_histories in histories.values():
        for stage, snapshots in episode_histories.items():
            episode_histories[stage] = sorted(snapshots, key=lambda event: event.revision)
    return latest, histories


def make_episode_cases(
    events: list[PlancyEvent],
    links: list[GoldLink],
    limit: int = 100,
    directory: dict[str, Any] | None = None,
) -> list[EvaluationCase]:
    """Build deterministic case quotas from complete causal episode records."""
    if limit < 100:
        return []
    latest, histories = _episode_groups(events)
    support_ids = sorted(
        episode_id
        for episode_id, stages in latest.items()
        if {
            "customer_email",
            "ticket",
            "slack",
            "meeting",
            "task",
            "merge_request",
            "deployment",
            "incident",
            "postmortem",
        }.issubset(stages)
    )
    commercial_ids = sorted(
        episode_id
        for episode_id, stages in latest.items()
        if {"deal", "quote", "invoice", "payment"}.issubset(stages)
    )
    if len(support_ids) < 6 or not commercial_ids:
        return []

    episode_link_pairs = {
        (link.source_entity_id, link.target_entity_id)
        for link in links
        if link.rule_id and link.rule_id.startswith("episode.")
    }
    if not episode_link_pairs:
        return []

    support_case_ids = _spread_episode_ids(support_ids, latest, max(CASE_QUOTAS.values()))
    commercial_case_ids = _spread_episode_ids(commercial_ids, latest, max(CASE_QUOTAS.values()))
    if len(support_case_ids) < 11 or len(commercial_case_ids) < 8:
        return []

    as_of_specs = (
        ("task", support_case_ids[0]),
        ("task", support_case_ids[1]),
        ("deal", commercial_case_ids[2]),
        ("deal", commercial_case_ids[3]),
        ("invoice", commercial_case_ids[4]),
        ("invoice", commercial_case_ids[5]),
        ("decision", support_case_ids[6]),
        ("decision", support_case_ids[7]),
    )

    enterprise_latest: dict[str, PlancyEvent] = {}
    enterprise_histories: dict[str, list[PlancyEvent]] = defaultdict(list)
    enterprise_by_external_id: dict[str, PlancyEvent] = {}
    for event in events:
        if event.op == "upsert" and event.payload:
            previous_external = enterprise_by_external_id.get(event.external_id)
            if previous_external is None or event.revision > previous_external.revision:
                enterprise_by_external_id[event.external_id] = event
        if (
            event.source in {"jira-redwood", "gitlab-redwood"}
            and event.external_id.startswith(("ER-", "dsid_"))
            and event.op == "upsert"
            and event.payload
        ):
            enterprise_histories[event.entity_id].append(event)
            previous = enterprise_latest.get(event.entity_id)
            if previous is None or event.revision > previous.revision:
                enterprise_latest[event.entity_id] = event
    for history in enterprise_histories.values():
        history.sort(key=lambda event: event.revision)
    enterprise_case_events = [
        event
        for event in enterprise_latest.values()
        if event.source == "jira-redwood" and event.external_id.startswith("ER-")
    ]
    cancelled_merge_requests = [
        event
        for event in enterprise_latest.values()
        if event.source == "gitlab-redwood"
        and event.external_id.startswith("dsid_")
        and (event.payload or {}).get("state") == "closed"
        and (event.payload or {}).get("close_reason")
    ]
    cancelled_merge_requests.sort(key=lambda event: event.external_id)

    numeric_records: list[tuple[str, PlancyEvent, PlancyEvent]] = []
    zero_balance_records: list[tuple[str, PlancyEvent, PlancyEvent]] = []
    nonzero_records: list[tuple[str, PlancyEvent, PlancyEvent]] = []
    partial_records: list[tuple[str, PlancyEvent, PlancyEvent]] = []
    overdue_records: list[tuple[str, PlancyEvent, PlancyEvent]] = []
    remaining_commercial_ids = [item for item in commercial_ids if item not in commercial_case_ids]
    for episode_id in commercial_case_ids + remaining_commercial_ids:
        invoice_history = histories[episode_id].get("invoice", [])
        payment_history = histories[episode_id].get("payment", [])
        if not invoice_history or not payment_history:
            continue
        payments_by_status = {str(_properties(payment).get("status")): payment for payment in payment_history}
        latest_payment = payment_history[-1]
        candidates = sorted(
            invoice_history,
            key=lambda event: (
                {"partial": 0, "overdue": 1, "issued": 2}.get(str(_properties(event).get("status")), 3),
                event.revision,
            ),
        )
        selected_invoice = next(
            (
                event
                for event in candidates
                if float(_properties(event).get("balance") or 0) > 0
                and str(_properties(event).get("status")) in {"partial", "overdue"}
            ),
            invoice_history[-1],
        )
        status = str(_properties(selected_invoice).get("status"))
        payment_status = (
            "partial"
            if status == "partial"
            else "pending"
            if status in {"overdue", "issued"}
            else "completed"
        )
        selected_payment = payments_by_status.get(payment_status, latest_payment)
        record = (episode_id, selected_invoice, selected_payment)
        numeric_records.append(record)
        if float(_properties(selected_invoice).get("balance") or 0) > 0:
            nonzero_records.append(record)
        if status == "partial":
            partial_records.append(record)
        if status == "overdue":
            overdue_records.append(record)
        paid_invoice = invoice_history[-1]
        if enterprise_case_events or str(_properties(paid_invoice).get("status")) != "voided":
            zero_balance_records.append((episode_id, paid_invoice, latest_payment))

    numeric_pair_records: list[
        tuple[
            tuple[str, PlancyEvent, PlancyEvent],
            tuple[str, PlancyEvent, PlancyEvent],
        ]
    ] = []
    usable_records = list(numeric_records)
    non_voided_records = [record for record in usable_records if _status(record[1]) != "voided"]
    family_records: dict[str, list[tuple[str, PlancyEvent, PlancyEvent]]] = defaultdict(list)
    for record in usable_records:
        family = str(_properties(latest[record[0]]["deal"]).get("financial_family") or "invoice_collections")
        family_records[family].append(record)
    preferred_records = (
        partial_records
        + overdue_records
        + [
            record
            for record in nonzero_records
            if record not in partial_records
            and record not in overdue_records
            and record in non_voided_records
        ]
    )
    preferred_records.extend(record for record in zero_balance_records if record in non_voided_records)
    if len(preferred_records) < 2:
        preferred_records = non_voided_records
    requested_families = (
        "invoice_collections",
        "credit_adjustment",
        "forecast_actual",
        "recurring_revenue",
        "margin_discount",
        "dso_ar",
        "supplier_costs",
        "voided_reissued",
        "invoice_collections",
        "forecast_actual",
    )
    for index, family in enumerate(requested_families):
        pool = family_records.get(family) or preferred_records
        if len(pool) < 1 or len(preferred_records) < 2:
            break
        first = pool[index % len(pool)]
        if len(pool) > 1:
            second = pool[(index + 1) % len(pool)]
        else:
            second = preferred_records[(index + 1) % len(preferred_records)]
        numeric_pair_records.append((first, second))
    if len(numeric_pair_records) != CASE_QUOTAS["numeric"]:
        return []

    external_why_records = _external_why_records(events)
    external_why_by_family: dict[str, list[dict[str, PlancyEvent]]] = defaultdict(list)
    for family, stages in external_why_records:
        external_why_by_family[family].append(stages)
    support_family_ids = [episode_id for episode_id in support_case_ids if "-SUP-" in episode_id]
    support_selections = [support_family_ids[index] for index in (0, 2, 4) if index < len(support_family_ids)]
    if len(support_selections) < 3:
        support_selections = support_case_ids[:3]
    why_specs: list[tuple[str, dict[str, PlancyEvent]]] = [
        ("support", latest[episode_id]) for episode_id in support_selections
    ]
    commercial_specs: list[dict[str, PlancyEvent]] = []
    support_by_account = {
        _episode_account_id(episode_id, latest[episode_id]): latest[episode_id] for episode_id in support_ids
    }
    for episode_id in commercial_case_ids:
        stages = dict(latest[episode_id])
        support_stages = support_by_account.get(_episode_account_id(episode_id, stages))
        if support_stages and {"meeting", "decision"}.issubset(support_stages):
            stages.update(
                {
                    "meeting": support_stages["meeting"],
                    "decision": support_stages["decision"],
                }
            )
        if "decision" in stages:
            commercial_specs.append(stages)
    why_specs.extend(("commercial", stages) for stages in commercial_specs[:2])
    enterprise_why_groups: dict[str, dict[str, PlancyEvent]] = defaultdict(dict)
    for event in events:
        payload = event.payload or {}
        chain_id = str(payload.get("redwoodCausalChainId") or "")
        stage = str(payload.get("redwoodStage") or "")
        if chain_id.startswith("enterprise-causal-batch-") and stage:
            enterprise_why_groups[chain_id][stage] = event
    selected_external_accounts: set[str] = set()
    for family, count in (
        ("external_competitive", 2),
        ("external_experiment", 1),
        ("external_operational", 1),
    ):
        candidates = external_why_by_family.get(family, [])
        ranked_candidates = sorted(
            candidates,
            key=lambda stages: (
                str(((stages.get("action").payload or {}).get("redwoodReferences") or {}).get("accountId"))
                in selected_external_accounts,
                str(((stages.get("action").payload or {}).get("redwoodReferences") or {}).get("accountId"))
                != "A004",
                str(stages.get("action").external_id),
            ),
        )
        for stages in ranked_candidates[:count]:
            why_specs.append((family, stages))
            account_id = str(
                ((stages.get("action").payload or {}).get("redwoodReferences") or {}).get("accountId")
            )
            if account_id:
                selected_external_accounts.add(account_id)
    used_why_accounts = {
        str(((stage.payload or {}).get("redwoodReferences") or {}).get("accountId"))
        for _family, stages in why_specs
        for stage in stages.values()
        if ((stage.payload or {}).get("redwoodReferences") or {}).get("accountId")
    }
    operational_candidates = [
        stages
        for stages in sorted(enterprise_why_groups.values(), key=lambda value: value["decision"].external_id)
        if {"decision", "task"}.issubset(stages)
    ]
    operational_candidates.sort(
        key=lambda stages: (
            str(((stages["decision"].payload or {}).get("redwoodReferences") or {}).get("accountId"))
            in used_why_accounts,
            stages["decision"].external_id,
        )
    )
    why_specs.extend(("operational", stages) for stages in operational_candidates[:2])
    if not enterprise_case_events and not external_why_records:
        why_specs = [("support", latest[episode_id]) for episode_id in support_case_ids]
    if len(why_specs) < CASE_QUOTAS["why"]:
        used_why_episode_ids = {
            str((event.payload or {}).get("redwoodEpisodeId"))
            for _family, stages in why_specs
            for event in stages.values()
            if (event.payload or {}).get("redwoodEpisodeId")
        }
        fallback_specs = [
            ("support", latest[episode_id])
            for episode_id in support_case_ids
            if episode_id not in used_why_episode_ids
        ]
        fallback_specs.extend(
            ("commercial", latest[episode_id])
            for episode_id in commercial_case_ids
            if episode_id not in used_why_episode_ids
        )
        why_specs.extend(fallback_specs)
    why_specs = why_specs[: CASE_QUOTAS["why"]]
    if len(why_specs) != CASE_QUOTAS["why"]:
        return []

    cases: list[EvaluationCase] = []
    cancellation_case_cursor = 0
    for category, quota in CASE_QUOTAS.items():
        for quota_index in range(quota):
            support_id = support_case_ids[quota_index % len(support_case_ids)]
            commercial_id = commercial_case_ids[quota_index % len(commercial_case_ids)]
            support = latest[support_id]
            support_history = histories[support_id]
            commercial_history = histories[commercial_id]
            support_principals = [rule.principal for rule in support["ticket"].acl.allow]
            principal = (
                support_principals[quota_index % len(support_principals)]
                if support_principals
                else "user:e001"
            )
            known_at = support["postmortem"].world_at
            evidence: list[dict[str, Any]]
            roots: list[dict[str, Any]] = []
            forbidden: list[dict[str, Any]] = []
            facts: list[dict[str, Any]]
            expect_not_found = False
            acl_scenario: str | None = None
            acl_context: list[dict[str, Any]] = []

            cancellation_event: PlancyEvent | None = None
            if (
                cancelled_merge_requests
                and category in _CANCELLATION_CASE_CATEGORIES
                and (category, quota_index) in _CANCELLATION_CASE_SLOTS
                and cancellation_case_cursor < len(cancelled_merge_requests)
            ):
                cancellation_event = cancelled_merge_requests[cancellation_case_cursor]
                cancellation_case_cursor += 1

            if cancellation_event is not None:
                cancellation_history = enterprise_histories[cancellation_event.entity_id]
                final_event = cancellation_history[-1]
                close_reason = (final_event.payload or {}).get("close_reason")
                closed_date = _event_date(final_event)

                if category == "history":
                    evidence = [
                        _canonical_evidence(snapshot, "full merge request lifecycle before cancellation")
                        for snapshot in cancellation_history
                    ]
                    facts = [
                        _fact("initial_status", _status(cancellation_history[0])),
                        _fact("final_status", _status(final_event)),
                        _fact("close_reason", close_reason),
                        _fact("closed_date", closed_date),
                    ]
                    question = _cancellation_question(cancellation_case_cursor - 1, final_event)
                elif category == "as_of":
                    previous_event = cancellation_history[-2]
                    evidence = [
                        _canonical_evidence(previous_event, "snapshot before cancellation was recorded"),
                        _canonical_evidence(final_event, "final cancelled snapshot"),
                    ]
                    known_at = (
                        previous_event.observed_at + (final_event.world_at - previous_event.observed_at) / 2
                    )
                    facts = [
                        _fact("old_status", _status(previous_event)),
                        _fact("final_status", _status(final_event)),
                        _fact("close_reason", close_reason),
                        _fact("closed_date", closed_date),
                    ]
                    question = _cancellation_question(cancellation_case_cursor - 1, final_event)
                elif category == "stale":
                    previous_event = cancellation_history[-2]
                    evidence = [
                        _canonical_evidence(previous_event, "superseded pre-cancellation snapshot"),
                        _canonical_evidence(final_event, "current cancelled snapshot"),
                    ]
                    facts = [
                        _fact("stale_status", _status(previous_event)),
                        _fact("current_status", _status(final_event)),
                        _fact("close_reason", close_reason),
                        _fact("closed_date", closed_date),
                    ]
                    known_at = previous_event.observed_at
                    question = _cancellation_question(cancellation_case_cursor - 1, final_event)
                elif category == "multi_hop":
                    payload = final_event.payload or {}
                    references = dict(payload.get("redwoodCausalReferences") or {})
                    references.update(payload.get("redwoodEnterpriseReferences") or {})
                    related = [
                        enterprise_by_external_id.get(str(references.get(key)))
                        for key in (
                            "emailId",
                            "ticketId",
                            "meetingId",
                            "decisionId",
                            "taskId",
                            "epicId",
                            "projectId",
                        )
                    ]
                    related_events = []
                    seen_related: set[str] = set()
                    for event in related:
                        if event is None or event.entity_id in seen_related:
                            continue
                        seen_related.add(event.entity_id)
                        related_events.append(event)
                    evidence = [
                        _canonical_evidence(item, "source-native cancellation context")
                        for item in (*related_events, final_event)
                    ]
                    facts = [
                        _fact("status", _status(final_event)),
                        _fact("close_reason", close_reason),
                        _fact("closed_date", closed_date),
                    ]
                    question = _cancellation_question(cancellation_case_cursor - 1, final_event)
                else:
                    evidence = [_canonical_evidence(final_event, "closed merge request snapshot")]
                    facts = [
                        _fact("status", _status(final_event)),
                        _fact("close_reason", close_reason),
                        _fact("closed_date", closed_date),
                    ]
                    question = _cancellation_question(cancellation_case_cursor - 1, final_event)
            elif category == "lookup":
                event = (
                    enterprise_case_events[quota_index % len(enterprise_case_events)]
                    if enterprise_case_events
                    else support["ticket"]
                )
                evidence = [_evidence(event, "latest customer support ticket snapshot")]
                facts = [
                    _fact("status", _status(event)),
                    _fact("owner", _jira_owner(event)),
                    _fact("status_date", _event_date(event)),
                ]
                question = _question(
                    quota_index, f"what final status is recorded for ticket {event.external_id}"
                )
            elif category == "why":
                why_family, why_stages = why_specs[quota_index]
                stage_order = (
                    "incident",
                    "deal_action",
                    "deployment",
                    "merge_request",
                    "payment",
                    "invoice",
                    "contract",
                    "quote",
                    "deal",
                    "task",
                    "decision",
                    "meeting",
                    "slack",
                    "ticket",
                    "customer_email",
                    "action",
                )
                chain = tuple(why_stages[stage] for stage in stage_order if stage in why_stages)
                evidence = [_evidence(item, "source-visible causal path") for item in chain]
                root_limit = 3 if why_family == "commercial" else 5
                roots = [
                    {"entity_id": _reference(item), "why": f"causal {item.object_type}"}
                    for item in chain[1 : root_limit + 1]
                ]
                if why_family.startswith("external") and len(roots) < 2:
                    source_document = why_stages.get("source_document")
                    if source_document is not None:
                        roots.append(
                            {
                                "entity_id": _reference(source_document),
                                "why": "external source document",
                            }
                        )
                terminal = chain[0]
                incident_fields = (terminal.payload or {}).get("fields", {})
                task = why_stages.get("task")
                decision = why_stages.get("decision")
                task_fields = (task.payload or {}).get("fields", {}) if task else {}
                task_summary = str(
                    task_fields.get("summary")
                    or ((task.payload or {}).get("summary") if task else None)
                    or terminal.external_id
                )
                decision_text = _decision_wording(decision) if decision else task_summary
                owner = (
                    _jira_owner(task)
                    if task and task.source == "jira-redwood"
                    else str(
                        (decision.payload or {}).get("protocol", {}).get("owner")
                        or (decision.payload or {}).get("authorId")
                        if decision
                        else None
                    )
                    or str((decision.payload or {}).get("author") if decision else None)
                    or str(
                        _properties(terminal).get("account_id")
                        or (terminal.payload or {}).get("account_id")
                        or "recorded owner"
                    )
                )
                status_value = _status(terminal)
                if terminal.object_type == "page" and terminal.payload:
                    terminal_stage = str(
                        (terminal.payload or {}).get("redwoodStage")
                        or (terminal.payload or {}).get("redwoodExternalStage")
                        or ""
                    )
                    status_value = (
                        "postmortem"
                        if terminal_stage == "postmortem"
                        or str(terminal.external_id).startswith(("PM-", "EXT-PM-", "ENT-BATCH-PM-"))
                        else "decision"
                    )
                facts = [
                    _fact(
                        "reason",
                        incident_fields.get("summary")
                        or (terminal.payload or {}).get("summary")
                        or terminal.external_id,
                    ),
                    _fact("status", status_value),
                    _fact("owner", owner),
                    _fact("decision", decision_text),
                    _fact("work_item", task_summary),
                    _fact("incident_date", _event_date(terminal)),
                ]
                forms = _EXTERNAL_WHY_QUESTION_FORMS.get(why_family)
                clause = (
                    forms[quota_index % len(forms)].format(external_id=terminal.external_id)
                    if forms
                    else _WHY_QUESTION_FORMS[quota_index].format(external_id=terminal.external_id)
                )
                question = _question(
                    quota_index,
                    clause,
                )
            elif category == "history":
                snapshots = support_history["task"]
                evidence = [_evidence(event, "full task status history") for event in snapshots]
                facts = [
                    _fact("initial_status", _status(snapshots[0])),
                    _fact("final_status", _status(snapshots[-1])),
                    _fact("owner", _jira_owner(snapshots[-1])),
                    _fact("completion_date", _event_date(snapshots[-1])),
                ]
                question = _question(
                    quota_index,
                    f"how did task {snapshots[-1].external_id} move from intake to completion",
                )
            elif category == "as_of":
                as_of_stage, as_of_episode_id = as_of_specs[quota_index]
                snapshots = histories[as_of_episode_id][as_of_stage]
                event, next_event = snapshots[0], snapshots[1]
                if as_of_stage in {"task", "deal", "invoice"} and len(snapshots) >= 3:
                    event, next_event = snapshots[1], snapshots[2]
                evidence = [
                    _evidence(event, f"old {as_of_stage} snapshot valid before the revision"),
                    _evidence(next_event, f"new {as_of_stage} snapshot that follows the cutoff"),
                ]
                known_at = event.observed_at + (next_event.world_at - event.observed_at) / 2
                if as_of_stage == "task":
                    facts = [
                        _fact("old_status", _status(event)),
                        _fact("new_status", _status(next_event)),
                        _fact("owner", _jira_owner(event)),
                        _fact("as_of_date", _event_date(event)),
                    ]
                elif as_of_stage == "deal":
                    facts = [
                        _fact("old_stage", _status(event)),
                        _fact("new_stage", _status(next_event)),
                        _fact("old_forecast", _status_or_value(event, "forecast_amount")),
                        _fact("as_of_date", _event_date(event)),
                    ]
                elif as_of_stage == "invoice":
                    facts = [
                        _fact("old_status", _status(event)),
                        _fact("new_status", _status(next_event)),
                        _fact("old_balance", _status_or_value(event, "balance")),
                        _fact("new_balance", _status_or_value(next_event, "balance")),
                        _fact("as_of_date", _event_date(event)),
                    ]
                else:
                    facts = [
                        _fact("old_decision", _decision_wording(event)),
                        _fact("new_decision", _decision_wording(next_event)),
                        _fact("decision_date", _event_date(event)),
                    ]
                question = _question(
                    quota_index,
                    _AS_OF_QUESTION_FORMS[as_of_stage][quota_index % 2].format(external_id=event.external_id),
                )
            elif category == "conflict":
                ticket, task = support["ticket"], support["task"]
                evidence = [
                    _evidence(ticket, "customer-facing record"),
                    _evidence(task, "engineering source of truth"),
                ]
                facts = [
                    _fact("ticket_status", _status(ticket)),
                    _fact("task_status", _status(task)),
                    _fact("owner", _jira_owner(task)),
                ]
                question = _question(
                    quota_index,
                    f"which status should be trusted when ticket {ticket.external_id} "
                    f"and task {task.external_id} disagree",
                )
            elif category == "stale":
                event = commercial_history["quote"][1]
                current = commercial_history["quote"][-1]
                evidence = [
                    _evidence(event, "superseded proposal snapshot"),
                    _evidence(current, "current proposal snapshot"),
                ]
                facts = [
                    _fact("stale_status", _status(event)),
                    _fact("current_status", _status(current)),
                    _fact("revision_date", _event_date(current)),
                ]
                known_at = event.observed_at
                question = _question(
                    quota_index, f"which proposal state for {event.external_id} was later superseded"
                )
            elif category == "impact":
                incident, deployment = support["incident"], support["deployment"]
                evidence = [
                    _evidence(incident, "incident names affected deployment"),
                    _evidence(deployment, "deployment names merged change"),
                ]
                facts = [
                    _fact("deployment", deployment.external_id),
                    _fact("reason", (incident.payload or {}).get("fields", {}).get("summary")),
                    _fact("status", _status(incident)),
                    _fact("incident_date", _event_date(incident)),
                ]
                question = _question(
                    quota_index, f"which deployment triggered incident {incident.external_id}"
                )
            elif category == "multi_hop":
                selected = (support["ticket"], support["meeting"], support["merge_request"])
                evidence = [_evidence(event, "cross-provider causal hop") for event in selected]
                facts = [
                    _fact("status", _status(support["ticket"])),
                    _fact("owner", _jira_owner(support["ticket"])),
                    _fact("decision", (support["meeting"].payload or {}).get("protocol", {}).get("decision")),
                ]
                question = _question(
                    quota_index,
                    f"how did ticket {support['ticket'].external_id} become merged change "
                    f"{support['merge_request'].external_id}",
                )
            elif category == "acl":
                latest_by_object: dict[tuple[str, str], PlancyEvent] = {}
                for candidate in events:
                    key = (candidate.object_type, candidate.entity_id)
                    previous = latest_by_object.get(key)
                    if previous is None or candidate.revision > previous.revision:
                        latest_by_object[key] = candidate
                invoices = sorted(
                    (event for event in latest_by_object.values() if event.object_type == "invoice"),
                    key=lambda event: event.world_at,
                )
                deals = sorted(
                    (event for event in latest_by_object.values() if event.object_type == "deal"),
                    key=lambda event: event.world_at,
                )
                scenarios = (
                    ("finance_full", "invoice", "user:lucas-young", "grp:finance"),
                    ("sales_margin_redacted", "deal", "user:noah-kim", "grp:sales"),
                    ("engineering_commercial_redacted", "deal", "user:jordan-patel", "grp:all-staff"),
                    ("unrelated_not_found", "support_issue", "user:unrelated-evaluator", "grp:engineering"),
                    ("manager_inherited", "invoice", "user:maya-chen", "grp:management"),
                    ("membership_change", "engineering_task", "user:e004", "grp:engineering"),
                    ("historical_as_of", "engineering_task", "user:e004", "grp:engineering"),
                    ("finance_secret_denied", "invoice", "user:e007", "grp:sales"),
                )
                acl_scenario, object_kind, principal, group = scenarios[quota_index]
                if object_kind == "deal":
                    candidates = deals
                elif object_kind == "support_issue":
                    candidates = sorted(
                        (
                            event
                            for event in latest_by_object.values()
                            if event.object_type == "issue" and event.container == "SUPPORT"
                        ),
                        key=lambda event: event.world_at,
                    )
                elif object_kind == "engineering_task":
                    candidates = sorted(
                        (
                            event
                            for event in latest_by_object.values()
                            if event.object_type == "issue" and event.container == "ENG"
                        ),
                        key=lambda event: event.world_at,
                    )
                else:
                    candidates = invoices
                if not candidates:
                    return []
                if acl_scenario == "membership_change":
                    private_event = next(
                        (event for event in reversed(candidates) if event.world_at.month >= 4), candidates[-1]
                    )
                    known_at = private_event.world_at
                elif acl_scenario == "historical_as_of":
                    cutoff_start = datetime(2026, 1, 5, tzinfo=UTC)
                    cutoff_end = datetime(2026, 4, 5, tzinfo=UTC)
                    historical_candidates = [
                        event for event in candidates if cutoff_start <= event.world_at < cutoff_end
                    ]
                    private_event = historical_candidates[0] if historical_candidates else candidates[0]
                    known_at = private_event.world_at + timedelta(hours=1)
                elif acl_scenario == "manager_inherited":
                    private_event = next(
                        (
                            event
                            for event in candidates
                            if event.world_at >= datetime(2026, 1, 5, tzinfo=UTC)
                            if event.acl
                            and any(rule.principal == "grp:management" for rule in event.acl.allow)
                        ),
                        candidates[0],
                    )
                    known_at = private_event.world_at
                else:
                    private_event = candidates[(quota_index * 7) % len(candidates)]
                    known_at = private_event.world_at
                evidence = [_evidence(private_event, "protected object and source-native ACL context")]
                acl_context = _acl_membership_context(directory, principal, group, known_at)
                if acl_scenario in {
                    "sales_margin_redacted",
                    "engineering_commercial_redacted",
                    "unrelated_not_found",
                    "membership_change",
                    "finance_secret_denied",
                }:
                    forbidden = [
                        {
                            "entity_id": _reference(private_event),
                            "facts": _secret_facts(private_event),
                        }
                    ]
                if acl_scenario in {"finance_full", "manager_inherited"}:
                    facts = _secret_facts(private_event)
                else:
                    properties = _properties(private_event)
                    facts = [
                        _fact(
                            "status",
                            _properties(private_event).get("status")
                            or _properties(private_event).get("stage")
                            or _status(private_event),
                        ),
                        _fact(
                            "account" if properties.get("account_id") else "reason",
                            properties.get("account_id")
                            or (private_event.payload or {}).get("fields", {}).get("summary")
                            or (private_event.payload or {}).get("summary")
                            or private_event.external_id,
                        ),
                        _fact("status_date", _event_date(private_event)),
                    ]
                question = _question(
                    quota_index,
                    (
                        f"for {private_event.object_type} {private_event.external_id}, "
                        f"what access result applies to principal {principal} "
                        f"through {group} membership"
                    ),
                )
                expect_not_found = acl_scenario in {
                    "unrelated_not_found",
                    "membership_change",
                    "finance_secret_denied",
                }
            elif category == "negative":
                evidence = []
                facts = []
                expect_not_found = True
                question = _question(
                    quota_index,
                    f"was a second undocumented outage recorded after incident "
                    f"{support['incident'].external_id}",
                )
            elif category == "implicit_ref":
                event = support["meeting"]
                evidence = [_evidence(event, "meeting transcript records the contextual reference")]
                protocol = (event.payload or {}).get("protocol", {})
                facts = [
                    _fact(
                        "implicit_decision",
                        protocol.get("decision"),
                    ),
                    _fact("owner", protocol.get("owner")),
                    _fact("decision_date", _event_date(event)),
                ]
                question = _question(
                    quota_index,
                    f"{_IMPLICIT_REFERENCE_PROMPTS[quota_index % len(_IMPLICIT_REFERENCE_PROMPTS)]} "
                    f"meeting {event.external_id}",
                )
            elif category == "numeric":
                first_record, second_record = numeric_pair_records[quota_index]
                first_id, invoice, payment = first_record
                second_id, other_invoice, other_payment = second_record
                deal = latest[first_id]["deal"]
                other_deal = latest[second_id]["deal"]
                evidence = [
                    _evidence(deal, "first deal in the portfolio calculation"),
                    _evidence(invoice, "first final invoice balance"),
                    _evidence(payment, "first final cumulative payment"),
                    _evidence(other_deal, "second deal in the portfolio calculation"),
                    _evidence(other_invoice, "second final invoice balance"),
                    _evidence(other_payment, "second final cumulative payment"),
                ]
                invoices = (invoice, other_invoice)
                payments = (payment, other_payment)
                invoice_total = sum(
                    float(_properties(item).get("amount") or 0)
                    for item in invoices
                    if str(_properties(item).get("status")) != "voided"
                )
                paid_total = sum(
                    float(
                        _properties(item).get("applied_amount")
                        or _properties(item).get("cumulative_amount")
                        or 0
                    )
                    for item in payments
                    if str(_properties(item).get("application_status")) == "completed"
                )
                credit_note_total = sum(
                    float(_properties(item).get("applicable_credit_note_amount") or 0)
                    for item in invoices
                    if str(_properties(item).get("status")) != "voided"
                )
                outstanding_total = round(invoice_total - paid_total - credit_note_total, 2)
                family = str(_properties(deal).get("financial_family") or "invoice_collections")
                facts = [
                    _fact("invoice_total", invoice_total),
                    _fact("paid_total", paid_total),
                    _fact("credit_note_total", credit_note_total),
                    _fact("outstanding_total", outstanding_total),
                    _fact("financial_family", family),
                    _fact("invoice_1_status", _status(invoice)),
                    _fact("invoice_2_status", _status(other_invoice)),
                    _fact("payment_1_status", _status(payment)),
                    _fact("payment_2_status", _status(other_payment)),
                ]
                family_properties = _properties(invoice)
                if family in {"forecast_actual", "invoice_collections"}:
                    facts.extend(
                        [
                            _fact("forecast_total", _properties(deal).get("forecast_amount")),
                            _fact("actual_invoice_total", _properties(invoice).get("amount")),
                        ]
                    )
                elif family == "recurring_revenue":
                    facts.extend(
                        [
                            _fact("arr", _properties(deal).get("arr")),
                            _fact("mrr", _properties(deal).get("mrr")),
                        ]
                    )
                elif family == "margin_discount":
                    facts.extend(
                        [
                            _fact("discount", _properties(deal).get("discount")),
                            _fact("margin", _properties(deal).get("margin")),
                        ]
                    )
                elif family == "dso_ar":
                    facts.extend(
                        [
                            _fact("dso_days", family_properties.get("days_to_collect")),
                            _fact("accounts_receivable", family_properties.get("balance")),
                        ]
                    )
                elif family == "supplier_costs":
                    facts.extend(
                        [
                            _fact("supplier_cost", family_properties.get("supplier_cost_amount")),
                            _fact("operating_cost_actual", family_properties.get("operating_cost_actual")),
                        ]
                    )
                elif family == "credit_adjustment":
                    facts.extend(
                        [
                            _fact("credit_note_id", family_properties.get("credit_note_id")),
                            _fact("credit_note_status", family_properties.get("credit_note_status")),
                        ]
                    )
                elif family == "voided_reissued":
                    facts.extend(
                        [
                            _fact("reissued_invoice_id", family_properties.get("reissued_invoice_id")),
                            _fact("invoice_lifecycle", "voided then reissued"),
                        ]
                    )
                known_at = max(payment.world_at, other_payment.world_at)
                question = _question(
                    quota_index,
                    _NUMERIC_QUESTION_FORMS[quota_index].format(
                        deal_a=deal.external_id,
                        deal_b=other_deal.external_id,
                    ),
                )
            else:
                event = (
                    enterprise_case_events[quota_index % len(enterprise_case_events)]
                    if category == "people" and enterprise_case_events
                    else support["ticket"]
                )
                owner = _jira_owner(event)
                evidence = [_evidence(event, "ticket assignee is source-native")]
                facts = [
                    _fact("owner", owner),
                    _fact("status", _status(event)),
                    _fact("assignment_date", _event_date(event)),
                ]
                question = _question(quota_index, f"who owns ticket {event.external_id}")

            cases.append(
                EvaluationCase(
                    id=f"R{len(cases) + 1:03d}",
                    split="dev" if len(cases) < 70 else "holdout",
                    category=category,
                    difficulty="tricky" if category in {"why", "multi_hop", "acl"} else "trivial",
                    language="en",
                    question=question,
                    principal=principal,
                    known_at=known_at,
                    evidence=evidence,
                    roots=roots,
                    forbidden=forbidden,
                    answer_facts=facts,
                    acl_scenario=acl_scenario,
                    acl_context=acl_context,
                    expect_not_found=expect_not_found,
                    rationale=(
                        f"Derived from source-visible records in {support_id}"
                        if category != "numeric"
                        else f"Reconciles emitted financial snapshots in {commercial_id}"
                    ),
                )
            )
    return cases[:limit]
