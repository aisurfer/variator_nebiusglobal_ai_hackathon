"""Source-native support-to-engineering histories for causal episodes."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from hashlib import sha256
from typing import Any

from variator.corpus.canonical import immutable_id
from variator.corpus.email_events import TOPICS, episode_thread_start
from variator.corpus.episodes import BusinessEpisode
from variator.corpus.models import Acl
from variator.corpus.redwood_profile import PROFILE_START

MEETING_DECISIONS = (
    "approve the rollback and ship the guarded fix",
    "prioritize the customer impact and add a validation checkpoint",
    "split the mitigation into an observable change and a follow-up task",
    "hold the release until the owner confirms the acceptance evidence",
    "ship the workaround first and schedule the durable correction",
    "add a security review before the engineering task is marked ready",
    "reproduce the failure, document the boundary, and then deploy the fix",
    "close the customer-visible gap with a monitored deployment",
)

TASK_FOCI = (
    "replay the webhook callback boundary",
    "separate stale cache reads from fresh retrievals",
    "trace ranking drift through corpus snapshots",
    "harden the connector retry budget",
    "add an operator handoff for partial failures",
    "preserve tenant filters during fallback search",
    "instrument the slow path before the next release",
    "reconcile duplicate ingestion checkpoints",
    "validate redaction at the export boundary",
    "tighten the permission refresh window",
    "compare regional latency during failover",
    "classify empty result sets by provider",
    "make evaluation fixtures reproduce the customer case",
    "split indexing work from query serving",
    "guard rollout when the feature flag is stale",
    "record provenance for transformed chunks",
    "cap concurrent backfills for shared workers",
    "repair missed webhook acknowledgements",
    "isolate ranking regressions from parser changes",
    "verify billing events against usage samples",
    "surface connector health in the operator view",
    "preserve ordering across incremental syncs",
    "rebuild the incident replay from source events",
    "add a quarantine path for malformed documents",
    "test recovery after an expired access grant",
    "reduce noisy alerts without hiding regressions",
    "validate multilingual token boundaries",
    "close the audit gap in export history",
    "measure freshness after a partial deployment",
    "make retry decisions visible to support",
    "protect archived tenants from live writes",
    "compare exact and approximate retrieval costs",
    "document the contract for provider callbacks",
    "reproduce intermittent cursor expiration",
    "route oversized documents to bounded workers",
    "verify deletion propagation across indexes",
    "add an evidence trail for manual overrides",
    "stabilize ranking when metadata is incomplete",
    "check fail-open behavior under key rotation",
    "split customer impact from internal noise",
    "measure queue age at the deployment gate",
    "preserve source links in generated answers",
    "test resumption after a worker restart",
    "compare incident symptoms with release markers",
    "make backfill progress safe to resume",
    "validate ownership during escalation",
    "record the rollback trigger in the runbook",
)

TASK_VERBS = (
    "Harden",
    "Uncouple",
    "Trace",
    "Tune",
    "Add",
    "Protect",
    "Instrument",
    "Reconcile",
    "Validate",
    "Tighten",
    "Compare",
    "Classify",
)

TASK_LEADS = (
    "Start from the observed customer signal and",
    "Use the incident evidence to",
    "Turn the meeting decision into work that will",
    "Keep the implementation narrow enough to",
    "Make the owner accountable for a change that will",
    "Use a replayable fixture to",
    "Before release, confirm that the change will",
    "Treat the support report as evidence to",
    "Separate the customer-visible correction from work that will",
    "Build the smallest measurable change that will",
    "Carry the decision record into code that will",
    "Use the deployment boundary to",
)

TASK_EVIDENCE = (
    "a callback trace and one failed acknowledgement",
    "a before-and-after retrieval sample",
    "the ranking evaluation slice attached to the ticket",
    "provider retry logs from the affected account",
    "the operator handoff recorded in the incident channel",
    "tenant-scoped queries from the customer reproduction",
    "the p95 latency sample from the release window",
    "two ingestion checkpoints with the same source cursor",
    "an export sample containing a redacted field",
    "the access refresh audit for the affected user",
    "regional traces captured during failover",
    "empty-result examples from the provider adapter",
)

TASK_ACCEPTANCE = (
    "the replay names the failing boundary and its expected response",
    "the snapshot shows the old and new behavior for the same record",
    "the evaluation slice passes without changing unrelated rankings",
    "the retry count and final error remain visible in source payloads",
    "an operator can resume the path without guessing the next owner",
    "a tenant filter is present in both the query and the evidence",
    "the release dashboard exposes the metric before rollout completes",
    "duplicate checkpoints are handled idempotently",
    "the exported record contains no protected value and keeps provenance",
    "an expired grant produces the documented denial and audit entry",
    "the failover comparison reports both regions on the same timeline",
    "the adapter distinguishes an empty result from a failed request",
)

TASK_RISKS = (
    "watching for a second callback while the first is still pending",
    "avoiding a cache fix that changes freshness guarantees",
    "keeping the evaluation result comparable with the previous run",
    "not turning a provider outage into an unbounded retry storm",
    "ensuring escalation does not duplicate the customer notification",
    "preventing a fallback query from crossing tenant boundaries",
    "avoiding alert suppression when the regression is real",
    "not advancing a cursor until the source record is durable",
    "keeping redaction consistent between preview and export",
    "avoiding a permission refresh that grants access too early",
    "not hiding a regional imbalance behind a global average",
    "preserving the distinction between no data and bad data",
)

COMMENT_TURNS = (
    "I translated the decision into a checkable work item and attached the evidence.",
    "The first reproduction isolates the boundary; I am keeping the customer record unchanged.",
    "The implementation is ready for review, with the acceptance signal called out below.",
    "I found a scope edge case and reopened the task until the owner confirms the treatment.",
    "The review is complete; the remaining risk is documented for the deployment window.",
)


def _acl(episode: BusinessEpisode) -> Acl:
    return Acl.model_validate(
        {
            "allow": [
                {"principal": f"user:{employee_id.casefold()}"} for employee_id in episode.employee_ids
            ],
            "deny": [],
            "inherit": False,
        }
    )


def _working_time(value: Any, employee_ids: tuple[str, ...]) -> Any:
    """Keep routine causal snapshots on workdays and within local work hours."""
    current = value
    profile_start = PROFILE_START.replace(tzinfo=value.tzinfo)
    for _ in range(370):
        if current.weekday() >= 5 or current.hour >= 18:
            current = (current + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
            continue
        if current.hour < 8:
            current = current.replace(hour=8, minute=0, second=0, microsecond=0)
        vacation = False
        for employee_id in employee_ids:
            if not employee_id.startswith("E") or not employee_id[1:].isdigit():
                continue
            number = int(employee_id[1:])
            if number < 1 or (number - 1) % 9 != 0:
                continue
            start = profile_start + timedelta(days=30 + number - 1)
            end = start + timedelta(days=7)
            if start <= current < end:
                current = end.replace(hour=8, minute=0, second=0, microsecond=0)
                vacation = True
                break
        if vacation:
            continue
        return current
    return current.replace(hour=17, minute=0, second=0, microsecond=0)


def _spec(
    episode: BusinessEpisode,
    *,
    source: str,
    object_type: str,
    external_id: str,
    revision: int,
    day_offset: int | float,
    base_at: Any,
    container: str,
    payload: dict[str, Any],
    acl_override: Acl | None = None,
) -> dict[str, Any]:
    return {
        "entity_id": immutable_id("redwood", source, object_type, external_id),
        "source": source,
        "object_type": object_type,
        "external_id": external_id,
        "revision": revision,
        "original": _working_time(base_at + timedelta(days=day_offset), episode.employee_ids),
        "calendar": "causal",
        "container": container,
        "acl": acl_override or _acl(episode),
        "payload": payload,
    }


def _task_profile(episode_index: int) -> dict[str, str]:
    focus_index = episode_index % len(TASK_FOCI)
    return {
        "focus": TASK_FOCI[focus_index],
        "verb": TASK_VERBS[episode_index % len(TASK_VERBS)],
        "lead": TASK_LEADS[episode_index % len(TASK_LEADS)],
        "evidence": TASK_EVIDENCE[episode_index % len(TASK_EVIDENCE)],
        "acceptance": TASK_ACCEPTANCE[episode_index % len(TASK_ACCEPTANCE)],
        "risk": TASK_RISKS[episode_index % len(TASK_RISKS)],
    }


def _task_comment(
    episode: BusinessEpisode,
    *,
    key: str,
    profile: dict[str, str],
    revision: int,
    status: str,
    created_at: Any,
) -> list[dict[str, Any]]:
    author_ids = (
        episode.employee_ids[(revision - 1) % len(episode.employee_ids)],
        episode.employee_ids[revision % len(episode.employee_ids)],
    )
    first = COMMENT_TURNS[(revision - 1) % len(COMMENT_TURNS)]
    second = f"Status is {status}; acceptance is {profile['acceptance']}. Keep watch for {profile['risk']}."
    bodies = (first, second)
    comments: list[dict[str, Any]] = []
    for index, (author_id, body) in enumerate(zip(author_ids, bodies, strict=True), start=1):
        comment_time = created_at + timedelta(hours=index - 3)
        comments.append(
            {
                "id": f"{key}-comment-{revision}-{index}",
                "author": {
                    "accountId": f"acct:{author_id}",
                    "displayName": author_id,
                    "emailAddress": f"{author_id.casefold()}@redwood.example",
                },
                "body": (f"Episode {episode.episode_id}, work item {profile['focus']}: {body}"),
                "created": comment_time.isoformat(),
            }
        )
    return comments


def _jira_payload(
    episode: BusinessEpisode,
    *,
    key: str,
    status: str,
    summary: str,
    description: str,
    stage: str,
    references: dict[str, str],
    created_at: Any,
    revision: int,
    comments: list[dict[str, Any]] | None = None,
    histories: list[dict[str, Any]] | None = None,
    labels: list[str] | None = None,
    priority: str = "High",
) -> dict[str, Any]:
    employee_id = episode.employee_ids[0]
    account = {
        "accountId": f"acct:{employee_id}",
        "displayName": employee_id,
        "emailAddress": f"{employee_id.casefold()}@redwood.example",
    }
    contract_key = f"PROJECT-{int(sha256(str(key).encode()).hexdigest()[:10], 16) % 900_000 + 1:06d}"
    if re.fullmatch(r"PROJECT-\d+", str(key)):
        contract_key = str(key)
    return {
        "key": contract_key,
        "id": key,
        "fields": {
            "summary": summary,
            "description": description,
            "issuetype": {"name": "Incident" if stage == "incident" else "Task"},
            "project": {"key": "PROJECT", "name": "Engineering"},
            "status": {
                "name": status,
                "statusCategory": {"key": "done" if status == "Done" else "indeterminate"},
            },
            "resolution": {"name": "Done"} if status == "Done" else None,
            "priority": {"name": priority},
            "assignee": account,
            "reporter": account,
            "creator": account,
            "created": created_at.isoformat(),
            "updated": (created_at + timedelta(days=revision - 1)).isoformat(),
            "labels": labels or ["episode", stage],
            "comment": {
                "comments": [dict(comment) for comment in (comments or [])] if stage == "task" else [],
            },
        },
        "names": {},
        "changelog": {"histories": [dict(history) for history in (histories or [])]},
        "remotelinks": [
            {"id": f"link-{target}", "relationship": label, "object": {"url": target}}
            for label, target in references.items()
        ],
        "redwoodEpisodeId": episode.episode_id,
        "redwoodStage": stage,
        "redwoodReferences": {"accountId": episode.account_id, **references},
    }


def _engineering_mr_payload(
    episode: BusinessEpisode,
    *,
    refs: Any,
    decision_id: str,
    task_profile: dict[str, str],
    common: dict[str, str],
    revision: int,
    state: str,
    occurred_at: Any,
) -> dict[str, Any]:
    """Render a deterministic GitLab history with source-native review lifecycle data."""
    digest = sha256(f"engineering-mr:{episode.episode_id}".encode()).hexdigest()
    variant = int(digest[:8], 16)
    change_kinds = (
        "mitigation",
        "adapter",
        "observability",
        "policy",
        "rollback",
        "docs-only",
        "test-only",
        "retry",
        "reconciliation",
    )
    kind = change_kinds[variant % len(change_kinds)]
    areas = (
        ("callbacks", "handler.py"),
        ("retrieval", "scorer.py"),
        ("permissions", "boundary.py"),
        ("billing", "ledger.py"),
        ("connectors", "reader.py"),
        ("release", "guard.yaml"),
        ("support", "replay.json"),
        ("runtime", "budget.go"),
    )
    area, filename = areas[(variant // 11) % len(areas)]
    if kind == "docs-only":
        filename = "runbook.md"
    elif kind == "test-only":
        filename = "regression_test.py"
    file_count = 1 + (variant // 5) % 8
    path_roles = (
        "core",
        "boundary",
        "fallback",
        "contract",
        "validation",
        "recovery",
        "telemetry",
        "compatibility",
    )
    path_stem = filename.rsplit(".", maxsplit=1)[0]

    def previous_deployment_paths() -> list[str]:
        return [
            f"src/{area}/deployed/{path_roles[index]}/{path_stem}.{filename.rsplit('.', 1)[-1]}"
            for index in range(file_count)
        ]

    if kind == "rollback":
        changed_files = previous_deployment_paths()
    else:
        changed_files = []
        for index in range(file_count):
            role = path_roles[index]
            if kind == "docs-only":
                changed_files.append(f"docs/{area}/{kind}/{role}.md")
            elif kind == "test-only":
                changed_files.append(f"tests/{area}/{kind}/test_{role}.py")
            elif index % 3 == 0:
                changed_files.append(f"src/{area}/{kind}/{role}_{path_stem}.{filename.rsplit('.', 1)[-1]}")
            elif index % 3 == 1:
                changed_files.append(f"tests/{area}/{kind}/test_{role}_{path_stem}.py")
            else:
                changed_files.append(f"docs/{area}/{kind}/{role}_{path_stem}.md")
    reviewer_count = variant % 4
    reviewers = [
        {
            "accountId": f"acct:{employee_id}",
            "displayName": employee_id,
            "emailAddress": f"{employee_id.casefold()}@redwood.example",
        }
        for employee_id in (
            episode.employee_ids[(variant + index * 3) % len(episode.employee_ids)]
            for index in range(reviewer_count)
        )
    ]
    has_conflict = variant % 23 in {0, 1}
    requested_changes = variant % 13 in {0, 1}
    is_draft = revision == 1 and variant % 19 in {0, 1}
    pipeline_count = 1 + (variant // 17) % 4
    failed_attempt = variant % 5 == 0
    if failed_attempt:
        pipeline_count = max(2, pipeline_count)
    pipelines = []
    for attempt in range(pipeline_count):
        failed = failed_attempt and attempt == 0 and revision < 3
        started_at = occurred_at + timedelta(minutes=attempt * 25)
        finished_at = started_at + timedelta(minutes=10 + (variant + attempt) % 15)
        pipelines.append(
            {
                "id": f"pipeline-{refs.merge_request_id}-{attempt + 1}",
                "attempt": attempt + 1,
                "status": "failed" if failed else "success" if state == "merged" else "running",
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "failed_checks": ["integration"] if failed else [],
                "recovery_commit": (
                    sha256(f"{refs.merge_request_id}:recovery:{attempt}".encode()).hexdigest()[:12]
                    if failed
                    else None
                ),
            }
        )
    commits = [
        {
            "id": sha256(f"{refs.merge_request_id}:commit:{index}".encode()).hexdigest()[:12],
            "message": (
                f"{('Add' if index == 0 else 'Refine')} {kind} for {task_profile['focus']}"
                f" in {changed_files[index % len(changed_files)]}"
            ),
            "paths": [changed_files[index % len(changed_files)]],
            "author": {
                "accountId": f"acct:{episode.employee_ids[index % len(episode.employee_ids)]}",
                "displayName": episode.employee_ids[index % len(episode.employee_ids)],
                "emailAddress": (
                    f"{episode.employee_ids[index % len(episode.employee_ids)].casefold()}@redwood.example"
                ),
            },
        }
        for index in range(1 + (variant // 29) % 8)
    ]
    comment_templates = (
        "I reproduced the customer boundary around {path}; please confirm the expected result.",
        "Requested change: keep {kind} scoped to {path} and the evidence in the ticket.",
        "The blocker is isolated to {path}; the next pipeline must include the recovery check.",
        "Approval follows the attached before-and-after result for {task} in {path}.",
    )
    comment_count = (variant // 31) % 5
    review_comments = [
        {
            "author": reviewers[index % len(reviewers)]
            if reviewers
            else {
                "accountId": f"acct:{episode.employee_ids[0]}",
                "displayName": episode.employee_ids[0],
                "emailAddress": f"{episode.employee_ids[0].casefold()}@redwood.example",
            },
            "body": comment_templates[(variant + index) % len(comment_templates)].format(
                path=changed_files[index % len(changed_files)],
                kind=kind,
                task=task_profile["focus"],
            ),
            "resolved": state == "merged" and not requested_changes,
        }
        for index in range(comment_count)
    ]
    return {
        "id": refs.merge_request_id,
        "iid": refs.merge_request_id,
        "title": f"Mitigation for {refs.ticket_id}",
        "description": (
            f"Episode {episode.episode_id}. Closes {refs.task_id}. "
            f"Implements decision {decision_id} from meeting {refs.meeting_id}. "
            f"Scope: {task_profile['focus']}. Acceptance: {task_profile['acceptance']}."
        ),
        "state": state,
        "created_at": (occurred_at - timedelta(days=5)).isoformat(),
        "updated_at": occurred_at.isoformat(),
        "merged_at": occurred_at.isoformat() if state == "merged" else None,
        "merge_commit_sha": sha256(f"{episode.episode_id}:merge".encode()).hexdigest()
        if state == "merged"
        else None,
        "review_status": (
            "requested_changes" if requested_changes else "approved" if revision >= 2 else "pending"
        ),
        "draft": is_draft,
        "merge_conflicts": has_conflict,
        "requested_changes": requested_changes,
        "approval_state": "rejected" if requested_changes else "approved" if state == "merged" else "pending",
        "reviewers": reviewers,
        "approvals": [
            {
                "user": reviewer,
                "approved": state == "merged" and not requested_changes,
                "at": occurred_at.isoformat(),
            }
            for reviewer in reviewers[: (0 if requested_changes else min(2, len(reviewers)))]
        ],
        "pipelines": pipelines,
        "changed_files": changed_files,
        "diff_summary": {
            "files_changed": len(changed_files),
            "insertions": 12 + variant % 90,
            "deletions": 1 + (variant // 37) % 20,
            "paths": list(changed_files),
        },
        "commits": commits,
        "review_comments": review_comments,
        "redwoodEpisodeId": episode.episode_id,
        "redwoodStage": "merge_request",
        "redwoodReferences": {**common, "taskId": refs.task_id, "decisionId": decision_id},
        "sourceLifecycle": {
            "changeKind": kind,
            "failedCheckRecovered": failed_attempt and revision >= 3,
            "mergeConflictResolved": has_conflict and revision >= 3,
            "previousDeploymentPaths": previous_deployment_paths() if kind == "rollback" else [],
            "previousDeploymentId": (f"{refs.deployment_id}-previous" if kind == "rollback" else None),
        },
    }


def materialize_engineering_specs(
    episodes: list[BusinessEpisode],
) -> list[dict[str, Any]]:
    """Emit complete nine-stage support and engineering chains."""
    ordered = sorted(episodes, key=lambda episode: (episode.started_at, episode.episode_id))
    support_scene = TOPICS.index("support")
    specs: list[dict[str, Any]] = []
    for episode_index, episode in enumerate(ordered):
        if episode.kind != "support_engineering":
            continue
        refs = episode.references
        base_at = episode_thread_start(episode_index, len(ordered), support_scene)
        email_id = f"<rw-chain-{episode.episode_id.casefold()}-support-m1@redwood.example>"
        common = {"accountId": episode.account_id}
        task_profile = _task_profile(episode_index)

        ticket_description = (
            f"Episode {episode.episode_id}: customer email {email_id} reports the problem. "
            f"Track investigation in ticket {refs.ticket_id}. The first work item is to "
            f"{task_profile['focus']}; evidence is {task_profile['evidence']}."
        )
        for revision, (offset, status) in enumerate(((1, "Open"), (2, "Triaged"), (3, "Escalated")), start=1):
            specs.append(
                _spec(
                    episode,
                    source="jira-redwood",
                    object_type="issue",
                    external_id=refs.ticket_id,
                    revision=revision,
                    day_offset=offset,
                    base_at=base_at,
                    container="SUPPORT",
                    payload=_jira_payload(
                        episode,
                        key=refs.ticket_id,
                        status=status,
                        summary=f"Customer escalation: {task_profile['focus']}",
                        description=ticket_description,
                        stage="ticket",
                        references={"customerEmailId": email_id},
                        created_at=base_at + timedelta(days=1),
                        revision=revision,
                    ),
                )
            )

        slack_id = f"SLACK-{episode.episode_id}"
        slack_text = (
            f"Episode {episode.episode_id}: investigating Jira ticket {refs.ticket_id}; "
            "the customer impact is confirmed and a decision meeting is required."
        )
        specs.append(
            _spec(
                episode,
                source="slack-redwood",
                object_type="message",
                external_id=slack_id,
                revision=1,
                day_offset=4,
                base_at=base_at,
                container="eng-incidents",
                payload={
                    "type": "message",
                    "channel": "eng-incidents",
                    "ts": (base_at + timedelta(days=4)).timestamp(),
                    "user": episode.employee_ids[0],
                    "text": slack_text,
                    "thread_ts": slack_id,
                    "redwoodEpisodeId": episode.episode_id,
                    "redwoodStage": "slack",
                    "redwoodReferences": {**common, "ticketId": refs.ticket_id},
                },
            )
        )
        edited_slack_payload = {
            "type": "message",
            "channel": "eng-incidents",
            "ts": (base_at + timedelta(days=4.5)).timestamp(),
            "user": episode.employee_ids[0],
            "text": (
                f"Episode {episode.episode_id}: edited update for Jira ticket {refs.ticket_id}; "
                "customer impact confirmed and mitigation decision is recorded in the meeting."
            ),
            "thread_ts": slack_id,
            "edited": {"user": episode.employee_ids[0], "ts": (base_at + timedelta(days=4.5)).timestamp()},
            "editReason": "Clarified customer impact before the decision meeting.",
            "redwoodEpisodeId": episode.episode_id,
            "redwoodStage": "slack",
            "redwoodReferences": {**common, "ticketId": refs.ticket_id},
        }
        specs.append(
            _spec(
                episode,
                source="slack-redwood",
                object_type="message",
                external_id=slack_id,
                revision=2,
                day_offset=4.5,
                base_at=base_at,
                container="eng-incidents",
                payload=edited_slack_payload,
            )
        )

        decision_text = MEETING_DECISIONS[episode_index % len(MEETING_DECISIONS)]
        meeting_variant = int(sha256(episode.episode_id.encode()).hexdigest()[:8], 16) % 5
        meeting_participants = list(episode.employee_ids)
        if meeting_variant == 0:
            meeting_participants = meeting_participants[:2]
        elif meeting_variant == 1:
            meeting_participants.append("E007")
        elif meeting_variant == 2:
            meeting_participants.extend(["E008", "E014"])
        meeting_participants = list(dict.fromkeys(meeting_participants))
        meeting_quote = (
            f"Following the previous customer sync, the team reviewed the latest report and "
            f"agreed to {decision_text}."
        )
        meeting_payload = {
            "id": refs.meeting_id,
            "title": f"Incident decision for {episode.account_id}",
            "date": (base_at + timedelta(days=5)).isoformat(),
            "participants": meeting_participants,
            "summary": f"Decision for ticket {refs.ticket_id}",
            "transcript": (
                f"{meeting_quote} Episode {episode.episode_id}. The resulting work is "
                f"task {refs.task_id}. The alternatives were a guarded rollback and a staged "
                "release; the group recorded the owner, due date, and remaining blocker."
            ),
            "protocol": {
                "decision": f"Approve mitigation task {refs.task_id}",
                "owner": episode.employee_ids[0],
                "follow_up": refs.task_id,
                "alternatives": "guarded rollback or staged release",
                "conflict": (
                    "support wanted an immediate workaround while engineering preferred more evidence"
                ),
                "due_date": (base_at + timedelta(days=8 + meeting_variant)).date().isoformat(),
            },
            "redwoodEpisodeId": episode.episode_id,
            "redwoodStage": "meeting",
            "redwoodReferences": {
                **common,
                "ticketId": refs.ticket_id,
                "taskId": refs.task_id,
            },
        }
        specs.append(
            _spec(
                episode,
                source="fireflies-redwood",
                object_type="transcript",
                external_id=refs.meeting_id,
                revision=1,
                day_offset=5,
                base_at=base_at,
                container="meetings",
                payload=meeting_payload,
            )
        )
        revised_meeting_payload = dict(meeting_payload)
        revised_meeting_payload["date"] = (base_at + timedelta(days=6)).isoformat()
        revised_meeting_payload["protocol"] = {
            "decision": f"Revised decision: ship mitigation task {refs.task_id}",
            "owner": episode.employee_ids[-1],
            "follow_up": refs.task_id,
        }
        revised_meeting_payload["transcript"] = (
            f"{meeting_quote} Revised protocol for episode {episode.episode_id}: the team confirmed task "
            f"{refs.task_id} after reviewing the customer report."
        )
        specs.append(
            _spec(
                episode,
                source="fireflies-redwood",
                object_type="transcript",
                external_id=refs.meeting_id,
                revision=2,
                day_offset=6,
                base_at=base_at,
                container="meetings",
                payload=revised_meeting_payload,
            )
        )

        decision_id = f"DEC-{refs.meeting_id}"
        decision_content = (
            f"Decision {decision_id} records the outcome of meeting {refs.meeting_id}: "
            f"the team agreed to {decision_text}. It authorizes task {refs.task_id} "
            f"for ticket {refs.ticket_id}."
        )
        decision_payload = {
            "id": decision_id,
            "title": f"Decision {decision_id}",
            "status": "current",
            "space": {"key": "ENG", "name": "Engineering"},
            "authorId": f"acct:{episode.employee_ids[-1]}",
            "createdAt": (base_at + timedelta(days=6.5)).isoformat(),
            "version": {
                "number": 1,
                "createdAt": (base_at + timedelta(days=6.5)).isoformat(),
            },
            "labels": {"results": [{"name": "decision"}]},
            "decision": f"Approve mitigation task {refs.task_id}",
            "decision_owner": episode.employee_ids[-1],
            "decision_date": (base_at + timedelta(days=6.5)).date().isoformat(),
            "body": {
                "atlas_doc_format": {
                    "value": json.dumps(
                        {
                            "type": "doc",
                            "version": 1,
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": decision_content}],
                                }
                            ],
                        },
                        sort_keys=True,
                    )
                }
            },
            "redwoodEpisodeId": episode.episode_id,
            "redwoodStage": "decision",
            "redwoodReferences": {
                **common,
                "meetingId": refs.meeting_id,
                "ticketId": refs.ticket_id,
                "taskId": refs.task_id,
            },
        }
        specs.append(
            _spec(
                episode,
                source="confluence-redwood",
                object_type="page",
                external_id=decision_id,
                revision=1,
                day_offset=6.5,
                base_at=base_at,
                container="ENG",
                payload=decision_payload,
            )
        )
        revised_decision_payload = json.loads(json.dumps(decision_payload))
        revised_decision_payload["version"] = {
            "number": 2,
            "createdAt": (base_at + timedelta(days=7)).isoformat(),
        }
        revised_decision_payload["body"]["atlas_doc_format"]["value"] = json.dumps(
            {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"Decision {decision_id} was revised after the owner review: "
                                    f"{decision_text}. Task {refs.task_id} remains the follow-up."
                                ),
                            }
                        ],
                    }
                ],
            },
            sort_keys=True,
        )
        revised_decision_payload["decision"] = f"Revised decision: ship mitigation task {refs.task_id}"
        revised_decision_payload["decision_owner"] = episode.employee_ids[-1]
        revised_decision_payload["decision_date"] = (base_at + timedelta(days=7)).date().isoformat()
        specs.append(
            _spec(
                episode,
                source="confluence-redwood",
                object_type="page",
                external_id=decision_id,
                revision=2,
                day_offset=7,
                base_at=base_at,
                container="ENG",
                payload=revised_decision_payload,
            )
        )

        task_description = (
            f"{task_profile['lead']} {task_profile['focus']}. "
            f"This implements decision {decision_id} from meeting {refs.meeting_id}; "
            f"the originating support record is {refs.ticket_id}. "
            f"Use {task_profile['evidence']} as the evidence. Acceptance: "
            f"{task_profile['acceptance']}. Risk: {task_profile['risk']}."
        )
        task_comments: list[dict[str, Any]] = []
        task_histories: list[dict[str, Any]] = []
        previous_task_status = "To Do"
        for revision, (offset, status) in enumerate(
            ((7, "To Do"), (8, "In Progress"), (9, "Reopened"), (10, "Review"), (11, "Done")), start=1
        ):
            if status != previous_task_status:
                author_id = episode.employee_ids[(revision - 1) % len(episode.employee_ids)]
                task_histories.append(
                    {
                        "id": f"{refs.task_id}-status-{revision}",
                        "author": {
                            "accountId": f"acct:{author_id}",
                            "displayName": author_id,
                            "emailAddress": f"{author_id.casefold()}@redwood.example",
                        },
                        "created": (base_at + timedelta(days=6 + revision)).isoformat(),
                        "items": [
                            {
                                "field": "status",
                                "fromString": previous_task_status,
                                "toString": status,
                            }
                        ],
                    }
                )
                previous_task_status = status
            task_comments.extend(
                _task_comment(
                    episode,
                    key=refs.task_id,
                    profile=task_profile,
                    revision=revision,
                    status=status,
                    created_at=base_at + timedelta(days=6 + revision),
                )
            )
            task_comments = task_comments[-8:]
            specs.append(
                _spec(
                    episode,
                    source="jira-redwood",
                    object_type="issue",
                    external_id=refs.task_id,
                    revision=revision,
                    day_offset=offset,
                    base_at=base_at,
                    container="ENG",
                    payload=_jira_payload(
                        episode,
                        key=refs.task_id,
                        status=status,
                        summary=f"{task_profile['verb']} {task_profile['focus']}",
                        description=task_description,
                        stage="task",
                        references={
                            "meetingId": refs.meeting_id,
                            "decisionId": decision_id,
                            "ticketId": refs.ticket_id,
                        },
                        created_at=base_at + timedelta(days=6),
                        revision=revision,
                        comments=task_comments,
                        histories=task_histories,
                        labels=["episode", "task", task_profile["focus"].split()[0]],
                        priority=("Highest" if episode_index % 7 == 0 else "High"),
                    ),
                )
            )

        for revision, (offset, state, _review_status) in enumerate(
            ((13, "opened", "pending"), (15, "opened", "approved"), (17, "merged", "approved")),
            start=1,
        ):
            specs.append(
                _spec(
                    episode,
                    source="gitlab-redwood",
                    object_type="merge_request",
                    external_id=refs.merge_request_id,
                    revision=revision,
                    day_offset=offset,
                    base_at=base_at,
                    container="ENG",
                    payload=_engineering_mr_payload(
                        episode,
                        refs=refs,
                        decision_id=decision_id,
                        task_profile=task_profile,
                        common=common,
                        revision=revision,
                        state=state,
                        occurred_at=base_at + timedelta(days=offset),
                    ),
                )
            )

        merged_mr_payload = _engineering_mr_payload(
            episode,
            refs=refs,
            decision_id=decision_id,
            task_profile=task_profile,
            common=common,
            revision=3,
            state="merged",
            occurred_at=base_at + timedelta(days=17),
        )
        deployment_changed_files = list(merged_mr_payload["changed_files"])
        deployment_body = (
            f"Episode {episode.episode_id}: deployment {refs.deployment_id} ships merged MR "
            f"{refs.merge_request_id} files {', '.join(deployment_changed_files)}."
        )
        specs.append(
            _spec(
                episode,
                source="confluence-redwood",
                object_type="page",
                external_id=refs.deployment_id,
                revision=1,
                day_offset=18,
                base_at=base_at,
                container="ENG",
                payload={
                    "id": refs.deployment_id,
                    "title": f"Deployment {refs.deployment_id}",
                    "status": "current",
                    "space": {"key": "ENG", "name": "Engineering"},
                    "authorId": f"acct:{episode.employee_ids[0]}",
                    "createdAt": (base_at + timedelta(days=17)).isoformat(),
                    "version": {"number": 1, "createdAt": (base_at + timedelta(days=17)).isoformat()},
                    "labels": {"results": [{"name": "deployment"}]},
                    "changed_files": deployment_changed_files,
                    "body": {
                        "atlas_doc_format": {
                            "value": json.dumps(
                                {
                                    "type": "doc",
                                    "version": 1,
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": deployment_body}],
                                        }
                                    ],
                                },
                                sort_keys=True,
                            )
                        }
                    },
                    "redwoodEpisodeId": episode.episode_id,
                    "redwoodStage": "deployment",
                    "redwoodReferences": {**common, "mergeRequestId": refs.merge_request_id},
                },
            )
        )

        incident_description = (
            f"Episode {episode.episode_id}: incident {refs.incident_id} was caused by deployment "
            f"{refs.deployment_id}; mitigation remains in {refs.task_id}."
        )
        specs.append(
            _spec(
                episode,
                source="jira-redwood",
                object_type="issue",
                external_id=refs.incident_id,
                revision=1,
                day_offset=19,
                base_at=base_at,
                container="ENG",
                payload=_jira_payload(
                    episode,
                    key=refs.incident_id,
                    status="Done",
                    summary=f"Regression after {refs.deployment_id}",
                    description=incident_description,
                    stage="incident",
                    references={"deploymentId": refs.deployment_id, "taskId": refs.task_id},
                    created_at=base_at + timedelta(days=14),
                    revision=1,
                ),
                acl_override=Acl.model_validate(
                    {
                        "allow": [
                            {"principal": "grp:engineering"},
                            {"principal": "grp:management"},
                        ],
                        "deny": [],
                        "inherit": False,
                    }
                ),
            )
        )

        postmortem_content = (
            f"Episode {episode.episode_id}: postmortem {refs.postmortem_id} analyzes incident "
            f"{refs.incident_id}, deployment {refs.deployment_id}, and corrective task {refs.task_id}."
        )
        specs.append(
            _spec(
                episode,
                source="xwiki-redwood",
                object_type="page",
                external_id=refs.postmortem_id,
                revision=1,
                day_offset=23,
                base_at=base_at,
                container="ENG",
                payload={
                    "id": refs.postmortem_id,
                    "title": f"Postmortem {refs.incident_id}",
                    "space": "Engineering",
                    "author": episode.employee_ids[0],
                    "created": (base_at + timedelta(days=18)).isoformat(),
                    "version": "1.1",
                    "content": postmortem_content,
                    "properties": {"incident_id": refs.incident_id},
                    "redwoodEpisodeId": episode.episode_id,
                    "redwoodStage": "postmortem",
                    "redwoodReferences": {**common, "incidentId": refs.incident_id},
                },
            )
        )
    return specs
