"""Materialize source-native Plancy events from the Solstice operational chain."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from variator.corpus.business_events import materialize_business_specs
from variator.corpus.canonical import immutable_id
from variator.corpus.email_events import materialize_email_specs
from variator.corpus.engineering_events import materialize_engineering_specs
from variator.corpus.episodes import plan_business_episodes
from variator.corpus.models import (
    Acl,
    CorpusDocument,
    DocumentMeta,
    MeetingProtocol,
    NorthStarReport,
    PlancyEvent,
    Tombstone,
)
from variator.corpus.normalization import normalize_timestamp
from variator.corpus.redwood_delivery import COMPANY_NAME, acl_for, customer_contact_identity
from variator.corpus.redwood_profile import PROFILE_START

TIME_FIELDS = (
    "timestamp",
    "changed_at",
    "created_at",
    "opened_at",
    "published_at",
    "held_at",
    "deployed_at",
    "updated_at",
    "completed_at",
    "resolved_at",
    "expected_close_date",
    "renewal_due_date",
    "date",
)

BUSINESS_EMAIL_SUBJECTS = (
    "Customer follow-up on {topic}",
    "Question about the {topic} work",
    "Next step for {topic}",
    "Evidence for the {topic} review",
    "A decision is needed on {topic}",
    "Checking the {topic} handoff",
    "{topic} update from the customer",
    "Can we close the {topic} question?",
    "Scope change for {topic}",
    "Request for the {topic} checkpoint",
    "The {topic} result and owner",
    "Follow-up before the {topic} review",
)


def _jira_contract_key(value: object) -> str:
    """Return the PROJECT-N key consumed by the downstream Jira parser."""
    text = str(value or "")
    if re.fullmatch(r"PROJECT-\d+", text):
        return text
    number = int(sha256(text.encode("utf-8")).hexdigest()[:10], 16) % 900_000 + 1
    return f"PROJECT-{number:06d}"


def _timestamp(row: dict[str, Any]) -> datetime:
    for field in TIME_FIELDS:
        if row.get(field):
            return normalize_timestamp(row[field])
    raise ValueError(f"row has no timestamp field: {row}")


def _source_account(value: object | None) -> dict[str, str] | None:
    if not value:
        return None
    text = str(value)
    return {
        "accountId": f"acct:{text}",
        "displayName": text,
        "emailAddress": f"{text.casefold()}@redwood.example",
    }


def _row_references(row: dict[str, Any]) -> str:
    """Render only explicit cross-system references that a human would put in text."""
    labels = {
        "linked_ticket_id": "customer ticket",
        "linked_task_id": "task",
        "linked_incident_id": "incident",
        "linked_deployment_id": "deployment",
        "linked_account_id": "account",
        "primary_account_id": "account",
        "linked_service_id": "service",
        "service_id": "service",
        "project_id": "project",
        "epic_id": "epic",
        "opportunity_id": "deal",
        "notes_document_id": "meeting notes",
        "document_id": "document",
        "pr_id": "merge request",
    }
    references = [f"{label} {row[field]}" for field, label in labels.items() if row.get(field)]
    return "; ".join(references)


def _implicit_context_payload(
    *, kind: str, target_stage: str, account: str, topic: str, variant: int
) -> dict[str, str]:
    """Return source-visible language that identifies a relation without an ID."""
    account_label = "the customer account" if re.fullmatch(r"[A-Z]+\d+", account) else account
    account_team = f"{account_label} team"
    topic_label = str(topic).replace("_", " ").strip()
    topic_label = re.sub(r"\b(?:workstream|batch)\s+\d+\b", "customer workstream", topic_label)
    topic_label = re.sub(r"\b(?:amazon reviews 2023|amazon reviews)\b", "customer feedback", topic_label)
    topic_label = re.sub(r"\bcc news\b", "market news", topic_label)
    topic_label = re.sub(r"\breddit\b", "community report", topic_label)
    topic_label = re.sub(r"^the\s+", "", topic_label, flags=re.IGNORECASE)
    phrases = (
        f"the {topic_label} agreement from {account_label}'s last customer review",
        f"the {topic_label} follow-up approved by {account_team} after discussion",
        f"the {topic_label} change agreed with {account_label} before the next checkpoint",
        f"the {topic_label} decision captured after {account_label} shared the evidence",
        f"the {topic_label} work assigned by the customer and {COMPANY_NAME} during the review",
        f"the {topic_label} outcome {account_label} asked us to confirm in writing",
        f"the {topic_label} risk raised during the account conversation",
        f"the {topic_label} handoff after the customer's latest clarification",
    )
    return {
        "kind": kind,
        "target_stage": target_stage,
        "phrase": phrases[variant % len(phrases)],
    }


def _event_acl(source: str, container: str, row: dict[str, Any] | None = None) -> Acl:
    """Materialize an explicit default-deny-aware ACL for every source event."""
    if source in {"mail-redwood", "gmail-redwood"} and row:
        participants = row.get("participant_employee_ids") or [row.get("employee_id")]
        employee_ids = sorted({str(value) for value in participants if value})
        if not employee_ids:
            return Acl.model_validate(acl_for(source, container, row))
        return Acl.model_validate(
            {
                "allow": [{"principal": f"user:{employee_id.casefold()}"} for employee_id in employee_ids],
                "deny": [],
                "inherit": False,
            }
        )
    return Acl.model_validate(acl_for(source, container, row))


def _container_for_document(document: CorpusDocument) -> str:
    """Choose a stable access container from source metadata without inspecting content."""
    metadata = document.provenance
    table = str(metadata.get("table", "")).casefold()
    title = str(metadata.get("title", "")).casefold()
    combined = f"{table} {title}"
    if any(token in combined for token in ("hr", "people", "employee")):
        return "HR"
    if any(token in combined for token in ("finance", "revenue", "billing")):
        return "FINANCE"
    if any(token in combined for token in ("commercial", "sales", "customer", "renewal")):
        return "COMMERCIAL"
    return "ENG"


def _jira_payload(
    *,
    key: str,
    row: dict[str, Any],
    status: str,
    histories: list[dict[str, Any]],
) -> dict[str, Any]:
    created_at = _timestamp(row)
    author = _source_account(row.get("author") or row.get("reporter_employee_id"))
    comment_author = author or _source_account(row.get("assignee_employee_id")) or _source_account("E001")
    description = str(row.get("content") or row.get("description") or row.get("summary") or "")
    references = _row_references(row)
    if references:
        description = f"{description}\n\nReferences: {references}."
    summary = str(row.get("summary") or row.get("content") or key)
    is_incident = (
        "incident" in key.casefold() or "-inc-" in key.casefold() or key.casefold().startswith("inc")
    )
    comment_templates = (
        "Reviewer question: which evidence supports the {summary} acceptance boundary?",
        "Author response: the source record confirms {summary}; the linked snapshot is authoritative.",
        "Blocker found: {summary} needs an owner decision before the next status transition.",
        "Scope update: keep {summary} limited to the current customer-impact boundary.",
        "Evidence attached: compare {summary} with the referenced source snapshot before closing.",
        "Reopen review: {summary} remains open until the regression check is reproduced.",
        "Close decision: {summary} is ready after the reviewer confirmed the acceptance evidence.",
    )
    seed = int(sha256(f"{key}:{created_at.isoformat()}".encode()).hexdigest()[:8], 16)
    context = " ".join(description.split())[:240]
    action_words = (
        "triage",
        "reproduce",
        "compare",
        "annotate",
        "verify",
        "reconcile",
        "escalate",
        "scope",
        "handoff",
        "test",
        "review",
        "approve",
        "reopen",
        "close",
        "monitor",
        "measure",
        "validate",
        "document",
        "route",
        "confirm",
        "challenge",
        "mitigate",
        "schedule",
        "audit",
        "inspect",
        "explain",
        "prioritize",
        "unblock",
        "rollback",
        "ship",
        "record",
    )
    review_dimensions = (
        "scope boundary",
        "customer evidence",
        "release gate",
        "owner handoff",
        "risk signal",
        "status history",
        "reproduction note",
        "contract check",
        "latency sample",
        "rollback point",
        "permission edge",
        "billing proof",
        "acceptance note",
        "dependency map",
        "support queue",
        "review question",
        "test fixture",
        "timeline gap",
        "quality bar",
        "decision record",
        "account context",
        "change summary",
        "source reference",
        "follow-up window",
        "failure mode",
        "service impact",
        "commercial term",
        "operational metric",
        "customer promise",
        "audit trail",
        "handoff note",
        "validation result",
    )
    evidence_postures = (
        "observed",
        "reproduced",
        "attached",
        "reconciled",
        "confirmed",
        "pending",
        "blocked",
        "approved",
        "scoped",
        "measured",
        "audited",
        "reviewed",
        "linked",
        "triaged",
        "explained",
        "resolved",
        "reopened",
        "scheduled",
        "tested",
        "compared",
        "accepted",
        "rejected",
        "escalated",
        "monitored",
        "documented",
        "verified",
        "challenged",
        "mitigated",
        "isolated",
        "validated",
        "owned",
        "ready",
    )
    comment_count = 0 if seed % 13 == 0 else 1 + seed % 5
    comments = []
    for index, template in enumerate(comment_templates[:comment_count], start=1):
        action = action_words[(seed + index * 13) % len(action_words)]
        dimension = review_dimensions[(seed + index * 17) % len(review_dimensions)]
        posture = evidence_postures[(seed + index * 19) % len(evidence_postures)]
        comments.append(
            {
                "id": f"{key}-comment-{index}",
                "author": comment_author,
                "body": (
                    f"{template.format(summary=summary)} Action lane: {action}. "
                    f"Review dimension: {dimension}. Evidence posture: {posture}. Context: {context}"
                ),
                "created": (created_at + timedelta(hours=(index - 1) * 4 + seed % 3)).isoformat(),
            }
        )
    contract_key = _jira_contract_key(key)
    return {
        "key": contract_key,
        "id": key,
        "fields": {
            "summary": summary,
            "description": description,
            "issuetype": {"name": "Bug" if is_incident else "Task"},
            "project": {"key": "PROJECT", "name": "Engineering"},
            "status": {
                "name": status,
                "statusCategory": {"key": "done" if status == "Done" else "indeterminate"},
            },
            "resolution": {"name": "Done"} if status == "Done" else None,
            "priority": {"name": str(row.get("priority") or "Medium").title()},
            "assignee": _source_account(row.get("assignee_employee_id")) or author,
            "reporter": author,
            "creator": author,
            "created": created_at.isoformat(),
            "updated": created_at.isoformat(),
            "labels": ["incident"] if is_incident else [],
            "comment": {"comments": comments},
        },
        "names": {},
        "changelog": {"histories": histories},
        "remotelinks": [],
    }


def _status_label(value: object, default: str = "To Do") -> str:
    """Normalize source task states to the provider-visible Jira vocabulary."""
    text = str(value or default).strip().replace("_", " ").replace("-", " ")
    normalized = " ".join(text.split()).casefold()
    labels = {
        "todo": "To Do",
        "to do": "To Do",
        "open": "Open",
        "in progress": "In Progress",
        "inprogress": "In Progress",
        "review": "Review",
        "in review": "Review",
        "validation": "Validation",
        "validated": "Validation",
        "blocked": "Blocked",
        "reopened": "Reopened",
        "re-opened": "Reopened",
        "done": "Done",
        "closed": "Closed",
        "completed": "Done",
        "cancelled": "Cancelled",
        "canceled": "Cancelled",
    }
    return labels.get(normalized, text.title() or default)


def _task_lifecycle_history(
    task: dict[str, Any],
    source_history: list[dict[str, Any]],
    *,
    task_index: int,
    task_count: int,
) -> list[dict[str, Any]]:
    """Return a deterministic, source-native status history for one Jira task.

    Existing source transitions are retained. Missing workflow edges are emitted
    as explicit Jira changelog entries so a terminal task cannot jump directly
    from intake to completion. A small deterministic subset of production-sized
    task sets exercises the reopen path without changing fixture-sized examples.
    """
    ordered_source = sorted(source_history, key=_timestamp)
    created_at = _timestamp(task)
    final_source = task.get("status") or (ordered_source[-1].get("to_status") if ordered_source else "To Do")
    final_status = _status_label(final_source)
    terminal = final_status in {"Done", "Closed", "Cancelled"}
    reopen = (
        terminal
        and final_status in {"Done", "Closed"}
        and task_count >= 100
        and int(sha256(f"task-reopen:{task.get('task_id') or task.get('id')}".encode()).hexdigest()[:4], 16)
        % 29
        == 0
    )
    records: list[dict[str, Any]] = []
    current = "To Do"
    previous_at = created_at

    def add_transition(
        target: str,
        occurred_at: datetime,
        *,
        source_record: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        nonlocal current, previous_at
        target = _status_label(target)
        if target == current:
            return
        if occurred_at <= previous_at:
            occurred_at = previous_at + timedelta(minutes=10)
        source_id = str((source_record or {}).get("status_event_id") or (source_record or {}).get("id") or "")
        records.append(
            {
                "task_id": str(task.get("task_id") or task.get("id")),
                "status_event_id": source_id
                or f"{task.get('task_id') or task.get('id')}-lifecycle-{len(records) + 1:02d}",
                "changed_at": occurred_at.isoformat(),
                "from_status": current,
                "to_status": target,
                "changed_by_employee_id": str(
                    (source_record or {}).get("changed_by_employee_id")
                    or (source_record or {}).get("author")
                    or task.get("assignee_employee_id")
                    or task.get("reporter_employee_id")
                    or "E001"
                ),
                "change_reason": reason
                or str((source_record or {}).get("change_reason") or "source_status_change"),
            }
        )
        current = target
        previous_at = occurred_at

    for source_record in ordered_source:
        target = _status_label(source_record.get("to_status") or source_record.get("status"))
        occurred_at = _timestamp(source_record)
        if target in {"Done", "Closed", "Cancelled"}:
            if current not in {"In Progress", "Review", "Validation", "Blocked"}:
                add_transition("In Progress", occurred_at - timedelta(minutes=20), reason="workflow_started")
            if target in {"Done", "Closed"} and current not in {"Review", "Validation"}:
                add_transition("Review", occurred_at - timedelta(minutes=10), reason="review_requested")
        add_transition(target, occurred_at, source_record=source_record)

    if not ordered_source:
        if final_status in {"In Progress", "Review", "Validation", "Blocked", "Done", "Closed", "Cancelled"}:
            add_transition("In Progress", created_at + timedelta(hours=1), reason="work_started")
        if final_status in {"Review", "Validation", "Done", "Closed"} and current not in {
            "Review",
            "Validation",
        }:
            add_transition("Review", created_at + timedelta(hours=2), reason="review_requested")
        if final_status in {"Done", "Closed", "Cancelled"}:
            add_transition(final_status, created_at + timedelta(hours=3), reason="source_terminal_state")
        elif final_status not in {"To Do", "Open"}:
            add_transition(final_status, created_at + timedelta(hours=3), reason="source_terminal_state")

    if terminal and current not in {"Done", "Closed", "Cancelled"}:
        if final_status in {"Done", "Closed"} and current not in {"Review", "Validation"}:
            add_transition("Review", previous_at + timedelta(minutes=10), reason="review_requested")
        add_transition(final_status, previous_at + timedelta(minutes=10), reason="source_terminal_state")
    if reopen and current in {"Done", "Closed"}:
        reopened_at = previous_at + timedelta(days=1)
        add_transition("Reopened", reopened_at, reason="acceptance_gap_reopened")
        add_transition("In Progress", reopened_at + timedelta(hours=1), reason="reopened_work_started")
        add_transition(current, reopened_at + timedelta(days=1), reason="reopened_work_completed")
    return records


def _jira_changelog(histories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render source status rows as cumulative Jira changelog entries."""
    return [
        {
            "id": str(history["status_event_id"]),
            "author": _source_account(history.get("changed_by_employee_id") or history.get("author")),
            "created": _timestamp(history).isoformat(),
            "items": [
                {
                    "field": "status",
                    "fromString": _status_label(history.get("from_status")),
                    "toString": _status_label(history.get("to_status")),
                }
            ],
        }
        for history in histories
    ]


def _task_specs(chains: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    history_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for history in chains.get("task_status_history", []):
        history_by_task[str(history["task_id"])].append(history)
    meetings = chains.get("meetings", [])

    specs: list[dict[str, Any]] = []
    tasks = list(chains.get("tasks", []))
    for task_index, task in enumerate(tasks):
        task = dict(task)
        task_id = str(task.get("task_id") or task.get("id"))
        decision_meeting = next(
            (
                str(meeting.get("meeting_id"))
                for meeting in meetings
                if meeting.get("meeting_id")
                and (
                    meeting.get("linked_incident_id") == task.get("linked_incident_id")
                    or meeting.get("linked_account_id") == task.get("linked_account_id")
                )
            ),
            None,
        )
        if decision_meeting:
            task["content"] = (
                f"{task.get('summary', task_id)}. This task implements the decision from meeting "
                f"{decision_meeting}. {_row_references(task)}"
            )
        histories = _task_lifecycle_history(
            task,
            history_by_task[task_id],
            task_index=task_index,
            task_count=len(tasks),
        )
        entity_id = immutable_id("redwood", "jira-redwood", "issue", task_id)
        payload_histories: list[dict[str, Any]] = []
        status = "To Do"
        states = [(task, status, list(payload_histories))]
        for history in histories:
            status = _status_label(history.get("to_status") or status)
            payload_histories.append(_jira_changelog([history])[0])
            states.append((history, status, list(payload_histories)))
        for revision, (snapshot_row, snapshot_status, snapshot_histories) in enumerate(states, start=1):
            original = _timestamp(snapshot_row)
            specs.append(
                {
                    "entity_id": entity_id,
                    "source": "jira-redwood",
                    "object_type": "issue",
                    "external_id": task_id,
                    "revision": revision,
                    "original": original,
                    "container": "ENG",
                    "acl": _event_acl("jira-redwood", "ENG", task),
                    "payload": _jira_payload(
                        key=task_id,
                        row=task,
                        status=snapshot_status,
                        histories=snapshot_histories,
                    ),
                }
            )
    return specs


def _single_revision_specs(chains: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    meeting_by_incident = {
        str(row.get("linked_incident_id")): str(row.get("meeting_id"))
        for row in chains.get("meetings", [])
        if row.get("linked_incident_id") and row.get("meeting_id")
    }
    task_by_id = {str(row.get("task_id")): row for row in chains.get("tasks", []) if row.get("task_id")}
    for ticket in chains.get("tickets", []):
        external_id = str(ticket.get("ticket_id") or ticket.get("id"))
        specs.append(
            {
                "entity_id": immutable_id("redwood", "jira-redwood", "issue", external_id),
                "source": "jira-redwood",
                "object_type": "issue",
                "external_id": external_id,
                "revision": 1,
                "original": _timestamp(ticket),
                "container": "ENG",
                "acl": _event_acl("jira-redwood", "ENG", ticket),
                "payload": _jira_payload(key=external_id, row=ticket, status="To Do", histories=[]),
            }
        )
    for incident in chains.get("incidents", []):
        external_id = str(incident.get("incident_id") or incident.get("id"))
        deployment_id = incident.get("linked_deployment_id")
        pull_request_id = incident.get("pull_request_id")
        payload = _jira_payload(key=external_id, row=incident, status="Done", histories=[])
        if deployment_id:
            payload["remotelinks"] = [
                {
                    "id": f"incident-{external_id}-cause",
                    "relationship": "is caused by",
                    "object": {
                        "url": f"https://wiki.redwood.example/deployments/{deployment_id}",
                        "title": f"Deployment {deployment_id}",
                    },
                }
            ]
        elif pull_request_id:
            payload["remotelinks"] = [
                {
                    "id": f"incident-{external_id}-cause",
                    "relationship": "is caused by",
                    "object": {
                        "url": (
                            "https://gitlab.redwood.example/platform/inference/-/merge_requests/"
                            f"{pull_request_id}"
                        ),
                        "title": f"Merge request {pull_request_id}",
                    },
                }
            ]
        specs.append(
            {
                "entity_id": immutable_id("redwood", "jira-redwood", "issue", external_id),
                "source": "jira-redwood",
                "object_type": "issue",
                "external_id": external_id,
                "revision": 1,
                "original": _timestamp(incident),
                "container": "ENG",
                "acl": _event_acl("jira-redwood", "ENG", incident),
                "payload": payload,
            }
        )
    for pull_request in chains.get("pull_requests", []):
        external_id = str(pull_request.get("pr_id") or pull_request.get("id"))
        task_id = str(pull_request.get("linked_task_id") or pull_request.get("task_id") or "")
        linked_task = task_by_id.get(task_id, {})
        decision_meeting = meeting_by_incident.get(str(linked_task.get("linked_incident_id") or ""))
        original = _timestamp(pull_request)
        content = str(pull_request.get("content") or f"Merge request {external_id}")
        topic = "reliability"
        topic_keywords = (
            ("deployment", "deployment"),
            ("billing", "billing"),
            ("security", "security"),
            ("connector", "connectors"),
            ("retrieval", "retrieval"),
            ("ranking", "ranking"),
            ("incident", "reliability"),
        )
        for keyword, candidate in topic_keywords:
            if keyword in content.casefold():
                topic = candidate
                break
        task_slug = re.sub(r"[^a-z0-9]+", "-", task_id.casefold()).strip("-") or external_id.casefold()
        change_path = f"src/platform/inference/{topic}/{task_slug}/implementation.py"
        changed_files = [change_path]
        reviewer = _source_account("E011")
        commit_id = sha256(f"{external_id}:source-native-commit".encode()).hexdigest()[:12]
        revision_times = [(1, original, "opened")]
        if pull_request.get("merged_at"):
            merged_at = normalize_timestamp(pull_request["merged_at"])
        else:
            merged_at = original + timedelta(days=2)
        if merged_at <= original:
            merged_at = original + timedelta(days=1)
        revision_times.append((2, merged_at, "merged"))
        for revision, occurred_at, state in revision_times:
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "gitlab-redwood", "merge_request", external_id),
                    "source": "gitlab-redwood",
                    "object_type": "merge_request",
                    "external_id": external_id,
                    "revision": revision,
                    "original": occurred_at,
                    "container": "platform/inference",
                    "acl": _event_acl("gitlab-redwood", "platform/inference", pull_request),
                    "payload": {
                        "id": external_id,
                        "iid": external_id,
                        "title": str(pull_request.get("content") or f"Merge request {external_id}"),
                        "description": "\n".join(
                            part
                            for part in (
                                f"Closes {task_id}." if task_id else "",
                                f"Implements the decision recorded in meeting {decision_meeting}."
                                if decision_meeting
                                else "",
                            )
                            if part
                        ),
                        "state": state,
                        "created_at": original.isoformat(),
                        "updated_at": occurred_at.isoformat(),
                        "merged_at": occurred_at.isoformat() if state == "merged" else None,
                        "merge_commit_sha": (
                            sha256(f"{external_id}:merge".encode()).hexdigest() if state == "merged" else None
                        ),
                        "reviewers": [reviewer],
                        "approvals": [
                            {
                                "user": reviewer,
                                "approved": state == "merged",
                                "at": (occurred_at - timedelta(hours=1)).isoformat(),
                            }
                        ],
                        "pipelines": [
                            {
                                "id": f"pipeline-{external_id}-1",
                                "attempt": 1,
                                "status": "success" if state == "merged" else "running",
                                "ref": f"feature/{task_slug}",
                                "stages": ["lint", "unit", "integration"],
                                "failed_checks": [],
                                "started_at": (occurred_at - timedelta(minutes=25)).isoformat(),
                                "finished_at": (occurred_at - timedelta(minutes=5)).isoformat(),
                                "recovery_commit": None,
                            }
                        ],
                        "changed_files": changed_files,
                        "diff_summary": {
                            "files_changed": len(changed_files),
                            "insertions": 14,
                            "deletions": 3,
                            "paths": list(changed_files),
                        },
                        "commits": [
                            {
                                "id": commit_id,
                                "message": f"Implement {topic} change in {change_path}",
                                "paths": list(changed_files),
                                "author": reviewer,
                            }
                        ],
                        "review_comments": [
                            {
                                "author": reviewer,
                                "body": f"Reviewed the {topic} change; verify {change_path} before merge.",
                                "resolved": state == "merged",
                            }
                        ],
                    },
                }
            )
    for message in chains.get("slack_messages", []):
        external_id = str(message.get("message_id") or message.get("id"))
        original = _timestamp(message)
        implicit_context = _implicit_context_payload(
            kind="slack_follow_up",
            target_stage=("task", "decision", "deployment", "incident", "deal")[
                int(sha256(external_id.encode()).hexdigest()[:4], 16) % 5
            ],
            account=str(message.get("account_id") or message.get("linked_account_id") or "the customer"),
            topic=str(message.get("topic") or "customer follow-up").replace("_", " "),
            variant=int(sha256(f"{external_id}:context".encode()).hexdigest()[:4], 16),
        )
        message_text = str(message.get("message_text") or message.get("content") or "")
        message_text = f"{message_text} The thread refers to {implicit_context['phrase']}.".strip()
        specs.append(
            {
                "entity_id": immutable_id("redwood", "slack-redwood", "message", external_id),
                "source": "slack-redwood",
                "object_type": "message",
                "external_id": external_id,
                "revision": 1,
                "original": original,
                "container": "eng-incidents",
                "acl": _event_acl("slack-redwood", "eng-incidents", message),
                "payload": {
                    "type": "message",
                    "channel": "eng-incidents",
                    "ts": original.timestamp(),
                    "user": str(message.get("author_employee_id") or message.get("author") or "system"),
                    "text": message_text,
                    "thread_ts": str(message.get("thread_id") or external_id),
                    "redwoodImplicitContext": implicit_context,
                },
            }
        )
    return specs


def _record_id(table: str, row: dict[str, Any], index: int) -> str:
    table_key = f"{table.removesuffix('s')}_id"
    for field in ("id", table_key):
        if row.get(field):
            return str(row[field])
    for field, value in row.items():
        if field.endswith("_id") and value:
            return str(value)
    return f"row-{index}"


def _confluence_payload(
    table: str,
    external_id: str,
    row: dict[str, Any],
    original: datetime,
    *,
    version_number: int = 1,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    title = str(row.get("title") or row.get("summary") or f"{table.replace('_', ' ').title()} {external_id}")
    content = str(row.get("content") or row.get("description") or json.dumps(row, sort_keys=True))
    author = str(row.get("author_employee_id") or row.get("owner_employee_id") or row.get("author") or "E001")
    adf = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": content}]}],
    }
    created = created_at or original
    return {
        "id": external_id,
        "title": title,
        "status": "current",
        "space": {"key": "ENG", "name": "Engineering"},
        "authorId": f"acct:{author}",
        "createdAt": created.isoformat(),
        "version": {
            "number": version_number,
            "createdAt": original.isoformat(),
            "authorId": f"acct:{author}",
        },
        "labels": {"results": [{"name": table}]},
        "body": {"atlas_doc_format": {"value": json.dumps(adf, ensure_ascii=False, sort_keys=True)}},
    }


def _graph_message_payload(external_id: str, row: dict[str, Any], original: datetime) -> dict[str, Any]:
    sender = str(row.get("employee_id") or row.get("author") or "system")
    employee_address = f"{sender.casefold()}@redwood.example"
    account_id = str(row.get("account_id") or "unknown").casefold()
    if account_id:
        customer_address = customer_contact_identity(
            account_id, str(row.get("account_name") or account_id)
        )["email"]
    else:
        customer_address = str(row.get("customer_email") or "customer.contact@customer.example")
    body = str(
        row.get("body")
        or row.get("content")
        or (
        f"Customer {row.get('account_id')} asks {COMPANY_NAME} to follow up on deal "
            f"{row.get('opportunity_id')}. Outcome: {row.get('outcome')}. "
            f"Please prepare commercial proposal QUOTE-{row.get('opportunity_id')} and, "
            f"after approval, invoice INV-{row.get('opportunity_id')}."
        )
    )
    topic = str(row.get("topic") or row.get("activity_type") or "account review").replace("_", " ")
    subject_seed = int(sha256(f"{external_id}:subject".encode()).hexdigest()[:8], 16)
    subject = str(
        row.get("subject") or BUSINESS_EMAIL_SUBJECTS[subject_seed % len(BUSINESS_EMAIL_SUBJECTS)]
    ).format(topic=topic)
    if row.get("account_name") and not row.get("subject"):
        subject = f"{subject} for {row['account_name']}"
    return {
        "id": external_id,
        "internetMessageId": external_id,
        "conversationId": f"rw-source-{sha256(external_id.encode()).hexdigest()[:16]}",
        "subject": subject,
        "from": {
            "emailAddress": {
                "address": customer_address,
                "name": row.get("account_name") or row.get("account_id"),
            }
        },
        "toRecipients": [{"emailAddress": {"address": employee_address, "name": sender}}],
        "ccRecipients": [],
        "receivedDateTime": original.isoformat(),
        "sentDateTime": original.isoformat(),
        "body": {"contentType": "text", "content": body},
        "attachments": [],
    }


def _gmail_message_payload(external_id: str, row: dict[str, Any], original: datetime) -> dict[str, Any]:
    sender = str(row.get("employee_id") or row.get("author") or "system")
    address = f"{sender.casefold()}@redwood.example"
    content = str(row.get("body") or row.get("content") or row.get("outcome") or "")
    return {
        "id": external_id,
        "threadId": f"thread-{external_id}",
        "labelIds": ["INBOX"],
        "snippet": content[:160],
        "internalDate": str(int(original.timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": str(row.get("subject") or external_id)},
                {"name": "From", "value": address},
                {"name": "To", "value": address},
            ],
            "body": {"size": len(content), "data": content},
        },
    }


def _xwiki_payload(external_id: str, row: dict[str, Any], original: datetime) -> dict[str, Any]:
    title = str(row.get("title") or row.get("document_type") or external_id)
    content = str(row.get("content") or row.get("root_cause_summary") or json.dumps(row, sort_keys=True))
    return {
        "id": external_id,
        "title": title,
        "space": "Regulations",
        "author": str(row.get("owner_employee_id") or row.get("author") or "E001"),
        "created": original.isoformat(),
        "version": "1.1",
        "content": content,
        "properties": {"source_table": row.get("document_type", "operational")},
    }


