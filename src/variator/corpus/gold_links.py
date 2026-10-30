"""Emit only source-expressed semantic links from the operational chain."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from variator.corpus.canonical import immutable_id
from variator.corpus.models import GoldLink, PlancyEvent


def _entity(source: str, object_type: str, external_id: object) -> str:
    return immutable_id("redwood", source, object_type, str(external_id))


def _row_entity(table: str, row: dict[str, Any], index: int) -> str:
    special = {
        "tasks": ("jira-redwood", "issue", row.get("task_id") or row.get("id")),
        "tickets": ("jira-redwood", "issue", row.get("ticket_id") or row.get("id")),
        "incidents": ("jira-redwood", "issue", row.get("incident_id") or row.get("id")),
        "pull_requests": ("gitlab-redwood", "merge_request", row.get("pr_id") or row.get("id")),
        "slack_messages": ("slack-redwood", "message", row.get("message_id") or row.get("id")),
        "crm_activities": ("mail-redwood", "message", row.get("activity_id") or row.get("id")),
        "postmortems": ("xwiki-redwood", "page", row.get("postmortem_id") or row.get("id")),
    }
    if table in special:
        source, object_type, external_id = special[table]
        return _entity(source, object_type, external_id)
    table_key = f"{table.removesuffix('s')}_id"
    raw_id = row.get("id") or row.get(table_key)
    if raw_id is None:
        raw_id = next((value for key, value in row.items() if key.endswith("_id") and value), index)
    return _entity("confluence-redwood", "page", f"{table}-{raw_id}")


def _human_payload_text(payload: object) -> str:
    """Extract source-visible prose while excluding generator metadata and IDs."""
    values: list[str] = []

    def collect(value: object, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key.casefold().startswith("redwood") or child_key in {
                    "id",
                    "external_id",
                    "entity_id",
                    "aliases",
                    "references",
                }:
                    continue
                collect(child, child_key)
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif isinstance(value, str) and value.strip():
            values.append(value)

    collect(payload)
    return " ".join(values)


def materialize_gold_links(
    events: list[PlancyEvent], chains: dict[str, list[dict[str, Any]]]
) -> list[GoldLink]:
    """Build evidence-backed links from foreign keys and native payload expressions."""
    event_by_key = {(event.source, event.object_type, event.external_id): event for event in events}
    event_by_alias: dict[str, PlancyEvent] = {}
    for event in events:
        if event.payload is None:
            continue
        for event_alias in event.aliases:
            previous = event_by_alias.get(event_alias)
            if previous is None or event.revision > previous.revision:
                event_by_alias[event_alias] = event
    links: list[GoldLink] = []

    def alias(source: str, object_type: str, external_id: object) -> str | None:
        event = event_by_key.get((source, object_type, str(external_id)))
        return event.aliases[0] if event and event.aliases else None

    def add(
        *,
        predicate: str,
        source_entity_id: str,
        target_entity_id: str,
        evidence_entity_ids: list[str],
        rule_id: str,
        expressed_as: str,
        evidence_quote: str,
        explicit: bool = True,
    ) -> None:
        source_event = event_by_alias.get(source_entity_id)
        if source_event is None or target_entity_id not in event_by_alias:
            return
        target_event = event_by_alias[target_entity_id]
        if rule_id == "episode.decision.meeting.v1" and source_event.world_at <= target_event.world_at:
            return
        if rule_id.startswith("commercial_chain.") and source_event.world_at <= target_event.world_at:
            source_original = source_event.original_occurred_at or source_event.world_at
            target_original = target_event.original_occurred_at or target_event.world_at
            if source_original <= target_original:
                return
        payload_text = _human_payload_text(source_event.payload).casefold()
        # Provider-native causal references may live in structured fields or
        # reference metadata rather than prose. Explicit links may use either
        # representation; implicit links remain prose-only below.
        serialized_payload = json.dumps(
            source_event.payload or {}, ensure_ascii=False, sort_keys=True
        ).casefold()
        target_tokens = {str(target_event.external_id).casefold(), evidence_quote.casefold()}
        if explicit and not any(
            token and (token in payload_text or token in serialized_payload) for token in target_tokens
        ):
            return
        if not explicit:
            if not evidence_quote or evidence_quote.casefold() not in payload_text:
                return
            if str(target_event.external_id).casefold() in payload_text:
                return
            if re.search(
                r"\b(?:jira|confluence|linear|gitlab|github|slack|task|ticket|meeting|mr|pr|doc(?:ument)?)\b[^\s]{0,2}\d",
                evidence_quote.casefold(),
            ):
                return
        links.append(
            GoldLink(
                link_id=f"gold-{len(links) + 1:04d}",
                predicate=predicate,
                source_entity_id=source_entity_id,
                target_entity_id=target_entity_id,
                evidence_entity_ids=evidence_entity_ids,
                explicit=explicit,
                expressed_as=expressed_as,
                evidence_quote=evidence_quote,
                rule_id=rule_id,
            )
        )

    for pull_request in chains.get("pull_requests", []):
        pull_request_id = pull_request.get("pr_id") or pull_request.get("id")
        task_id = pull_request.get("linked_task_id") or pull_request.get("task_id")
        source = alias("gitlab-redwood", "merge_request", pull_request_id)
        target = alias("jira-redwood", "issue", task_id)
        if source and target:
            add(
                predicate="closes",
                source_entity_id=source,
                target_entity_id=target,
                evidence_entity_ids=[source],
                rule_id="merge_request.closes_task.v2",
                expressed_as="mr_text",
                evidence_quote=f"Closes {task_id}",
            )

    latest_events: dict[str, PlancyEvent] = {}
    for event in events:
        if event.payload is None:
            continue
        previous = latest_events.get(event.entity_id)
        if previous is None or event.revision > previous.revision:
            latest_events[event.entity_id] = event

    # Emit enterprise batches as sequential source-native causal paths.
    batch_stages: dict[str, dict[str, list[PlancyEvent]]] = defaultdict(lambda: defaultdict(list))
    for event in latest_events.values():
        payload = event.payload or {}
        chain_id = payload.get("redwoodCausalChainId")
        stage = payload.get("redwoodStage") or payload.get("redwoodExternalStage")
        if chain_id and str(chain_id).startswith("enterprise-causal-batch-") and stage:
            batch_stages[str(chain_id)][str(stage)].append(event)

    def batch_alias(stage: str, chain: dict[str, list[PlancyEvent]]) -> str | None:
        candidates = chain.get(stage, [])
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: str(item.external_id))[0].aliases[0]

    for _chain_id, stages in batch_stages.items():
        email = batch_alias("customer_email", stages)
        ticket = batch_alias("ticket", stages)
        meeting = batch_alias("meeting", stages)
        decision = batch_alias("decision", stages)
        task = batch_alias("task", stages)
        deployment = batch_alias("deployment", stages)
        incident = batch_alias("incident", stages)
        postmortem = batch_alias("postmortem", stages)
        transitions = (
            ("informed_by", ticket, email, "enterprise_batch.ticket.email.v1", "emailId"),
            ("informed_by", meeting, ticket, "enterprise_batch.meeting.ticket.v1", "ticketId"),
            ("decided_in", decision, meeting, "enterprise_batch.decision.meeting.v1", "meetingId"),
            ("decided_in", task, decision, "enterprise_batch.task.decision.v1", "decisionId"),
            ("caused_by", incident, deployment, "enterprise_batch.incident.deployment.v1", "deploymentId"),
            ("informed_by", postmortem, incident, "enterprise_batch.postmortem.incident.v1", "incidentId"),
        )
        for predicate, source, target, rule_id, reference_key in transitions:
            if not source or not target:
                continue
            source_event = event_by_alias.get(source)
            reference = (
                str((source_event.payload or {}).get("redwoodCausalReferences", {}).get(reference_key) or "")
                if source_event
                else ""
            )
            add(
                predicate=predicate,
                source_entity_id=source,
                target_entity_id=target,
                evidence_entity_ids=[source],
                rule_id=rule_id,
                expressed_as="text",
                evidence_quote=reference,
            )
        if task:
            for merge_request in stages.get("merge_request", []):
                add(
                    predicate="closes",
                    source_entity_id=merge_request.aliases[0],
                    target_entity_id=task,
                    evidence_entity_ids=[merge_request.aliases[0]],
                    rule_id="enterprise_batch.mr.task.v1",
                    expressed_as="mr_text",
                    evidence_quote=str(
                        (merge_request.payload or {}).get("redwoodCausalReferences", {}).get("taskId") or ""
                    ),
                )
        if deployment:
            deployment_event = event_by_alias.get(deployment)
            deployment_text = json.dumps(
                deployment_event.payload if deployment_event else {}, ensure_ascii=False
            )
            for merge_request in stages.get("merge_request", []):
                if (merge_request.payload or {}).get("state") == "closed":
                    continue
                merge_id = str(merge_request.external_id)
                if merge_id not in deployment_text:
                    continue
                add(
                    predicate="implements",
                    source_entity_id=deployment,
                    target_entity_id=merge_request.aliases[0],
                    evidence_entity_ids=[deployment],
                    rule_id="enterprise_batch.deployment.mr.v1",
                    expressed_as="text",
                    evidence_quote=merge_id,
                )

    for event in latest_events.values():
        if event.source == "gitlab-redwood" and event.payload and event.aliases:
            description = str(event.payload.get("description") or "")
            match = re.search(r"\bCloses\s+(ER-[A-F0-9]{10})\b", description, re.IGNORECASE)
            if match:
                task_id = match.group(1).upper()
                target = alias("jira-redwood", "issue", task_id)
                if target:
                    add(
                        predicate="closes",
                        source_entity_id=event.aliases[0],
                        target_entity_id=target,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id="enterprise_merge_request.closes_task.v2",
                        expressed_as="mr_text",
                        evidence_quote=f"Closes {task_id}",
                    )
            causal_refs = event.payload.get("redwoodCausalReferences", {})
            if False and event.external_id.startswith("dsid_") and causal_refs:
                causal_targets = (
                    (
                        "confluence-redwood",
                        "page",
                        "decisionId",
                        "implements",
                        "enterprise_merge_request.decision.v1",
                    ),
                    (
                        "confluence-redwood",
                        "page",
                        "epicId",
                        "part_of",
                        "enterprise_merge_request.epic.v1",
                    ),
                    (
                        "confluence-redwood",
                        "page",
                        "projectId",
                        "part_of",
                        "enterprise_merge_request.project.v1",
                    ),
                    (
                        "fireflies-redwood",
                        "transcript",
                        "meetingId",
                        "informed_by",
                        "enterprise_merge_request.meeting.v1",
                    ),
                    (
                        "jira-redwood",
                        "issue",
                        "ticketId",
                        "informed_by",
                        "enterprise_merge_request.ticket.v1",
                    ),
                    (
                        "mail-redwood",
                        "message",
                        "emailId",
                        "informed_by",
                        "enterprise_merge_request.customer_email.v1",
                    ),
                    (
                        "confluence-redwood",
                        "page",
                        "deploymentId",
                        "implements",
                        "enterprise_merge_request.deployment.v1",
                    ),
                    (
                        "jira-redwood",
                        "issue",
                        "incidentId",
                        "caused_by",
                        "enterprise_merge_request.incident.v1",
                    ),
                    (
                        "xwiki-redwood",
                        "page",
                        "postmortemId",
                        "informed_by",
                        "enterprise_merge_request.postmortem.v1",
                    ),
                )
                for target_source, target_type, reference_key, predicate, rule_id in causal_targets:
                    reference = causal_refs.get(reference_key)
                    target = alias(target_source, target_type, reference)
                    if not reference or not target:
                        continue
                    add(
                        predicate=predicate,
                        source_entity_id=event.aliases[0],
                        target_entity_id=target,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id=rule_id,
                        expressed_as="text",
                        evidence_quote=str(reference),
                    )
        if (
            event.source == "confluence-redwood"
            and event.external_id.startswith("enterprise-project-")
            and event.payload
            and event.aliases
        ):
            references = event.payload.get("redwoodReferences", {})
            dependencies = references.get("dependsOnProjectIds") or [references.get("dependsOnProjectId")]
            for dependency in dependencies:
                target = alias("confluence-redwood", "page", dependency)
                if dependency and target:
                    add(
                        predicate="informed_by",
                        source_entity_id=event.aliases[0],
                        target_entity_id=target,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id="enterprise_project.depends_on.v1",
                        expressed_as="page_property",
                        evidence_quote=str(dependency),
                    )

        if event.source == "confluence-redwood" and event.payload and event.aliases:
            references = event.payload.get("redwoodReferences") or {}
            if event.external_id.startswith("enterprise-epic-"):
                project_id = references.get("projectId")
                target = alias("confluence-redwood", "page", project_id)
                if project_id and target:
                    add(
                        predicate="part_of",
                        source_entity_id=event.aliases[0],
                        target_entity_id=target,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id="enterprise_epic.project.v1",
                        expressed_as="page_property",
                        evidence_quote=str(project_id),
                    )
            elif event.external_id.startswith("enterprise-decision-"):
                for target_key, rule_id in (
                    ("projectId", "enterprise_decision.project.v1"),
                    ("epicId", "enterprise_decision.epic.v1"),
                ):
                    target_id = references.get(target_key)
                    target = alias("confluence-redwood", "page", target_id)
                    if target_id and target:
                        add(
                            predicate="part_of",
                            source_entity_id=event.aliases[0],
                            target_entity_id=target,
                            evidence_entity_ids=[event.aliases[0]],
                            rule_id=rule_id,
                            expressed_as="page_property",
                            evidence_quote=str(target_id),
                        )

        if event.source == "jira-redwood" and event.external_id.startswith("ER-") and event.aliases:
            description = str(event.payload.get("fields", {}).get("description") or "")
            theme_match = re.search(
                r"decision\s+(enterprise-decision-[\w-]+)\s+in\s+epic\s+"
                r"(enterprise-epic-[\w-]+)\s+for\s+project\s+(enterprise-project-[\w-]+)",
                description,
                re.IGNORECASE,
            )
            if theme_match:
                decision_id, epic_id, project_id = theme_match.groups()
                for predicate, target_id, rule_id in (
                    ("implements", decision_id, "enterprise_task.decision.v3"),
                    ("part_of", epic_id, "enterprise_task.epic.v3"),
                    ("part_of", project_id, "enterprise_task.project.v3"),
                ):
                    target = alias("confluence-redwood", "page", target_id)
                    if target:
                        add(
                            predicate=predicate,
                            source_entity_id=event.aliases[0],
                            target_entity_id=target,
                            evidence_entity_ids=[event.aliases[0]],
                            rule_id=rule_id,
                            expressed_as="text",
                            evidence_quote=target_id,
                        )
            enterprise_refs = event.payload.get("redwoodEnterpriseReferences") or {}
            if str(enterprise_refs.get("workId") or "").startswith("enterprise-ordinary-"):
                for target_key, predicate, rule_id in (
                    ("decisionId", "decided_in", "enterprise_ordinary.task.decision.v1"),
                    ("epicId", "part_of", "enterprise_ordinary.task.epic.v1"),
                    ("projectId", "part_of", "enterprise_ordinary.task.project.v1"),
                ):
                    target_id = enterprise_refs.get(target_key)
                    target = alias("confluence-redwood", "page", target_id)
                    if not target_id or not target:
                        continue
                    add(
                        predicate=predicate,
                        source_entity_id=event.aliases[0],
                        target_entity_id=target,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id=rule_id,
                        expressed_as="text",
                        evidence_quote=str(target_id),
                    )
            elif event.external_id.startswith("enterprise-ordinary-decision-"):
                for target_key, rule_id in (
                    ("projectId", "enterprise_ordinary.decision.project.v1"),
                    ("epicId", "enterprise_ordinary.decision.epic.v1"),
                ):
                    target_id = references.get(target_key)
                    target = alias("confluence-redwood", "page", target_id)
                    if target_id and target:
                        add(
                            predicate="part_of",
                            source_entity_id=event.aliases[0],
                            target_entity_id=target,
                            evidence_entity_ids=[event.aliases[0]],
                            rule_id=rule_id,
                            expressed_as="page_property",
                            evidence_quote=str(target_id),
                        )

        causal_refs = event.payload.get("redwoodCausalReferences", {}) if event.payload else {}
        if causal_refs and event.aliases:
            if event.source == "jira-redwood" and event.external_id.startswith("ER-"):
                decision_record = alias("confluence-redwood", "page", causal_refs.get("decisionId"))
                if decision_record:
                    add(
                        predicate="implements",
                        source_entity_id=event.aliases[0],
                        target_entity_id=decision_record,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id="enterprise_task.decision_record.v1",
                        expressed_as="text",
                        evidence_quote=str(causal_refs["decisionId"]),
                    )
            elif event.source == "jira-redwood" and event.external_id.startswith("ENT-TKT-"):
                email = alias("mail-redwood", "message", causal_refs.get("emailId"))
                if email:
                    add(
                        predicate="informed_by",
                        source_entity_id=event.aliases[0],
                        target_entity_id=email,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id="enterprise_ticket.customer_email.v1",
                        expressed_as="text",
                        evidence_quote=str(causal_refs["emailId"]),
                    )
            elif event.source == "fireflies-redwood" and event.external_id.startswith("ENT-MTG-"):
                ticket = alias("jira-redwood", "issue", causal_refs.get("ticketId"))
                if ticket:
                    add(
                        predicate="informed_by",
                        source_entity_id=event.aliases[0],
                        target_entity_id=ticket,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id="enterprise_meeting.ticket.v1",
                        expressed_as="text",
                        evidence_quote=str(causal_refs["ticketId"]),
                    )
            elif event.source == "confluence-redwood" and event.external_id.startswith(
                "enterprise-decision-record-"
            ):
                meeting = alias("fireflies-redwood", "transcript", causal_refs.get("meetingId"))
                if meeting:
                    add(
                        predicate="decided_in",
                        source_entity_id=event.aliases[0],
                        target_entity_id=meeting,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id="enterprise_decision.meeting.v1",
                        expressed_as="page_property",
                        evidence_quote=str(causal_refs["meetingId"]),
                    )

        if event.source == "gitlab-redwood" and event.payload and event.aliases:
            enterprise_refs = event.payload.get("redwoodEnterpriseReferences") or {}
            work_id = str(enterprise_refs.get("workId") or "")
            if work_id.startswith("enterprise-ordinary-"):
                task_id = enterprise_refs.get("taskId")
                task_target = alias("jira-redwood", "issue", task_id)
                if task_id and task_target:
                    add(
                        predicate="closes",
                        source_entity_id=event.aliases[0],
                        target_entity_id=task_target,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id="enterprise_ordinary.mr.task.v1",
                        expressed_as="mr_text",
                        evidence_quote=f"Closes {task_id}",
                    )
                decision_id = enterprise_refs.get("decisionId")
                decision_target = alias("confluence-redwood", "page", decision_id)
                if decision_id and decision_target:
                    add(
                        predicate="decided_in",
                        source_entity_id=event.aliases[0],
                        target_entity_id=decision_target,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id="enterprise_ordinary.mr.decision.v1",
                        expressed_as="mr_text",
                        evidence_quote=str(decision_id),
                    )

        if (
            event.source == "confluence-redwood"
            and event.external_id.startswith("ENT-ORD-DEP-")
            and event.payload
            and event.aliases
        ):
            references = event.payload.get("redwoodEnterpriseReferences") or {}
            for merge_request_id in references.get("mergeRequestIds") or []:
                target = alias("gitlab-redwood", "merge_request", merge_request_id)
                if not target:
                    continue
                merge_event = event_by_alias.get(target)
                if merge_event and (merge_event.payload or {}).get("state") != "merged":
                    continue
                add(
                    predicate="implements",
                    source_entity_id=event.aliases[0],
                    target_entity_id=target,
                    evidence_entity_ids=[event.aliases[0]],
                    rule_id="enterprise_ordinary.deployment.mr.v1",
                    expressed_as="page_property",
                    evidence_quote=str(merge_request_id),
                )

        if (
            event.source == "jira-redwood"
            and event.external_id.startswith("ENT-ORD-INC-")
            and event.payload
            and event.aliases
        ):
            references = event.payload.get("redwoodEnterpriseReferences") or {}
            deployment_id = references.get("deploymentId")
            target = alias("confluence-redwood", "page", deployment_id)
            if deployment_id and target:
                add(
                    predicate="caused_by",
                    source_entity_id=event.aliases[0],
                    target_entity_id=target,
                    evidence_entity_ids=[event.aliases[0]],
                    rule_id="enterprise_ordinary.incident.deployment.v1",
                    expressed_as="remote_link",
                    evidence_quote=str(deployment_id),
                )

        external_refs = event.payload.get("redwoodExternalReferences", {}) if event.payload else {}
        if external_refs and event.aliases:
            external_links = (
                (
                    "jira-redwood",
                    "EXT-TKT-",
                    "actionExternalId",
                    "informed_by",
                    "external_ticket.action.v1",
                    "text",
                ),
                (
                    "jira-redwood",
                    "external-signal-meeting-",
                    "ticketId",
                    "informed_by",
                    "external_meeting.ticket.v1",
                    "text",
                ),
                (
                    "fireflies-redwood",
                    "EXT-DEC-",
                    "meetingId",
                    "decided_in",
                    "external_decision.meeting.v1",
                    "page_property",
                ),
                (
                    "confluence-redwood",
                    "EXT-TASK-",
                    "decisionId",
                    "implements",
                    "external_task.decision.v1",
                    "text",
                ),
                (
                    "jira-redwood",
                    "EXT-MR-",
                    "taskId",
                    "closes",
                    "external_mr.task.v1",
                    "mr_text",
                ),
                (
                    "gitlab-redwood",
                    "EXT-DEP-",
                    "mergeRequestId",
                    "implements",
                    "external_deployment.mr.v1",
                    "page_property",
                ),
                (
                    "confluence-redwood",
                    "EXT-INC-",
                    "deploymentId",
                    "caused_by",
                    "external_incident.deployment.v1",
                    "remote_link",
                ),
                (
                    "jira-redwood",
                    "EXT-PM-",
                    "incidentId",
                    "informed_by",
                    "external_postmortem.incident.v1",
                    "page_property",
                ),
            )
            for target_source, prefix, reference_key, predicate, rule_id, expressed_as in external_links:
                if not event.external_id.startswith(prefix):
                    continue
                target_type = {
                    "mail-redwood": "message",
                    "jira-redwood": "issue",
                    "fireflies-redwood": "transcript",
                    "confluence-redwood": "page",
                }.get(target_source, "page")
                if reference_key == "actionExternalId":
                    target_source = str(external_refs.get("actionSource") or target_source)
                    target_type = str(external_refs.get("actionObjectType") or target_type)
                target = alias(target_source, target_type, external_refs.get(reference_key))
                if target:
                    add(
                        predicate=predicate,
                        source_entity_id=event.aliases[0],
                        target_entity_id=target,
                        evidence_entity_ids=[event.aliases[0]],
                        rule_id=rule_id,
                        expressed_as=expressed_as,
                        evidence_quote=str(external_refs[reference_key]),
                    )

    financial_by_episode: dict[str, dict[str, PlancyEvent]] = defaultdict(dict)
    for event in latest_events.values():
        payload = event.payload or {}
        properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
        episode_id = payload.get("redwoodEpisodeId") or properties.get("redwood_episode_id")
        if episode_id and event.object_type in {
            "payment",
            "invoice",
            "contract",
            "quote",
        }:
            financial_by_episode[str(episode_id)][event.object_type] = event
    for stages in financial_by_episode.values():
        for source_type, target_type, predicate, rule_id, reference_key in (
            ("payment", "invoice", "informed_by", "commercial_chain.payment.invoice.v1", "invoice_id"),
            ("invoice", "contract", "part_of", "commercial_chain.invoice.contract.v1", "contract_id"),
            ("contract", "quote", "part_of", "commercial_chain.contract.quote.v1", "quote_id"),
        ):
            source_event = stages.get(source_type)
            target_event = stages.get(target_type)
            if not source_event or not target_event or not source_event.aliases:
                continue
            payload = source_event.payload or {}
            properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
            reference = str(properties.get(reference_key) or target_event.external_id)
            add(
                predicate=predicate,
                source_entity_id=source_event.aliases[0],
                target_entity_id=target_event.aliases[0],
                evidence_entity_ids=[source_event.aliases[0]],
                rule_id=rule_id,
                expressed_as="page_property",
                evidence_quote=reference,
            )

    for deployment in chains.get("deployments", []):
        deployment_id = deployment.get("deployment_id")
        pull_request_id = deployment.get("pr_id")
        source = alias("confluence-redwood", "page", f"deployments-{deployment_id}")
        target = alias("gitlab-redwood", "merge_request", pull_request_id)
        if source and target:
            add(
                predicate="implements",
                source_entity_id=source,
                target_entity_id=target,
                evidence_entity_ids=[source],
                rule_id="deployment.pr_id.v2",
                expressed_as="page_property",
                evidence_quote=str(pull_request_id),
            )

    for incident in chains.get("incidents", []):
        incident_id = incident.get("incident_id") or incident.get("id")
        deployment_id = incident.get("linked_deployment_id")
        source = alias("jira-redwood", "issue", incident_id)
        target = alias("confluence-redwood", "page", f"deployments-{deployment_id}")
        if source and target:
            add(
                predicate="caused_by",
                source_entity_id=source,
                target_entity_id=target,
                evidence_entity_ids=[source],
                rule_id="incident.deployment_id.v2",
                expressed_as="remote_link",
                evidence_quote=str(deployment_id),
            )
        pull_request_id = incident.get("pull_request_id")
        if incident_id and pull_request_id:
            source_entity = _entity("jira-redwood", "issue", incident_id)
            target_entity = _entity("gitlab-redwood", "merge_request", pull_request_id)
            if source_entity in {event.entity_id for event in events} and target_entity in {
                event.entity_id for event in events
            }:
                links.append(
                    GoldLink(
                        link_id=f"gold-{len(links) + 1:04d}",
                        predicate="caused_by",
                        source_entity_id=source_entity,
                        target_entity_id=target_entity,
                        evidence_entity_ids=[source_entity],
                        explicit=True,
                        expressed_as="remote_link",
                        evidence_quote=str(pull_request_id),
                        rule_id="incident.pull_request_id.v1",
                    )
                )

    for postmortem in chains.get("postmortems", []):
        postmortem_id = postmortem.get("postmortem_id")
        incident_id = postmortem.get("incident_id")
        source = alias("xwiki-redwood", "page", f"postmortems-{postmortem_id}")
        target = alias("jira-redwood", "issue", incident_id)
        if source and target:
            add(
                predicate="informed_by",
                source_entity_id=source,
                target_entity_id=target,
                evidence_entity_ids=[source],
                rule_id="postmortem.incident_id.v2",
                expressed_as="page_property",
                evidence_quote=str(incident_id),
            )

    legacy_fixture = not any(
        (event.payload or {}).get("redwoodEpisodeId")
        or (event.payload or {}).get("properties", {}).get("redwood_episode_id")
        for event in events
    )
    if legacy_fixture:
        event_entities = {event.entity_id for event in events}
        for document in chains.get("documents", []):
            document_id = document.get("document_id") or document.get("id")
            incident_id = document.get("linked_incident_id")
            source_entity = _entity("confluence-redwood", "page", f"documents-{document_id}")
            target_entity = _entity("jira-redwood", "issue", incident_id)
            if incident_id and source_entity in event_entities and target_entity in event_entities:
                links.append(
                    GoldLink(
                        link_id=f"gold-{len(links) + 1:04d}",
                        predicate="informed_by",
                        source_entity_id=source_entity,
                        target_entity_id=target_entity,
                        evidence_entity_ids=[source_entity],
                        explicit=True,
                        expressed_as="page_property",
                        evidence_quote=str(incident_id),
                        rule_id="source_field.linked_incident_id.v1",
                    )
                )

    for task in chains.get("tasks", []):
        task_id = task.get("task_id") or task.get("id")
        ticket_id = task.get("linked_ticket_id") or task.get("ticket_id")
        source = alias("jira-redwood", "issue", task_id)
        target = alias("jira-redwood", "issue", ticket_id)
        if source and target:
            add(
                predicate="informed_by",
                source_entity_id=source,
                target_entity_id=target,
                evidence_entity_ids=[source],
                rule_id="task.ticket_id.v2",
                expressed_as="text",
                evidence_quote=str(ticket_id),
            )

    for meeting in chains.get("meetings", []):
        meeting_id = meeting.get("meeting_id")
        source = alias("fireflies-redwood", "transcript", meeting_id)
        if not source:
            continue
        for task in chains.get("tasks", []):
            if not (
                meeting.get("linked_incident_id")
                and task.get("linked_incident_id") == meeting.get("linked_incident_id")
            ) and not (
                meeting.get("linked_account_id")
                and task.get("linked_account_id") == meeting.get("linked_account_id")
            ):
                continue
            target = alias("jira-redwood", "issue", task.get("task_id"))
            if target:
                add(
                    predicate="decided_in",
                    source_entity_id=source,
                    target_entity_id=target,
                    evidence_entity_ids=[source],
                    rule_id="meeting.followup_task.v2",
                    expressed_as="decision_item",
                    evidence_quote=str(task.get("task_id")),
                )

    ordered_meetings = sorted(chains.get("meetings", []), key=lambda row: str(row.get("held_at", "")))
    for previous_meeting, current_meeting in zip(ordered_meetings, ordered_meetings[1:], strict=False):
        source = alias("fireflies-redwood", "transcript", current_meeting.get("meeting_id"))
        target = alias("fireflies-redwood", "transcript", previous_meeting.get("meeting_id"))
        if source and target:
            add(
                predicate="informed_by",
                source_entity_id=source,
                target_entity_id=target,
                evidence_entity_ids=[source],
                rule_id="meeting.previous_sync_implicit.v2",
                expressed_as="text",
                evidence_quote="Following the previous customer sync",
                explicit=False,
            )

    first_activity: dict[str, str] = {}
    for activity in sorted(chains.get("crm_activities", []), key=lambda row: str(row.get("timestamp", ""))):
        opportunity_id = str(activity.get("opportunity_id") or "")
        if opportunity_id and opportunity_id not in first_activity:
            first_activity[opportunity_id] = str(activity.get("activity_id"))
        message_external_id = f"<crm-{activity.get('activity_id')}@redwood.example>"
        source = alias("mail-redwood", "message", message_external_id)
        target = alias("hubspot-redwood", "deal", opportunity_id)
        if source and target:
            add(
                predicate="informed_by",
                source_entity_id=source,
                target_entity_id=target,
                evidence_entity_ids=[source],
                rule_id="customer_email.deal_reference.v2",
                expressed_as="text",
                evidence_quote=opportunity_id,
            )

    for opportunity in chains.get("opportunities", []):
        opportunity_id = str(opportunity.get("opportunity_id"))
        quote = alias("hubspot-redwood", "quote", f"QUOTE-{opportunity_id}")
        deal = alias("hubspot-redwood", "deal", opportunity_id)
        if quote and deal:
            add(
                predicate="informed_by",
                source_entity_id=quote,
                target_entity_id=deal,
                evidence_entity_ids=[quote],
                rule_id="quote.deal_id.v2",
                expressed_as="text",
                evidence_quote=opportunity_id,
            )
        activity_id = first_activity.get(opportunity_id)
        email = (
            alias("mail-redwood", "message", f"<crm-{activity_id}@redwood.example>") if activity_id else None
        )
        if quote and email:
            add(
                predicate="informed_by",
                source_entity_id=quote,
                target_entity_id=email,
                evidence_entity_ids=[quote],
                rule_id="quote.customer_email.v2",
                expressed_as="text",
                evidence_quote=f"<crm-{activity_id}@redwood.example>",
            )
        invoice = alias("hubspot-redwood", "invoice", f"INV-{opportunity_id}")
        if invoice and quote:
            add(
                predicate="informed_by",
                source_entity_id=invoice,
                target_entity_id=quote,
                evidence_entity_ids=[invoice],
                rule_id="invoice.quote_id.v2",
                expressed_as="text",
                evidence_quote=f"QUOTE-{opportunity_id}",
            )

    messages_by_thread: dict[str, list[PlancyEvent]] = {}
    for event in events:
        if event.source == "slack-redwood" and event.payload:
            messages_by_thread.setdefault(str(event.payload.get("thread_ts")), []).append(event)
    for thread_messages in messages_by_thread.values():
        ordered = sorted(thread_messages, key=lambda event: event.world_at)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if previous.entity_id == current.entity_id:
                continue
            if not previous.aliases or not current.aliases:
                continue
            add(
                predicate="replies_to",
                source_entity_id=current.aliases[0],
                target_entity_id=previous.aliases[0],
                evidence_entity_ids=[current.aliases[0]],
                rule_id="slack.thread_reply.v2",
                expressed_as="reply",
                evidence_quote=str(current.payload.get("thread_ts")),
            )

    for event in events:
        if event.source != "mail-redwood" or not event.payload or not event.aliases:
            continue
        in_reply_to = event.payload.get("inReplyTo")
        if not in_reply_to:
            continue
        target = alias("mail-redwood", "message", in_reply_to)
        if target:
            add(
                predicate="replies_to",
                source_entity_id=event.aliases[0],
                target_entity_id=target,
                evidence_entity_ids=[event.aliases[0]],
                rule_id="graph.in_reply_to.v2",
                expressed_as="reply",
                evidence_quote=str(in_reply_to),
            )

    episode_stages: dict[str, dict[str, PlancyEvent]] = {}
    for event in events:
        payload = event.payload or {}
        properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
        episode_id = (
            payload.get("redwoodEpisodeId")
            or payload.get("redwoodExternalChainId")
            or properties.get("redwood_episode_id")
        )
        stage = payload.get("redwoodStage") or payload.get("redwoodExternalStage")
        if not stage and event.source == "hubspot-redwood":
            stage = event.object_type
        if not episode_id or not stage or not event.aliases:
            continue
        stage_events = episode_stages.setdefault(str(episode_id), {})
        previous = stage_events.get(str(stage))
        if previous is None or event.revision > previous.revision:
            stage_events[str(stage)] = event

    context_stages: dict[str, dict[str, PlancyEvent]] = defaultdict(dict)
    for event in latest_events.values():
        payload = event.payload or {}
        chain_id = (
            payload.get("redwoodEpisodeId")
            or payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
        )
        stage = payload.get("redwoodStage") or payload.get("redwoodExternalStage")
        if chain_id and stage and event.aliases:
            previous = context_stages[str(chain_id)].get(str(stage))
            if previous is None or event.revision > previous.revision:
                context_stages[str(chain_id)][str(stage)] = event

    episode_edges = (
        ("ticket", "customer_email", "informed_by", "episode.ticket.email.v1", True, None),
        ("slack", "ticket", "informed_by", "episode.slack.ticket.v1", True, None),
        (
            "meeting",
            "slack",
            "informed_by",
            "episode.meeting.implicit_slack.v1",
            False,
            "customer report",
        ),
        (
            "decision",
            "meeting",
            "decided_in",
            "episode.decision.meeting.v1",
            True,
            None,
        ),
        (
            "task",
            "decision",
            "decided_in",
            "episode.task.decision.v1",
            True,
            None,
        ),
        ("task", "meeting", "decided_in", "episode.task.meeting.v1", True, None),
        ("merge_request", "task", "closes", "episode.mr.task.v1", True, None),
        (
            "deployment",
            "merge_request",
            "implements",
            "episode.deployment.mr.v1",
            True,
            None,
        ),
        (
            "incident",
            "deployment",
            "caused_by",
            "episode.incident.deployment.v1",
            True,
            None,
        ),
        (
            "postmortem",
            "incident",
            "informed_by",
            "episode.postmortem.incident.v1",
            True,
            None,
        ),
    )
    for stages in episode_stages.values():
        for source_stage, target_stage, predicate, rule_id, explicit, quote in episode_edges:
            source_event = stages.get(source_stage)
            target_event = stages.get(target_stage)
            if not source_event or not target_event:
                continue
            if rule_id == "episode.meeting.implicit_slack.v1":
                transcript = str((source_event.payload or {}).get("transcript") or "")
                evidence_quote = (
                    "customer report" if "customer report" in transcript.casefold() else transcript
                )
            elif quote:
                evidence_quote = quote
            else:
                evidence_quote = target_event.external_id
            add(
                predicate=predicate,
                source_entity_id=source_event.aliases[0],
                target_entity_id=target_event.aliases[0],
                evidence_entity_ids=[source_event.aliases[0]],
                rule_id=rule_id,
                expressed_as="text",
                evidence_quote=evidence_quote,
                explicit=explicit,
            )

        if any((event.payload or {}).get("redwoodExternalStage") for event in stages.values()):
            external_order = (
                "action",
                "ticket",
                "meeting",
                "decision",
                "task",
                "merge_request",
                "deployment",
                "incident",
                "postmortem",
                "deal_action",
            )
            external_predicates = {
                "meeting": "informed_by",
                "ticket": "informed_by",
                "decision": "decided_in",
                "task": "decided_in",
                "merge_request": "closes",
                "deployment": "implements",
                "incident": "caused_by",
                "postmortem": "informed_by",
                "deal_action": "informed_by",
            }
            present = [stage for stage in external_order if stage in stages]
            for target_stage, source_stage in zip(present, present[1:], strict=False):
                source_event = stages[source_stage]
                target_event = stages[target_stage]
                add(
                    predicate=external_predicates.get(source_stage, "informed_by"),
                    source_entity_id=source_event.aliases[0],
                    target_entity_id=target_event.aliases[0],
                    evidence_entity_ids=[source_event.aliases[0]],
                    rule_id=f"external_chain.{source_stage}.{target_stage}.v1",
                    expressed_as="text",
                    evidence_quote=target_event.external_id,
                )

        commercial_order = ("deal", "quote", "contract", "invoice", "payment")
        present = [stage for stage in commercial_order if stage in stages]
        commercial_pairs = (
            ("deal", "quote"),
            ("contract", "quote"),
            ("invoice", "contract"),
            ("payment", "invoice"),
        )
        for source_stage, target_stage in commercial_pairs:
            if source_stage not in present or target_stage not in present:
                continue
            source_event = stages[source_stage]
            target_event = stages[target_stage]
            add(
                predicate="informed_by",
                source_entity_id=source_event.aliases[0],
                target_entity_id=target_event.aliases[0],
                evidence_entity_ids=[source_event.aliases[0]],
                rule_id=f"commercial_chain.{source_stage}.{target_stage}.v1",
                expressed_as="text",
                evidence_quote=target_event.external_id,
            )

    proposal_emails: dict[str, PlancyEvent] = {}
    for event in events:
        payload = event.payload or {}
        if (
            event.source == "mail-redwood"
            and payload.get("redwoodTopic") == "proposal"
            and str(payload.get("id", "")).rstrip(">").endswith("-proposal-m1@redwood.example")
        ):
            proposal_emails[str(payload.get("redwoodEpisodeId"))] = event
    for episode_id, email_event in proposal_emails.items():
        deal_event = episode_stages.get(episode_id, {}).get("deal")
        if not deal_event or not email_event.aliases or not deal_event.aliases:
            continue
        add(
            predicate="informed_by",
            source_entity_id=deal_event.aliases[0],
            target_entity_id=email_event.aliases[0],
            evidence_entity_ids=[deal_event.aliases[0]],
            rule_id="episode.commercial.approval.v1",
            expressed_as="text",
            evidence_quote=str(email_event.external_id),
        )

    external_targets = {
        str((event.payload or {}).get("redwood_metadata", {}).get("canonical_document_id")): event
        for event in events
        if event.payload and event.payload.get("redwood_metadata", {}).get("canonical_document_id")
    }
    for event in events:
        payload = event.payload or {}
        canonical_id = payload.get("canonicalDocumentId")
        target = external_targets.get(str(canonical_id))
        if not payload.get("redwoodExternalAction") or not target or not event.aliases or not target.aliases:
            continue
        add(
            predicate="informed_by",
            source_entity_id=event.aliases[0],
            target_entity_id=target.aliases[0],
            evidence_entity_ids=[event.aliases[0]],
            rule_id="external_context.source_document.v1",
            expressed_as="text",
            evidence_quote=str(canonical_id),
        )

    # Resolve natural-language references through the episode context. The
    # target is deliberately absent from the source phrase; only the episode
    # and target role connect the records.
    for event in latest_events.values():
        payload = event.payload or {}
        context = payload.get("redwoodImplicitContext")
        episode_id = (
            payload.get("redwoodEpisodeId")
            or payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
        )
        if not isinstance(context, dict) or not episode_id or not event.aliases:
            continue
        target_stage = str(context.get("target_stage") or "")
        target = context_stages.get(str(episode_id), {}).get(target_stage)
        phrase = str(context.get("phrase") or "")
        if not target or not phrase or not target.aliases:
            continue
        add(
            predicate=str(context.get("predicate") or "informed_by"),
            source_entity_id=event.aliases[0],
            target_entity_id=target.aliases[0],
            evidence_entity_ids=[event.aliases[0]],
            rule_id=f"implicit.{str(context.get('kind') or target_stage)}.v1",
            expressed_as="text",
            evidence_quote=phrase,
            explicit=False,
        )

    # Account-risk external episodes end in a real HubSpot deal action when
    # the CLI attaches one. Keep this link source-native and evidence-backed:
    # the deal description contains the decision reference, while the
    # episode context resolves the exact decision object.
    for stages in context_stages.values():
        deal_action = stages.get("deal_action")
        decision = stages.get("decision")
        if not deal_action or not decision or not deal_action.aliases or not decision.aliases:
            continue
        payload = deal_action.payload or {}
        references = payload.get("redwoodExternalReferences") or {}
        decision_id = str(references.get("decisionId") or decision.external_id)
        add(
            predicate="informed_by",
            source_entity_id=deal_action.aliases[0],
            target_entity_id=decision.aliases[0],
            evidence_entity_ids=[deal_action.aliases[0]],
            rule_id="external_deal_action.decision.v1",
            expressed_as="text",
            evidence_quote=decision_id,
        )

    # Finance adjustments are first-class source records. Their foreign-key
    # links make a voided invoice/reissue and a credit note auditable without
    # making either object part of the legacy invoice entity history.
    for event in latest_events.values():
        payload = event.payload or {}
        properties = payload.get("properties")
        if not isinstance(properties, dict) or not event.aliases:
            continue
        record_type = str(properties.get("record_type") or "")
        if record_type == "invoice":
            target_id = str(properties.get("replaces_invoice_id") or "")
            target = alias("hubspot-redwood", "invoice", target_id)
            if target:
                add(
                    predicate="supersedes",
                    source_entity_id=event.aliases[0],
                    target_entity_id=target,
                    evidence_entity_ids=[event.aliases[0]],
                    rule_id="invoice_reissue.replaces_invoice.v1",
                    expressed_as="text",
                    evidence_quote=target_id,
                )
        elif record_type == "credit_note":
            target_id = str(properties.get("applies_to_invoice_id") or "")
            target = alias("hubspot-redwood", "invoice", target_id)
            if target:
                add(
                    predicate="informed_by",
                    source_entity_id=event.aliases[0],
                    target_entity_id=target,
                    evidence_entity_ids=[event.aliases[0]],
                    rule_id="credit_note.applies_to_invoice.v1",
                    expressed_as="text",
                    evidence_quote=target_id,
                )

    unique = {
        (
            link.predicate,
            link.source_entity_id,
            link.target_entity_id,
            link.rule_id,
            link.expressed_as,
        ): link
        for link in links
    }
    reference_aliases = {
        str(reference): event.aliases[0]
        for event in events
        if event.aliases
        for reference in (event.entity_id, event.external_id, *event.aliases)
    }
    return [
        link.model_copy(
            update={
                "link_id": f"gold-{index:04d}",
                "source_entity_id": reference_aliases.get(link.source_entity_id, link.source_entity_id),
                "target_entity_id": reference_aliases.get(link.target_entity_id, link.target_entity_id),
                "evidence_entity_ids": [
                    reference_aliases.get(reference, reference) for reference in link.evidence_entity_ids
                ],
            }
        )
        for index, link in enumerate(unique.values(), start=1)
    ]