def _structured_meeting_protocol(payload: dict[str, Any], world_at: datetime) -> None:
    """Normalize every transcript protocol to a readable structured contract."""
    if not isinstance(payload.get("participants"), list):
        return
    raw_protocol = payload.get("protocol")
    if isinstance(raw_protocol, str):
        try:
            raw_protocol = json.loads(raw_protocol)
        except json.JSONDecodeError:
            raw_protocol = {"decision": raw_protocol}
    protocol = dict(raw_protocol) if isinstance(raw_protocol, dict) else {}
    attendees = [
        {
            "employeeId": str(employee_id),
            "principal": f"user:{str(employee_id).casefold()}",
        }
        for employee_id in dict.fromkeys(str(value) for value in payload["participants"] if value)
    ]
    decision = str(protocol.get("decision") or payload.get("summary") or "Record the meeting outcome.")
    owner = str(protocol.get("owner") or (payload["participants"] or ["E001"])[0])
    follow_up = str(protocol.get("follow_up") or protocol.get("followUp") or "meeting follow-up")
    scheduled_at = str(payload.get("date") or world_at.isoformat())
    agenda_topic = str(payload.get("summary") or payload.get("title") or "Operational review")
    due_date = protocol.get("due_date") or protocol.get("dueDate")
    action = {
        "id": follow_up,
        "owner": owner,
        "status": "open",
        "dueAt": str(due_date) if due_date else scheduled_at,
    }
    protocol.update(
        {
            "attendees": attendees,
            "agenda": [{"topic": agenda_topic, "context": str(payload.get("title") or agenda_topic)}],
            "decisions": [{"decision": decision, "owner": owner, "timestamp": scheduled_at}],
            "owners": [
                {
                    "employeeId": owner,
                    "principal": f"user:{owner.casefold()}",
                    "responsibility": decision,
                }
            ],
            "actions": [action],
            "timestamps": {
                "scheduledAt": scheduled_at,
                "recordedAt": world_at.isoformat(),
                "updatedAt": world_at.isoformat(),
            },
            "decision": decision,
            "owner": owner,
            "follow_up": follow_up,
        }
    )
    payload["protocol"] = MeetingProtocol.model_validate(protocol).model_dump(mode="json", by_alias=True)


def _attachment_document_meta(
    attachment: dict[str, Any], *, parent_event_id: str, parent_external_id: str
) -> dict[str, Any]:
    """Return deterministic metadata linking an attachment to its parent message."""
    attachment_id = str(attachment.get("id") or attachment.get("attachmentId") or "attachment")
    filename = str(attachment.get("name") or attachment.get("filename") or attachment_id)
    mime_type = str(attachment.get("contentType") or attachment.get("mimeType") or "application/octet-stream")
    source_document = str(filename.rsplit(".", maxsplit=1)[0])
    checksum = sha256(
        json.dumps(
            {key: value for key, value in attachment.items() if key != "documentMeta"},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return DocumentMeta(
        attachmentId=attachment_id,
        filename=filename,
        mimeType=mime_type,
        sourceDocument=source_document,
        sourceDocumentId=source_document,
        checksum=checksum,
        parentEventId=parent_event_id,
        parentExternalId=parent_external_id,
    ).model_dump(mode="json", by_alias=True)


def _add_attachment_metadata(payload: object, *, parent_event_id: str, parent_external_id: str) -> None:
    """Attach documentMeta to all nested provider attachment records."""
    if isinstance(payload, dict):
        attachments = payload.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if isinstance(attachment, dict):
                    attachment["documentMeta"] = _attachment_document_meta(
                        attachment,
                        parent_event_id=parent_event_id,
                        parent_external_id=parent_external_id,
                    )
        for value in payload.values():
            _add_attachment_metadata(
                value,
                parent_event_id=parent_event_id,
                parent_external_id=parent_external_id,
            )
    elif isinstance(payload, list):
        for value in payload:
            _add_attachment_metadata(
                value,
                parent_event_id=parent_event_id,
                parent_external_id=parent_external_id,
            )


def _additional_provider_specs(
    chains: dict[str, list[dict[str, Any]]], financial_specs: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Materialize native payloads for provider adapters backed by Solstice tables."""
    specs: list[dict[str, Any]] = []

    for task in chains.get("tasks", []):
        try:
            original = _timestamp(task)
        except ValueError:
            continue
        external_id = str(task.get("task_id") or task.get("id"))
        specs.append(
            {
                "entity_id": immutable_id("redwood", "linear-redwood", "issue", external_id),
                "source": "linear-redwood",
                "object_type": "issue",
                "external_id": external_id,
                "revision": 1,
                "original": original,
                "container": "engineering",
                "acl": _event_acl("linear-redwood", "engineering", task),
                "payload": {
                    "id": external_id,
                    "identifier": external_id,
                    "title": str(task.get("summary") or task.get("content") or external_id),
                    "description": str(task.get("content") or ""),
                    "status": str(task.get("status") or "Todo"),
                    "team": str(task.get("team_id") or "engineering"),
                    "priority": str(task.get("priority") or "medium"),
                },
            }
        )

    account_names = {
        str(row.get("account_id")): str(row.get("account_name_synthetic") or row.get("account_id"))
        for row in chains.get("accounts", [])
    }
    activity_by_opportunity: dict[str, str] = {}
    for activity in sorted(chains.get("crm_activities", []), key=_timestamp):
        opportunity_id = str(activity.get("opportunity_id") or "")
        if opportunity_id and opportunity_id not in activity_by_opportunity:
            activity_by_opportunity[opportunity_id] = str(activity.get("activity_id"))

    for table in ("accounts", "opportunities", "renewals", "support_tickets"):
        for row in chains.get(table, []):
            try:
                original = _timestamp(row)
            except ValueError:
                original = PROFILE_START.replace(tzinfo=UTC)
            raw_id = _record_id(table, row, 0)
            object_type = {
                "accounts": "company",
                "opportunities": "deal",
                "renewals": "deal",
                "support_tickets": "ticket",
            }[table]
            external_id = raw_id
            properties = {key: value for key, value in row.items() if key not in {"id"}}
            account_id = str(row.get("account_id") or raw_id)
            properties["account_name"] = account_names.get(account_id, account_id)
            properties["description"] = (
                f"{object_type.title()} {raw_id} for customer {account_names.get(account_id, account_id)} "
                f"({account_id}). References: {json.dumps(row, sort_keys=True)}"
            )
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "hubspot-redwood", object_type, external_id),
                    "source": "hubspot-redwood",
                    "object_type": object_type,
                    "external_id": external_id,
                    "revision": 1,
                    "original": original,
                    "container": "COMMERCIAL",
                    "acl": _event_acl("hubspot-redwood", "COMMERCIAL", row),
                    "payload": {
                        "id": external_id,
                        "properties": properties,
                        "archived": False,
                        "createdAt": original.isoformat(),
                        "updatedAt": original.isoformat(),
                    },
                }
            )

            if table == "opportunities":
                amount = float(row.get("contract_value_usd") or 0)
                quote_id = f"QUOTE-{raw_id}"
                activity_id = activity_by_opportunity.get(raw_id)
                message_id = f"<crm-{activity_id}@redwood.example>" if activity_id else "customer email"
                quote_text = (
                    f"Commercial proposal {quote_id} for customer "
                    f"{account_names.get(account_id, account_id)} "
                    f"and deal {raw_id}. Amount USD {amount:,.0f}. Prepared after customer email "
                    f"{message_id}."
                )
                quote_at = original - timedelta(days=5)
                quote_properties = {
                    "hs_object_id": quote_id,
                    "deal_id": raw_id,
                    "account_id": account_id,
                    "amount": amount,
                    "currency": "USD",
                    "source_email_id": message_id,
                    "status": "accepted" if row.get("stage") == "closed_won" else "sent",
                    "description": quote_text,
                }
                specs.append(
                    {
                        "entity_id": immutable_id("redwood", "hubspot-redwood", "quote", quote_id),
                        "source": "hubspot-redwood",
                        "object_type": "quote",
                        "external_id": quote_id,
                        "revision": 1,
                        "original": quote_at,
                        "container": "COMMERCIAL",
                        "acl": _event_acl("hubspot-redwood", "COMMERCIAL", row),
                        "payload": {
                            "id": quote_id,
                            "properties": quote_properties,
                            "archived": False,
                            "createdAt": quote_at.isoformat(),
                            "updatedAt": quote_at.isoformat(),
                        },
                    }
                )
                if row.get("stage") == "closed_won":
                    invoice_id = f"INV-{raw_id}"
                    invoice_at = original + timedelta(days=2)
                    invoice_text = (
                        f"Invoice {invoice_id} issued to {account_names.get(account_id, account_id)} "
                        f"for USD {amount:,.0f}. It bills accepted proposal {quote_id} and deal {raw_id}."
                    )
                    specs.append(
                        {
                            "entity_id": immutable_id("redwood", "hubspot-redwood", "invoice", invoice_id),
                            "source": "hubspot-redwood",
                            "object_type": "invoice",
                            "external_id": invoice_id,
                            "revision": 1,
                            "original": invoice_at,
                            "container": "FINANCE",
                            "acl": _event_acl("hubspot-redwood", "FINANCE", row),
                            "payload": {
                                "id": invoice_id,
                                "properties": {
                                    "account_id": account_id,
                                    "deal_id": raw_id,
                                    "quote_id": quote_id,
                                    "amount": amount,
                                    "currency": "USD",
                                    "status": "paid",
                                    "description": invoice_text,
                                },
                                "archived": False,
                                "createdAt": invoice_at.isoformat(),
                                "updatedAt": invoice_at.isoformat(),
                            },
                        }
                    )

    ordered_meetings = sorted(chains.get("meetings", []), key=_timestamp)
    meeting_decisions = (
        "the owner will implement the follow-up",
        "the team will validate the evidence before changing scope",
        "support will confirm the customer impact and engineering will own the fix",
        "the release manager will document the rollback boundary",
        "finance and sales will reconcile the commercial consequence",
        "security will approve the next checkpoint before implementation",
    )
    for meeting_index, meeting in enumerate(ordered_meetings):
        try:
            original = _timestamp(meeting)
        except ValueError:
            continue
        external_id = str(meeting.get("meeting_id") or meeting.get("id"))
        discussion_modes = (
            "the evidence review opened with the customer impact timeline",
            "the owner compared the current signal with the previous baseline",
            "the group challenged the proposed scope and requested a narrower rollout",
            "support brought the latest account history and engineering checked the runbook",
            "finance asked for the commercial consequence before implementation",
            "security requested an explicit boundary for the sensitive records",
            "the release manager walked through rollback options and operational risk",
            "the team reconciled the incident summary with the customer conversation",
            "the participants reviewed a disputed status and named the source of truth",
            "the product lead contrasted two alternatives using the attached evidence",
            "the engineers tested the proposed follow-up against the service contract",
            "the account owner connected the request to the renewal checkpoint",
            "the group inspected the dependency graph before accepting the change",
            "the facilitator asked each owner to state the next observable result",
            "the reviewers compared expected and observed delivery dates",
            "the team isolated the smallest reversible action for the next cycle",
        )
        alternatives = (
            "keep the existing behavior and monitor it",
            "ship a limited fix behind a release gate",
            "defer the change until the missing evidence arrives",
            "reopen the task and collect a second customer confirmation",
            "split the work between support and engineering",
            "use the prior approved baseline as the rollback point",
            "ask finance to validate the amount before proceeding",
            "route the risk to the service owner for a final sign-off",
        )
        concerns = (
            "latency budget",
            "customer trust",
            "rollback safety",
            "ownership clarity",
            "billing accuracy",
            "access boundaries",
            "evidence freshness",
        )
        agenda_topics = (
            "contract boundaries",
            "data lineage",
            "release readiness",
            "customer escalation",
            "capacity planning",
            "access review",
            "forecast variance",
            "support ownership",
            "test evidence",
            "incident scope",
            "renewal timing",
            "service reliability",
            "pricing impact",
            "workflow migration",
            "quality gates",
            "provider compatibility",
            "rollback controls",
            "account health",
            "privacy review",
            "operational debt",
            "benchmark drift",
            "stakeholder alignment",
            "invoice reconciliation",
            "supplier risk",
            "meeting follow-up",
            "decision ownership",
            "customer evidence",
            "deployment guardrails",
            "task aging",
            "commercial priority",
            "observability coverage",
        )
        mode = discussion_modes[(meeting_index * 7) % len(discussion_modes)]
        alternative = alternatives[(meeting_index * 11) % len(alternatives)]
        concern = concerns[(meeting_index * 17) % len(concerns)]
        agenda = agenda_topics[(meeting_index * 19) % len(agenda_topics)]
        account = str(meeting.get("linked_account_id") or "the account")
        implicit_context = _implicit_context_payload(
            kind="meeting_follow_up",
            target_stage=("decision", "task", "deployment", "incident", "deal")[meeting_index % 5],
            account=account,
            topic=agenda,
            variant=meeting_index,
        )
        transcript = (
            f"In meeting {external_id}, {mode}. Participants reviewed account "
            f"{meeting.get('linked_account_id')} and incident {meeting.get('linked_incident_id') or 'none'}. "
            f"The team considered whether to {alternative}, with special attention to {concern} "
            f"and agenda topic {agenda}. One alternative remained open for comparison; "
            f"they decided that "
            f"{meeting_decisions[meeting_index % len(meeting_decisions)]}. "
            f"The written decision is in {meeting.get('notes_document_id')}. Follow-up owner is "
            f"{meeting.get('owner_employee_id')}; the next check is tied to the recorded evidence. "
            f"The discussion also refers to {implicit_context['phrase']}."
        )
        if meeting_index > 0:
            transcript += (
                " Following the previous customer sync, the team reconfirmed the earlier decision "
                "without repeating its meeting identifier."
            )
        specs.append(
            {
                "entity_id": immutable_id("redwood", "fireflies-redwood", "transcript", external_id),
                "source": "fireflies-redwood",
                "object_type": "transcript",
                "external_id": external_id,
                "revision": 1,
                "original": original,
                "container": "meetings",
                "acl": _event_acl("fireflies-redwood", "meetings", meeting),
                "payload": {
                    "id": external_id,
                    "title": str(meeting.get("title") or external_id),
                    "date": original.isoformat(),
                    "participants": [str(meeting.get("owner_employee_id"))],
                    "summary": str(meeting.get("meeting_type") or "operational meeting"),
                    "transcript": transcript,
                    "redwoodImplicitContext": implicit_context,
                },
            }
        )

    health_rows = sorted(chains.get("account_health_daily", []), key=_timestamp)
    health_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in health_rows:
        health_by_account[str(row.get("account_id"))].append(row)
    all_financial_specs = financial_specs or []
    financial_start = min(
        (spec["original"] for spec in all_financial_specs),
        default=PROFILE_START.replace(tzinfo=UTC),
    )
    financial_end = max((spec["original"] for spec in all_financial_specs), default=financial_start)

    def financial_as_of(cutoff: datetime) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for spec in all_financial_specs:
            if spec["original"] > cutoff:
                continue
            previous = latest.get(str(spec["entity_id"]))
            if previous is None or int(spec["revision"]) > int(previous["revision"]):
                latest[str(spec["entity_id"])] = spec
        return list(latest.values())

    def rows_of(rows: list[dict[str, Any]], object_type: str) -> list[dict[str, Any]]:
        return [spec["payload"]["properties"] for spec in rows if spec["object_type"] == object_type]

    for report_index in range(26):
        rows = [
            account_rows[min(len(account_rows) - 1, report_index)]
            for account_rows in health_by_account.values()
            if account_rows
        ]
        if not rows:
            continue
        original = PROFILE_START.replace(tzinfo=UTC) + timedelta(days=7 * report_index)
        financial_cutoff = financial_start + (financial_end - financial_start) * (report_index / 25)
        financial_rows = financial_as_of(financial_cutoff)

        deal_rows = rows_of(financial_rows, "deal")
        invoice_rows = rows_of(financial_rows, "invoice")
        payment_rows = rows_of(financial_rows, "payment")
        contract_rows = rows_of(financial_rows, "contract")
        amendment_rows = rows_of(financial_rows, "amendment")
        supplier_rows = rows_of(financial_rows, "supplier_invoice")
        operating_rows = rows_of(financial_rows, "operating_cost")
        forecast_total = sum(float(row.get("forecast_amount") or 0) for row in deal_rows)
        actual_total = sum(
            float(row.get("amount") or 0) for row in invoice_rows if row.get("status") == "paid"
        )
        arr_total = sum(float(row.get("arr") or 0) for row in contract_rows if row.get("status") == "active")
        mrr_total = arr_total / 12
        receivable_total = sum(float(row.get("balance") or 0) for row in invoice_rows)
        overdue_total = sum(
            float(row.get("balance") or 0) for row in invoice_rows if row.get("status") == "overdue"
        )
        paid_total = sum(float(row.get("cumulative_amount") or 0) for row in payment_rows)
        churned = [row for row in contract_rows if row.get("status") == "churned"]
        active = [row for row in contract_rows if row.get("status") == "active"]
        churn_rate = len(churned) / max(1, len(active) + len(churned))
        expansion_total = sum(
            float(row.get("delta_arr") or 0) for row in amendment_rows if row.get("status") == "approved"
        )
        discount_total = sum(float(row.get("discount") or 0) for row in deal_rows)
        margin = sum(float(row.get("margin") or 0) for row in active) / max(1, len(active))
        supplier_total = sum(
            float(row.get("amount") or 0) for row in supplier_rows if row.get("status") == "paid"
        )
        operating_total = sum(
            float(row.get("amount") or 0) for row in operating_rows if row.get("status") == "actual"
        )
        iso = original.isocalendar()
        year, week = iso.year, iso.week
        active_accounts = len({str(row.get("account_id")) for row in rows})
        average_health = sum(float(row.get("health_score") or 0) for row in rows) / len(rows)
        external_id = f"north-star-{year}-W{week:02d}"
        source_ids = ", ".join(str(row.get("account_health_id")) for row in rows)
        source_records = [
            {
                "id": str(row.get("account_health_id")),
                "type": "account_health_daily",
                "accountId": str(row.get("account_id")),
                "recordedAt": _timestamp(row).isoformat(),
            }
            for row in rows
        ]
        source_records.extend(
            {
                "id": str(spec.get("external_id")),
                "type": str(spec.get("object_type")),
                "revision": int(spec.get("revision", 1)),
            }
            for spec in financial_rows
        )
        financials = {
            "arr_usd": arr_total,
            "mrr_usd": mrr_total,
            "forecast_revenue_usd": forecast_total,
            "actual_revenue_usd": actual_total,
            "supplier_cost_usd": supplier_total,
            "operating_cost_usd": operating_total,
            "accounts_receivable_usd": receivable_total,
            "overdue_usd": overdue_total,
            "paid_usd": paid_total,
            "churn_rate": churn_rate,
            "expansion_usd": expansion_total,
            "discount_usd": discount_total,
            "gross_margin_pct": margin,
            "dso_days": sum(float(row.get("days_to_collect") or 0) for row in invoice_rows)
            / max(1, sum(row.get("days_to_collect") is not None for row in invoice_rows)),
        }
        report = NorthStarReport(
            reportPeriod={
                "start": original.date().isoformat(),
                "end": (original + timedelta(days=6)).date().isoformat(),
                "label": f"{year}-W{week:02d}",
            },
            owner={"employeeId": "E001", "principal": "user:e001"},
            sourceRecords=source_records,
            metrics=[
                {
                    "id": "active_customers",
                    "value": active_accounts,
                    "unit": "customers",
                    "sourceRecordIds": [
                        record["id"] for record in source_records if record["type"] == "account_health_daily"
                    ],
                },
                {
                    "id": "average_account_health",
                    "value": round(average_health, 4),
                    "unit": "score",
                    "sourceRecordIds": [
                        record["id"] for record in source_records if record["type"] == "account_health_daily"
                    ],
                },
                *[
                    {
                        "id": metric_id,
                        "value": value,
                        "unit": (
                            "ratio"
                            if metric_id.endswith(("rate", "pct"))
                            else "USD"
                            if metric_id.endswith("_usd")
                            else "days"
                        ),
                        "sourceRecordIds": [
                            record["id"]
                            for record in source_records
                            if record["type"] not in {"account_health_daily"}
                        ],
                    }
                    for metric_id, value in financials.items()
                ],
            ],
            financials=financials,
            reconciliation={
                "financials": (
                    financials["mrr_usd"] * 12 == financials["arr_usd"]
                    and financials["actual_revenue_usd"] <= financials["forecast_revenue_usd"]
                    or financials["forecast_revenue_usd"] == 0
                ),
                "checks": {
                    "mrr_from_arr": financials["mrr_usd"] * 12 == financials["arr_usd"],
                    "actual_not_above_forecast": financials["actual_revenue_usd"]
                    <= financials["forecast_revenue_usd"]
                    or financials["forecast_revenue_usd"] == 0,
                    "receivable_from_invoices": financials["accounts_receivable_usd"]
                    == sum(float(row.get("balance") or 0) for row in invoice_rows),
                },
                "sourceRecordIds": [record["id"] for record in source_records],
            },
        ).model_dump(mode="json", by_alias=True)
        content = (
            f"Weekly North Star report {year}-W{week:02d}. Active customers: {active_accounts}. "
            f"Average account health: {average_health:.2f}. Source health records: {source_ids}. "
            f"ARR {arr_total:.2f}; MRR {mrr_total:.2f}; Forecast {forecast_total:.2f}; "
            f"Actual {actual_total:.2f}; Supplier costs {supplier_total:.2f}; "
            f"Operating costs {operating_total:.2f}; Accounts receivable {receivable_total:.2f}; "
            f"Overdue {overdue_total:.2f}; Paid {paid_total:.2f}; Churn {churn_rate:.4f}; "
            f"Expansion {expansion_total:.2f}; Discount {discount_total:.2f}; Margin {margin:.4f}."
        )
        north_star_payload = _confluence_payload(
            "weekly_report",
            external_id,
            {"title": f"Weekly North Star {year}-W{week:02d}", "content": content},
            original,
        )
        north_star_payload.update(report)
        specs.append(
            {
                "entity_id": immutable_id("redwood", "confluence-redwood", "page", external_id),
                "source": "confluence-redwood",
                "object_type": "page",
                "external_id": external_id,
                "revision": 1,
                "original": original,
                "calendar": "weekly",
                "container": "LEADERSHIP",
                "acl": _event_acl("confluence-redwood", "LEADERSHIP"),
                "payload": north_star_payload,
            }
        )

    for activity in chains.get("crm_activities", []):
        try:
            original = _timestamp(activity)
        except ValueError:
            continue
        external_id = f"gmail-{_record_id('crm_activities', activity, 0)}"
        specs.append(
            {
                "entity_id": immutable_id("redwood", "gmail-redwood", "message", external_id),
                "source": "gmail-redwood",
                "object_type": "message",
                "external_id": external_id,
                "revision": 1,
                "original": original,
                "container": "mail",
                "acl": _event_acl("gmail-redwood", "mail", activity),
                "payload": _gmail_message_payload(external_id, activity, original),
            }
        )

    for document in chains.get("documents", []):
        try:
            original = _timestamp(document)
        except ValueError:
            continue
        external_id = f"drive-{_record_id('documents', document, 0)}"
        specs.append(
            {
                "entity_id": immutable_id("redwood", "google-drive-redwood", "file", external_id),
                "source": "google-drive-redwood",
                "object_type": "file",
                "external_id": external_id,
                "revision": 1,
                "original": original,
                "container": "drive",
                "acl": _event_acl("google-drive-redwood", "drive", document),
                "payload": {
                    "id": external_id,
                    "name": str(document.get("title") or external_id),
                    "mimeType": "application/vnd.google-apps.document",
                    "description": str(document.get("document_type") or "operational document"),
                    "modifiedTime": original.isoformat(),
                    "webViewLink": f"https://drive.google.com/open?id={external_id}",
                },
            }
        )
    return specs


def _confluence_specs(chains: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    handled_tables = {
        "tickets",
        "tasks",
        "task_status_history",
        "pull_requests",
        "incidents",
        "slack_messages",
    }
    specs: list[dict[str, Any]] = []
    for table, rows in sorted(chains.items()):
        if table in handled_tables:
            continue
        for index, row in enumerate(rows, start=1):
            try:
                original = _timestamp(row)
            except ValueError:
                continue
            external_id = f"{table}-{_record_id(table, row, index)}"
            if table == "crm_activities":
                source, object_type = "mail-redwood", "message"
                external_id = f"<crm-{_record_id(table, row, index)}@redwood.example>"
                payload = _graph_message_payload(external_id, row, original)
                acl = _event_acl(source, "COMMERCIAL", row)
            elif table == "postmortems":
                source, object_type = "xwiki-redwood", "page"
                payload = _xwiki_payload(external_id, row, original)
                acl = _event_acl(source, "ENG", row)
            else:
                source, object_type = "confluence-redwood", "page"
                payload = _confluence_payload(table, external_id, row, original)
                acl = _event_acl(source, "ENG", row)
            revision_times = [(1, original)]
            if table == "documents" and row.get("updated_at"):
                updated_at = normalize_timestamp(row["updated_at"])
                if updated_at > original:
                    revision_times.append((2, updated_at))
            for revision, occurred_at in revision_times:
                revision_payload = payload
                if source == "confluence-redwood":
                    revision_payload = _confluence_payload(
                        table,
                        external_id,
                        row,
                        occurred_at,
                        version_number=revision,
                        created_at=original,
                    )
                specs.append(
                    {
                        "entity_id": immutable_id("redwood", source, object_type, external_id),
                        "source": source,
                        "object_type": object_type,
                        "external_id": external_id,
                        "revision": revision,
                        "original": occurred_at,
                        "container": "ENG",
                        "acl": acl,
                        "payload": revision_payload,
                    }
                )
    return specs


ENTERPRISE_TASK_FOCI = (
    "freshness watermark",
    "tenant boundary",
    "query replay",
    "source cursor",
    "evidence retention",
    "ranking calibration",
    "fallback isolation",
    "operator signal",
    "webhook ordering",
    "connector quota",
    "index checkpoint",
    "provider timeout",
    "schema compatibility",
    "retry budget",
    "ingestion watermark",
    "export provenance",
    "permission cache",
    "role transition",
    "access revocation",
    "redaction boundary",
    "audit receipt",
    "key rotation",
    "credential expiry",
    "tenant quarantine",
    "regional failover",
    "queue pressure",
    "latency budget",
    "release marker",
    "rollback trigger",
    "deployment evidence",
    "incident replay",
    "recovery checkpoint",
    "billing usage",
    "invoice matching",
    "contract amendment",
    "renewal forecast",
    "margin attribution",
    "payment reconciliation",
    "receivables aging",
    "proposal approval",
    "customer adoption",
    "support handoff",
    "account health",
    "escalation owner",
    "service objective",
    "workload shaping",
    "capacity guard",
    "evaluation slice",
    "golden query",
    "regression gate",
    "benchmark variance",
    "retrieval trace",
    "document lineage",
    "chunk boundary",
    "metadata repair",
    "language coverage",
    "parser fallback",
    "security review",
    "compliance export",
    "data residency",
    "privacy exception",
    "governance decision",
    "leadership checkpoint",
    "strategic alignment",
)

ORDINARY_PROBLEM_VARIANTS = (
    "customer reconciliation loses tenant context after a retry resumes",
    "provider callbacks arrive out of order and leave the operational state ambiguous",
    "the release gate cannot distinguish a recoverable failure from a customer-visible regression",
    "permission changes remain stale in the serving path after an account update",
    "usage evidence is split across source systems and cannot support a defensible handoff",
    "the fallback path hides the original failure signal from the operator",
    "a schema change can strand records between the ingestion and export boundaries",
    "the current control does not preserve a traceable decision through the customer workflow",
    "repeated work is reprocessed when the source cursor advances after a partial failure",
    "the support team lacks a reliable signal for when the operational promise is at risk",
    "a policy edge case causes inconsistent treatment across tenants and environments",
    "the recovery path changes behavior without leaving evidence for the next reviewer",
    "the handoff between engineering and operations drops the acceptance evidence",
    "a delayed source update makes the dashboard disagree with the authoritative record",
    "the service cannot explain why a change passed one boundary and failed the next",
    "a customer-impacting exception is handled manually instead of through a repeatable control",
)

ORDINARY_CONTEXT_VARIANTS = (
    "The triggering evidence is a source request that crosses a provider boundary.",
    "The work follows a review of the customer path and the associated operational record.",
    "The affected flow combines an asynchronous event with a stateful engineering handoff.",
    "The current implementation is safe for the happy path but weak at the recovery boundary.",
    "The evidence spans the serving path, its audit trail, and the release review.",
    "The request is ordinary maintenance with a clear customer promise and a bounded blast radius.",
    "The change must be understood by both the implementing engineer and the on-call owner.",
    "The source record identifies a repeatable control gap rather than a one-off data correction.",
    "The workflow is already in use, so the change must preserve existing consumers while closing the gap.",
    "The issue was observed during routine review of a customer-facing engineering boundary.",
    "The operational context includes a dependency on a shared service or provider contract.",
    "The evidence is sufficient to define a testable change without inventing a new product scope.",
    "The change belongs to a governed workstream and must remain traceable through review.",
    "The request combines a measurable service behavior with an explicit ownership handoff.",
    "The affected path has an established rollback or containment expectation.",
    "The work is bounded to the named control surface and its source-visible evidence.",
)

ORDINARY_OUTCOME_VARIANTS = (
    "leave the control in a state that an operator can verify and safely hand off",
    "make the customer-visible behavior consistent across retries and normal processing",
    "produce an auditable result that supports the governing decision",
    "restore the expected boundary without widening the change beyond the approved scope",
    "give reviewers direct evidence that the failure path is covered",
    "make the next state transition deterministic and explainable",
    "reduce manual intervention while preserving a clear recovery path",
    "return a source-visible result that downstream deployment checks can consume",
    "keep the affected records aligned with the authoritative source after recovery",
    "make the ownership handoff actionable for both engineering and operations",
    "close the control gap with a repeatable test and an explicit release signal",
    "enable a bounded rollout whose outcome can be compared with the source evidence",
    "preserve the customer promise while making the operational trade-off explicit",
    "turn the observed exception into a documented and reviewable control",
    "give the team a reliable signal for acceptance, rollback, or escalation",
    "complete the ordinary work item with evidence that remains useful after release",
)

ORDINARY_OWNER_VARIANTS = (
    "The assigned engineering owner must trace the change from evidence through review and handoff.",
    (
        "The owner is responsible for reproducing the boundary, recording the decision, "
        "and coordinating validation."
    ),
    "The owner must keep the source reference current and explain any deviation before approval.",
    "The owner is accountable for the implementation, the evidence package, and the release handoff.",
    "The owner must align the service behavior with the agreed customer and operational expectation.",
    "The owner is responsible for isolating the failure mode before changing the shared control.",
    (
        "The owner must make the acceptance evidence legible to a reviewer who was not in "
        "the original discussion."
    ),
    "The owner is accountable for preserving rollback readiness while the change is evaluated.",
    "The owner must reconcile the source record with the resulting state before marking the work complete.",
    ("The owner is responsible for naming the next operator action if the acceptance check does not pass."),
    "The owner must verify the dependent boundary and document the observed result in the review record.",
    "The owner is accountable for the decision trace, not only for the code change itself.",
    "The owner must coordinate the change with the service or provider boundary named in the evidence.",
    (
        "The owner is responsible for a focused implementation that can be reviewed and "
        "rolled back independently."
    ),
    "The owner must confirm that the customer-impacting behavior is covered before release approval.",
    "The owner is accountable for closing the evidence loop across implementation, validation, and handoff.",
)

ORDINARY_CONSTRAINT_VARIANTS = (
    "Keep the change backward compatible and do not alter unrelated tenant behavior.",
    "Preserve source ordering and avoid introducing a second authority for the same state.",
    "Do not widen permissions, retention, or customer scope as part of this work.",
    "Keep the rollout reversible and preserve the existing containment path.",
    "Use the existing provider contract and avoid a breaking migration in this work item.",
    "Keep the implementation within the approved service boundary and the available review evidence.",
    "Preserve auditability even when the recovery path is exercised.",
    "Do not hide failed checks or replace the source evidence with a synthetic success signal.",
    "Keep the change deterministic under retries, duplicate delivery, and delayed observation.",
    "Protect the current customer promise while the operational control is being tightened.",
    "Avoid coupling the fix to a single deployment or reviewer.",
    "Keep the acceptance path usable by on-call staff outside the original team.",
    "Preserve the dependency contract and document any required sequencing explicitly.",
    "Do not change numeric calculations or access policy outside the named boundary.",
    "Keep the scope limited to the observed failure mode and its source-native evidence.",
    "Retain a clear rollback point and a reviewable explanation for any exception.",
)

ORDINARY_ACCEPTANCE_VARIANTS = (
    "Confirm the reproduced boundary, pass the focused regression check, and attach the observed result.",
    "Verify normal and recovery paths, reconcile the source record, and obtain reviewer approval.",
    "Demonstrate that the expected state is reached without duplicate work or lost evidence.",
    "Show the customer-visible behavior, the operational signal, and the rollback decision in review.",
    "Pass the contract check, preserve the audit trail, and record the owner handoff.",
    "Exercise the dependent boundary, compare before and after evidence, and document the outcome.",
    "Prove that the control is deterministic under retry and that the next state is explainable.",
    "Attach a green validation result, a scoped diff, and a clear release recommendation.",
    "Confirm the change remains contained, reviewable, and reversible at the named boundary.",
    "Reproduce the original evidence, verify the corrected behavior, and close the review question.",
    "Demonstrate source alignment, successful recovery, and an explicit escalation path for failure.",
    "Validate the customer promise against the operational record before the work is marked done.",
    "Show that downstream consumers receive the expected state and that no unrelated path changed.",
    "Complete the regression evidence, reviewer sign-off, and handoff note in the source record.",
    "Verify the dependency ordering, the release signal, and the rollback point.",
    "Provide evidence for the problem, expected outcome, owner handoff, and final acceptance decision.",
)

DECISION_LENS_ACTIONS = (
    "protect the evidence boundary",
    "make ownership explicit",
    "preserve rollback readiness",
    "bound the customer impact",
    "keep the provider contract stable",
    "make the release signal explainable",
    "reconcile the source and serving records",
    "keep the operational handoff reviewable",
)

DECISION_LENS_OBJECTS = (
    "during retries",
    "at the customer handoff",
    "before release approval",
    "across the dependency sequence",
    "within the tenant boundary",
    "when the source is delayed",
    "after recovery completes",
    "for the next operator checkpoint",
)

DECISION_LENSES = tuple(
    f"{action} {target}" for action in DECISION_LENS_ACTIONS for target in DECISION_LENS_OBJECTS
)


def _ordinary_task_description(
    *,
    external_id: str,
    task_focus: str,
    ordinary_outcome: str,
    theme_index: int,
    batch_index: int,
    batch_slot: int,
    source_context: str,
    project_id: str,
    epic_id: str,
    decision_id: str,
) -> str:
    """Build deterministic business prose for one ordinary task."""
    digest = sha256(f"ordinary-semantics:{external_id}".encode()).hexdigest()
    selectors = [int(digest[offset : offset + 2], 16) % 16 for offset in range(0, 12, 2)]
    problem = ORDINARY_PROBLEM_VARIANTS[(theme_index + selectors[0]) % 16]
    context = ORDINARY_CONTEXT_VARIANTS[(batch_index + selectors[1]) % 16]
    outcome = ORDINARY_OUTCOME_VARIANTS[(theme_index + batch_index + selectors[2]) % 16]
    owner = ORDINARY_OWNER_VARIANTS[(batch_slot + theme_index + selectors[3]) % 16]
    constraint = ORDINARY_CONSTRAINT_VARIANTS[(batch_index + batch_slot + selectors[4]) % 16]
    acceptance = ORDINARY_ACCEPTANCE_VARIANTS[(theme_index + batch_index + selectors[5]) % 16]
    return (
        f"Problem: {problem} The affected control is the {task_focus} boundary.\n"
        f"Context: {context} Source evidence: {source_context}\n"
        f"Expected outcome: {outcome}.\n"
        f"Owner responsibility: {owner}\n"
        f"Constraints: {constraint}\n"
        f"Acceptance criteria: {acceptance}\n"
        f"Governance: implement decision {decision_id} in epic {epic_id} for project {project_id}."
    )


BATCH_FOCUS_ADJECTIVES = (
    "adaptive",
    "bounded",
    "canonical",
    "customer-led",
    "defensive",
    "diagnostic",
    "durable",
    "evidence-based",
    "explicit",
    "fail-safe",
    "fresh",
    "granular",
    "idempotent",
    "incremental",
    "isolated",
    "measurable",
    "observable",
    "ordered",
    "portable",
    "predictable",
    "replayable",
    "resumable",
    "safe",
    "scoped",
    "source-visible",
    "stable",
    "tenant-aware",
    "traceable",
    "verifiable",
    "versioned",
    "visible",
    "well-bounded",
)

BATCH_FOCUS_NOUNS = (
    "handoff",
    "checkpoint",
    "reconciliation",
    "replay",
    "fallback",
    "watermark",
    "gate",
    "ledger",
    "boundary",
    "snapshot",
    "escalation",
    "validation",
    "rollback",
    "audit",
    "migration",
    "triage",
    "ownership",
    "cutover",
    "recovery",
    "alert",
    "sampling",
    "lineage",
    "approval",
    "routing",
    "provenance",
    "capacity",
    "resilience",
    "freshness",
    "coverage",
    "quality",
    "delivery",
    "readiness",
)


def _batch_focus(batch_index: int) -> str:
    index = batch_index - 1
    return (
        f"{BATCH_FOCUS_ADJECTIVES[index % len(BATCH_FOCUS_ADJECTIVES)]} "
        f"{BATCH_FOCUS_NOUNS[index // len(BATCH_FOCUS_ADJECTIVES)]}"
    )


ORDINARY_ENTERPRISE_OUTCOMES = (
    "deployment",
    "deployment",
    "experiment",
    "rollback",
    "cancelled",
    "deployment",
    "task_only",
    "deployment",
    "experiment",
    "rollback",
    "deployment",
    "task_only",
    "deployment",
    "experiment",
    "rollback",
    "cancelled",
    "deployment",
    "task_only",
    "deployment",
    "experiment",
    "rollback",
    "deployment",
    "task_only",
    "deployment",
    "experiment",
    "rollback",
    "cancelled",
    "deployment",
    "task_only",
    "deployment",
    "experiment",
    "rollback",
    "task_only",
    "deployment",
    "experiment",
    "rollback",
    "cancelled",
    "deployment",
    "task_only",
    "deployment",
    "experiment",
    "rollback",
    "deployment",
    "task_only",
    "deployment",
    "experiment",
    "rollback",
    "cancelled",
    "deployment",
    "task_only",
    "deployment",
    "experiment",
    "rollback",
    "deployment",
    "task_only",
    "deployment",
    "experiment",
    "rollback",
    "cancelled",
    "deployment",
    "task_only",
    "deployment",
    "experiment",
    "rollback",
)


def _ordinary_enterprise_outcome(work_id: str) -> str:
    digest = sha256(f"ordinary-outcome:{work_id}".encode()).hexdigest()
    if int(digest[:8], 16) % 257 == 0:
        return "incident"
    return ORDINARY_ENTERPRISE_OUTCOMES[int(digest[:8], 16) % len(ORDINARY_ENTERPRISE_OUTCOMES)]


def _enterprise_theme(external_id: str) -> tuple[str, str, str, int]:
    theme_index = int(sha256(external_id.encode()).hexdigest()[:8], 16) % 64 + 1
    theme = (
        "retrieval",
        "connectors",
        "ranking",
        "security",
        "observability",
        "billing",
        "reliability",
        "evaluation",
    )[(theme_index - 1) % 8]
    slug = f"{theme}-{theme_index:02d}"
    return (
        f"enterprise-project-{slug}",
        f"enterprise-epic-{slug}",
        f"enterprise-decision-{slug}",
        theme_index,
    )


CHAIN_STAGE_ORDER = (
    "action",
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

CHAIN_GAP_RANGES = {
    ("action", "customer_email"): (90, 720),
    ("customer_email", "ticket"): (180, 1_440),
    ("ticket", "meeting"): (240, 2_880),
    ("meeting", "decision"): (60, 1_440),
    ("decision", "task"): (240, 2_880),
    ("task", "merge_request"): (1_440, 7_200),
    ("merge_request", "deployment"): (240, 2_880),
    ("deployment", "incident"): (15, 1_440),
    ("incident", "postmortem"): (1_440, 7_200),
}


def _chain_stage_offsets(chain_id: str, present_stages: set[str]) -> dict[str, timedelta]:
    """Create stable, non-uniform source timestamps for the stages in a chain."""
    offsets: dict[str, timedelta] = {}
    elapsed = 0
    previous_index: int | None = None
    for stage_index, stage in enumerate(CHAIN_STAGE_ORDER):
        if stage not in present_stages:
            continue
        if previous_index is not None:
            for gap_index in range(previous_index, stage_index):
                previous = CHAIN_STAGE_ORDER[gap_index]
                current = CHAIN_STAGE_ORDER[gap_index + 1]
                low, high = CHAIN_GAP_RANGES[(previous, current)]
                digest = sha256(f"{chain_id}:{previous}:{current}".encode()).hexdigest()
                elapsed += low + int(digest[:8], 16) % (high - low + 1)
        offsets[stage] = timedelta(minutes=elapsed)
        previous_index = stage_index
    return offsets


def _enterprise_batch_chain_specs(
    batch_index: int,
    base_original: datetime,
    project_id: str,
    epic_id: str,
    task_id: str,
    chain_id: str,
    email_id: str,
    ticket_id: str,
    meeting_id: str,
    decision_id: str,
    deployment_id: str,
    incident_id: str,
    postmortem_id: str,
) -> list[dict[str, Any]]:
    original = max(PROFILE_START.replace(tzinfo=UTC), base_original - timedelta(hours=10))
    batch_focus = _batch_focus(batch_index)
    references = {
        "emailId": email_id,
        "ticketId": ticket_id,
        "meetingId": meeting_id,
        "decisionId": decision_id,
        "projectId": project_id,
        "epicId": epic_id,
        "taskId": task_id,
        "deploymentId": deployment_id,
        "incidentId": incident_id,
        "postmortemId": postmortem_id,
    }

    def event_spec(
        source: str,
        object_type: str,
        external_id: str,
        occurred_at: datetime,
        container: str,
        payload: dict[str, Any],
        row: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload.setdefault(
            "redwoodReferences",
            {"accountId": f"A{((batch_index - 1) % 6) + 1:03d}"},
        )
        return {
            "entity_id": immutable_id("redwood", source, object_type, external_id),
            "source": source,
            "object_type": object_type,
            "external_id": external_id,
            "revision": 1,
            "original": occurred_at,
            "calendar": "enterprise",
            "container": container,
            "acl": _event_acl(source, container, row),
            "payload": payload,
        }

    email_payload = _graph_message_payload(
        email_id,
        {
            "employee_id": "E003",
            "subject": (
                (
                    "Customer asks about retrieval behavior"
                    if batch_index % 5 == 0
                    else "Customer asks about connector coverage"
                    if batch_index % 5 == 1
                    else "Customer asks about billing reconciliation"
                    if batch_index % 5 == 2
                    else "Customer asks about release safeguards"
                    if batch_index % 5 == 3
                    else "Customer asks about incident follow-up"
                )
                + f" for {batch_focus}"
            ),
            "body": (
        f"The customer asks {COMPANY_NAME} to resolve workstream {batch_index:04d}. "
                f"Create {ticket_id}, review it in {meeting_id}, and record {decision_id}."
            ),
            "account_id": f"ENT-BATCH-{batch_index:04d}",
            "customer_email": f"batch-{batch_index:04d}@customer.example",
        },
        original,
    )
    email_payload.update(
        {
            "redwoodCausalChainId": chain_id,
            "redwoodStage": "customer_email",
            "redwoodCausalReferences": references,
        }
    )
    email_implicit_context = _implicit_context_payload(
        kind="enterprise_customer_reference",
        target_stage=("task", "decision", "deployment", "incident", "deal")[batch_index % 5],
        account=f"workstream {batch_index:04d}",
        topic=batch_focus,
        variant=batch_index,
    )
    email_payload["body"]["content"] += f" The customer also referred to {email_implicit_context['phrase']}."
    email_payload["redwoodImplicitContext"] = email_implicit_context
    ticket_payload = _jira_payload(
        key=ticket_id,
        row={
            "summary": f"Trace customer workstream {batch_index:04d}",
            "content": (
                f"Customer email {email_id} reports a {batch_focus} problem that must be made explicit "
                f"before the release checkpoint. "
                + _ordinary_task_description(
                    external_id=ticket_id,
                    task_focus=batch_focus,
                    ordinary_outcome="benchmark_customer_resolution",
                    theme_index=(batch_index - 1) % 64 + 1,
                    batch_index=batch_index,
                    batch_slot=0,
                    source_context=(
                        f"The customer record is {email_id}; route the investigation through "
                        f"meeting {meeting_id} and preserve the source-visible evidence."
                    ),
                    project_id=project_id,
                    epic_id=epic_id,
                    decision_id=decision_id,
                )
            ),
            "timestamp": (original + timedelta(hours=2)).isoformat(),
            "reporter_employee_id": "E003",
            "assignee_employee_id": "E011",
        },
        status="Open",
        histories=[],
    )
    ticket_payload.update(
        {"redwoodCausalChainId": chain_id, "redwoodStage": "ticket", "redwoodCausalReferences": references}
    )
    meeting_participants = ["E003", "E011", "E012"]
    if batch_index % 5 == 0:
        meeting_participants = meeting_participants[:2]
    elif batch_index % 5 == 1:
        meeting_participants.append("E007")
    elif batch_index % 5 == 2:
        meeting_participants.extend(["E008", "E014"])
    meeting_participants = list(dict.fromkeys(meeting_participants))
    meeting_payload = {
        "id": meeting_id,
        "title": f"Workstream {batch_index:04d} decision review",
        "date": (original + timedelta(hours=4)).isoformat(),
        "participants": meeting_participants,
        "summary": f"Review {ticket_id} and agree the next source-visible change.",
        "transcript": (
            f"The team reviewed {email_id} and {ticket_id} for the {batch_focus} workstream. "
            f"The group compared the customer evidence, named the owner, and agreed to record "
            f"{decision_id} before {task_id} proceeds."
        ),
        "protocol": {
            "decision": f"Approve workstream {batch_index:04d} task {task_id}",
            "owner": "E011",
            "follow_up": task_id,
        },
        "redwoodCausalChainId": chain_id,
        "redwoodStage": "meeting",
        "redwoodCausalReferences": references,
    }
    implicit_context = _implicit_context_payload(
        kind="enterprise_meeting_reference",
        target_stage=("decision", "task", "deployment", "incident", "deal")[batch_index % 5],
        account=f"workstream {batch_index:04d}",
        topic=batch_focus,
        variant=batch_index,
    )
    meeting_payload["transcript"] += f" The group also discussed {implicit_context['phrase']}."
    meeting_payload["redwoodImplicitContext"] = implicit_context
    decision_payload = _confluence_payload(
        "decision",
        decision_id,
        {
            "title": f"Decision for workstream {batch_index:04d}",
            "content": (
                f"Decision {decision_id} follows {meeting_id}; it authorizes {task_id} "
                f"in epic {epic_id} and project {project_id}."
            ),
        },
        original + timedelta(hours=6),
    )
    decision_payload.update(
        {"redwoodCausalChainId": chain_id, "redwoodStage": "decision", "redwoodCausalReferences": references}
    )
    batch_task_row = {
        "task_id": task_id,
        "summary": f"Implement {batch_focus} workstream decision",
        "content": (
            f"Implement {decision_id} from {meeting_id}; preserve the customer evidence "
            f"from {ticket_id} and report the {batch_focus} result against {project_id}."
        ),
        "timestamp": (original + timedelta(hours=8)).isoformat(),
        "reporter_employee_id": "E011",
        "assignee_employee_id": "E012",
        "status": "In Progress",
    }
    batch_task_history = _task_lifecycle_history(
        batch_task_row,
        [],
        task_index=batch_index,
        task_count=1000,
    )
    task_payload = _jira_payload(
        key=task_id,
        row=batch_task_row,
        status="In Progress",
        histories=_jira_changelog(batch_task_history),
    )
    task_payload.update(
        {"redwoodCausalChainId": chain_id, "redwoodStage": "task", "redwoodCausalReferences": references}
    )
    deployment_payload = _confluence_payload(
        "deployment",
        deployment_id,
        {
            "title": f"Deployment for workstream {batch_index:04d}",
            "content": f"Deployment {deployment_id} ships the reviewed changes from {task_id}.",
        },
        original + timedelta(hours=10),
    )
    deployment_payload.update(
        {
            "redwoodCausalChainId": chain_id,
            "redwoodStage": "deployment",
            "redwoodCausalReferences": references,
        }
    )
    incident_payload = _jira_payload(
        key=incident_id,
        row={
            "summary": f"Regression after deployment {deployment_id}",
            "content": (
                f"Incident {incident_id} was caused by deployment {deployment_id}; "
                f"mitigation remains in {task_id}."
            ),
            "timestamp": (original + timedelta(hours=12)).isoformat(),
            "reporter_employee_id": "E011",
            "assignee_employee_id": "E011",
        },
        status="Done",
        histories=[],
    )
    incident_payload.update(
        {"redwoodCausalChainId": chain_id, "redwoodStage": "incident", "redwoodCausalReferences": references}
    )
    postmortem_payload = {
        "id": postmortem_id,
        "title": f"Postmortem for {incident_id}",
        "space": "Engineering",
        "author": "E011",
        "created": (original + timedelta(hours=14)).isoformat(),
        "version": "1.1",
        "content": f"Postmortem {postmortem_id} analyzes {incident_id}, {deployment_id}, and {task_id}.",
        "properties": {"incident_id": incident_id},
        "redwoodCausalChainId": chain_id,
        "redwoodStage": "postmortem",
        "redwoodCausalReferences": references,
    }
    return [
        event_spec(
            "mail-redwood",
            "message",
            email_id,
            original,
            "COMMERCIAL",
            email_payload,
            {"participant_employee_ids": ["E003"]},
        ),
        event_spec(
            "jira-redwood", "issue", ticket_id, original + timedelta(hours=2), "SUPPORT", ticket_payload
        ),
        event_spec(
            "fireflies-redwood",
            "transcript",
            meeting_id,
            original + timedelta(hours=4),
            "meetings",
            meeting_payload,
        ),
        event_spec(
            "confluence-redwood",
            "page",
            decision_id,
            original + timedelta(hours=6),
            "ENG",
            decision_payload,
        ),
        event_spec("jira-redwood", "issue", task_id, original + timedelta(hours=8), "ENG", task_payload),
        event_spec(
            "confluence-redwood",
            "page",
            deployment_id,
            original + timedelta(hours=10),
            "ENG",
            deployment_payload,
        ),
        event_spec(
            "jira-redwood", "issue", incident_id, original + timedelta(hours=12), "ENG", incident_payload
        ),
        event_spec(
            "xwiki-redwood",
            "page",
            postmortem_id,
            original + timedelta(hours=14),
            "ENG",
            postmortem_payload,
        ),
    ]


def _enterprise_mr_payload(
    *,
    external_id: str,
    title: str,
    description: str,
    state: str,
    revision: int,
    original: datetime,
    occurred_at: datetime,
    cancelled: bool,
    project_id: str,
    task_focus: str,
    bucket: int,
    theme_index: int,
    task_id: str,
    causal_chain_id: str,
    causal_references: dict[str, str],
    work_id: str | None = None,
    ordinary_outcome: str | None = None,
    benchmark: bool = False,
) -> dict[str, Any]:
    """Create a deterministic GitLab payload with realistic per-MR variation."""
    digest = sha256(f"enterprise-mr:{external_id}".encode()).hexdigest()
    variant = int(digest[:8], 16)
    change_kinds = (
        "parser",
        "adapter",
        "policy",
        "checkpoint",
        "retry",
        "metrics",
        "contract",
        "redaction",
        "cache",
        "migration",
        "fallback",
        "rollback",
        "docs-only",
        "test-only",
    )
    change_profiles = (
        ("api", "handlers", "endpoint.py"),
        ("worker", "queues", "consumer.py"),
        ("storage", "migrations", "schema.sql"),
        ("policy", "rules", "evaluator.py"),
        ("observability", "dashboards", "release.yaml"),
        ("client", "sdk", "transport.ts"),
        ("ingestion", "connectors", "reader.py"),
        ("retrieval", "ranking", "scorer.py"),
        ("billing", "reconciliation", "ledger.py"),
        ("security", "redaction", "boundary.py"),
        ("ops", "runbooks", "rollback.md"),
        ("quality", "fixtures", "regression.json"),
        ("platform", "config", "defaults.yaml"),
        ("runtime", "timeouts", "budget.go"),
        ("analytics", "reports", "snapshot.sql"),
        ("migration", "backfill", "checkpoint.py"),
    )
    focus_slug = re.sub(r"[^a-z0-9]+", "-", task_focus.casefold()).strip("-")
    kind = change_kinds[variant % len(change_kinds)]
    area, layer, filename = change_profiles[(variant // 7) % len(change_profiles)]
    if kind == "docs-only":
        filename = "runbook.md"
        layer = "documentation"
    elif kind == "test-only":
        filename = "test_regression.py"
        layer = "tests"
    file_count = 1 + variant % 20
    commit_count = 1 + (variant // 7) % 8
    reviewer_count = variant % 4
    approval_count = 0 if state == "closed" else (variant // 11) % 4
    comment_count = (variant // 13) % 9
    pipeline_count = 1 + (variant // 17) % 4
    failed_attempt = variant % 5 == 0
    if failed_attempt:
        pipeline_count = max(2, pipeline_count)
    is_draft = state != "merged" and variant % 29 in {0, 1}
    has_merge_conflict = state != "merged" and variant % 31 in {0, 1}
    requested_changes = state != "merged" and variant % 17 in {0, 1}
    if cancelled:
        is_draft = False
        requested_changes = False
    reviewers = [
        _source_account(f"E{12 + ((variant // 19) + index * 7) % 28:03d}") for index in range(reviewer_count)
    ]
    approvals = [
        {
            "user": _source_account(f"E{12 + ((variant // 23) + index * 5) % 28:03d}"),
            "approved": state == "merged" and index < approval_count,
            "at": (occurred_at - timedelta(hours=index + 1)).isoformat(),
        }
        for index in range(approval_count)
    ]
    path_roles = (
        "core",
        "boundary",
        "fallback",
        "contract",
        "validation",
        "recovery",
        "telemetry",
        "compatibility",
        "migration",
        "checkpoint",
        "ownership",
        "replay",
        "fixture",
        "rollout",
        "backfill",
        "audit",
        "handoff",
        "guard",
        "sampling",
        "verification",
    )
    path_stem = filename.rsplit(".", maxsplit=1)[0]

    def previous_deployment_paths() -> list[str]:
        return [
            (
                f"src/{area}/{focus_slug}/deployed/{layer}/{path_roles[index]}/"
                f"{path_stem}.{filename.rsplit('.', 1)[-1]}"
            )
            for index in range(file_count)
        ]

    if kind == "rollback":
        changed_files = previous_deployment_paths()
    else:
        changed_files = []
        for index in range(file_count):
            role = path_roles[index]
            if kind == "docs-only":
                changed_files.append(f"docs/{area}/{focus_slug}/{kind}/{layer}/{role}.md")
            elif kind == "test-only":
                changed_files.append(f"tests/{area}/{focus_slug}/{kind}/{layer}/test_{role}.py")
            elif index % 4 == 0:
                changed_files.append(
                    f"src/{area}/{focus_slug}/{kind}/{layer}/{role}_{path_stem}.{filename.rsplit('.', 1)[-1]}"
                )
            elif index % 4 == 1:
                changed_files.append(f"tests/{area}/{focus_slug}/{kind}/{layer}/test_{role}_{path_stem}.py")
            elif index % 4 == 2:
                changed_files.append(f"config/{area}/{focus_slug}/{kind}/{layer}/{role}_{path_stem}.yaml")
            else:
                changed_files.append(f"docs/{area}/{focus_slug}/{kind}/{layer}/{role}_{path_stem}.md")
    pipelines = []
    for attempt in range(pipeline_count):
        failed = failed_attempt and attempt == 0 and revision < 3
        attempt_started = occurred_at + timedelta(minutes=attempt * 40)
        attempt_finished = attempt_started + timedelta(minutes=12 + (variant + attempt) % 18)
        pipelines.append(
            {
                "id": f"pipeline-{external_id}-{attempt + 1}",
                "attempt": attempt + 1,
                "status": "failed" if failed else "success" if revision >= 3 else "running",
                "ref": f"feature/{focus_slug}",
                "stages": ["lint", "unit", "integration", "security"][: 2 + (variant + attempt) % 3],
                "failed_checks": ["integration"] if failed else [],
                "started_at": attempt_started.isoformat(),
                "finished_at": attempt_finished.isoformat(),
                "recovery_commit": (
                    sha256(f"{external_id}:recovery:{attempt}".encode()).hexdigest()[:12] if failed else None
                ),
            }
        )
    commits = []
    for index in range(commit_count):
        commit_path = changed_files[index % len(changed_files)]
        commits.append(
            {
                "id": sha256(f"{external_id}:commit:{index}".encode()).hexdigest()[:12],
                "message": (
                    f"{('Add' if index == 0 else 'Refine')} {kind} handling for {task_focus} in {commit_path}"
                ),
                "paths": [commit_path],
                "author": _source_account(f"E{12 + ((variant // 29) + index) % 28:03d}"),
            }
        )
    review_comment_templates = (
        "Please verify the {kind} evidence for {task_focus}; the changed path is {path}.",
        "I reproduced the boundary around {path}; please confirm the expected behavior.",
        "Requested change: narrow {kind} scope in {path} before approval and attach the observed result.",
        "The reviewer found a blocker in {path}; the next pipeline must cover the recovery path.",
        "Evidence check completed for {task_focus} in {path}; this comment records the release decision.",
    )
    review_comments = [
        {
            "author": reviewers[index % len(reviewers)] if reviewers else _source_account("E011"),
            "body": review_comment_templates[(variant + index) % len(review_comment_templates)].format(
                kind=kind,
                task_focus=task_focus,
                path=changed_files[index % len(changed_files)],
            ),
            "resolved": state == "merged" and index < max(1, comment_count // 2),
        }
        for index in range(comment_count)
    ]
    payload = {
        "id": external_id,
        "iid": external_id,
        "title": title,
        "description": description,
        "state": state,
        "created_at": original.isoformat(),
        "updated_at": occurred_at.isoformat(),
        "merged_at": occurred_at.isoformat() if state == "merged" else None,
        "merge_commit_sha": sha256(f"{external_id}:merge".encode()).hexdigest()
        if state == "merged"
        else None,
        "review_status": "cancelled" if state == "closed" else "approved" if revision >= 2 else "pending",
        "close_reason": "cancelled_after_scope_review" if state == "closed" else None,
        "draft": is_draft,
        "merge_conflicts": has_merge_conflict,
        "requested_changes": requested_changes,
        "approval_state": (
            "rejected" if requested_changes else "approved" if state == "merged" else "pending"
        ),
        "reviewers": reviewers,
        "approvals": approvals,
        "pipelines": pipelines,
        "changed_files": changed_files,
        "diff_summary": {
            "files_changed": file_count,
            "insertions": 8 + (variant % 180),
            "deletions": 1 + ((variant // 31) % 45),
            "paths": list(changed_files),
        },
        "commits": commits,
        "review_comments": review_comments,
        "redwoodTheme": {
            "projectId": project_id,
            "epicId": f"enterprise-epic-{project_id.removeprefix('enterprise-project-')}",
            "decisionId": f"enterprise-decision-{project_id.removeprefix('enterprise-project-')}",
        },
        "sourceLifecycle": {
            "taskId": task_id,
            "workId": work_id or causal_chain_id,
            "workType": "benchmark" if benchmark else "ordinary",
            "outcome": ordinary_outcome or "incident_chain",
            "revision": revision,
            "failedCheckRecovered": failed_attempt and revision >= 3,
            "changeKind": kind,
            "previousDeploymentPaths": previous_deployment_paths() if kind == "rollback" else [],
            "previousDeploymentId": (f"{project_id}-previous-deployment" if kind == "rollback" else None),
        },
    }
    if benchmark:
        payload.update(
            {
                "redwoodCausalChainId": causal_chain_id,
                "redwoodStage": "merge_request",
                "redwoodCausalReferences": causal_references,
            }
        )
    else:
        payload.update(
            {
                "redwoodEnterpriseWorkId": work_id,
                "redwoodEnterpriseStage": "merge_request",
                "redwoodEnterpriseReferences": causal_references,
            }
        )
    return payload


def _enterprise_specs(documents: list[CorpusDocument]) -> list[dict[str, Any]]:
    plan_original = PROFILE_START.replace(tzinfo=UTC)
    specs: list[dict[str, Any]] = []
    for theme_index in range(1, 65):
        theme = (
            "retrieval",
            "connectors",
            "ranking",
            "security",
            "observability",
            "billing",
            "reliability",
            "evaluation",
        )[(theme_index - 1) % 8]
        slug = f"{theme}-{theme_index:02d}"
        project_id = f"enterprise-project-{slug}"
        epic_id = f"enterprise-epic-{slug}"
        decision_id = f"enterprise-decision-{slug}"
        dependency_indices = []
        if 13 <= theme_index <= 24:
            dependency_indices.append(1 + (theme_index - 13) % 12)
        elif 25 <= theme_index <= 40:
            dependency_indices.append(13 + (theme_index - 25) % 12)
            if theme_index % 4 == 0:
                dependency_indices.append(1 + (theme_index // 4) % 12)
        elif 41 <= theme_index <= 52:
            dependency_indices.append(25 + (theme_index - 41) % 16)
            if theme_index % 3 == 0:
                dependency_indices.append(13 + (theme_index // 3) % 12)
            if theme_index == 41:
                dependency_indices.append(37)
            elif theme_index == 42:
                dependency_indices.append(38)
        elif 53 <= theme_index <= 64:
            dependency_indices.append(41 + (theme_index - 53) % 12)
            if theme_index % 4 == 0:
                dependency_indices.append(25 + (theme_index // 4) % 16)

        def project_id_for(index: int) -> str:
            dependency_theme = (
                "retrieval",
                "connectors",
                "ranking",
                "security",
                "observability",
                "billing",
                "reliability",
                "evaluation",
            )[(index - 1) % 8]
            return f"enterprise-project-{dependency_theme}-{index:02d}"

        dependency_ids = [project_id_for(index) for index in dependency_indices]
        project_focus = ENTERPRISE_TASK_FOCI[theme_index - 1]
        project_problem = ORDINARY_PROBLEM_VARIANTS[(theme_index * 3) % len(ORDINARY_PROBLEM_VARIANTS)]
        project_context = ORDINARY_CONTEXT_VARIANTS[(theme_index * 5) % len(ORDINARY_CONTEXT_VARIANTS)]
        project_outcome = ORDINARY_OUTCOME_VARIANTS[(theme_index * 7) % len(ORDINARY_OUTCOME_VARIANTS)]
        project_dependency_text = (
            "Dependencies: this project has no upstream dependency."
            if not dependency_ids
            else f"Dependencies: this project depends on project {', '.join(dependency_ids)}."
        )
        causal_chain_id = f"enterprise-causal-{theme_index:02d}"
        causal_email_id = f"<enterprise-customer-{theme_index:02d}@customer.example>"
        causal_ticket_id = f"ENT-TKT-{theme_index:02d}"
        causal_meeting_id = f"ENT-MTG-{theme_index:02d}"
        causal_decision_id = f"enterprise-decision-record-{slug}"
        theme_original = plan_original + timedelta(days=theme_index % 26)
        causal_references = {
            "emailId": causal_email_id,
            "ticketId": causal_ticket_id,
            "meetingId": causal_meeting_id,
            "decisionId": causal_decision_id,
        }
        for object_type, external_id, title, content in (
            (
                "project",
                project_id,
                f"{project_id.replace('-', ' ').title()} project",
                f"Project charter for the {project_id} workstream. Problem: {project_problem} "
                f"Context: {project_context} Focus: {project_focus}. Expected outcome: {project_outcome} "
                f"{project_dependency_text}",
            ),
            (
                "epic",
                epic_id,
                f"{epic_id.replace('-', ' ').title()} epic",
                f"Epic scope for {epic_id}. It addresses {project_problem} in the {project_focus} "
                f"boundary and must cite project {project_id}. The delivery outcome is {project_outcome}.",
            ),
            (
                "decision",
                decision_id,
                f"{decision_id.replace('-', ' ').title()} decision",
                f"Decision record for {decision_id}. Problem: {project_problem} Context: {project_context} "
                f"The decision governs epic {epic_id} in project {project_id}. Chosen outcome: "
                f"{project_outcome} The constraint is to keep the {project_focus} boundary reviewable.",
            ),
        ):
            root_offset = {
                "project": timedelta(0),
                "epic": timedelta(hours=1),
                "decision": timedelta(hours=2),
            }[object_type]
            root_original = theme_original + root_offset
            root_payload = _confluence_payload(
                object_type,
                external_id,
                {"title": title, "content": content},
                root_original,
            )
            root_payload["redwoodReferences"] = {
                "projectId": project_id,
                "epicId": epic_id,
                "decisionId": decision_id,
            }
            if object_type == "project":
                root_payload["redwoodReferences"].update(
                    {
                        "dependsOnProjectId": dependency_ids[0] if dependency_ids else None,
                        "dependsOnProjectIds": dependency_ids,
                    }
                )
            elif object_type == "epic":
                root_payload["redwoodReferences"] = {
                    "epicId": epic_id,
                    "projectId": project_id,
                    "decisionId": decision_id,
                }
            else:
                root_payload["redwoodReferences"] = {
                    "decisionId": decision_id,
                    "projectId": project_id,
                    "epicId": epic_id,
                }
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "confluence-redwood", "page", external_id),
                    "source": "confluence-redwood",
                    "object_type": "page",
                    "external_id": external_id,
                    "revision": 1,
                    "original": root_original,
                    "calendar": "enterprise",
                    "container": "ENG",
                    "acl": _event_acl("confluence-redwood", "ENG"),
                    "payload": root_payload,
                }
            )
        email_payload = _graph_message_payload(
            causal_email_id,
            {
                "employee_id": "E003",
                "subject": f"Customer request for {slug}",
                "body": (
                f"Customer asks {COMPANY_NAME} to investigate the {theme} requirement. "
                    f"Please create ticket {causal_ticket_id}, discuss it in meeting "
                    f"{causal_meeting_id}, and record the decision {causal_decision_id}."
                ),
                "account_id": f"ENT-{theme_index:02d}",
                "customer_email": f"customer-{theme_index:02d}@customer.example",
            },
            theme_original,
        )
        email_payload.update(
            {
                "redwoodCausalChainId": causal_chain_id,
                "redwoodStage": "customer_email",
                "redwoodCausalReferences": causal_references,
            }
        )
        specs.append(
            {
                "entity_id": immutable_id("redwood", "mail-redwood", "message", causal_email_id),
                "source": "mail-redwood",
                "object_type": "message",
                "external_id": causal_email_id,
                "revision": 1,
                "original": theme_original,
                "calendar": "enterprise",
                "container": "COMMERCIAL",
                "acl": _event_acl("mail-redwood", "COMMERCIAL", {"participant_employee_ids": ["E003"]}),
                "payload": email_payload,
            }
        )
        ticket_payload = _jira_payload(
            key=causal_ticket_id,
            row={
                "summary": f"Customer request for {slug}",
                "content": (
                    f"Customer email {causal_email_id} identifies a {theme} problem requiring a "
                    f"source-visible owner decision. "
                    + _ordinary_task_description(
                        external_id=causal_ticket_id,
                        task_focus=f"{theme} customer request",
                        ordinary_outcome="causal_customer_resolution",
                        theme_index=theme_index,
                        batch_index=theme_index,
                        batch_slot=1,
                        source_context=(
                            f"The customer record is {causal_email_id}; route the review through "
                            f"meeting {causal_meeting_id} before decision {causal_decision_id}."
                        ),
                        project_id=project_id,
                        epic_id=epic_id,
                        decision_id=causal_decision_id,
                    )
                ),
                "timestamp": (theme_original + timedelta(hours=6)).isoformat(),
                "reporter_employee_id": "E003",
                "assignee_employee_id": "E010",
            },
            status="Open",
            histories=[],
        )
        ticket_payload.update(
            {
                "redwoodCausalChainId": causal_chain_id,
                "redwoodStage": "ticket",
                "redwoodCausalReferences": causal_references,
            }
        )
        specs.append(
            {
                "entity_id": immutable_id("redwood", "jira-redwood", "issue", causal_ticket_id),
                "source": "jira-redwood",
                "object_type": "issue",
                "external_id": causal_ticket_id,
                "revision": 1,
                "original": theme_original + timedelta(hours=6),
                "calendar": "enterprise",
                "container": "SUPPORT",
                "acl": _event_acl("jira-redwood", "SUPPORT"),
                "payload": ticket_payload,
            }
        )
        meeting_payload = {
            "id": causal_meeting_id,
            "title": f"Customer decision meeting for {slug}",
            "date": (theme_original + timedelta(days=1)).isoformat(),
            "participants": ["E003", "E010", "E011"],
            "summary": f"Review {causal_ticket_id} and agree the {theme} change",
            "transcript": (
                f"The team reviewed customer email {causal_email_id} and ticket "
                f"{causal_ticket_id}. The group agreed to record decision {causal_decision_id} "
                f"before the related merge requests proceed."
            ),
            "protocol": {
                "decision": f"Record and implement {causal_decision_id}",
                "owner": "E010",
                "follow_up": causal_decision_id,
            },
            "redwoodCausalChainId": causal_chain_id,
            "redwoodStage": "meeting",
            "redwoodCausalReferences": causal_references,
        }
        specs.append(
            {
                "entity_id": immutable_id("redwood", "fireflies-redwood", "transcript", causal_meeting_id),
                "source": "fireflies-redwood",
                "object_type": "transcript",
                "external_id": causal_meeting_id,
                "revision": 1,
                "original": theme_original + timedelta(days=1),
                "calendar": "enterprise",
                "container": "meetings",
                "acl": _event_acl("fireflies-redwood", "meetings"),
                "payload": meeting_payload,
            }
        )
        decision_payload = _confluence_payload(
            "decision",
            causal_decision_id,
            {
                "title": f"Decision record for {slug}",
                "content": (
                    f"Decision {causal_decision_id} follows meeting {causal_meeting_id}. "
                    f"It authorizes the five thematic tasks in project {project_id} "
                    f"and traces back to customer ticket {causal_ticket_id}. "
                    f"Decision lens: {DECISION_LENSES[(theme_index - 1) % len(DECISION_LENSES)]}."
                ),
            },
            theme_original + timedelta(days=2),
        )
        decision_payload.update(
            {
                "redwoodCausalChainId": causal_chain_id,
                "redwoodStage": "decision",
                "redwoodCausalReferences": causal_references,
            }
        )
        specs.append(
            {
                "entity_id": immutable_id("redwood", "confluence-redwood", "page", causal_decision_id),
                "source": "confluence-redwood",
                "object_type": "page",
                "external_id": causal_decision_id,
                "revision": 1,
                "original": theme_original + timedelta(days=2),
                "calendar": "enterprise",
                "container": "ENG",
                "acl": _event_acl("confluence-redwood", "ENG"),
                "payload": decision_payload,
            }
        )
        work_items = (
            ("Harden", "retry boundaries", "failure-path evidence"),
            ("Benchmark", "ranking quality", "latency and relevance measurements"),
            ("Integrate", "provider callbacks", "contract and compatibility checks"),
            ("Document", "operator handoff", "runbook and ownership evidence"),
            ("Roll out", "release controls", "deployment and rollback evidence"),
        )
        for bucket in range(5):
            task_id = f"ER-{sha256(f'{theme_index}:{bucket}'.encode()).hexdigest()[:10].upper()}"
            task_original = plan_original + timedelta(days=theme_index % 26, hours=bucket + 1)
            statuses = (
                ("To Do", "In Progress", "Review", "Done")
                if bucket % 2 == 0
                else ("Backlog", "In Progress", "Blocked", "In Progress", "Validation", "Done")
            )
            histories: list[dict[str, Any]] = []
            previous_status = statuses[0]
            for revision, status in enumerate(statuses, start=1):
                occurred_at = task_original + timedelta(days=revision - 1, hours=(revision * 3) % 8)
                if revision > 1:
                    histories.append(
                        {
                            "id": f"{task_id}-status-{revision}",
                            "author": _source_account(f"E{10 + (theme_index + bucket) % 30:03d}"),
                            "created": occurred_at.isoformat(),
                            "items": [
                                {
                                    "field": "status",
                                    "fromString": previous_status,
                                    "toString": status,
                                }
                            ],
                        }
                    )
                owner = f"E{10 + (theme_index + bucket) % 30:03d}"
                work_verb, work_area, evidence_type = work_items[bucket]
                task_focus = ENTERPRISE_TASK_FOCI[theme_index - 1]
                task_row = {
                    "summary": (f"{work_verb} {task_focus} through {work_area} for workstream {bucket + 1}"),
                    "content": (
                        f"Coordinate {task_focus} through {work_area} for decision {decision_id} "
                        f"in epic {epic_id} "
                        f"for project {project_id}. The acceptance evidence is {evidence_type}; "
                        "record review feedback, blockers, ownership, and release readiness "
                        "against the linked merge requests."
                    ),
                    "created_at": task_original.isoformat(),
                    "reporter_employee_id": owner,
                    "assignee_employee_id": owner,
                    "priority": ("high", "medium", "low")[bucket % 3],
                }
                payload = _jira_payload(
                    key=task_id,
                    row=task_row,
                    status=status,
                    histories=list(histories),
                )
                payload["redwoodCausalChainId"] = causal_chain_id
                payload["redwoodCausalReferences"] = causal_references
                payload["fields"]["comment"]["comments"] = [
                    {
                        "id": f"{task_id}-comment-{revision}",
                        "author": _source_account(owner),
                        "body": (
                            f"Workstream {bucket + 1} update: {status}. Reviewed {work_area} "
                            f"for {theme}; evidence is {evidence_type}. Next checkpoint is "
                            f"revision {revision + 1}."
                        ),
                        "created": occurred_at.isoformat(),
                    }
                ]
                specs.append(
                    {
                        "entity_id": immutable_id("redwood", "jira-redwood", "issue", task_id),
                        "source": "jira-redwood",
                        "object_type": "issue",
                        "external_id": task_id,
                        "revision": revision,
                        "original": occurred_at,
                        "calendar": "enterprise",
                        "container": "ENG",
                        "acl": _event_acl("jira-redwood", "ENG"),
                        "payload": payload,
                    }
                )
                previous_status = status
    enterprise_mr_index = 0
    batch_merge_request_ids: dict[int, list[str]] = defaultdict(list)
    emitted_batch_chains: set[int] = set()
    ordinary_work_groups: dict[str, dict[str, Any]] = {}
    for document in documents:
        if document.provenance.get("source") != "enterprise_rag":
            continue
        original = document.timestamp or PROFILE_START.replace(tzinfo=UTC)
        external_id = str(document.provenance.get("dsid") or document.doc_id).replace(":", "-")
        container = _container_for_document(document)
        if document.source_type.casefold() == "github":
            source, object_type = "gitlab-redwood", "merge_request"
            project_id, epic_id, decision_id, theme_index = _enterprise_theme(external_id)
            bucket = int(sha256(external_id.encode()).hexdigest()[8:12], 16) % 5
            enterprise_mr_index += 1
            batch_index = (enterprise_mr_index - 1) // 5 + 1
            batch_slot = (enterprise_mr_index - 1) % 5
            benchmark = batch_slot == 0
            ordinary_work_id: str | None = None
            ordinary_outcome: str | None = None
            if benchmark:
                batch_merge_request_ids[batch_index].append(external_id)
            else:
                ordinary_slot = 2 if batch_slot in {2, 3} else batch_slot
                ordinary_work_id = f"enterprise-ordinary-{batch_index:04d}-{ordinary_slot}"
                ordinary_outcome = _ordinary_enterprise_outcome(ordinary_work_id)
            task_id = (
                f"ENT-BATCH-TASK-{batch_index:04d}"
                if benchmark
                else f"ER-{sha256(f'ordinary-task:{ordinary_work_id}'.encode()).hexdigest()[:10].upper()}"
            )
            causal_chain_id = f"enterprise-causal-batch-{batch_index:04d}"
            causal_email_id = f"<enterprise-batch-{batch_index:04d}@customer.example>"
            causal_ticket_id = f"ENT-BATCH-TKT-{batch_index:04d}"
            causal_meeting_id = f"ENT-BATCH-MTG-{batch_index:04d}"
            causal_decision_id = f"enterprise-batch-decision-{batch_index:04d}"
            causal_deployment_id = f"ENT-BATCH-DEP-{batch_index:04d}"
            causal_incident_id = f"ENT-BATCH-INC-{batch_index:04d}"
            causal_postmortem_id = f"ENT-BATCH-PM-{batch_index:04d}"
            if benchmark and batch_index not in emitted_batch_chains:
                specs.extend(
                    _enterprise_batch_chain_specs(
                        batch_index,
                        original,
                        project_id,
                        epic_id,
                        task_id,
                        causal_chain_id,
                        causal_email_id,
                        causal_ticket_id,
                        causal_meeting_id,
                        causal_decision_id,
                        causal_deployment_id,
                        causal_incident_id,
                        causal_postmortem_id,
                    )
                )
                task_only_work_id = f"enterprise-ordinary-task-only-{batch_index:04d}"
                task_only_id = f"ER-TASKONLY-{batch_index:04d}"
                task_only_focus = ENTERPRISE_TASK_FOCI[(enterprise_mr_index - 1) % len(ENTERPRISE_TASK_FOCI)]
                task_only_decision_id = (
                    f"enterprise-ordinary-decision-"
                    f"{sha256(task_only_work_id.encode()).hexdigest()[:10].upper()}"
                )
                task_only_payload = _jira_payload(
                    key=task_only_id,
                    row={
                        "summary": f"Investigate {task_only_focus} evidence",
                        "content": _ordinary_task_description(
                            external_id=task_only_id,
                            task_focus=task_only_focus,
                            ordinary_outcome="task_only",
                            theme_index=theme_index,
                            batch_index=batch_index,
                            batch_slot=0,
                            source_context=document.content,
                            project_id=project_id,
                            epic_id=epic_id,
                            decision_id=task_only_decision_id,
                        ),
                        "timestamp": (original + timedelta(hours=1)).isoformat(),
                        "reporter_employee_id": f"E{10 + theme_index % 30:03d}",
                        "assignee_employee_id": f"E{12 + batch_index % 28:03d}",
                    },
                    status="To Do",
                    histories=[],
                )
                task_only_payload.update(
                    {
                        "redwoodEnterpriseWorkId": task_only_work_id,
                        "redwoodEnterpriseStage": "task",
                        "redwoodEnterpriseReferences": {
                            "workId": task_only_work_id,
                            "taskId": task_only_id,
                            "projectId": project_id,
                            "epicId": epic_id,
                            "decisionId": task_only_decision_id,
                        },
                    }
                )
                task_only_decision_payload = _confluence_payload(
                    "decision",
                    task_only_decision_id,
                    {
                        "title": f"Governing decision for {task_only_work_id}",
                        "content": (
                            f"Decision {task_only_decision_id} governs task {task_only_id}. "
                            f"It belongs to epic {epic_id} in project {project_id} and sets the "
                            "evidence boundary for the ordinary maintenance work."
                        ),
                    },
                    original + timedelta(minutes=30),
                )
                task_only_decision_payload.update(
                    {
                        "redwoodEnterpriseWorkId": task_only_work_id,
                        "redwoodEnterpriseStage": "decision",
                        "redwoodEnterpriseReferences": {
                            "workId": task_only_work_id,
                            "taskId": task_only_id,
                            "projectId": project_id,
                            "epicId": epic_id,
                            "decisionId": task_only_decision_id,
                        },
                    }
                )
                specs.append(
                    {
                        "entity_id": immutable_id(
                            "redwood", "confluence-redwood", "page", task_only_decision_id
                        ),
                        "source": "confluence-redwood",
                        "object_type": "page",
                        "external_id": task_only_decision_id,
                        "revision": 1,
                        "original": original + timedelta(minutes=30),
                        "calendar": "enterprise",
                        "container": "ENG",
                        "acl": _event_acl("confluence-redwood", "ENG"),
                        "payload": task_only_decision_payload,
                    }
                )
                specs.append(
                    {
                        "entity_id": immutable_id("redwood", "jira-redwood", "issue", task_only_id),
                        "source": "jira-redwood",
                        "object_type": "issue",
                        "external_id": task_only_id,
                        "revision": 1,
                        "original": original + timedelta(hours=1),
                        "calendar": "enterprise",
                        "container": "ENG",
                        "acl": _event_acl("jira-redwood", "ENG"),
                        "payload": task_only_payload,
                    }
                )
                emitted_batch_chains.add(batch_index)
            if not benchmark and ordinary_work_id not in ordinary_work_groups:
                ordinary_focus = ENTERPRISE_TASK_FOCI[(enterprise_mr_index - 1) % len(ENTERPRISE_TASK_FOCI)]
                ordinary_anchor = max(
                    original,
                    plan_original + timedelta(days=theme_index % 26, hours=2 + 52),
                )
                ordinary_task_at = ordinary_anchor - timedelta(hours=4)
                ordinary_owner = f"E{12 + (theme_index + batch_slot) % 28:03d}"
                ordinary_work_groups[ordinary_work_id] = {
                    "work_id": ordinary_work_id,
                    "task_id": task_id,
                    "outcome": ordinary_outcome,
                    "project_id": project_id,
                    "epic_id": epic_id,
                    "decision_id": (
                        f"enterprise-ordinary-decision-"
                        f"{sha256(ordinary_work_id.encode()).hexdigest()[:10].upper()}"
                    ),
                    "theme_index": theme_index,
                    "original": original,
                    "anchor": ordinary_anchor,
                    "container": container,
                    "merge_request_ids": [],
                    "merged_ids": [],
                    "cancelled_ids": [],
                }
                ordinary_task_row = {
                    "task_id": task_id,
                    "summary": f"{ordinary_outcome.replace('_', ' ').title()} work for {ordinary_focus}",
                    "content": _ordinary_task_description(
                        external_id=task_id,
                        task_focus=ordinary_focus,
                        ordinary_outcome=ordinary_outcome or "maintenance",
                        theme_index=theme_index,
                        batch_index=batch_index,
                        batch_slot=batch_slot,
                        source_context=document.content,
                        project_id=project_id,
                        epic_id=epic_id,
                        decision_id=ordinary_work_groups[ordinary_work_id]["decision_id"],
                    ),
                    "timestamp": ordinary_task_at.isoformat(),
                    "reporter_employee_id": f"E{10 + theme_index % 30:03d}",
                    "assignee_employee_id": ordinary_owner,
                    "project_id": project_id,
                    "epic_id": epic_id,
                    "status": "Cancelled" if ordinary_outcome == "cancelled" else "In Progress",
                }
                ordinary_task_history = _task_lifecycle_history(
                    ordinary_task_row,
                    [],
                    task_index=enterprise_mr_index,
                    task_count=1000,
                )
                task_payload = _jira_payload(
                    key=task_id,
                    row=ordinary_task_row,
                    status=str(ordinary_task_row["status"]),
                    histories=_jira_changelog(ordinary_task_history),
                )
                task_payload.update(
                    {
                        "redwoodEnterpriseWorkId": ordinary_work_id,
                        "redwoodEnterpriseStage": "task",
                        "redwoodEnterpriseReferences": {
                            "taskId": task_id,
                            "projectId": project_id,
                            "epicId": epic_id,
                            "decisionId": ordinary_work_groups[ordinary_work_id]["decision_id"],
                            "workId": ordinary_work_id,
                        },
                    }
                )
                specs.append(
                    {
                        "entity_id": immutable_id("redwood", "jira-redwood", "issue", task_id),
                        "source": "jira-redwood",
                        "object_type": "issue",
                        "external_id": task_id,
                        "revision": 1,
                        "original": ordinary_task_at,
                        "calendar": "enterprise",
                        "container": "ENG",
                        "acl": _event_acl("jira-redwood", "ENG"),
                        "payload": task_payload,
                    }
                )
                ordinary_decision_id = ordinary_work_groups[ordinary_work_id]["decision_id"]
                ordinary_decision_at = ordinary_task_at - timedelta(hours=2)
                decision_lens = DECISION_LENSES[
                    int(sha256(ordinary_work_id.encode()).hexdigest()[:4], 16) % len(DECISION_LENSES)
                ]
                ordinary_decision_payload = _confluence_payload(
                    "decision",
                    ordinary_decision_id,
                    {
                        "title": f"Governing decision for {ordinary_work_id}",
                        "content": (
                            f"Decision {ordinary_decision_id} governs task {task_id} for the "
                            f"{ordinary_outcome.replace('_', ' ')} outcome. It belongs to epic "
                            f"{epic_id} in project {project_id}; the decision sets the source-visible "
                            "scope, owner handoff, and acceptance boundary for this independent work group. "
                            f"Decision lens: {decision_lens}."
                        ),
                    },
                    ordinary_decision_at,
                )
                ordinary_decision_payload.update(
                    {
                        "redwoodEnterpriseWorkId": ordinary_work_id,
                        "redwoodEnterpriseStage": "decision",
                        "redwoodEnterpriseReferences": {
                            "workId": ordinary_work_id,
                            "taskId": task_id,
                            "projectId": project_id,
                            "epicId": epic_id,
                            "decisionId": ordinary_decision_id,
                        },
                    }
                )
                specs.append(
                    {
                        "entity_id": immutable_id(
                            "redwood", "confluence-redwood", "page", ordinary_decision_id
                        ),
                        "source": "confluence-redwood",
                        "object_type": "page",
                        "external_id": ordinary_decision_id,
                        "revision": 1,
                        "original": ordinary_decision_at,
                        "calendar": "enterprise",
                        "container": "ENG",
                        "acl": _event_acl("confluence-redwood", "ENG"),
                        "payload": ordinary_decision_payload,
                    }
                )
            causal_references = {
                "emailId": causal_email_id,
                "ticketId": causal_ticket_id,
                "meetingId": causal_meeting_id,
                "decisionId": causal_decision_id,
                "projectId": project_id,
                "epicId": epic_id,
                "taskId": task_id,
                "deploymentId": causal_deployment_id,
                "incidentId": causal_incident_id,
                "postmortemId": causal_postmortem_id,
                "mergeRequestId": external_id,
            }
            if not benchmark:
                causal_references = {
                    "workId": ordinary_work_id or "",
                    "taskId": task_id,
                    "projectId": project_id,
                    "epicId": epic_id,
                    "decisionId": ordinary_work_groups[ordinary_work_id]["decision_id"],
                    "mergeRequestId": external_id,
                }
                ordinary_work_groups[ordinary_work_id]["merge_request_ids"].append(external_id)
                original = ordinary_work_groups[ordinary_work_id]["anchor"]
            merge_sha = sha256(f"enterprise:{external_id}:merge".encode()).hexdigest()
            cancelled = (not benchmark) and int(sha256(external_id.encode()).hexdigest()[:4], 16) % 101 == 0
            final_state = "closed" if cancelled else "merged"
            if not cancelled:
                ordinary_work_groups.get(ordinary_work_id, {}).get("merged_ids", []).append(external_id)
            else:
                ordinary_work_groups.get(ordinary_work_id, {}).get("cancelled_ids", []).append(external_id)
            for revision, offset, state in (
                (1, timedelta(0), "opened"),
                (2, timedelta(hours=8), "opened"),
                (3, timedelta(days=1), final_state),
            ):
                occurred_at = original + offset
                task_focus = ENTERPRISE_TASK_FOCI[(enterprise_mr_index - 1) % len(ENTERPRISE_TASK_FOCI)]
                if benchmark:
                    description = (
                        f"{document.content}\n\nCloses {task_id}. Review lifecycle for {external_id}. "
                        f"This change follows customer email {causal_email_id}, ticket {causal_ticket_id}, "
                        f"meeting {causal_meeting_id}, and decision {causal_decision_id}. "
                        f"It implements epic {epic_id} in project {project_id}."
                    )
                else:
                    description = (
                        f"{document.content}\n\nCloses {task_id}. This ordinary "
                        f"{ordinary_outcome.replace('_', ' ')} "
                        f"change belongs to {ordinary_work_id}, implements decision {decision_id} "
                        f"in epic {epic_id}, and changes the {task_focus} boundary for {project_id}."
                    )
                if cancelled:
                    description += (
                        " Work was closed because the change was cancelled after scope review; "
                        "no deployment was created."
                    )
                specs.append(
                    {
                        "entity_id": immutable_id("redwood", source, object_type, external_id),
                        "source": source,
                        "object_type": object_type,
                        "external_id": external_id,
                        "revision": revision,
                        "original": occurred_at,
                        "calendar": "enterprise",
                        "container": container,
                        "acl": _event_acl(source, container),
                        "payload": _enterprise_mr_payload(
                            external_id=external_id,
                            title=str(document.provenance.get("title") or external_id),
                            description=description,
                            state=state,
                            revision=revision,
                            original=original,
                            occurred_at=occurred_at,
                            cancelled=cancelled,
                            project_id=project_id,
                            task_focus=task_focus,
                            bucket=bucket,
                            theme_index=theme_index,
                            task_id=task_id,
                            causal_chain_id=causal_chain_id,
                            causal_references=causal_references,
                            work_id=ordinary_work_id,
                            ordinary_outcome=ordinary_outcome,
                            benchmark=benchmark,
                        ),
                        "legacy_payload": {
                            "id": external_id,
                            "iid": external_id,
                            "title": str(document.provenance.get("title") or external_id),
                            "description": description,
                            "state": state,
                            "created_at": original.isoformat(),
                            "updated_at": occurred_at.isoformat(),
                            "merged_at": occurred_at.isoformat() if state == "merged" else None,
                            "merge_commit_sha": merge_sha if state == "merged" else None,
                            "review_status": (
                                "cancelled"
                                if state == "closed"
                                else "approved"
                                if revision >= 2
                                else "pending"
                            ),
                            "close_reason": "cancelled_after_scope_review" if state == "closed" else None,
                            "redwoodCausalChainId": causal_chain_id,
                            "redwoodStage": "merge_request",
                            "redwoodCausalReferences": causal_references,
                            "redwoodTheme": {
                                "projectId": project_id,
                                "epicId": epic_id,
                                "decisionId": decision_id,
                            },
                            "reviewers": [
                                _source_account(f"E{12 + (bucket % 7):03d}"),
                                _source_account(f"E{20 + (theme_index % 9):03d}"),
                            ],
                            "approvals": [
                                {
                                    "user": _source_account(f"E{12 + (bucket % 7):03d}"),
                                    "approved": state == "merged" and revision >= 2,
                                    "at": occurred_at.isoformat(),
                                }
                            ],
                            "pipelines": [
                                {
                                    "id": f"pipeline-{external_id}-{revision}",
                                    "status": (
                                        "success"
                                        if state == "merged"
                                        else "failed"
                                        if revision == 2
                                        else "running"
                                    ),
                                    "ref": f"feature/{external_id.casefold()}",
                                    "stages": ["lint", "unit", "integration"],
                                    "failed_checks": (
                                        [] if state == "merged" else ["integration"] if revision == 2 else []
                                    ),
                                }
                            ],
                            "changed_files": [
                                f"src/{project_id.removeprefix('enterprise-project-')}/handler.py",
                                f"tests/{project_id.removeprefix('enterprise-project-')}/test_handler.py",
                            ],
                            "diff_summary": {
                                "files_changed": 2,
                                "insertions": 18 + bucket * 7,
                                "deletions": 3 + (theme_index % 5),
                            },
                            "commits": [
                                {
                                    "id": sha256(f"{external_id}:commit:{revision}".encode()).hexdigest()[
                                        :12
                                    ],
                                    "message": f"Refine {task_focus} acceptance path",
                                    "author": _source_account(f"E{12 + (bucket % 7):03d}"),
                                }
                            ],
                            "review_comments": [
                                {
                                    "author": _source_account(f"E{20 + (theme_index % 9):03d}"),
                                    "body": (
                                        f"Please attach evidence for {task_focus};"
                                        " the follow-up pipeline must remain green."
                                    ),
                                    "resolved": state == "merged",
                                }
                            ],
                        },
                    }
                )
            continue
        else:
            source, object_type = "confluence-redwood", "page"
            payload = _confluence_payload(
                "enterprise_rag",
                external_id,
                {"title": document.provenance.get("title"), "content": document.content},
                original,
            )
        specs.append(
            {
                "entity_id": immutable_id("redwood", source, object_type, external_id),
                "source": source,
                "object_type": object_type,
                "external_id": external_id,
                "revision": 1,
                "original": original,
                "calendar": "enterprise",
                "container": container,
                "acl": _event_acl(source, container),
                "payload": payload,
            }
        )
    for work_id, group in sorted(ordinary_work_groups.items()):
        merged_ids = list(group["merged_ids"])
        outcome = str(group["outcome"])
        for spec in specs:
            if spec.get("external_id") != group["task_id"] or not isinstance(spec.get("payload"), dict):
                continue
            task_references = spec["payload"].setdefault("redwoodEnterpriseReferences", {})
            task_references.update(
                {
                    "workId": work_id,
                    "taskId": str(group["task_id"]),
                    "projectId": str(group["project_id"]),
                    "epicId": str(group["epic_id"]),
                    "decisionId": str(group["decision_id"]),
                    "mergeRequestIds": list(group["merge_request_ids"]),
                }
            )
            break
        if not merged_ids or group["cancelled_ids"] or outcome in {"task_only", "cancelled"}:
            continue
        deployment_id = f"ENT-ORD-DEP-{sha256(work_id.encode()).hexdigest()[:10].upper()}"
        deployment_changed_files = []
        for spec in specs:
            if spec.get("external_id") not in merged_ids or int(spec.get("revision", 0)) != 3:
                continue
            payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
            deployment_changed_files.extend(payload.get("changed_files") or [])
        deployment_changed_files = list(dict.fromkeys(deployment_changed_files))
        references = {
            "workId": work_id,
            "taskId": str(group["task_id"]),
            "mergeRequestIds": merged_ids,
            "projectId": str(group["project_id"]),
            "epicId": str(group["epic_id"]),
            "decisionId": str(group["decision_id"]),
            "outcome": outcome,
        }
        deployment_payload = _confluence_payload(
            "deployment",
            deployment_id,
            {
                "title": f"{outcome.replace('_', ' ').title()} deployment for {work_id}",
                "content": (
                    f"Deployment {deployment_id} ships merged merge requests {', '.join(merged_ids)} "
                    f"from task {group['task_id']} for {outcome.replace('_', ' ')} work."
                ),
                "changed_files": deployment_changed_files,
            },
            max(
                spec["original"]
                for spec in specs
                if spec.get("external_id") in merged_ids and spec.get("revision") == 3
            )
            + timedelta(hours=6),
        )
        deployment_payload.update(
            {
                "changed_files": deployment_changed_files,
                "redwoodEnterpriseWorkId": work_id,
                "redwoodEnterpriseStage": "deployment",
                "redwoodEnterpriseReferences": references,
            }
        )
        deployment_original = deployment_payload.get("createdAt")
        deployment_at = normalize_timestamp(deployment_original) if deployment_original else group["original"]
        specs.append(
            {
                "entity_id": immutable_id("redwood", "confluence-redwood", "page", deployment_id),
                "source": "confluence-redwood",
                "object_type": "page",
                "external_id": deployment_id,
                "revision": 1,
                "original": deployment_at,
                "calendar": "enterprise",
                "container": "ENG",
                "acl": _event_acl("confluence-redwood", "ENG"),
                "payload": deployment_payload,
            }
        )
        for spec in specs:
            if spec.get("external_id") in merged_ids and isinstance(spec.get("payload"), dict):
                spec["payload"].setdefault("redwoodEnterpriseReferences", {})["deploymentId"] = deployment_id
        if outcome != "incident":
            continue
        incident_id = f"ENT-ORD-INC-{sha256(f'{work_id}:incident'.encode()).hexdigest()[:10].upper()}"
        incident_payload = _jira_payload(
            key=incident_id,
            row={
                "summary": f"Regression after {deployment_id}",
                "content": (
                    f"Incident {incident_id} was caused by deployment {deployment_id}; "
                    f"the ordinary work item {work_id} requires mitigation."
                ),
                "timestamp": (deployment_at + timedelta(hours=8)).isoformat(),
                "reporter_employee_id": "E011",
                "assignee_employee_id": "E011",
            },
            status="Done",
            histories=[],
        )
        incident_payload.update(
            {
                "redwoodStage": "incident",
                "redwoodEnterpriseWorkId": work_id,
                "redwoodEnterpriseStage": "incident",
                "redwoodEnterpriseReferences": {**references, "deploymentId": deployment_id},
            }
        )
        specs.append(
            {
                "entity_id": immutable_id("redwood", "jira-redwood", "issue", incident_id),
                "source": "jira-redwood",
                "object_type": "issue",
                "external_id": incident_id,
                "revision": 1,
                "original": deployment_at + timedelta(hours=8),
                "calendar": "enterprise",
                "container": "ENG",
                "acl": _event_acl("jira-redwood", "ENG"),
                "payload": incident_payload,
            }
        )

    for batch_index, merge_request_ids in batch_merge_request_ids.items():
        deployment_id = f"ENT-BATCH-DEP-{batch_index:04d}"
        for spec in specs:
            if spec.get("external_id") != deployment_id:
                continue
            payload = spec.get("payload")
            if not isinstance(payload, dict):
                continue
            merged_ids = [
                merge_id
                for merge_id in merge_request_ids
                if any(
                    other.get("external_id") == merge_id
                    and isinstance(other.get("payload"), dict)
                    and other["payload"].get("state") == "merged"
                    and int(other.get("revision", 0)) == 3
                    for other in specs
                )
            ]
            references = payload.setdefault("redwoodCausalReferences", {})
            references["mergeRequestIds"] = merged_ids
            payload["mergeRequestIds"] = merged_ids
            payload["content"] = (
                f"Deployment {deployment_id} ships merged merge requests "
                f"{', '.join(merged_ids)} from task {references.get('taskId')}."
            )
            break
    return specs


def _document_identity(document: CorpusDocument) -> tuple[str, str, str]:
    """Return the source identity used for the canonical document projection."""
    provenance = document.provenance
    raw_id = str(provenance.get("dsid") or document.doc_id.rsplit(":", maxsplit=1)[-1])
    if provenance.get("source") == "enterprise_rag":
        if document.source_type.casefold() == "github":
            return "gitlab-redwood", "merge_request", raw_id.replace(":", "-")
        return "confluence-redwood", "page", raw_id.replace(":", "-")
    table = str(provenance.get("table") or document.source_type)
    external_id = f"{table}-{raw_id}"
    if table == "crm_activities":
        return "mail-redwood", "message", f"<crm-{raw_id}@redwood.example>"
    if table == "postmortems":
        return "xwiki-redwood", "page", external_id
    return "confluence-redwood", "page", external_id


def _canonical_fallback_specs(
    documents: list[CorpusDocument], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Ensure every canonical document has one native source projection."""
    covered = {str(spec["entity_id"]) for spec in existing}
    specs: list[dict[str, Any]] = []
    for document in documents:
        source, object_type, external_id = _document_identity(document)
        entity_id = immutable_id("redwood", source, object_type, external_id)
        if entity_id in covered:
            continue
        original = document.timestamp or PROFILE_START.replace(tzinfo=UTC)
        container = _container_for_document(document)
        if source == "mail-redwood":
            payload = _graph_message_payload(
                external_id,
                {"subject": document.provenance.get("title"), "content": document.content},
                original,
            )
        elif source == "xwiki-redwood":
            payload = _xwiki_payload(
                external_id,
                {"title": document.provenance.get("title"), "content": document.content},
                original,
            )
        elif source == "gitlab-redwood":
            payload = {
                "id": external_id,
                "iid": external_id,
                "title": str(document.provenance.get("title") or external_id),
                "description": document.content,
                "state": "opened",
                "created_at": original.isoformat(),
                "updated_at": original.isoformat(),
            }
        else:
            payload = _confluence_payload(
                "canonical",
                external_id,
                {"title": document.provenance.get("title"), "content": document.content},
                original,
            )
        payload["redwood_metadata"] = {
            "canonical_document_id": document.doc_id,
            "provenance": document.provenance,
        }
        specs.append(
            {
                "entity_id": entity_id,
                "source": source,
                "object_type": object_type,
                "external_id": external_id,
                "revision": 1,
                "original": original,
                "calendar": (
                    "external"
                    if document.provenance.get("source")
                    in {"cc_news", "amazon_reviews", "amazon_reviews_2023", "reddit"}
                    else "operational"
                ),
                "container": container,
                "acl": _event_acl(source, container),
                "payload": payload,
            }
        )
        covered.add(entity_id)
    return specs


def _external_action_specs(documents: list[CorpusDocument]) -> list[dict[str, Any]]:
    """Turn external observations into source-native employee work with explicit references."""
    specs: list[dict[str, Any]] = []
    for index, document in enumerate(documents, start=1):
        source_kind = str(document.provenance.get("source") or "").casefold()
        if source_kind not in {"cc_news", "reddit", "amazon_reviews", "amazon_reviews_2023"}:
            continue
        original = document.timestamp or PROFILE_START.replace(tzinfo=UTC)
        canonical_id = document.doc_id
        title = str(document.provenance.get("title") or canonical_id)
        action_id = f"external-action-{source_kind}-{index:04d}"
        chain_id = f"external-chain-{index:04d}"
        meeting_id = f"external-signal-meeting-{index:04d}"
        followup_ticket_id = f"EXT-TKT-{index:04d}"
        followup_decision_id = f"EXT-DEC-{index:04d}"
        followup_task_id = f"EXT-TASK-{index:04d}"
        followup_mr_id = f"EXT-MR-{index:04d}"
        followup_deployment_id = f"EXT-DEP-{index:04d}"
        followup_incident_id = f"EXT-INC-{index:04d}"
        followup_postmortem_id = f"EXT-PM-{index:04d}"
        external_change_slug = re.sub(r"[^a-z0-9]+", "-", canonical_id.casefold()).strip("-")[:60]
        external_account_id = f"A{((index - 1) % 6) + 1:03d}"
        if source_kind == "cc_news":
            outcome_family = "competitive_analysis"
        elif source_kind == "reddit":
            outcome_family = (
                "customer_support"
                if index % 5 in {0, 1}
                else "experiment"
                if index % 5 == 2
                else "incident_remediation"
                if index % 5 == 3
                else "account_risk"
            )
        else:
            outcome_family = (
                "negative_review_fix"
                if index % 4 in {0, 1}
                else "account_risk"
                if index % 4 == 2
                else "incident_remediation"
            )
        action_reference = {
            "actionSource": "",
            "actionObjectType": "",
            "actionExternalId": "",
            "canonicalDocumentId": canonical_id,
        }
        chain_references = {
            **action_reference,
            "ticketId": followup_ticket_id,
            "meetingId": meeting_id,
            "decisionId": followup_decision_id,
            "taskId": followup_task_id,
            "mergeRequestId": followup_mr_id,
            "deploymentId": followup_deployment_id,
            "incidentId": followup_incident_id,
            "postmortemId": followup_postmortem_id,
        }
        if source_kind == "cc_news":
            source, object_type, container = "slack-redwood", "message", "eng-incidents"
            payload = {
                "type": "message",
                "channel": "competitive-intel",
                "ts": (original + timedelta(days=1)).timestamp(),
                "user": "E002",
                "text": (
                    f"Competitive analysis: {title} reports a market change. "
                    f"Source document {canonical_id} should inform the pricing review."
                ),
                "thread_ts": action_id,
            }
        elif source_kind == "reddit":
            source, object_type, container = "mail-redwood", "message", "mail"
            payload = _graph_message_payload(
                f"<{action_id}@redwood.example>",
                {
                    "employee_id": "E003",
                    "subject": f"Customer signal from Reddit: {title}",
                    "body": (
                        f"Customer signal from external document {canonical_id}: {document.content}. "
                        "Please open a support follow-up."
                    ),
                    "account_id": "A001",
                    "customer_email": "signals@customer.example",
                },
                original + timedelta(days=1),
            )
        else:
            source, object_type, container = "jira-redwood", "issue", "SUPPORT"
            row = {
                "ticket_id": action_id,
                "summary": f"Negative review follow-up: {title}",
                "content": (
                    f"Review escalation from external document {canonical_id}: {document.content}. "
                    "Customer support must investigate and respond."
                ),
                "author_employee_id": "E004",
                "reporter_employee_id": "E004",
                "assignee_employee_id": "E005",
                "timestamp": original + timedelta(days=1),
            }
            payload = _jira_payload(key=action_id, row=row, status="To Do", histories=[])
        provider_external_id = action_id if source != "mail-redwood" else f"<{action_id}@redwood.example>"
        action_context = _implicit_context_payload(
            kind=f"external_{outcome_family}_reference",
            target_stage=(
                "decision"
                if outcome_family in {"competitive_analysis", "account_risk"}
                else "deployment"
                if outcome_family == "negative_review_fix"
                else "task"
            ),
            account=str(document.provenance.get("account_id") or "the customer"),
            topic=source_kind.replace("_", " "),
            variant=index,
        )
        phrase = action_context["phrase"]
        if source == "slack-redwood":
            payload["text"] += f" The conversation also refers to {phrase}."
        elif source == "mail-redwood":
            payload["body"]["content"] += f" The customer also refers to {phrase}."
        else:
            payload.setdefault("fields", {}).setdefault("description", "")
            payload["fields"]["description"] += f" The review also discusses {phrase}."
        action_reference.update(
            {
                "actionSource": source,
                "actionObjectType": object_type,
                "actionExternalId": provider_external_id if source == "mail-redwood" else action_id,
            }
        )
        chain_references.update(action_reference)
        payload.update(
            {
                "redwoodExternalAction": True,
                "externalSource": source_kind,
                "canonicalDocumentId": canonical_id,
                "redwoodExternalTitle": title,
                "redwoodExternalChainId": chain_id,
                "redwoodExternalStage": "action",
                "redwoodExternalReferences": chain_references,
                "redwoodReferences": {"accountId": external_account_id},
                "redwoodImplicitContext": action_context,
            }
        )
        specs.append(
            {
                "entity_id": immutable_id("redwood", source, object_type, provider_external_id),
                "source": source,
                "object_type": object_type,
                "external_id": provider_external_id,
                "revision": 1,
                "original": original + timedelta(days=1),
                "calendar": "external",
                "container": container,
                "acl": _event_acl(source, container),
                "payload": payload,
            }
        )
        action_entity_id = immutable_id("redwood", source, object_type, provider_external_id)
        if source_kind == "cc_news":
            analysis_id = f"competitive-analysis-{index:04d}"
            analysis_payload = _confluence_payload(
                "competitive_analysis",
                analysis_id,
                {
                    "title": f"Competitive analysis: {title}",
                    "content": (
                        f"Competitive analysis based on external document {canonical_id}. "
                        f"The product council will evaluate the market signal: {document.content}"
                    ),
                },
                original + timedelta(days=3),
            )
            analysis_payload.update(
                {
                    "redwoodExternalAction": True,
                    "externalSource": source_kind,
                    "canonicalDocumentId": canonical_id,
                    "redwoodExternalTitle": title,
                    "redwoodExternalChainId": chain_id,
                    "redwoodExternalStage": "decision",
                    "redwoodExternalReferences": chain_references,
                    "redwoodReferences": {"accountId": external_account_id},
                    "redwoodImplicitContext": action_context,
                }
            )
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "confluence-redwood", "page", analysis_id),
                    "source": "confluence-redwood",
                    "object_type": "page",
                    "external_id": analysis_id,
                    "revision": 1,
                    "original": original + timedelta(days=3),
                    "calendar": "external",
                    "container": "COMMERCIAL",
                    "acl": _event_acl("confluence-redwood", "COMMERCIAL"),
                    "payload": analysis_payload,
                }
            )
            meeting_payload = {
                "id": meeting_id,
                "title": f"Market signal review: {title}",
                "date": (original + timedelta(days=2)).isoformat(),
                "participants": ["E002", "E003", "E005"],
                "summary": "Review the market signal before recording the competitive decision.",
                "transcript": (
                    f"The team reviewed Slack discussion {action_id} about {canonical_id}. "
                    "They compared pricing and positioning alternatives before the product council "
                    "recorded the competitive analysis."
                ),
                "protocol": {
                    "decision": f"Record competitive analysis {analysis_id} and revisit pricing assumptions.",
                    "owner": "E005",
                    "follow_up": analysis_id,
                },
                "redwoodExternalAction": True,
                "externalSource": source_kind,
                "canonicalDocumentId": canonical_id,
                "redwoodExternalTitle": title,
                "redwoodExternalChainId": chain_id,
                "redwoodExternalStage": "meeting",
                "redwoodExternalReferences": chain_references,
                "redwoodReferences": {"accountId": external_account_id},
            }
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "fireflies-redwood", "transcript", meeting_id),
                    "source": "fireflies-redwood",
                    "object_type": "transcript",
                    "external_id": meeting_id,
                    "revision": 1,
                    "original": original + timedelta(days=2),
                    "calendar": "external",
                    "container": "meetings",
                    "acl": _event_acl("fireflies-redwood", "meetings"),
                    "payload": meeting_payload,
                }
            )
        if outcome_family != "incident_remediation":

            def add_external_stage(
                *,
                stage: str,
                stage_source: str,
                stage_object_type: str,
                stage_external_id: str,
                stage_container: str,
                stage_at: datetime,
                stage_payload: dict[str, Any],
                _source_kind: str = source_kind,
                _canonical_id: str = canonical_id,
                _title: str = title,
                _chain_id: str = chain_id,
                _chain_references: dict[str, str] = chain_references,
                _external_account_id: str = external_account_id,
            ) -> None:
                stage_payload.update(
                    {
                        "redwoodExternalAction": True,
                        "externalSource": _source_kind,
                        "canonicalDocumentId": _canonical_id,
                        "redwoodExternalTitle": _title,
                        "redwoodExternalChainId": _chain_id,
                        "redwoodExternalStage": stage,
                        "redwoodExternalReferences": _chain_references,
                        "redwoodReferences": {"accountId": _external_account_id},
                    }
                )
                specs.append(
                    {
                        "entity_id": immutable_id(
                            "redwood", stage_source, stage_object_type, stage_external_id
                        ),
                        "source": stage_source,
                        "object_type": stage_object_type,
                        "external_id": stage_external_id,
                        "revision": 1,
                        "original": stage_at,
                        "calendar": "external",
                        "container": stage_container,
                        "acl": _event_acl(stage_source, stage_container),
                        "payload": stage_payload,
                    }
                )

            outcome_at = original + timedelta(days=2)
            if outcome_family == "competitive_analysis":
                pass
            elif outcome_family == "account_risk":
                meeting_context = _implicit_context_payload(
                    kind="external_account_risk_reference",
                    target_stage="decision",
                    account=str(document.provenance.get("account_id") or "the account"),
                    topic="renewal risk",
                    variant=index + 2,
                )
                meeting_payload = {
                    "id": meeting_id,
                    "title": f"Account risk review: {title}",
                    "date": outcome_at.isoformat(),
                    "participants": ["E002", "E005", "E008"],
                    "summary": f"Assess the account risk raised by {canonical_id}.",
                    "transcript": (
                        f"The account team reviewed {canonical_id} and discussed "
                        f"{meeting_context['phrase']}. "
                        "They compared renewal evidence and agreed on an owner for the next commercial check."
                    ),
                    "protocol": {
                        "decision": "Keep the renewal risk visible and assign a commercial follow-up.",
                        "owner": "E008",
                        "follow_up": "commercial review",
                    },
                    "redwoodImplicitContext": meeting_context,
                }
                add_external_stage(
                    stage="meeting",
                    stage_source="fireflies-redwood",
                    stage_object_type="transcript",
                    stage_external_id=meeting_id,
                    stage_container="meetings",
                    stage_at=outcome_at,
                    stage_payload=meeting_payload,
                )
                decision_payload = _confluence_payload(
                    "external_decision",
                    followup_decision_id,
                    {
                        "title": f"Renewal decision for {title}",
                        "content": (
                            f"The account risk from {canonical_id} was reviewed in {meeting_id}. "
                            f"Decision: maintain a renewal action for the customer."
                        ),
                    },
                    outcome_at + timedelta(days=1),
                )
                add_external_stage(
                    stage="decision",
                    stage_source="confluence-redwood",
                    stage_object_type="page",
                    stage_external_id=followup_decision_id,
                    stage_container="COMMERCIAL",
                    stage_at=outcome_at + timedelta(days=1),
                    stage_payload=decision_payload,
                )
            elif outcome_family == "customer_support":
                ticket_payload = _jira_payload(
                    key=followup_ticket_id,
                    row={
                        "summary": f"Customer follow-up for {title}",
                        "content": (
                            f"Customer email {provider_external_id} asks for help with {canonical_id}. "
                            "Support will reproduce the reported behavior and assign the next task."
                        ),
                        "timestamp": outcome_at.isoformat(),
                        "reporter_employee_id": "E005",
                        "assignee_employee_id": "E006",
                    },
                    status="Open",
                    histories=[],
                )
                add_external_stage(
                    stage="ticket",
                    stage_source="jira-redwood",
                    stage_object_type="issue",
                    stage_external_id=followup_ticket_id,
                    stage_container="SUPPORT",
                    stage_at=outcome_at,
                    stage_payload=ticket_payload,
                )
                task_payload = _jira_payload(
                    key=followup_task_id,
                    row={
                        "summary": f"Reproduce {title}",
                        "content": (
                            f"Resolve ticket {followup_ticket_id} from {canonical_id}; capture the "
                            "customer-visible result and the safe workaround."
                        ),
                        "timestamp": (outcome_at + timedelta(days=2)).isoformat(),
                        "reporter_employee_id": "E006",
                        "assignee_employee_id": "E007",
                    },
                    status="In Progress",
                    histories=[],
                )
                add_external_stage(
                    stage="task",
                    stage_source="jira-redwood",
                    stage_object_type="issue",
                    stage_external_id=followup_task_id,
                    stage_container="ENG",
                    stage_at=outcome_at + timedelta(days=2),
                    stage_payload=task_payload,
                )
            elif outcome_family == "negative_review_fix":
                ticket_payload = _jira_payload(
                    key=followup_ticket_id,
                    row={
                        "summary": f"Investigate review complaint: {title}",
                        "content": (
                            f"Negative review {canonical_id} created a support ticket. "
                            "Engineering must reproduce the setup failure before proposing a fix."
                        ),
                        "timestamp": outcome_at.isoformat(),
                        "reporter_employee_id": "E004",
                        "assignee_employee_id": "E006",
                    },
                    status="Open",
                    histories=[],
                )
                add_external_stage(
                    stage="ticket",
                    stage_source="jira-redwood",
                    stage_object_type="issue",
                    stage_external_id=followup_ticket_id,
                    stage_container="SUPPORT",
                    stage_at=outcome_at,
                    stage_payload=ticket_payload,
                )
                task_payload = _jira_payload(
                    key=followup_task_id,
                    row={
                        "summary": "Fix the reviewed integration issue",
                        "content": (
                            f"Implement the fix for {followup_ticket_id} after reviewing {canonical_id}."
                        ),
                        "timestamp": (outcome_at + timedelta(days=2)).isoformat(),
                        "reporter_employee_id": "E006",
                        "assignee_employee_id": "E007",
                    },
                    status="In Progress",
                    histories=[],
                )
                add_external_stage(
                    stage="task",
                    stage_source="jira-redwood",
                    stage_object_type="issue",
                    stage_external_id=followup_task_id,
                    stage_container="ENG",
                    stage_at=outcome_at + timedelta(days=2),
                    stage_payload=task_payload,
                )
                mr_payload = {
                    "id": followup_mr_id,
                    "iid": followup_mr_id,
                    "title": "Fix integration issue from customer review",
                    "description": f"Closes {followup_task_id}; addresses evidence from {canonical_id}.",
                    "state": "merged",
                    "created_at": (outcome_at + timedelta(days=3)).isoformat(),
                    "updated_at": (outcome_at + timedelta(days=4)).isoformat(),
                    "merged_at": (outcome_at + timedelta(days=4)).isoformat(),
                    "merge_commit_sha": sha256(f"{followup_mr_id}:merge".encode()).hexdigest(),
                    "reviewers": [_source_account("E011")],
                    "approvals": [{"user": _source_account("E011"), "approved": True}],
                    "changed_files": ["src/support/adapter.py", "tests/support/adapter_test.py"],
                    "diff_summary": {
                        "files_changed": 2,
                        "insertions": 18,
                        "deletions": 4,
                        "paths": ["src/support/adapter.py", "tests/support/adapter_test.py"],
                    },
                    "commits": [
                        {
                            "id": sha256(f"{followup_mr_id}:commit".encode()).hexdigest()[:12],
                            "message": "Fix reviewed integration path",
                            "paths": ["src/support/adapter.py", "tests/support/adapter_test.py"],
                            "author": _source_account("E007"),
                        }
                    ],
                    "review_comments": [
                        {
                            "author": _source_account("E011"),
                            "body": "The customer reproduction is covered in src/support/adapter.py.",
                            "resolved": True,
                        }
                    ],
                }
                add_external_stage(
                    stage="merge_request",
                    stage_source="gitlab-redwood",
                    stage_object_type="merge_request",
                    stage_external_id=followup_mr_id,
                    stage_container="ENG",
                    stage_at=outcome_at + timedelta(days=3),
                    stage_payload=mr_payload,
                )
                deployment_payload = _confluence_payload(
                    "deployment",
                    followup_deployment_id,
                    {
                        "title": f"Deployment for {title}",
                        "content": (
                            f"Deployment {followup_deployment_id} ships {followup_mr_id} after the review."
                        ),
                    },
                    outcome_at + timedelta(days=5),
                )
                add_external_stage(
                    stage="deployment",
                    stage_source="confluence-redwood",
                    stage_object_type="page",
                    stage_external_id=followup_deployment_id,
                    stage_container="ENG",
                    stage_at=outcome_at + timedelta(days=5),
                    stage_payload=deployment_payload,
                )
            elif outcome_family == "experiment":
                meeting_context = _implicit_context_payload(
                    kind="external_experiment_reference",
                    target_stage="task",
                    account="the product team",
                    topic="experiment scope",
                    variant=index + 3,
                )
                meeting_payload = {
                    "id": meeting_id,
                    "title": f"Experiment review: {title}",
                    "date": outcome_at.isoformat(),
                    "participants": ["E003", "E007", "E011"],
                    "summary": "Decide whether the external signal merits a controlled experiment.",
                    "transcript": (
                        f"The team reviewed {canonical_id} and discussed {meeting_context['phrase']}."
                    ),
                    "protocol": {
                        "decision": "Run a bounded experiment.",
                        "owner": "E007",
                        "follow_up": followup_task_id,
                    },
                    "redwoodImplicitContext": meeting_context,
                }
                add_external_stage(
                    stage="meeting",
                    stage_source="fireflies-redwood",
                    stage_object_type="transcript",
                    stage_external_id=meeting_id,
                    stage_container="meetings",
                    stage_at=outcome_at,
                    stage_payload=meeting_payload,
                )
                decision_payload = _confluence_payload(
                    "external_decision",
                    followup_decision_id,
                    {
                        "title": "Experiment decision",
                        "content": f"Decision follows {meeting_id} and tests the signal from {canonical_id}.",
                    },
                    outcome_at + timedelta(days=1),
                )
                add_external_stage(
                    stage="decision",
                    stage_source="confluence-redwood",
                    stage_object_type="page",
                    stage_external_id=followup_decision_id,
                    stage_container="ENG",
                    stage_at=outcome_at + timedelta(days=1),
                    stage_payload=decision_payload,
                )
                task_payload = _jira_payload(
                    key=followup_task_id,
                    row={
                        "summary": "Run external signal experiment",
                        "content": f"Implement {followup_decision_id} from {meeting_id}.",
                        "timestamp": (outcome_at + timedelta(days=2)).isoformat(),
                        "reporter_employee_id": "E007",
                        "assignee_employee_id": "E011",
                    },
                    status="In Progress",
                    histories=[],
                )
                add_external_stage(
                    stage="task",
                    stage_source="jira-redwood",
                    stage_object_type="issue",
                    stage_external_id=followup_task_id,
                    stage_container="ENG",
                    stage_at=outcome_at + timedelta(days=2),
                    stage_payload=task_payload,
                )
                mr_payload = {
                    "id": followup_mr_id,
                    "iid": followup_mr_id,
                    "title": "Run bounded signal experiment",
                    "description": f"Closes {followup_task_id}; tests {canonical_id}.",
                    "state": "merged",
                    "created_at": (outcome_at + timedelta(days=3)).isoformat(),
                    "updated_at": (outcome_at + timedelta(days=4)).isoformat(),
                    "merged_at": (outcome_at + timedelta(days=4)).isoformat(),
                    "merge_commit_sha": sha256(f"{followup_mr_id}:merge".encode()).hexdigest(),
                    "reviewers": [_source_account("E011"), _source_account("E012")],
                    "approvals": [{"user": _source_account("E011"), "approved": True}],
                    "changed_files": [
                        "experiments/signal/config.yaml",
                        "experiments/signal/runner.py",
                        "tests/experiments/signal_test.py",
                    ],
                    "diff_summary": {
                        "files_changed": 3,
                        "insertions": 24,
                        "deletions": 5,
                        "paths": [
                            "experiments/signal/config.yaml",
                            "experiments/signal/runner.py",
                            "tests/experiments/signal_test.py",
                        ],
                    },
                    "commits": [
                        {
                            "id": sha256(f"{followup_mr_id}:commit".encode()).hexdigest()[:12],
                            "message": "Add bounded signal experiment",
                            "paths": [
                                "experiments/signal/config.yaml",
                                "experiments/signal/runner.py",
                            ],
                            "author": _source_account("E011"),
                        }
                    ],
                    "review_comments": [
                        {
                            "author": _source_account("E012"),
                            "body": (
                                "The experiment has a measurable stop condition in "
                                "experiments/signal/runner.py."
                            ),
                            "resolved": True,
                        }
                    ],
                }
                add_external_stage(
                    stage="merge_request",
                    stage_source="gitlab-redwood",
                    stage_object_type="merge_request",
                    stage_external_id=followup_mr_id,
                    stage_container="ENG",
                    stage_at=outcome_at + timedelta(days=3),
                    stage_payload=mr_payload,
                )
            continue
        if index >= 1:
            meeting_id = f"external-signal-meeting-{index:04d}"
            chain_id = f"external-chain-{index:04d}"
            followup_ticket_id = f"EXT-TKT-{index:04d}"
            followup_decision_id = f"EXT-DEC-{index:04d}"
            followup_task_id = f"EXT-TASK-{index:04d}"
            followup_mr_id = f"EXT-MR-{index:04d}"
            followup_deployment_id = f"EXT-DEP-{index:04d}"
            followup_incident_id = f"EXT-INC-{index:04d}"
            followup_postmortem_id = f"EXT-PM-{index:04d}"
            action_reference = {
                "actionSource": source,
                "actionObjectType": object_type,
                "actionExternalId": provider_external_id,
                "canonicalDocumentId": canonical_id,
            }
            chain_references = {
                **action_reference,
                "ticketId": followup_ticket_id,
                "meetingId": meeting_id,
                "decisionId": followup_decision_id,
                "taskId": followup_task_id,
                "mergeRequestId": followup_mr_id,
                "deploymentId": followup_deployment_id,
                "incidentId": followup_incident_id,
                "postmortemId": followup_postmortem_id,
            }
            for action_spec in reversed(specs):
                if action_spec.get("entity_id") == action_entity_id:
                    action_spec["payload"].update(
                        {
                            "redwoodExternalChainId": chain_id,
                            "redwoodExternalStage": "action",
                            "redwoodExternalReferences": chain_references,
                        }
                    )
                    break
            meeting_payload = {
                "id": meeting_id,
                "title": f"External signal review: {title}",
                "date": (original + timedelta(days=3)).isoformat(),
                "participants": ["E002", "E003", "E005"],
                "summary": f"Review external document {canonical_id}",
                "transcript": (
                    f"The team reviewed external document {canonical_id} and agreed to track "
                    "the signal in customer support and product planning."
                ),
                "protocol": {
                    "decision": f"Create a follow-up action for {canonical_id}",
                    "owner": "E005",
                    "follow_up": action_id,
                },
                "redwoodExternalAction": True,
                "externalSource": source_kind,
                "canonicalDocumentId": canonical_id,
                "redwoodExternalTitle": title,
                "redwoodExternalChainId": chain_id,
                "redwoodExternalStage": "meeting",
                "redwoodExternalReferences": chain_references,
            }
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "fireflies-redwood", "transcript", meeting_id),
                    "source": "fireflies-redwood",
                    "object_type": "transcript",
                    "external_id": meeting_id,
                    "revision": 1,
                    "original": original + timedelta(days=3),
                    "calendar": "external",
                    "container": "meetings",
                    "acl": _event_acl("fireflies-redwood", "meetings"),
                    "payload": meeting_payload,
                }
            )
            chain_at = original + timedelta(days=4)
            ticket_payload = _jira_payload(
                key=followup_ticket_id,
                row={
                    "summary": f"Follow up external signal {title}",
                    "content": (
                        f"Ticket {followup_ticket_id} was created from employee action "
                        f"{provider_external_id} for external document {canonical_id}."
                    ),
                    "timestamp": chain_at.isoformat(),
                    "reporter_employee_id": "E005",
                    "assignee_employee_id": "E006",
                },
                status="Open",
                histories=[],
            )
            ticket_payload.update(
                {
                    "redwoodExternalChainId": chain_id,
                    "redwoodExternalStage": "ticket",
                    "redwoodExternalReferences": chain_references,
                }
            )
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "jira-redwood", "issue", followup_ticket_id),
                    "source": "jira-redwood",
                    "object_type": "issue",
                    "external_id": followup_ticket_id,
                    "revision": 1,
                    "original": chain_at,
                    "calendar": "external",
                    "container": "SUPPORT",
                    "acl": _event_acl("jira-redwood", "SUPPORT"),
                    "payload": ticket_payload,
                }
            )
            decision_payload = _confluence_payload(
                "external_decision",
                followup_decision_id,
                {
                    "title": f"Decision from external signal {index:04d}",
                    "content": (
                        f"Decision {followup_decision_id} follows meeting {meeting_id} about "
                        f"ticket {followup_ticket_id}; the team will track the signal through "
                        f"task {followup_task_id}."
                    ),
                },
                chain_at + timedelta(days=1),
            )
            decision_payload.update(
                {
                    "redwoodExternalChainId": chain_id,
                    "redwoodExternalStage": "decision",
                    "redwoodExternalReferences": chain_references,
                }
            )
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "confluence-redwood", "page", followup_decision_id),
                    "source": "confluence-redwood",
                    "object_type": "page",
                    "external_id": followup_decision_id,
                    "revision": 1,
                    "original": chain_at + timedelta(days=1),
                    "calendar": "external",
                    "container": "ENG",
                    "acl": _event_acl("confluence-redwood", "ENG"),
                    "payload": decision_payload,
                }
            )
            task_payload = _jira_payload(
                key=followup_task_id,
                row={
                    "summary": f"Implement response to external signal {index:04d}",
                    "content": (
                        f"Implement decision {followup_decision_id} from meeting {meeting_id}; "
                        f"source document is {canonical_id}."
                    ),
                    "timestamp": (chain_at + timedelta(days=2)).isoformat(),
                    "reporter_employee_id": "E006",
                    "assignee_employee_id": "E007",
                },
                status="In Progress",
                histories=[],
            )
            task_payload.update(
                {
                    "redwoodExternalChainId": chain_id,
                    "redwoodExternalStage": "task",
                    "redwoodExternalReferences": chain_references,
                }
            )
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "jira-redwood", "issue", followup_task_id),
                    "source": "jira-redwood",
                    "object_type": "issue",
                    "external_id": followup_task_id,
                    "revision": 1,
                    "original": chain_at + timedelta(days=2),
                    "calendar": "external",
                    "container": "ENG",
                    "acl": _event_acl("jira-redwood", "ENG"),
                    "payload": task_payload,
                }
            )
            merge_payload = {
                "id": followup_mr_id,
                "iid": followup_mr_id,
                "title": f"External signal response {index:04d}",
                "description": (
                    f"Closes {followup_task_id}. Implements {followup_decision_id} "
                    f"after reviewing {canonical_id}."
                ),
                "state": "merged",
                "created_at": (chain_at + timedelta(days=3)).isoformat(),
                "updated_at": (chain_at + timedelta(days=4)).isoformat(),
                "merged_at": (chain_at + timedelta(days=4)).isoformat(),
                "merge_commit_sha": sha256(f"{followup_mr_id}:merge".encode()).hexdigest(),
                "review_status": "approved",
                "redwoodExternalChainId": chain_id,
                "redwoodExternalStage": "merge_request",
                "redwoodExternalReferences": chain_references,
            }
            generic_change_path = f"src/external/{outcome_family}/{external_change_slug}/response.py"
            merge_payload.update(
                {
                    "reviewers": [_source_account("E011")],
                    "approvals": [{"user": _source_account("E011"), "approved": True}],
                    "changed_files": [generic_change_path],
                    "diff_summary": {
                        "files_changed": 1,
                        "insertions": 12,
                        "deletions": 2,
                        "paths": [generic_change_path],
                    },
                    "commits": [
                        {
                            "id": sha256(f"{followup_mr_id}:commit".encode()).hexdigest()[:12],
                            "message": f"Implement {outcome_family} response in {generic_change_path}",
                            "paths": [generic_change_path],
                            "author": _source_account("E011"),
                        }
                    ],
                    "review_comments": [
                        {
                            "author": _source_account("E011"),
                            "body": f"The {outcome_family} response is covered in {generic_change_path}.",
                            "resolved": True,
                        }
                    ],
                }
            )
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "gitlab-redwood", "merge_request", followup_mr_id),
                    "source": "gitlab-redwood",
                    "object_type": "merge_request",
                    "external_id": followup_mr_id,
                    "revision": 1,
                    "original": chain_at + timedelta(days=4),
                    "calendar": "external",
                    "container": "ENG",
                    "acl": _event_acl("gitlab-redwood", "ENG"),
                    "payload": merge_payload,
                }
            )
            deployment_payload = _confluence_payload(
                "deployment",
                followup_deployment_id,
                {
                    "title": f"Deployment {followup_deployment_id}",
                    "content": (
                        f"Deployment {followup_deployment_id} ships merge request {followup_mr_id} "
                        f"for external document {canonical_id}."
                    ),
                },
                chain_at + timedelta(days=5),
            )
            deployment_payload.update(
                {
                    "redwoodExternalChainId": chain_id,
                    "redwoodExternalStage": "deployment",
                    "redwoodExternalReferences": chain_references,
                }
            )
            specs.append(
                {
                    "entity_id": immutable_id(
                        "redwood", "confluence-redwood", "page", followup_deployment_id
                    ),
                    "source": "confluence-redwood",
                    "object_type": "page",
                    "external_id": followup_deployment_id,
                    "revision": 1,
                    "original": chain_at + timedelta(days=5),
                    "calendar": "external",
                    "container": "ENG",
                    "acl": _event_acl("confluence-redwood", "ENG"),
                    "payload": deployment_payload,
                }
            )
            incident_payload = _jira_payload(
                key=followup_incident_id,
                row={
                    "summary": f"Monitor outcome of external signal {index:04d}",
                    "content": (
                        f"Incident {followup_incident_id} records the outcome of deployment "
                        f"{followup_deployment_id}."
                    ),
                    "timestamp": (chain_at + timedelta(days=6)).isoformat(),
                    "reporter_employee_id": "E008",
                    "assignee_employee_id": "E009",
                },
                status="Done",
                histories=[],
            )
            incident_payload.update(
                {
                    "redwoodExternalChainId": chain_id,
                    "redwoodExternalStage": "incident",
                    "redwoodExternalReferences": chain_references,
                }
            )
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "jira-redwood", "issue", followup_incident_id),
                    "source": "jira-redwood",
                    "object_type": "issue",
                    "external_id": followup_incident_id,
                    "revision": 1,
                    "original": chain_at + timedelta(days=6),
                    "calendar": "external",
                    "container": "ENG",
                    "acl": _event_acl("jira-redwood", "ENG"),
                    "payload": incident_payload,
                }
            )
            postmortem_payload = _xwiki_payload(
                followup_postmortem_id,
                {
                    "title": f"Postmortem {followup_incident_id}",
                    "content": (
                        f"Postmortem {followup_postmortem_id} reviews incident {followup_incident_id}, "
                        f"deployment {followup_deployment_id}, and external document {canonical_id}."
                    ),
                },
                chain_at + timedelta(days=7),
            )
            postmortem_payload.update(
                {
                    "redwoodExternalChainId": chain_id,
                    "redwoodExternalStage": "postmortem",
                    "redwoodExternalReferences": chain_references,
                }
            )
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "xwiki-redwood", "page", followup_postmortem_id),
                    "source": "xwiki-redwood",
                    "object_type": "page",
                    "external_id": followup_postmortem_id,
                    "revision": 1,
                    "original": chain_at + timedelta(days=7),
                    "calendar": "external",
                    "container": "ENG",
                    "acl": _event_acl("xwiki-redwood", "ENG"),
                    "payload": postmortem_payload,
                }
            )
    return specs


def _readable_alias(spec: dict[str, Any], payload: dict[str, Any]) -> str:
    source = str(spec["source"])
    external_id = str(spec["external_id"])
    object_type = str(spec["object_type"])
    if source == "jira-redwood":
        return f"jira.{external_id}"
    if source == "confluence-redwood":
        return f"confluence.page-{external_id}"
    if source == "mail-redwood":
        return f"mail.{payload.get('id', external_id)}"
    if source == "gitlab-redwood":
        return f"gitlab.platform-inference.mr-{external_id}".replace("/", "-")
    if source == "xwiki-redwood":
        return f"xwiki.{external_id}"
    provider = source.removesuffix("-redwood")
    return f"{provider}.{object_type}-{external_id}".replace("/", "-")


def _align_payload_time(
    payload: dict[str, Any], source: str, world_at: datetime, created_at: datetime
) -> dict[str, Any]:
    """Move structured provider timestamps into the same company calendar as the event."""
    aligned = deepcopy(payload)
    current = world_at.isoformat()
    created = created_at.isoformat()
    if source == "jira-redwood":
        fields = aligned.get("fields", {})
        fields["created"] = created
        fields["updated"] = current
        for history in aligned.get("changelog", {}).get("histories", []):
            # Changelog timestamps are source-native status transitions. Keep
            # their relative chronology instead of collapsing every entry onto
            # the materialized snapshot timestamp.
            history.setdefault("created", current)
    elif source == "confluence-redwood":
        aligned["createdAt"] = created
        aligned.setdefault("version", {})["createdAt"] = current
    elif source == "mail-redwood":
        aligned["receivedDateTime"] = current
        aligned["sentDateTime"] = current
        aligned.setdefault(
            "conversationId",
            f"rw-source-{sha256(str(aligned.get('id', 'message')).encode()).hexdigest()[:16]}",
        )
    elif source == "gitlab-redwood":
        aligned["created_at"] = created
        aligned["updated_at"] = current
        if aligned.get("state") == "merged":
            aligned["merged_at"] = current
    elif source == "xwiki-redwood":
        aligned["created"] = created
        aligned["modified"] = current
    elif source == "slack-redwood":
        aligned["ts"] = f"{world_at.timestamp():.6f}"
    elif source == "gmail-redwood":
        aligned["internalDate"] = str(int(world_at.timestamp() * 1000))
    elif source == "hubspot-redwood":
        aligned["createdAt"] = created
        aligned["updatedAt"] = current
    elif source == "linear-redwood":
        aligned["createdAt"] = created
        aligned["updatedAt"] = current
    elif source == "fireflies-redwood":
        aligned["date"] = current
    elif source == "google-drive-redwood":
        aligned["createdTime"] = created
        aligned["modifiedTime"] = current
    return aligned


def _schedule_specs(specs: list[dict[str, Any]]) -> list[tuple[datetime, dict[str, Any]]]:
    """Distribute each data layer across one shared 180-day company calendar."""

    def after(previous: datetime, spec: dict[str, Any]) -> datetime:
        """Return a deterministic timestamp strictly after ``previous``."""
        candidate = previous + timedelta(minutes=10)
        scheduled = _business_calendar_time(candidate, spec)
        if scheduled <= previous:
            scheduled = candidate
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        stage = str(
            payload.get("redwoodStage")
            or payload.get("redwoodExternalStage")
            or payload.get("redwoodEnterpriseStage")
            or spec.get("object_type")
            or ""
        )
        if stage in {"incident", "mitigation", "emergency_deployment"}:
            return scheduled
        while scheduled.weekday() >= 5 or scheduled.hour >= 18:
            scheduled = (scheduled + timedelta(days=1)).replace(
                hour=8,
                minute=0,
                second=0,
                microsecond=0,
            )
        if scheduled.hour < 8:
            scheduled = scheduled.replace(hour=8, minute=0, second=0, microsecond=0)
        return scheduled

    profile_start = PROFILE_START.replace(tzinfo=UTC)
    result: list[tuple[datetime, dict[str, Any]]] = []
    for calendar in ("operational", "causal", "business", "enterprise", "external", "email", "weekly"):
        group = [spec for spec in specs if spec.get("calendar", "operational") == calendar]
        if not group:
            continue
        if calendar == "weekly":
            result.extend((_business_calendar_time(spec["original"], spec), spec) for spec in group)
            continue
        if calendar in {"enterprise", "external"}:
            chain_key = "redwoodCausalChainId" if calendar == "enterprise" else "redwoodExternalChainId"
            stage_key = "redwoodStage" if calendar == "enterprise" else "redwoodExternalStage"
            stage_order = {
                "action": 0,
                "customer_email": 1,
                "ticket": 2,
                "meeting": 3,
                "decision": 4,
                "task": 5,
                "merge_request": 6,
                "deployment": 7,
                "incident": 8,
                "postmortem": 9,
            }
            chained: dict[str, list[dict[str, Any]]] = defaultdict(list)
            unchained: list[dict[str, Any]] = []
            for spec in group:
                payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
                chain_id = payload.get(chain_key)
                if chain_id and str(payload.get(stage_key)) in stage_order:
                    chained[str(chain_id)].append(spec)
                else:
                    unchained.append(spec)

            ordered_chains = sorted(
                chained,
                key=lambda chain_id: (
                    0 if str(chain_id).startswith("enterprise-causal-batch-") else 1,
                    str(chain_id),
                ),
            )
            chain_count = max(1, len(ordered_chains) - 1)
            for chain_rank, chain_id in enumerate(ordered_chains):
                chain_start = profile_start + timedelta(
                    days=3 + (166 * chain_rank // chain_count),
                    hours=8 + (chain_rank % 4),
                )
                while chain_start.weekday() >= 5:
                    chain_start += timedelta(days=1)
                chain_employee_ids = set().union(
                    *(_payload_employee_ids(item.get("payload")) for item in chained[chain_id])
                )
                vacation_ends = []
                for employee_id in chain_employee_ids:
                    if not employee_id[1:].isdigit():
                        continue
                    number = int(employee_id[1:])
                    if number < 1 or (number - 1) % 9 != 0:
                        continue
                    vacation_ends.append(
                        (
                            profile_start + timedelta(days=30 + number - 1),
                            profile_start + timedelta(days=37 + number - 1),
                        )
                    )
                for vacation_start, vacation_end in vacation_ends:
                    if vacation_start <= chain_start < vacation_end:
                        chain_start = vacation_end.replace(hour=8, minute=0)
                while chain_start.weekday() >= 5:
                    chain_start += timedelta(days=1)
                by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for spec in chained[chain_id]:
                    payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
                    by_stage[str(payload.get(stage_key))].append(spec)
                stage_offsets = _chain_stage_offsets(chain_id, set(by_stage))
                previous: datetime | None = None
                for stage, stage_specs in sorted(by_stage.items(), key=lambda item: stage_order[item[0]]):
                    stage_specs.sort(
                        key=lambda spec: (
                            str(spec.get("entity_id")),
                            int(spec.get("revision", 0)),
                        )
                    )
                    stage_at = chain_start + stage_offsets.get(
                        stage, timedelta(minutes=stage_order[stage] * 15)
                    )
                    for offset, spec in enumerate(stage_specs):
                        world_at = stage_at + timedelta(minutes=offset * (17 + chain_rank % 13))
                        world_at = _business_calendar_time(world_at, spec)
                        if previous is not None and world_at <= previous:
                            world_at = after(previous, spec)
                        result.append((world_at, spec))
                        previous = world_at
            source_document_times: dict[str, datetime] = {}
            for scheduled_at, spec in result:
                payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
                if payload.get("redwoodExternalStage") != "action":
                    continue
                canonical_id = payload.get("canonicalDocumentId")
                if canonical_id:
                    source_document_times[str(canonical_id)] = scheduled_at
            roots = [
                spec
                for spec in unchained
                if str(spec.get("external_id", "")).startswith(
                    ("enterprise-project-", "enterprise-epic-", "enterprise-decision-")
                )
            ]
            for root_index, spec in enumerate(
                sorted(
                    roots,
                    key=lambda item: (
                        int(str(item.get("external_id", "0")).rsplit("-", 1)[-1])
                        if str(item.get("external_id", "0")).rsplit("-", 1)[-1].isdigit()
                        else 0,
                        0
                        if str(item.get("external_id", "")).startswith("enterprise-project-")
                        else 1
                        if str(item.get("external_id", "")).startswith("enterprise-epic-")
                        else 2,
                        str(item.get("external_id")),
                    ),
                )
            ):
                external_id = str(spec.get("external_id", ""))
                suffix = external_id.rsplit("-", 1)[-1]
                theme_index = int(suffix) if suffix.isdigit() else root_index + 1
                root_days = _working_days(profile_start)
                root_day = root_days[min(len(root_days) - 1, (theme_index - 1) * len(root_days) // 64)]
                root_time = datetime.combine(root_day, datetime.min.time(), tzinfo=UTC).replace(
                    hour=9 + root_index % 3
                )
                result.append((_business_calendar_time(root_time, spec), spec))
            unchained = [spec for spec in unchained if spec not in roots]
            if unchained:
                earliest = min(spec["original"] for spec in unchained)
                latest = max(spec["original"] for spec in unchained)
                span = latest - earliest
                for spec in unchained:
                    payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
                    metadata = payload.get("redwood_metadata")
                    canonical_id = (
                        metadata.get("canonical_document_id") if isinstance(metadata, dict) else None
                    )
                    source_time = source_document_times.get(str(canonical_id)) if canonical_id else None
                    if source_time is not None:
                        calendar_time = source_time - timedelta(minutes=5)
                        result.append((calendar_time, spec))
                    else:
                        ratio = (spec["original"] - earliest) / span if span else 0
                        calendar_time = profile_start + timedelta(days=25) + timedelta(days=145 * ratio)
                        result.append((_business_calendar_time(calendar_time, spec), spec))
            continue
        if calendar == "email":
            ordered_specs = sorted(
                group,
                key=lambda spec: (
                    spec["original"],
                    str(spec.get("entity_id")),
                    int(spec.get("revision", 0)),
                ),
            )
            original_days = sorted({spec["original"].date() for spec in ordered_specs})
            working_days = [
                (profile_start + timedelta(days=offset)).date()
                for offset in range(180)
                if (profile_start + timedelta(days=offset)).weekday() < 5
            ]
            day_rank = {
                day: round(index * (len(working_days) - 1) / max(1, len(original_days) - 1))
                for index, day in enumerate(original_days)
            }

            def vacation_conflict(candidate: datetime, spec: dict[str, Any]) -> bool:
                for employee_id in _payload_employee_ids(spec.get("payload")):
                    if not employee_id.startswith("E") or not employee_id[1:].isdigit():
                        continue
                    number = int(employee_id[1:])
                    if number < 1 or (number - 1) % 9 != 0:
                        continue
                    vacation_start = profile_start + timedelta(days=30 + number - 1)
                    vacation_end = vacation_start + timedelta(days=7)
                    if vacation_start <= candidate < vacation_end:
                        return True
                return False

            for spec in ordered_specs:
                rank = day_rank[spec["original"].date()]
                hour = min(17, max(8, spec["original"].hour))
                minute = spec["original"].minute
                candidates: list[datetime] = []
                for distance in range(len(working_days)):
                    for candidate_rank in (rank + distance, rank - distance):
                        if 0 <= candidate_rank < len(working_days):
                            candidates.append(
                                datetime.combine(
                                    working_days[candidate_rank],
                                    datetime.min.time(),
                                    tzinfo=UTC,
                                ).replace(hour=hour, minute=minute)
                            )
                    if candidates and not vacation_conflict(candidates[-1], spec):
                        break
                candidate = next(
                    (value for value in candidates if not vacation_conflict(value, spec)),
                    candidates[0],
                )
                result.append((candidate, spec))
            continue
        if calendar == "operational":
            source_objects = sorted({(str(spec["source"]), str(spec["object_type"])) for spec in group})
            for source, object_type in source_objects:
                source_group = sorted(
                    [
                        spec
                        for spec in group
                        if str(spec["source"]) == source and str(spec["object_type"]) == object_type
                    ],
                    key=lambda spec: (
                        spec["original"],
                        str(spec.get("entity_id")),
                        int(spec.get("revision", 0)),
                    ),
                )
                for index, spec in enumerate(source_group):
                    result.append(
                        (
                            _balanced_working_time(
                                index=index,
                                total=len(source_group),
                                profile_start=profile_start,
                                spec=spec,
                                source_time=spec["original"],
                            ),
                            spec,
                        )
                    )
            continue
        if calendar in {"causal", "business"}:
            source_objects = sorted({(str(spec["source"]), str(spec["object_type"])) for spec in group})
            for source, object_type in source_objects:
                source_group = sorted(
                    [
                        spec
                        for spec in group
                        if str(spec["source"]) == source and str(spec["object_type"]) == object_type
                    ],
                    key=lambda spec: (
                        spec["original"],
                        str(spec.get("entity_id")),
                        int(spec.get("revision", 0)),
                    ),
                )
                for index, spec in enumerate(source_group):
                    result.append(
                        (
                            _balanced_working_time(
                                index=index,
                                total=len(source_group),
                                profile_start=profile_start,
                                spec=spec,
                                source_time=spec["original"],
                            ),
                            spec,
                        )
                    )
            continue
        for source in sorted({str(spec["source"]) for spec in group}):
            source_group = [spec for spec in group if str(spec["source"]) == source]
            original_by_entity: dict[str, datetime] = {}
            for spec in source_group:
                entity_id = str(spec["entity_id"])
                previous = original_by_entity.get(entity_id)
                if previous is None or spec["original"] < previous:
                    original_by_entity[entity_id] = spec["original"]
            entities = sorted(
                original_by_entity,
                key=lambda entity_id: (original_by_entity[entity_id], entity_id),
            )
            entity_rank = {entity_id: index for index, entity_id in enumerate(entities)}
            denominator = max(1, len(entities) - 1)
            for spec in source_group:
                rank = entity_rank[str(spec["entity_id"])]
                base = profile_start + timedelta(days=178) * (rank / denominator)
                revision_offset = timedelta(hours=6 * (int(spec["revision"]) - 1))
                result.append((_business_calendar_time(base + revision_offset, spec), spec))
    adjusted: list[tuple[datetime, dict[str, Any]]] = []
    last_by_entity: dict[str, datetime] = {}
    for world_at, spec in sorted(
        result,
        key=lambda item: (str(item[1]["entity_id"]), int(item[1]["revision"]), item[0]),
    ):
        entity_id = str(spec["entity_id"])
        previous = last_by_entity.get(entity_id)
        if previous is not None and world_at <= previous:
            world_at = previous + timedelta(minutes=10)
        last_by_entity[entity_id] = world_at
        adjusted.append((world_at, spec))
    conversation_indexes: dict[str, list[int]] = defaultdict(list)
    for index, (_world_at, spec) in enumerate(adjusted):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        conversation_id = payload.get("conversationId")
        if conversation_id:
            conversation_indexes[str(conversation_id)].append(index)
    for indexes in conversation_indexes.values():
        previous = None
        for index in sorted(indexes, key=lambda item: adjusted[item][1]["original"]):
            world_at, spec = adjusted[index]
            if previous is not None and world_at <= previous:
                world_at = previous + timedelta(minutes=10)
                adjusted[index] = (world_at, spec)
            previous = world_at
    stage_order = {
        "customer_email": 1,
        "ticket": 2,
        "slack": 3,
        "meeting": 4,
        "decision": 5,
        "task": 6,
        "merge_request": 7,
        "deployment": 8,
        "incident": 9,
        "postmortem": 10,
    }
    episode_indexes: dict[str, list[int]] = defaultdict(list)
    for index, (_world_at, spec) in enumerate(adjusted):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        episode_id = payload.get("redwoodEpisodeId")
        stage = payload.get("redwoodStage")
        if episode_id and stage in stage_order:
            episode_indexes[str(episode_id)].append(index)
    for indexes in episode_indexes.values():
        previous: datetime | None = None
        latest_by_stage: dict[str, int] = {}
        for index in indexes:
            stage = str((adjusted[index][1].get("payload") or {}).get("redwoodStage"))
            current = latest_by_stage.get(stage)
            if current is None or int(adjusted[index][1]["revision"]) > int(adjusted[current][1]["revision"]):
                latest_by_stage[stage] = index
        ordered_indexes = sorted(
            latest_by_stage.values(),
            key=lambda index: stage_order[str((adjusted[index][1].get("payload") or {}).get("redwoodStage"))],
        )
        for index in ordered_indexes:
            world_at, spec = adjusted[index]
            if previous is not None and world_at <= previous:
                world_at = after(previous, spec)
                adjusted[index] = (world_at, spec)
            previous = world_at
    business_order = {
        "deal": 1,
        "quote": 2,
        "contract": 3,
        "invoice": 4,
        "payment": 5,
        "amendment": 6,
        "supplier_invoice": 7,
        "operating_cost": 8,
    }
    business_indexes: dict[str, list[int]] = defaultdict(list)
    for index, (_world_at, spec) in enumerate(adjusted):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
        episode_id = payload.get("redwoodEpisodeId") or properties.get("redwood_episode_id")
        if episode_id and str(spec.get("object_type")) in business_order:
            business_indexes[str(episode_id)].append(index)
    for indexes in business_indexes.values():
        previous = None
        indexes_by_type: dict[str, list[int]] = defaultdict(list)
        for index in indexes:
            object_type = str(adjusted[index][1]["object_type"])
            indexes_by_type[object_type].append(index)
        for _object_type, type_indexes in sorted(
            indexes_by_type.items(), key=lambda item: business_order[item[0]]
        ):
            if _object_type not in {
                "deal",
                "quote",
                "contract",
                "invoice",
                "payment",
                "amendment",
            }:
                continue
            first_index = min(
                type_indexes,
                key=lambda index: int(adjusted[index][1].get("revision", 0)),
            )
            first_at, first_spec = adjusted[first_index]
            if previous is not None and first_at <= previous:
                target = after(previous, first_spec)
                shift = target - first_at
                for index in type_indexes:
                    world_at, spec = adjusted[index]
                    adjusted[index] = (world_at + shift, spec)
                first_at = target
            previous = first_at
    grouped_indexes: dict[str, list[int]] = defaultdict(list)
    ungrouped_indexes: list[int] = []
    for index, (_world_at, spec) in enumerate(adjusted):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = (
            payload.get("redwoodEpisodeId")
            or payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEnterpriseWorkId")
        )
        stage = str(
            payload.get("redwoodStage")
            or payload.get("redwoodExternalStage")
            or payload.get("redwoodEnterpriseStage")
            or ""
        )
        if group_id and stage in {
            "action",
            "customer_email",
            "ticket",
            "slack",
            "meeting",
            "decision",
            "task",
            "merge_request",
            "deployment",
            "incident",
            "postmortem",
            "deal",
            "quote",
            "invoice",
            "payment",
            "contract",
            "amendment",
        }:
            grouped_indexes[str(group_id)].append(index)
        else:
            ungrouped_indexes.append(index)
    stage_order_all = {
        stage: index
        for index, stage in enumerate(
            (
                "action",
                "customer_email",
                "ticket",
                "slack",
                "meeting",
                "decision",
                "task",
                "merge_request",
                "deployment",
                "incident",
                "postmortem",
                "deal",
                "quote",
                "contract",
                "invoice",
                "payment",
                "amendment",
            )
        )
    }
    for group_id, indexes in grouped_indexes.items():
        previous: datetime | None = None
        latest_by_stage: dict[str, int] = {}
        for index in indexes:
            payload = (
                adjusted[index][1].get("payload")
                if isinstance(adjusted[index][1].get("payload"), dict)
                else {}
            )
            stage = str(
                payload.get("redwoodStage")
                or payload.get("redwoodExternalStage")
                or payload.get("redwoodEnterpriseStage")
                or ""
            )
            current = latest_by_stage.get(stage)
            if current is None or int(adjusted[index][1].get("revision", 0)) > int(
                adjusted[current][1].get("revision", 0)
            ):
                latest_by_stage[stage] = index
        for index in sorted(
            latest_by_stage.values(),
            key=lambda item: (
                stage_order_all.get(
                    str(
                        (adjusted[item][1].get("payload") or {}).get("redwoodStage")
                        or (adjusted[item][1].get("payload") or {}).get("redwoodExternalStage")
                        or (adjusted[item][1].get("redwoodEnterpriseStage") or "")
                    ),
                    99,
                ),
                adjusted[item][0],
            ),
        ):
            world_at, spec = adjusted[index]
            payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
            stage = str(
                payload.get("redwoodStage")
                or payload.get("redwoodExternalStage")
                or payload.get("redwoodEnterpriseStage")
                or ""
            )
            if str(group_id).startswith("enterprise-ordinary-"):
                prior_times = [
                    adjusted[prior_index][0]
                    for prior_index in indexes
                    if stage_order_all.get(
                        str(
                            (
                                adjusted[prior_index][1].get("payload")
                                if isinstance(adjusted[prior_index][1].get("payload"), dict)
                                else {}
                            ).get("redwoodStage")
                            or (
                                adjusted[prior_index][1].get("payload")
                                if isinstance(adjusted[prior_index][1].get("payload"), dict)
                                else {}
                            ).get("redwoodExternalStage")
                            or (
                                adjusted[prior_index][1].get("payload")
                                if isinstance(adjusted[prior_index][1].get("payload"), dict)
                                else {}
                            ).get("redwoodEnterpriseStage")
                            or ""
                        ),
                        99,
                    )
                    < stage_order_all.get(stage, 99)
                ]
                if prior_times and world_at <= max(prior_times):
                    world_at = after(max(prior_times), spec)
            if (not 8 <= world_at.hour < 18 or _has_vacation_conflict(world_at, spec)) and stage not in {
                "incident",
                "mitigation",
                "emergency_deployment",
            }:
                world_at = _business_calendar_time(world_at, spec)
            if previous is not None and world_at <= previous:
                world_at = after(previous, spec)
            adjusted[index] = (world_at, spec)
            previous = world_at
    for index in ungrouped_indexes:
        world_at, spec = adjusted[index]
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        stage = str(
            payload.get("redwoodStage")
            or payload.get("redwoodExternalStage")
            or spec.get("object_type")
            or ""
        )
        if (not 8 <= world_at.hour < 18 or _has_vacation_conflict(world_at, spec)) and stage not in {
            "incident",
            "mitigation",
            "emergency_deployment",
        }:
            adjusted[index] = (_business_calendar_time(world_at, spec), spec)
    for indexes in conversation_indexes.values():
        previous: datetime | None = None
        for index in sorted(indexes, key=lambda item: adjusted[item][1]["original"]):
            world_at, spec = adjusted[index]
            if previous is not None and world_at <= previous:
                world_at = after(previous, spec)
            if not 8 <= world_at.hour < 18 or _has_vacation_conflict(world_at, spec):
                world_at = _business_calendar_time(world_at, spec)
            if previous is not None and world_at <= previous:
                world_at = after(previous, spec)
            adjusted[index] = (world_at, spec)
            previous = world_at

    # Mutated snapshots can be scheduled after the normal calendar passes.  A
    # final vacation sweep keeps every revision safe, then re-establishes the
    # latest causal stage order if moving a revision crosses a later stage.
    for index, (world_at, spec) in enumerate(adjusted):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        stage = str(
            payload.get("redwoodStage")
            or payload.get("redwoodExternalStage")
            or payload.get("redwoodEnterpriseStage")
            or ""
        )
        if _has_vacation_conflict(world_at, spec) and stage not in {
            "incident",
            "mitigation",
            "emergency_deployment",
        }:
            adjusted[index] = (_business_calendar_time(world_at, spec), spec)
    for group_id, indexes in grouped_indexes.items():
        previous: datetime | None = None
        latest_by_stage: dict[str, int] = {}
        for index in indexes:
            payload = adjusted[index][1].get("payload") or {}
            stage = str(
                payload.get("redwoodStage")
                or payload.get("redwoodExternalStage")
                or payload.get("redwoodEnterpriseStage")
                or ""
            )
            current = latest_by_stage.get(stage)
            if current is None or int(adjusted[index][1].get("revision", 0)) > int(
                adjusted[current][1].get("revision", 0)
            ):
                latest_by_stage[stage] = index
        for index in sorted(
            latest_by_stage.values(),
            key=lambda item: (
                stage_order_all.get(
                    str(
                        (adjusted[item][1].get("payload") or {}).get("redwoodStage")
                        or (adjusted[item][1].get("payload") or {}).get("redwoodExternalStage")
                        or (adjusted[item][1].get("payload") or {}).get("redwoodEnterpriseStage")
                        or ""
                    ),
                    99,
                ),
                adjusted[item][0],
            ),
        ):
            world_at, spec = adjusted[index]
            payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
            stage = str(
                payload.get("redwoodStage")
                or payload.get("redwoodExternalStage")
                or payload.get("redwoodEnterpriseStage")
                or ""
            )
            if str(group_id).startswith("enterprise-ordinary-"):
                prior_times = [
                    adjusted[prior_index][0]
                    for prior_index in indexes
                    if stage_order_all.get(
                        str(
                            (
                                adjusted[prior_index][1].get("payload")
                                if isinstance(adjusted[prior_index][1].get("payload"), dict)
                                else {}
                            ).get("redwoodStage")
                            or (
                                adjusted[prior_index][1].get("payload")
                                if isinstance(adjusted[prior_index][1].get("payload"), dict)
                                else {}
                            ).get("redwoodExternalStage")
                            or (
                                adjusted[prior_index][1].get("payload")
                                if isinstance(adjusted[prior_index][1].get("payload"), dict)
                                else {}
                            ).get("redwoodEnterpriseStage")
                            or ""
                        ),
                        99,
                    )
                    < stage_order_all.get(stage, 99)
                ]
                if prior_times and world_at <= max(prior_times):
                    world_at = after(max(prior_times), spec)
            if previous is not None and world_at <= previous:
                adjusted[index] = (after(previous, spec), spec)
                world_at = adjusted[index][0]
            previous = world_at

    # Rebuild the groups from the final payloads and shift the complete stage
    # history together.  This covers providers whose revisions were injected
    # after the first grouping pass.
    final_groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index, (_world_at, spec) in enumerate(adjusted):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = (
            payload.get("redwoodEpisodeId")
            or payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
        )
        stage = str(
            payload.get("redwoodStage")
            or payload.get("redwoodExternalStage")
            or payload.get("redwoodEnterpriseStage")
            or spec.get("object_type")
            or ""
        )
        if group_id and stage in stage_order_all:
            final_groups[str(group_id)][stage].append(index)
    for stages in final_groups.values():
        previous: datetime | None = None
        for stage in sorted(stages, key=stage_order_all.get):
            stage_indexes = stages[stage]
            latest_index = max(
                stage_indexes,
                key=lambda item: int(adjusted[item][1].get("revision", 0)),
            )
            current_at = adjusted[latest_index][0]
            if previous is not None and current_at <= previous:
                target_at = after(previous, adjusted[latest_index][1])
                shift = target_at - current_at
                for item in stage_indexes:
                    world_at, spec = adjusted[item]
                    adjusted[item] = (world_at + shift, spec)
                current_at = target_at
            previous = current_at

    business_groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    business_stage_order = ("deal", "quote", "contract", "invoice", "payment", "amendment")
    for index, (_world_at, spec) in enumerate(adjusted):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
        group_id = payload.get("redwoodEpisodeId") or properties.get("redwood_episode_id")
        object_type = str(spec.get("object_type") or "")
        if group_id and object_type in business_stage_order:
            business_groups[str(group_id)][object_type].append(index)
    profile_end = profile_start + timedelta(days=179, hours=8)
    for stages in business_groups.values():
        previous: datetime | None = None
        for object_type in business_stage_order:
            stage_indexes = stages.get(object_type, [])
            if not stage_indexes:
                continue
            latest_index = max(
                stage_indexes,
                key=lambda item: int(adjusted[item][1].get("revision", 0)),
            )
            latest_at = adjusted[latest_index][0]
            if previous is not None and latest_at <= previous:
                target_at = after(previous, adjusted[latest_index][1])
                shift = target_at - latest_at
                for item in stage_indexes:
                    world_at, spec = adjusted[item]
                    adjusted[item] = (world_at + shift, spec)
                latest_at = target_at
            previous = latest_at
        deal_indexes = stages.get("deal", [])
        quote_indexes = stages.get("quote", [])
        if deal_indexes and quote_indexes:
            first_deal = min(
                deal_indexes,
                key=lambda item: int(adjusted[item][1].get("revision", 0)),
            )
            first_quote = min(
                quote_indexes,
                key=lambda item: int(adjusted[item][1].get("revision", 0)),
            )
            deal_at = adjusted[first_deal][0]
            quote_at = adjusted[first_quote][0]
            if deal_at >= quote_at:
                next_quote = sorted(adjusted[item][0] for item in quote_indexes if item != first_quote)
                candidate = deal_at + timedelta(minutes=10)
                if next_quote and candidate >= next_quote[0]:
                    candidate = quote_at - timedelta(minutes=10)
                    world_at, spec = adjusted[first_deal]
                    adjusted[first_deal] = (candidate, spec)
                else:
                    world_at, spec = adjusted[first_quote]
                    adjusted[first_quote] = (candidate, spec)
        group_indexes = [index for indexes in stages.values() for index in indexes]
        if group_indexes:
            latest_group_at = max(adjusted[index][0] for index in group_indexes)
            if latest_group_at > profile_end:
                shift = profile_end - latest_group_at
                for index in group_indexes:
                    world_at, spec = adjusted[index]
                    adjusted[index] = (world_at + shift, spec)

    # The provider histories above can be injected after the first causal
    # grouping pass.  Align the latest meeting/decision snapshots and the
    # proposal thread with their dependent commercial state once more at the
    # end of materialization.
    episode_groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index, (_world_at, spec) in enumerate(adjusted):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = payload.get("redwoodEpisodeId")
        if not group_id:
            continue
        stage = str(
            payload.get("redwoodStage")
            or payload.get("redwoodExternalStage")
            or spec.get("object_type")
            or ""
        )
        episode_groups[str(group_id)][stage].append(index)
    for episode_id, stages in episode_groups.items():
        meeting_indexes = stages.get("meeting", [])
        decision_indexes = stages.get("decision", [])
        if meeting_indexes and decision_indexes:
            meeting_latest = max(
                meeting_indexes,
                key=lambda item: int(adjusted[item][1].get("revision", 0)),
            )
            decision_latest = max(
                decision_indexes,
                key=lambda item: int(adjusted[item][1].get("revision", 0)),
            )
            meeting_at = adjusted[meeting_latest][0]
            decision_at = adjusted[decision_latest][0]
            if decision_at <= meeting_at:
                target = after(meeting_at, adjusted[decision_latest][1])
                shift = target - decision_at
                for item in decision_indexes:
                    world_at, spec = adjusted[item]
                    adjusted[item] = (world_at + shift, spec)

        deal_indexes = [
            index for index in stages.get("deal", []) if str(adjusted[index][1].get("object_type")) == "deal"
        ]
        proposal_first = [
            index
            for index in range(len(adjusted))
            if (
                str((adjusted[index][1].get("payload") or {}).get("redwoodEpisodeId")) == str(episode_id)
                and (adjusted[index][1].get("payload") or {}).get("redwoodTopic") == "proposal"
                and str((adjusted[index][1].get("payload") or {}).get("id", "")).endswith(
                    "-proposal-m1@redwood.example"
                )
            )
        ]
        if not deal_indexes or not proposal_first:
            continue
        deal_latest = max(
            deal_indexes,
            key=lambda item: int(adjusted[item][1].get("revision", 0)),
        )
        proposal_index = min(proposal_first, key=lambda item: adjusted[item][0])
        deal_at = adjusted[deal_latest][0]
        proposal_at = adjusted[proposal_index][0]
        if proposal_at < deal_at:
            continue
        conversation_id = (adjusted[proposal_index][1].get("payload") or {}).get("conversationId")
        thread_indexes = [
            index
            for index, (_world_at, spec) in enumerate(adjusted)
            if conversation_id and (spec.get("payload") or {}).get("conversationId") == conversation_id
        ]
        if not thread_indexes:
            continue
        target = deal_at - timedelta(minutes=10)
        proposal_spec = adjusted[proposal_index][1]
        while (
            target.weekday() >= 5
            or target.hour < 8
            or target.hour >= 18
            or _has_vacation_conflict(target, proposal_spec)
        ):
            target = (target - timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
        shift = target - proposal_at
        previous: datetime | None = None
        for item in sorted(thread_indexes, key=lambda value: adjusted[value][1]["original"]):
            world_at, spec = adjusted[item]
            candidate = world_at + shift
            candidate_payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
            candidate_stage = str(
                candidate_payload.get("redwoodStage") or candidate_payload.get("redwoodExternalStage") or ""
            )
            if (
                candidate.weekday() >= 5
                or candidate.hour < 8
                or candidate.hour >= 18
                or _has_vacation_conflict(candidate, spec)
            ) and candidate_stage not in {"incident", "mitigation", "emergency_deployment"}:
                candidate = _business_calendar_time(candidate, spec)
            if previous is not None and candidate <= previous:
                candidate = after(previous, spec)
            adjusted[item] = (candidate, spec)
            previous = candidate
    # Recheck the final schedule after all causal shifts. Later alignment passes
    # can move a routine revision back into a weekend, vacation, or off-hours.
    routine_groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    routine_ungrouped: list[int] = []
    for index, (_world_at, spec) in enumerate(adjusted):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = (
            payload.get("redwoodEpisodeId")
            or payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEnterpriseWorkId")
        )
        stage = str(
            payload.get("redwoodStage")
            or payload.get("redwoodExternalStage")
            or payload.get("redwoodEnterpriseStage")
            or spec.get("object_type")
            or ""
        )
        if group_id and stage in stage_order_all:
            routine_groups[str(group_id)][stage].append(index)
        else:
            routine_ungrouped.append(index)

    def is_on_call(spec: dict[str, Any]) -> bool:
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        stage = str(
            payload.get("redwoodStage")
            or payload.get("redwoodExternalStage")
            or payload.get("redwoodEnterpriseStage")
            or ""
        )
        return stage in {"incident", "mitigation", "emergency_deployment"}

    def schedule_after(previous: datetime | None, value: datetime, spec: dict[str, Any]) -> datetime:
        candidate = value
        if previous is not None and candidate <= previous:
            candidate = previous + timedelta(minutes=10)
        if is_on_call(spec):
            return candidate
        while (
            candidate.weekday() >= 5
            or candidate.hour < 8
            or candidate.hour >= 18
            or _has_vacation_conflict(candidate, spec)
        ):
            candidate = _business_calendar_time(candidate, spec)
            if previous is not None and candidate <= previous:
                candidate = _business_calendar_time(previous + timedelta(minutes=10), spec)
        return candidate

    for stages in routine_groups.values():
        previous: datetime | None = None
        for stage in sorted(stages, key=stage_order_all.get):
            for index in sorted(
                stages[stage],
                key=lambda item: (
                    adjusted[item][1].get("original"),
                    int(adjusted[item][1].get("revision", 0)),
                ),
            ):
                world_at, spec = adjusted[index]
                world_at = schedule_after(previous, world_at, spec)
                adjusted[index] = (world_at, spec)
                previous = world_at

    for index in routine_ungrouped:
        world_at, spec = adjusted[index]
        adjusted[index] = (schedule_after(None, world_at, spec), spec)

    ordinary_groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index, (_world_at, spec) in enumerate(adjusted):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        work_id = payload.get("redwoodEnterpriseWorkId")
        stage = str(payload.get("redwoodEnterpriseStage") or "")
        if work_id and str(work_id).startswith("enterprise-ordinary-") and stage:
            ordinary_groups[str(work_id)][stage].append(index)
    for stages in ordinary_groups.values():
        previous: datetime | None = None
        for stage in ("decision", "task", "merge_request", "deployment", "incident"):
            stage_indexes = stages.get(stage, [])
            if not stage_indexes:
                continue
            first_at = min(adjusted[index][0] for index in stage_indexes)
            if previous is not None and first_at <= previous:
                target = after(previous, adjusted[stage_indexes[0]][1])
                shift = target - first_at
                for index in stage_indexes:
                    world_at, spec = adjusted[index]
                    adjusted[index] = (world_at + shift, spec)
            previous = max(adjusted[index][0] for index in stage_indexes)

    # Spread routine provider/object streams across the shared working-day
    # calendar. Causal and provider-history ordering is retained by assigning
    # slots in the scheduler's current deterministic order.
    routine_object_types = {
        "issue",
        "page",
        "message",
        "merge_request",
        "transcript",
        "file",
        "crm_object",
        "operating_cost",
        "supplier_invoice",
        "deal",
        "quote",
        "contract",
        "invoice",
        "payment",
        "amendment",
        "ticket",
    }
    routine_groups_by_source_object: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, (_world_at, spec) in enumerate(adjusted):
        if str(spec.get("object_type")) not in routine_object_types or is_on_call(spec):
            continue
        routine_groups_by_source_object[(str(spec.get("source")), str(spec.get("object_type")))].append(index)
    working_days = _working_days(profile_start)
    for indexes in routine_groups_by_source_object.values():
        ordered = sorted(
            indexes,
            key=lambda item: (
                adjusted[item][0],
                str(adjusted[item][1].get("entity_id")),
                int(adjusted[item][1].get("revision", 0)),
            ),
        )
        for rank, index in enumerate(ordered):
            world_at, spec = adjusted[index]
            slot = round(rank * (len(working_days) - 1) / max(1, len(ordered) - 1))
            target_day = working_days[min(slot, len(working_days) - 1)]
            target = datetime.combine(target_day, datetime.min.time(), tzinfo=UTC).replace(
                hour=min(17, max(8, world_at.hour)),
                minute=world_at.minute,
                second=world_at.second,
                microsecond=world_at.microsecond,
            )
            adjusted[index] = (schedule_after(None, target, spec), spec)

    # Re-establish revision order after the distribution pass.
    history_indexes: dict[str, list[int]] = defaultdict(list)
    for index, (_world_at, spec) in enumerate(adjusted):
        history_indexes[str(spec.get("entity_id"))].append(index)
    for indexes in history_indexes.values():
        previous: datetime | None = None
        for index in sorted(indexes, key=lambda item: int(adjusted[item][1].get("revision", 0))):
            world_at, spec = adjusted[index]
            world_at = schedule_after(previous, world_at, spec)
            adjusted[index] = (world_at, spec)
            previous = world_at

    # Re-establish causal stage chronology after both balancing passes. The
    # whole revision history of a stage moves together, so provider histories
    # remain chronological while the source-native causal chain stays intact.
    causal_groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index, (_world_at, spec) in enumerate(adjusted):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = (
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or payload.get("redwoodEnterpriseWorkId")
        )
        stage = str(payload.get("redwoodStage") or payload.get("redwoodExternalStage") or "")
        if group_id and stage in stage_order_all:
            causal_groups[str(group_id)][stage].append(index)
    for stages in causal_groups.values():
        previous: datetime | None = None
        for stage in sorted(stages, key=stage_order_all.get):
            indexes = stages[stage]
            latest_index = max(indexes, key=lambda item: int(adjusted[item][1].get("revision", 0)))
            current = adjusted[latest_index][0]
            if previous is not None and current <= previous:
                target = schedule_after(previous, current, adjusted[latest_index][1])
                shift = target - current
                for index in indexes:
                    world_at, spec = adjusted[index]
                    adjusted[index] = (world_at + shift, spec)
                current = target
            previous = current

    conversation_groups: dict[str, list[int]] = defaultdict(list)
    for index, (_world_at, spec) in enumerate(adjusted):
        if spec.get("operation") == "delete":
            continue
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        conversation_id = payload.get("conversationId")
        if conversation_id:
            conversation_groups[str(conversation_id)].append(index)
    for indexes in conversation_groups.values():
        previous: datetime | None = None
        for index in sorted(
            indexes,
            key=lambda item: (
                adjusted[item][1].get("original"),
                int(adjusted[item][1].get("revision", 0)),
            ),
        ):
            world_at, spec = adjusted[index]
            world_at = schedule_after(previous, world_at, spec)
            adjusted[index] = (world_at, spec)
            previous = world_at

    # Scheduler output is the canonical event stream. Materialization must
    # only assign sequence numbers and serialize this already ordered list.
    ordered = sorted(
        adjusted,
        key=lambda item: (
            item[0],
            str(item[1].get("entity_id")),
            int(item[1].get("revision", 0)),
        ),
    )
    final_stage_order = {
        stage: index
        for index, stage in enumerate(
            (
                "action",
                "customer_email",
                "ticket",
                "slack",
                "meeting",
                "decision",
                "task",
                "merge_request",
                "deployment",
                "incident",
                "postmortem",
            )
        )
    }
    final_causal_groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index, (_world_at, spec) in enumerate(ordered):
        if spec.get("operation") == "delete":
            continue
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = (
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or payload.get("redwoodEnterpriseWorkId")
        )
        stage = str(payload.get("redwoodStage") or payload.get("redwoodExternalStage") or "")
        if group_id and stage in final_stage_order:
            final_causal_groups[str(group_id)][stage].append(index)
    for group_id, stages in final_causal_groups.items():
        previous: datetime | None = None
        benchmark_anchor: datetime | None = None
        if str(group_id).startswith("enterprise-causal-batch-") and "customer_email" in stages:
            customer_indexes = stages["customer_email"]
            customer_latest = max(
                customer_indexes,
                key=lambda item: int(ordered[item][1].get("revision", 0)),
            )
            benchmark_anchor = ordered[customer_latest][0]
        for stage in sorted(stages, key=lambda value: final_stage_order.get(value, 99)):
            indexes = stages[stage]
            latest_index = max(indexes, key=lambda item: int(ordered[item][1].get("revision", 0)))
            current = ordered[latest_index][0]
            if benchmark_anchor is not None:
                stage_index = final_stage_order[stage]
                variant = int(sha256(str(group_id).encode()).hexdigest()[:8], 16)
                gap_minutes = 30 + ((variant >> (stage_index % 16)) + stage_index * 113) % 150
                target = benchmark_anchor if previous is None else previous + timedelta(minutes=gap_minutes)
                target = schedule_after(previous, target, ordered[latest_index][1])
                shift = target - current
                for index in indexes:
                    world_at, spec = ordered[index]
                    ordered[index] = (world_at + shift, spec)
                current = target
                previous = current
                continue
            target = schedule_after(previous, current, ordered[latest_index][1])
            if target != current:
                shift = target - current
                for index in indexes:
                    world_at, spec = ordered[index]
                    ordered[index] = (world_at + shift, spec)
                current = target
            previous = current

    business_stage_order = {
        stage: index
        for index, stage in enumerate(("deal", "quote", "contract", "invoice", "payment", "amendment"))
    }
    business_groups_final: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index, (_world_at, spec) in enumerate(ordered):
        if spec.get("operation") == "delete":
            continue
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
        group_id = payload.get("redwoodEpisodeId") or properties.get("redwood_episode_id")
        stage = str(spec.get("object_type") or "")
        if group_id and stage in business_stage_order:
            business_groups_final[str(group_id)][stage].append(index)
    for stages in business_groups_final.values():
        previous: datetime | None = None
        for stage in sorted(stages, key=lambda value: business_stage_order[value]):
            indexes = stages[stage]
            first_index = min(indexes, key=lambda item: int(ordered[item][1].get("revision", 0)))
            current = ordered[first_index][0]
            target = schedule_after(previous, current, ordered[first_index][1])
            if target != current:
                shift = target - current
                for index in indexes:
                    world_at, spec = ordered[index]
                    ordered[index] = (world_at + shift, spec)
                current = target
            previous = current

    final_ordinary_groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index, (_world_at, spec) in enumerate(ordered):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        work_id = payload.get("redwoodEnterpriseWorkId")
        stage = str(payload.get("redwoodEnterpriseStage") or "")
        if work_id and str(work_id).startswith("enterprise-ordinary-") and stage:
            final_ordinary_groups[str(work_id)][stage].append(index)
    for stages in final_ordinary_groups.values():
        previous: datetime | None = None
        for stage in ("decision", "task", "merge_request", "deployment", "incident"):
            indexes = stages.get(stage, [])
            if not indexes:
                continue
            first_index = min(indexes, key=lambda item: ordered[item][0])
            current = ordered[first_index][0]
            target = schedule_after(previous, current, ordered[first_index][1])
            if target != current:
                shift = target - current
                for index in indexes:
                    world_at, spec = ordered[index]
                    ordered[index] = (world_at + shift, spec)
                current = target
            previous = max(ordered[index][0] for index in indexes)

    final_history_indexes: dict[str, list[int]] = defaultdict(list)
    for index, (_world_at, spec) in enumerate(ordered):
        final_history_indexes[str(spec.get("entity_id"))].append(index)
    for indexes in final_history_indexes.values():
        previous: datetime | None = None
        for index in sorted(indexes, key=lambda item: int(ordered[item][1].get("revision", 0))):
            world_at, spec = ordered[index]
            world_at = schedule_after(previous, world_at, spec)
            if world_at != ordered[index][0]:
                ordered[index] = (world_at, spec)
            previous = world_at

    # XWiki postmortems are the largest grouped routine stream. Reassign the
    # complete deterministic group slots across working weekdays before the
    # final causal repair so the provider/object distribution stays balanced.
    xwiki_groups: dict[str, list[int]] = defaultdict(list)
    for index, (_world_at, spec) in enumerate(ordered):
        if str(spec.get("source")) != "xwiki-redwood":
            continue
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = (
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or (spec.get("tombstone") or {}).get("entity_id")
            or f"entity:{spec.get('entity_id')}"
        )
        xwiki_groups[str(group_id)].append(index)
    xwiki_days = _working_days(profile_start)
    xwiki_ordered = sorted(
        xwiki_groups.values(),
        key=lambda indexes: max(ordered[index][0] for index in indexes),
    )
    for rank, indexes in enumerate(xwiki_ordered):
        current = max(ordered[index][0] for index in indexes)
        slot = round(rank * (len(xwiki_days) - 1) / max(1, len(xwiki_ordered) - 1))
        target = datetime.combine(xwiki_days[slot], datetime.min.time(), tzinfo=UTC).replace(
            hour=min(17, max(8, current.hour)),
            minute=current.minute,
            second=current.second,
            microsecond=current.microsecond,
        )
        shift = target - current
        for index in indexes:
            world_at, spec = ordered[index]
            ordered[index] = (world_at + shift, spec)

    # A final stage-level repair follows the history/calendar pass. It moves
    # whole stage histories together, so it cannot invalidate provider
    # revision chronology while restoring causal chronology.
    final_repair_groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index, (_world_at, spec) in enumerate(ordered):
        if spec.get("operation") == "delete":
            continue
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = (
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or payload.get("redwoodEnterpriseWorkId")
        )
        stage = str(payload.get("redwoodStage") or payload.get("redwoodExternalStage") or "")
        if group_id and stage in final_stage_order:
            final_repair_groups[str(group_id)][stage].append(index)
    for stages in final_repair_groups.values():
        previous: datetime | None = None
        for stage in sorted(stages, key=lambda value: final_stage_order.get(value, 99)):
            indexes = stages[stage]
            latest_index = max(indexes, key=lambda item: int(ordered[item][1].get("revision", 0)))
            current = ordered[latest_index][0]
            target = schedule_after(previous, current, ordered[latest_index][1])
            if target != current:
                shift = target - current
                for index in indexes:
                    world_at, spec = ordered[index]
                    ordered[index] = (world_at + shift, spec)
                current = target
            previous = current

    # Balance sparse provider/object streams by moving complete causal groups.
    # A candidate shift is accepted only when every routine event in that
    # group remains on a working-day schedule, so chronology and operational
    # calendar semantics survive the distribution repair.
    distribution_targets = {
        ("fireflies-redwood", "transcript"),
        ("hubspot-redwood", "deal"),
        ("slack-redwood", "message"),
    }
    distribution_groups: dict[str, list[int]] = defaultdict(list)
    target_latest: dict[str, dict[str, int]] = defaultdict(dict)
    latest_target_by_entity: dict[str, int] = {}
    for index, (_world_at, spec) in enumerate(ordered):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
        group_id = (
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or properties.get("redwood_episode_id")
            or payload.get("redwoodEnterpriseWorkId")
            or (spec.get("tombstone") or {}).get("entity_id")
            or f"entity:{spec.get('entity_id')}"
        )
        group_key = str(group_id)
        distribution_groups[group_key].append(index)
        target = (str(spec.get("source")), str(spec.get("object_type")))
        if target in distribution_targets and not is_on_call(spec):
            entity_id = str(spec.get("entity_id"))
            current = latest_target_by_entity.get(entity_id)
            if current is None or int(spec.get("revision", 0)) > int(
                ordered[current][1].get("revision", 0)
            ):
                latest_target_by_entity[entity_id] = index
                target_latest[group_key][entity_id] = index

    calendar_end = profile_start + timedelta(days=180, hours=8)
    for group_indexes in distribution_groups.values():
        earliest = min(ordered[index][0] for index in group_indexes)
        if earliest < profile_start:
            shift = profile_start + timedelta(hours=8) - earliest
            if max(ordered[index][0] for index in group_indexes) + shift <= calendar_end:
                for index in group_indexes:
                    world_at, spec = ordered[index]
                    ordered[index] = (world_at + shift, spec)

    distribution_counts: dict[str, Counter[int]] = defaultdict(Counter)
    for group in target_latest.values():
        for index in group.values():
            spec = ordered[index][1]
            stream = f"{spec.get('source')}:{spec.get('object_type')}"
            distribution_counts[stream][ordered[index][0].weekday()] += 1

    def distribution_score(counts: dict[str, Counter[int]]) -> int:
        return sum(value * value for counter in counts.values() for value in counter.values())

    def routine_group_safe(indexes: list[int], shift: timedelta) -> bool:
        for index in indexes:
            world_at, spec = ordered[index]
            candidate = world_at + shift
            if candidate < profile_start or candidate > calendar_end:
                return False
            if is_on_call(spec):
                continue
            if candidate.weekday() >= 5 or not 8 <= candidate.hour < 18:
                return False
            if _has_vacation_conflict(candidate, spec):
                return False
        return True

    for group_key in sorted(
        target_latest,
        key=lambda value: max(ordered[index][0] for index in target_latest[value].values()),
    ):
        group_indexes = distribution_groups[group_key]
        current_targets = list(target_latest[group_key].values())
        for index in current_targets:
            spec = ordered[index][1]
            stream = f"{spec.get('source')}:{spec.get('object_type')}"
            distribution_counts[stream][ordered[index][0].weekday()] -= 1
        baseline = {stream: Counter(counter) for stream, counter in distribution_counts.items()}
        candidates: list[tuple[int, int, timedelta]] = []
        for day_delta in range(-14, 15):
            shift = timedelta(days=day_delta)
            if not routine_group_safe(group_indexes, shift):
                continue
            candidate_counts = {stream: Counter(counter) for stream, counter in baseline.items()}
            for index in current_targets:
                spec = ordered[index][1]
                stream = f"{spec.get('source')}:{spec.get('object_type')}"
                candidate_counts[stream][(ordered[index][0] + shift).weekday()] += 1
            candidates.append((distribution_score(candidate_counts), abs(day_delta), shift))
        if not candidates:
            continue
        _score, _distance, shift = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        if shift:
            for index in group_indexes:
                world_at, spec = ordered[index]
                ordered[index] = (world_at + shift, spec)
        for index in current_targets:
            spec = ordered[index][1]
            stream = f"{spec.get('source')}:{spec.get('object_type')}"
            distribution_counts[stream][ordered[index][0].weekday()] += 1

    # Restore causal stage order after a group distribution candidate, then
    # cap only groups that crossed the shared calendar boundary. Both passes
    # move complete stages/groups, preserving source history order.
    post_distribution_groups: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for index, (_world_at, spec) in enumerate(ordered):
        if spec.get("operation") == "delete":
            continue
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = (
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or payload.get("redwoodEnterpriseWorkId")
        )
        stage = str(payload.get("redwoodStage") or payload.get("redwoodExternalStage") or "")
        if group_id and stage in final_stage_order:
            post_distribution_groups[str(group_id)][stage].append(index)
    for stages in post_distribution_groups.values():
        previous: datetime | None = None
        for stage in sorted(stages, key=lambda value: final_stage_order.get(value, 99)):
            indexes = stages[stage]
            latest_index = max(indexes, key=lambda item: int(ordered[item][1].get("revision", 0)))
            current = ordered[latest_index][0]
            target = schedule_after(previous, current, ordered[latest_index][1])
            if target != current:
                shift = target - current
                for index in indexes:
                    world_at, spec = ordered[index]
                    ordered[index] = (world_at + shift, spec)
                current = target
            previous = current

    late_groups: dict[str, list[int]] = defaultdict(list)
    for index, (_world_at, spec) in enumerate(ordered):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = (
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or payload.get("redwoodEnterpriseWorkId")
            or (spec.get("tombstone") or {}).get("entity_id")
            or f"entity:{spec.get('entity_id')}"
        )
        late_groups[str(group_id)].append(index)
    for indexes in late_groups.values():
        latest = max(ordered[index][0] for index in indexes)
        if latest <= calendar_end:
            continue
        safe_shift: timedelta | None = None
        for day_delta in range(0, -15, -1):
            candidate_shift = timedelta(days=day_delta)
            if latest + candidate_shift <= calendar_end and routine_group_safe(indexes, candidate_shift):
                safe_shift = candidate_shift
                break
        if safe_shift is None:
            continue
        for index in indexes:
            world_at, spec = ordered[index]
            ordered[index] = (world_at + safe_shift, spec)

    final_history_indexes: dict[str, list[int]] = defaultdict(list)
    for index, (_world_at, spec) in enumerate(ordered):
        final_history_indexes[str(spec.get("entity_id"))].append(index)
    for indexes in final_history_indexes.values():
        previous: datetime | None = None
        for index in sorted(indexes, key=lambda item: int(ordered[item][1].get("revision", 0))):
            world_at, spec = ordered[index]
            world_at = schedule_after(previous, world_at, spec)
            ordered[index] = (world_at, spec)
            previous = world_at

    final_causal_groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index, (_world_at, spec) in enumerate(ordered):
        if spec.get("operation") == "delete":
            continue
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = (
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or payload.get("redwoodEnterpriseWorkId")
        )
        stage = str(payload.get("redwoodStage") or payload.get("redwoodExternalStage") or "")
        if group_id and stage in final_stage_order:
            final_causal_groups[str(group_id)][stage].append(index)
    for stages in final_causal_groups.values():
        previous: datetime | None = None
        for stage in sorted(stages, key=lambda value: final_stage_order.get(value, 99)):
            indexes = stages[stage]
            latest_index = max(indexes, key=lambda item: int(ordered[item][1].get("revision", 0)))
            current = ordered[latest_index][0]
            target = schedule_after(previous, current, ordered[latest_index][1])
            if target != current:
                shift = target - current
                for index in indexes:
                    world_at, spec = ordered[index]
                    ordered[index] = (world_at + shift, spec)
                current = target
            previous = current

    final_history_indexes = defaultdict(list)
    for index, (_world_at, spec) in enumerate(ordered):
        final_history_indexes[str(spec.get("entity_id"))].append(index)
    for indexes in final_history_indexes.values():
        previous = None
        for index in sorted(indexes, key=lambda item: int(ordered[item][1].get("revision", 0))):
            world_at, spec = ordered[index]
            world_at = schedule_after(previous, world_at, spec)
            ordered[index] = (world_at, spec)
            previous = world_at

    final_late_groups: dict[str, list[int]] = defaultdict(list)
    for index, (_world_at, spec) in enumerate(ordered):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        group_id = (
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or payload.get("redwoodEnterpriseWorkId")
            or (spec.get("tombstone") or {}).get("entity_id")
            or f"entity:{spec.get('entity_id')}"
        )
        final_late_groups[str(group_id)].append(index)
    for indexes in final_late_groups.values():
        latest = max(ordered[index][0] for index in indexes)
        if latest <= calendar_end:
            continue
        safe_shift: timedelta | None = None
        for day_delta in range(0, -15, -1):
            candidate_shift = timedelta(days=day_delta)
            if latest + candidate_shift <= calendar_end and routine_group_safe(indexes, candidate_shift):
                safe_shift = candidate_shift
                break
        if safe_shift is None:
            continue
        for index in indexes:
            world_at, spec = ordered[index]
            ordered[index] = (world_at + safe_shift, spec)

    # Give the small HubSpot deal stream its own deterministic balancing pass.
    # These deals often share an episode with several business snapshots, so
    # the complete episode moves together and business chronology is retained.
    hubspot_groups: dict[str, list[int]] = defaultdict(list)
    hubspot_targets: dict[str, list[int]] = defaultdict(list)
    latest_hubspot: dict[str, int] = {}
    for index, (_world_at, spec) in enumerate(ordered):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
        group_id = (
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or properties.get("redwood_episode_id")
            or payload.get("redwoodEnterpriseWorkId")
            or (spec.get("tombstone") or {}).get("entity_id")
            or f"entity:{spec.get('entity_id')}"
        )
        group_key = str(group_id)
        hubspot_groups[group_key].append(index)
        if spec.get("source") != "hubspot-redwood" or spec.get("object_type") != "deal":
            continue
        entity_id = str(spec.get("entity_id"))
        current = latest_hubspot.get(entity_id)
        if current is None or int(spec.get("revision", 0)) > int(
            ordered[current][1].get("revision", 0)
        ):
            latest_hubspot[entity_id] = index
    for index in latest_hubspot.values():
        payload = (
            ordered[index][1].get("payload")
            if isinstance(ordered[index][1].get("payload"), dict)
            else {}
        )
        properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
        group_id = (
            payload.get("redwoodCausalChainId")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodEpisodeId")
            or properties.get("redwood_episode_id")
            or payload.get("redwoodEnterpriseWorkId")
            or f"entity:{ordered[index][1].get('entity_id')}"
        )
        hubspot_targets[str(group_id)].append(index)

    deal_counts = Counter(ordered[index][0].weekday() for index in latest_hubspot.values())

    def deal_score(counter: Counter[int]) -> tuple[float, int, int]:
        values = [counter[weekday] for weekday in range(5)]
        nonzero = [value for value in values if value]
        return (
            max(values, default=0) / max(1, min(nonzero, default=0)),
            max(values, default=0),
            -min(nonzero, default=0),
        )

    for group_key in sorted(hubspot_targets):
        target_indexes = hubspot_targets[group_key]
        baseline = Counter(deal_counts)
        for index in target_indexes:
            baseline[ordered[index][0].weekday()] -= 1
        current_score = deal_score(Counter(deal_counts))
        candidates: list[tuple[tuple[float, int, int], int, timedelta]] = []
        for day_delta in range(-7, 8):
            shift = timedelta(days=day_delta)
            group_indexes = hubspot_groups[group_key]
            if not routine_group_safe(group_indexes, shift):
                continue
            candidate = Counter(baseline)
            for index in target_indexes:
                candidate[(ordered[index][0] + shift).weekday()] += 1
            candidates.append((deal_score(candidate), abs(day_delta), shift))
        if not candidates:
            continue
        candidate_score, _distance, shift = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        if candidate_score >= current_score:
            shift = timedelta(0)
        for index in target_indexes:
            deal_counts[ordered[index][0].weekday()] -= 1
        if shift:
            for index in hubspot_groups[group_key]:
                world_at, spec = ordered[index]
                ordered[index] = (world_at + shift, spec)
        for index in target_indexes:
            deal_counts[ordered[index][0].weekday()] += 1

    # Business deals are the first stage of their HubSpot episode. A small
    # Thursday surplus can therefore be corrected by moving only complete
    # deal histories one working day earlier; downstream quote/contract
    # stages remain causally later and no unrelated provider moves.
    deal_histories: dict[str, list[int]] = defaultdict(list)
    deal_latest: dict[str, int] = {}
    for index, (_world_at, spec) in enumerate(ordered):
        if spec.get("source") != "hubspot-redwood" or spec.get("object_type") != "deal":
            continue
        entity_id = str(spec.get("entity_id"))
        deal_histories[entity_id].append(index)
        current = deal_latest.get(entity_id)
        if current is None or int(spec.get("revision", 0)) > int(ordered[current][1].get("revision", 0)):
            deal_latest[entity_id] = index

    deal_counts = Counter(ordered[index][0].weekday() for index in deal_latest.values())
    while deal_counts and max(deal_counts.values()) / max(1, min(deal_counts.values())) > 1.5:
        candidates: list[tuple[tuple[float, int, int], str, timedelta]] = []
        current_score = deal_score(deal_counts)
        for entity_id, latest_index in sorted(deal_latest.items()):
            if ordered[latest_index][0].weekday() != max(deal_counts, key=deal_counts.get):
                continue
            indexes = deal_histories[entity_id]
            shift = timedelta(days=-1)
            if not routine_group_safe(indexes, shift):
                continue
            candidate = Counter(deal_counts)
            weekday = ordered[latest_index][0].weekday()
            candidate[weekday] -= 1
            candidate[(ordered[latest_index][0] + shift).weekday()] += 1
            score = deal_score(candidate)
            if score < current_score:
                candidates.append((score, entity_id, shift))
        if not candidates:
            break
        _score, entity_id, shift = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        latest_index = deal_latest[entity_id]
        deal_counts[ordered[latest_index][0].weekday()] -= 1
        for index in deal_histories[entity_id]:
            world_at, spec = ordered[index]
            ordered[index] = (world_at + shift, spec)
        deal_counts[ordered[latest_index][0].weekday()] += 1

    # Enterprise ordinary work has its own source-native stage field. Keep
    # this repair after every balancing pass, because a later provider
    # distribution shift must not reintroduce an MR/deployment inversion.
    enterprise_ordinary_groups: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for index, (_world_at, spec) in enumerate(ordered):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        work_id = payload.get("redwoodEnterpriseWorkId")
        stage = str(payload.get("redwoodEnterpriseStage") or "")
        if work_id and str(work_id).startswith("enterprise-ordinary-") and stage:
            enterprise_ordinary_groups[str(work_id)][stage].append(index)
    for stages in enterprise_ordinary_groups.values():
        previous: datetime | None = None
        for stage in ("decision", "task", "merge_request", "deployment", "incident"):
            indexes = stages.get(stage, [])
            if not indexes:
                continue
            for index in sorted(indexes, key=lambda item: int(ordered[item][1].get("revision", 0))):
                world_at, spec = ordered[index]
                world_at = schedule_after(previous, world_at, spec)
                ordered[index] = (world_at, spec)
                previous = world_at

    # The ordinary repair above can move downstream Jira/GitLab stages onto a
    # different weekday. Rebalance complete ordinary groups once more using
    # latest provider observations, preserving every causal stage together.
    latest_by_entity: dict[str, int] = {}
    for index, (_world_at, spec) in enumerate(ordered):
        entity_id = str(spec.get("entity_id"))
        current = latest_by_entity.get(entity_id)
        if current is None or int(spec.get("revision", 0)) > int(
            ordered[current][1].get("revision", 0)
        ):
            latest_by_entity[entity_id] = index

    ordinary_distribution_groups: dict[str, list[int]] = defaultdict(list)
    ordinary_distribution_targets: dict[str, list[int]] = defaultdict(list)
    routine_distribution_counts: dict[str, Counter[int]] = defaultdict(Counter)
    for index, (_world_at, spec) in enumerate(ordered):
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        work_id = payload.get("redwoodEnterpriseWorkId")
        if work_id and str(work_id).startswith("enterprise-ordinary-"):
            ordinary_distribution_groups[str(work_id)].append(index)
    for index in latest_by_entity.values():
        _world_at, spec = ordered[index]
        if str(spec.get("object_type")) not in routine_object_types or is_on_call(spec):
            continue
        stream = f"{spec.get('source')}:{spec.get('object_type')}"
        routine_distribution_counts[stream][ordered[index][0].weekday()] += 1
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        work_id = payload.get("redwoodEnterpriseWorkId")
        if work_id and str(work_id).startswith("enterprise-ordinary-"):
            ordinary_distribution_targets[str(work_id)].append(index)

    for group_key in sorted(ordinary_distribution_targets):
        target_indexes = ordinary_distribution_targets[group_key]
        group_indexes = ordinary_distribution_groups[group_key]
        for index in target_indexes:
            spec = ordered[index][1]
            stream = f"{spec.get('source')}:{spec.get('object_type')}"
            routine_distribution_counts[stream][ordered[index][0].weekday()] -= 1
        baseline = {
            stream: Counter(counter) for stream, counter in routine_distribution_counts.items()
        }
        candidates: list[tuple[int, int, timedelta]] = []
        for day_delta in range(-14, 15):
            shift = timedelta(days=day_delta)
            if not routine_group_safe(group_indexes, shift):
                continue
            candidate_counts = {stream: Counter(counter) for stream, counter in baseline.items()}
            for index in target_indexes:
                spec = ordered[index][1]
                stream = f"{spec.get('source')}:{spec.get('object_type')}"
                candidate_counts[stream][(ordered[index][0] + shift).weekday()] += 1
            candidates.append((distribution_score(candidate_counts), abs(day_delta), shift))
        if candidates:
            _score, _distance, shift = min(candidates, key=lambda item: (item[0], item[1], item[2]))
            if shift:
                for index in group_indexes:
                    world_at, spec = ordered[index]
                    ordered[index] = (world_at + shift, spec)
        for index in target_indexes:
            spec = ordered[index][1]
            stream = f"{spec.get('source')}:{spec.get('object_type')}"
            routine_distribution_counts[stream][ordered[index][0].weekday()] += 1

    return sorted(
        ordered,
        key=lambda item: (
            item[0],
            str(item[1].get("entity_id")),
            int(item[1].get("revision", 0)),
        ),
    )


def _deletion_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append deterministic provider deletion observations for retained source objects."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for spec in specs:
        grouped[str(spec["entity_id"])].append(spec)
    deletions: list[dict[str, Any]] = []
    for _entity_id, history in sorted(grouped.items()):
        first = min(history, key=lambda item: int(item["revision"]))
        source = str(first["source"])
        external_id = str(first["external_id"])
        if source == "mail-redwood" and external_id.startswith("<rw-chain-"):
            continue
        if source not in {"confluence-redwood", "mail-redwood", "xwiki-redwood", "google-drive-redwood"}:
            continue
        if source == "confluence-redwood" and external_id.startswith(
            (
                "enterprise-project-",
                "enterprise-epic-",
                "enterprise-decision-",
                "enterprise-ordinary-decision-",
                "enterprise-batch-decision-",
            )
        ):
            continue
        marker = f"{source}:{external_id}"
        selected = "delete-me" in external_id.casefold() or (
            int(sha256(marker.encode()).hexdigest()[:4], 16) % 89 == 0
        )
        if not selected:
            continue
        latest = max(history, key=lambda item: int(item["revision"]))
        deletion = deepcopy(latest)
        deletion["revision"] = int(latest["revision"]) + 1
        deletion["original"] = latest["original"] + timedelta(days=3)
        deletion["operation"] = "delete"
        deletion["deletion_reason"] = "retention_policy"
        deletions.append(deletion)
    return deletions


def _payload_employee_id(payload: object) -> str | None:
    if isinstance(payload, dict):
        for key in ("user", "author", "authorId", "employee_id", "assignee"):
            value = payload.get(key)
            if isinstance(value, str) and value.upper().startswith("E"):
                return value.upper()
            if isinstance(value, dict):
                nested = _payload_employee_id(value)
                if nested:
                    return nested
        for value in payload.values():
            nested = _payload_employee_id(value)
            if nested:
                return nested
    elif isinstance(payload, list):
        for value in payload:
            nested = _payload_employee_id(value)
            if nested:
                return nested
    return None


def _payload_employee_ids(payload: object) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, str):
        found.update(re.findall(r"\bE\d{1,3}\b", payload.upper()))
    elif isinstance(payload, dict):
        for _key, value in payload.items():
            if isinstance(value, str):
                found.update(re.findall(r"\bE\d{1,3}\b", value.upper()))
            else:
                found.update(_payload_employee_ids(value))
    elif isinstance(payload, list):
        for value in payload:
            found.update(_payload_employee_ids(value))
    return found


def _business_calendar_time(value: datetime, spec: dict[str, Any]) -> datetime:
    """Place routine activity in working hours and move deterministic vacations forward."""
    current = value
    digest = sha256(f"{spec['entity_id']}:{spec['revision']}".encode()).hexdigest()
    target_hour = 8 + int(digest[:2], 16) % 10
    target_minute = int(digest[2:4], 16) % 60
    payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
    on_call = str(payload.get("redwoodOnCallKind") or "") in {
        "incident",
        "mitigation",
        "emergency_deployment",
    }
    if on_call and value.weekday() >= 5:
        return value
    employee_ids = _payload_employee_ids(spec.get("payload"))
    vacation_indexes = {
        int(employee_id[1:]) - 1
        for employee_id in employee_ids
        if employee_id[1:].isdigit() and int(employee_id[1:]) >= 1 and (int(employee_id[1:]) - 1) % 9 == 0
    }
    for _ in range(20):
        if current.weekday() >= 5 or current.hour >= 18:
            current = (current + timedelta(days=1)).replace(
                hour=target_hour, minute=target_minute, second=0, microsecond=0
            )
            continue
        if current.hour < 8:
            current = current.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        vacation_end = None
        for vacation_index in sorted(vacation_indexes):
            vacation_start = PROFILE_START.replace(tzinfo=UTC) + timedelta(days=30 + vacation_index)
            candidate_end = PROFILE_START.replace(tzinfo=UTC) + timedelta(days=37 + vacation_index)
            if vacation_start <= current < candidate_end:
                vacation_end = candidate_end
                break
        if vacation_end is not None:
            current = vacation_end.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
            continue
        return current.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    return current.replace(hour=17, minute=0, second=0, microsecond=0)


def _working_days(profile_start: datetime, days: int = 180) -> list[datetime.date]:
    """Return the shared weekday calendar used by deterministic scheduling."""
    return [
        (profile_start + timedelta(days=offset)).date()
        for offset in range(days)
        if (profile_start + timedelta(days=offset)).weekday() < 5
    ]


def _balanced_working_time(
    *,
    index: int,
    total: int,
    profile_start: datetime,
    spec: dict[str, Any],
    source_time: datetime,
) -> datetime:
    """Place ordered source records across weekdays before materialization."""
    days = _working_days(profile_start)
    slot = round(index * (len(days) - 1) / max(1, total - 1))
    target_day = days[min(max(slot, 0), len(days) - 1)]
    hour = min(17, max(8, source_time.hour))
    candidate = datetime.combine(target_day, datetime.min.time(), tzinfo=UTC).replace(
        hour=hour,
        minute=source_time.minute,
        second=source_time.second,
        microsecond=source_time.microsecond,
    )
    return _business_calendar_time(candidate, spec)


def _has_vacation_conflict(value: datetime, spec: dict[str, Any]) -> bool:
    """Return whether an employee named by a payload is on deterministic leave."""
    employee_ids = _payload_employee_ids(spec.get("payload"))
    for employee_id in employee_ids:
        if not employee_id.startswith("E") or not employee_id[1:].isdigit():
            continue
        number = int(employee_id[1:])
        if number < 1 or (number - 1) % 9 != 0:
            continue
        vacation_start = PROFILE_START.replace(tzinfo=UTC) + timedelta(days=30 + number - 1)
        vacation_end = vacation_start + timedelta(days=7)
        if vacation_start <= value < vacation_end:
            return True
    return False


def _ensure_jira_task_lifecycle(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand single-snapshot Jira tasks into deterministic source-native histories."""
    by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for spec in specs:
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
        issue_type = (fields.get("issuetype") or {}).get("name")
        if (
            spec.get("source") == "jira-redwood"
            and spec.get("object_type") == "issue"
            and issue_type == "Task"
        ):
            by_entity[str(spec.get("entity_id"))].append(spec)

    replacements: dict[str, list[dict[str, Any]]] = {}
    for entity_id, history in by_entity.items():
        if len(history) != 1:
            continue
        original_spec = history[0]
        payload = original_spec.get("payload") if isinstance(original_spec.get("payload"), dict) else {}
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
        digest = int(
            sha256(f"task-lifecycle:{original_spec.get('external_id')}".encode()).hexdigest()[:8],
            16,
        )
        bucket = digest % 100
        if bucket < 70:
            statuses = ("To Do", "In Progress", "Review", "Done")
        elif bucket < 80:
            statuses = ("To Do", "In Progress", "Cancelled")
        elif bucket < 88:
            statuses = ("To Do", "In Progress", "Review", "Done", "Reopened", "In Progress", "Done")
        else:
            statuses = ("To Do", "In Progress", "Open")
        base_at = original_spec["original"]
        histories: list[dict[str, Any]] = []
        expanded: list[dict[str, Any]] = []
        previous_status = statuses[0]
        for revision, status in enumerate(statuses, start=1):
            revision_spec = deepcopy(original_spec)
            revision_spec["revision"] = revision
            revision_spec["original"] = base_at + timedelta(days=revision - 1)
            revision_payload = deepcopy(payload)
            revision_fields = revision_payload.setdefault("fields", {})
            revision_fields["status"] = {
                "name": status,
                "statusCategory": {
                    "key": "done" if status in {"Done", "Closed"} else "indeterminate"
                },
            }
            revision_fields["resolution"] = (
                {"name": "Cancelled"}
                if status == "Cancelled"
                else {"name": "Done"}
                if status == "Done"
                else None
            )
            if status != previous_status:
                employee_id = str((revision_fields.get("assignee") or {}).get("accountId") or "acct:E001")
                employee_id = employee_id.removeprefix("acct:")
                histories.append(
                    {
                        "id": f"{original_spec.get('external_id')}-status-{revision}",
                        "author": {
                            "accountId": f"acct:{employee_id}",
                            "displayName": employee_id,
                            "emailAddress": f"{employee_id.casefold()}@redwood.example",
                        },
                        "created": (base_at + timedelta(days=revision - 1)).isoformat(),
                        "items": [
                            {
                                "field": "status",
                                "fromString": previous_status,
                                "toString": status,
                            }
                        ],
                    }
                )
                previous_status = status
            revision_payload["changelog"] = {"histories": deepcopy(histories)}
            revision_spec["payload"] = revision_payload
            expanded.append(revision_spec)
        replacements[entity_id] = expanded

    if not replacements:
        return specs
    output: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for spec in specs:
        entity_id = str(spec.get("entity_id"))
        if entity_id in replacements:
            if entity_id not in emitted:
                output.extend(replacements[entity_id])
                emitted.add(entity_id)
            continue
        output.append(spec)
    return output


def materialize_source_events(
    chains: dict[str, list[dict[str, Any]]],
    documents: list[CorpusDocument] | None = None,
    seed: int = 42,
) -> list[PlancyEvent]:
    """Emit ordered full snapshots from operational rows and explicit status history."""
    episodes = plan_business_episodes(chains, seed)
    financial_specs = materialize_business_specs(chains, episodes)
    specs = (
        _task_specs(chains)
        + _single_revision_specs(chains)
        + _confluence_specs(chains)
        + _additional_provider_specs(chains, financial_specs)
        + financial_specs
        + materialize_engineering_specs(episodes)
        + materialize_email_specs(chains, episodes)
    )
    if documents:
        specs.extend(_enterprise_specs(documents))
        specs.extend(_canonical_fallback_specs(documents, specs))
        specs.extend(_external_action_specs(documents))
        gitlab_document = next(
            (
                document
                for document in documents
                if document.provenance.get("source") == "enterprise_rag"
                and document.source_type.casefold() == "github"
            ),
            None,
        )
        if gitlab_document is not None:
            merge_request_id = str(gitlab_document.provenance.get("dsid") or gitlab_document.doc_id)
            calendar_review_id = f"commit-{merge_request_id.replace(':', '-')}-calendar-review"
            calendar_review_at = PROFILE_START.replace(tzinfo=UTC) + timedelta(days=176, hours=9)
            specs.append(
                {
                    "entity_id": immutable_id("redwood", "gitlab-redwood", "commit", calendar_review_id),
                    "source": "gitlab-redwood",
                    "object_type": "commit",
                    "external_id": calendar_review_id,
                    "revision": 1,
                    "original": calendar_review_at,
                    "calendar": "weekly",
                    "container": "ENG",
                    "acl": _event_acl("gitlab-redwood", "ENG"),
                    "payload": {
                        "id": calendar_review_id,
                        "short_id": sha256(calendar_review_id.encode()).hexdigest()[:12],
                        "title": "Calendar review commit for the source-native merge request history",
                        "message": (
                            f"Record the source-native calendar review for merge request "
                            f"{merge_request_id} and retain the operational audit trail."
                        ),
                        "author_name": COMPANY_NAME,
                        "author_email": "platform@redwood.example",
                        "committed_date": calendar_review_at.isoformat(),
                        "merge_request_id": merge_request_id,
                    },
                }
            )
    specs = _ensure_jira_task_lifecycle(specs)
    specs.extend(_deletion_specs(specs))
    if not specs:
        return []
    scheduled = _schedule_specs(specs)
    created_by_entity: dict[str, datetime] = {}
    for world_at, spec in scheduled:
        entity_id = str(spec["entity_id"])
        previous = created_by_entity.get(entity_id)
        if previous is None or world_at < previous:
            created_by_entity[entity_id] = world_at
    events: list[PlancyEvent] = []
    for sequence, (world_at, spec) in enumerate(scheduled, start=1):
        revision = int(spec["revision"])
        previous_event_id = f"{spec['entity_id']}:{revision - 1}" if revision > 1 else None
        raw_payload = spec["payload"]
        alias_payload = raw_payload if isinstance(raw_payload, dict) else {}
        common_event = {
            "entity_id": str(spec["entity_id"]),
            "source": str(spec["source"]),
            "object_type": str(spec["object_type"]),
            "external_id": str(spec["external_id"]),
            "revision": revision,
            "sequence": sequence,
            "world_at": world_at,
            "observed_at": world_at + timedelta(minutes=1),
            "previous_event_id": previous_event_id,
            "original_occurred_at": spec["original"],
            "time_transform_id": f"{spec.get('calendar', 'operational')}-to-redwood-180d-v2",
            "container": str(spec["container"]),
            "aliases": [_readable_alias(spec, alias_payload)],
        }
        if spec.get("operation") == "delete":
            events.append(
                PlancyEvent.delete(
                    **common_event,
                    tombstone=Tombstone(
                        entity_id=str(spec["entity_id"]),
                        revision=revision,
                        deleted_at=world_at,
                        effective_acl=spec["acl"],
                        reason=str(spec.get("deletion_reason") or "source_deleted"),
                    ),
                )
            )
            continue
        payload = _align_payload_time(
            raw_payload,
            str(spec["source"]),
            world_at,
            created_by_entity[str(spec["entity_id"])],
        )
        if isinstance(payload, dict):
            parent_event_id = f"{spec['entity_id']}:{revision}"
            if str(spec.get("object_type")) == "transcript":
                _structured_meeting_protocol(payload, world_at)
            _add_attachment_metadata(
                payload,
                parent_event_id=parent_event_id,
                parent_external_id=str(spec["external_id"]),
            )
            payload["id"] = str(spec["external_id"])
            if str(spec.get("source")) == "jira-redwood":
                fields = payload.get("fields")
                if isinstance(fields, dict):
                    fields["key"] = _jira_contract_key(spec["external_id"])
        if isinstance(payload, dict):
            payload["redwoodAclDepartment"] = {
                "FINANCE": "Finance",
                "COMMERCIAL": "Commercial",
                "SUPPORT": "Support",
                "ENG": "Engineering",
                "meetings": "People",
                "mail": "Support",
            }.get(str(spec.get("container")), str(spec.get("container")))
        events.append(PlancyEvent.upsert(**common_event, acl=spec["acl"], payload=payload))
    return events
