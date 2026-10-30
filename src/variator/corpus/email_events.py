"""Source-native Microsoft Graph messages for deterministic business episodes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from variator.corpus.canonical import immutable_id
from variator.corpus.episodes import BusinessEpisode
from variator.corpus.models import Acl
from variator.corpus.redwood_delivery import COMPANY_NAME, customer_contact_identity
from variator.corpus.redwood_profile import PROFILE_START

THREADS_PER_EPISODE = 13
TOPICS = (
    "discovery",
    "support",
    "implementation",
    "security",
    "proposal",
    "legal",
    "invoice",
    "payment",
    "deployment",
    "incident",
    "review",
    "renewal",
    "executive",
)
REPLY_DELAYS = (
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(days=1),
    timedelta(days=3),
)
MESSAGE_OPENINGS = (
    "Following yesterday's customer review",
    "After checking the implementation notes",
    "During the account handoff",
    "Based on the support team's latest evidence",
    "Ahead of the next steering meeting",
    "After validating the shared timeline",
    "Following the product council discussion",
    "During the weekly delivery checkpoint",
    "After comparing the contract and rollout plan",
    "Based on the customer's latest clarification",
    "Following the security review",
    "After the operations team reproduced the issue",
    "During the commercial approval round",
    "Following the deployment readiness check",
    "After finance reconciled the open items",
    "Based on the incident follow-up",
    "During the executive account review",
    "After the owner confirmed the next milestone",
)
MESSAGE_REQUESTS = (
    "confirm the accountable owner",
    "record the agreed due date",
    "attach the supporting evidence",
    "clarify the customer impact",
    "verify the approved amount",
    "name the release milestone",
    "document the decision rationale",
    "identify the unresolved blocker",
    "confirm the revised scope",
    "summarize the security constraint",
    "reconcile the payment status",
    "state the rollout dependency",
    "capture the support response",
    "verify the acceptance criteria",
    "record the commercial exception",
    "confirm the deployment window",
    "explain the status transition",
    "link the follow-up work item",
)
MESSAGE_NEXT_STEPS = (
    "The owner will update the ticket before the next customer call.",
    "The decision will be recorded in the meeting protocol.",
    "Engineering will cite this note in the merge request.",
    "Finance will reconcile the amount against the invoice.",
    "Support will keep the customer informed until closure.",
    "The release manager will verify the deployment record.",
    "Sales will update the deal stage after written approval.",
    "The account team will review the risk at the weekly checkpoint.",
    "Security will attach its assessment to the shared record.",
    "Product will compare the request with the current roadmap.",
    "Operations will preserve the evidence for the postmortem.",
    "The customer success owner will confirm the outcome in writing.",
    "Legal will record the approved exception in the proposal.",
    "The project lead will split the work into traceable changes.",
    "The incident commander will connect the symptom to the deployment.",
    "The reviewer will leave source-visible comments before approval.",
    "The billing owner will track the remaining balance.",
    "The team will revisit the decision if the underlying facts change.",
)

SUBJECT_FORMS = (
    "{account}: {topic} follow-up on {focus}",
    "{topic} question from {account}",
    "Can we confirm {focus} for {account}?",
    "{account} - {focus} before the next review",
    "Notes from the {topic} review: {focus}",
    "A decision is needed on {focus}",
    "{account} asked about the {topic} timeline",
    "What should we carry forward from {focus}?",
    "{topic} handoff: owner and next step",
    "Checking the {topic} evidence before Friday",
    "The customer needs an answer on {focus}",
    "Follow-up after the {topic} conversation",
)

THREAD_LENGTH_CUTOFFS = (
    (80, 1),
    (230, 2),
    (400, 3),
    (550, 4),
    (680, 5),
    (790, 6),
    (870, 7),
    (925, 8),
    (967, 9),
    (997, 10),
)


def _thread_length(thread_index: int) -> int:
    """Return a skewed, deterministic thread length rather than equal buckets."""
    bucket = (thread_index * 73 + 41) % 997
    for cutoff, length in THREAD_LENGTH_CUTOFFS:
        if bucket < cutoff:
            return length
    return 10


def _thread_kind(thread_index: int, thread_length: int) -> str:
    """Classify a thread using business meaning, not a uniqueness marker."""
    if thread_length == 1:
        return "unanswered"
    if thread_index % 17 == 0:
        return "escalation"
    if thread_index % 13 == 0:
        return "approval"
    if thread_index % 19 == 0:
        return "rejection"
    if thread_index % 7 == 0:
        return "delayed_response"
    return "standard"


def _message_intent(topic: str, thread_index: int, message_index: int) -> str:
    """Give messages realistic workflow intent for downstream filtering and audits."""
    if topic in {"proposal", "invoice", "payment", "renewal"} and message_index >= 2:
        return "approval" if (thread_index + message_index) % 5 in {0, 1} else "rejection"
    if (
        topic in {"support", "security", "incident", "deployment"}
        and (thread_index + message_index) % 11 == 0
    ):
        return "escalation"
    if message_index == 1:
        return "question"
    if message_index == 2:
        return "evidence_update"
    return "follow_up"


IMPLICIT_CONTEXT_TARGETS = {
    "discovery": "deal",
    "support": "task",
    "implementation": "task",
    "security": "decision",
    "proposal": "deal",
    "legal": "decision",
    "invoice": "deal",
    "payment": "deal",
    "deployment": "deployment",
    "incident": "incident",
    "review": "decision",
    "renewal": "deal",
    "executive": "decision",
}


def _implicit_context(
    episode: BusinessEpisode, topic: str, context: str, account_name: str
) -> dict[str, str]:
    """Describe a recoverable relationship without leaking an entity identifier."""
    account = account_name or "the customer account"
    phrases = (
        f"the {topic} agreement from the latest {account} customer review",
        f"the {topic} follow-up the {account} team approved after discussion",
        f"the {topic} change agreed with {account} before the next checkpoint",
        f"the {topic} decision captured after {account} shared this evidence",
        f"the {topic} work the customer and {COMPANY_NAME} assigned during the review",
        f"the {topic} outcome that {account} asked us to confirm in writing",
    )
    phrase = phrases[(len(context) + len(episode.episode_id) + len(topic)) % len(phrases)]
    return {
        "kind": f"customer_{topic}_reference",
        "target_stage": IMPLICIT_CONTEXT_TARGETS[topic],
        "phrase": phrase,
    }


TOPIC_CONTEXT = {
    "discovery": (
        "a new search workflow for the support team",
        "a reporting gap between product usage and the renewal plan",
        "a request to compare two rollout options before committing",
        "an adoption signal that differs by region and user role",
        "a customer objective that needs an owner and a measurable result",
        "a scope boundary that was clarified after the account review",
    ),
    "support": (
        "a timeout that affects the morning support queue",
        "a customer symptom reproduced only after a stale refresh token",
        "a handoff where the customer needs a status before Friday",
        "a missing result that appears only for one tenant configuration",
        "a support escalation with a known impact window and a safe workaround",
        "a repeated callback failure that the customer has attached to a ticket",
    ),
    "implementation": (
        "an acceptance test for incremental indexing",
        "a migration step that must be resumable after a worker restart",
        "an ownership change at the query-serving boundary",
        "a connector contract that needs an explicit error state",
        "a rollout slice with a measurable freshness target",
        "a change boundary that keeps the customer configuration intact",
    ),
    "security": (
        "a permission refresh observed after a role was removed",
        "a threat-model question about data leaving the tenant boundary",
        "an access review for a contractor joining the support rotation",
        "a redaction check on an export containing protected fields",
        "a key-rotation window that overlaps with a scheduled deployment",
        "an approval boundary between customer evidence and internal notes",
    ),
    "proposal": (
        "a fixed-price proposal with a staged implementation option",
        "an estimate that separates discovery from the production milestone",
        "a commercial exception requested for the first contract year",
        "a proposal revision after the customer reduced the initial scope",
        "a pricing comparison that includes support coverage and response time",
        "an approval packet whose assumptions need to be visible to finance",
    ),
    "legal": (
        "a data-processing clause that changes the onboarding sequence",
        "an approved exception with a named expiry date",
        "a renewal obligation that depends on the security appendix",
        "a liability question raised against the service-level language",
        "a redline that changes who may access customer evidence",
        "a contract amendment awaiting confirmation from both signatories",
    ),
    "invoice": (
        "an issued invoice with a milestone amount and a net-30 term",
        "a billing account whose purchase order number was corrected",
        "a service credit that must appear before the invoice is sent",
        "an invoice line that needs reconciliation with the accepted scope",
        "a tax treatment question for a cross-border customer account",
        "a final invoice prepared after the deployment acceptance note",
    ),
    "payment": (
        "a settlement record that is still missing the remittance advice",
        "a remaining balance split between two approved purchase orders",
        "a receipt that arrived after the account crossed its due date",
        "a partial payment that changes the accounts-receivable forecast",
        "a disputed charge waiting for the customer success owner",
        "a payment confirmation that should close the matching exception",
    ),
    "deployment": (
        "a release window with a rollback checkpoint after the first region",
        "a canary deployment whose owner needs the final acceptance signal",
        "a production change that depends on the connector migration",
        "a rollout plan adjusted for a customer blackout period",
        "a deployment record that must link the release to the approved ticket",
        "a staged launch where support needs the exact customer-facing wording",
    ),
    "incident": (
        "a failed request correlated with one deployment marker",
        "an incident timeline with a gap between detection and acknowledgement",
        "a mitigation boundary that prevented a broader customer impact",
        "a regional error spike with a different signature in the second zone",
        "a recurrence that needs comparison with the previous postmortem",
        "a customer-visible failure whose workaround is safe but temporary",
    ),
    "review": (
        "a postmortem finding about queue age during backfill",
        "a corrective action that needs an owner outside the incident team",
        "a reliability signal that improved after the last deployment",
        "a follow-up finding that changes the next quarter's test plan",
        "an evidence review where the source record and dashboard disagree",
        "a lessons-learned item that should become a release gate",
    ),
    "renewal": (
        "a renewal date tied to the customer's adoption milestone",
        "an expansion option dependent on two teams reaching target usage",
        "an account risk caused by an unresolved support trend",
        "a renewal forecast revised after the finance reconciliation",
        "a customer success plan that names the next measurable outcome",
        "a contract extension waiting on the security review result",
    ),
    "executive": (
        "a decision owner for the weekly customer outcome",
        "a leadership checkpoint with one unresolved dependency",
        "a portfolio result that combines delivery and receivables",
        "an executive risk review after the incident follow-up",
        "a cross-functional decision that needs one accountable sponsor",
        "a weekly outcome whose variance needs an explanation, not a status label",
    ),
}

DIALOGUE_TURNS = (
    (
        "I am opening this thread with the concrete situation so the next owner can act "
        "without reconstructing the context."
    ),
    (
        "Thanks for the detail. I have matched the request to the source record and am "
        "checking the boundary before proposing a change."
    ),
    (
        "Here is the evidence from our side: the observed signal is specific to this account "
        "and does not reproduce in the control case."
    ),
    (
        "I can take the next action, but I need the decision recorded against the referenced "
        "object so the history remains auditable."
    ),
    (
        "One constraint changed since the previous note. I am narrowing the scope and calling "
        "out the tradeoff instead of silently changing the plan."
    ),
    (
        "The proposed outcome is now testable. I will attach the result and name the owner "
        "who confirms it at the next checkpoint."
    ),
    (
        "We agree with that treatment. Please preserve the exception and the date in the "
        "source record before advancing the status."
    ),
    (
        "The work is moving to the next system of record; I am linking the handoff so support "
        "can follow the same timeline."
    ),
    (
        "The latest check is complete. The remaining question is whether the customer-visible "
        "result is sufficient for closure."
    ),
    (
        "Closing this thread with the agreed result, the unresolved risk, and the follow-up "
        "that should survive the current conversation."
    ),
)


def episode_thread_start(episode_index: int, total_episodes: int, scene_index: int) -> datetime:
    """Return the deterministic original timestamp for an episode email thread."""
    total_threads = total_episodes * THREADS_PER_EPISODE
    thread_index = episode_index * THREADS_PER_EPISODE + scene_index
    day_index = round(thread_index * 179 / max(1, total_threads - 1))
    return PROFILE_START.replace(tzinfo=UTC) + timedelta(days=day_index, hours=8 + thread_index % 8)


def _next_working_time(
    value: datetime,
    employee_ids: tuple[str, ...],
    *,
    prefer_before_vacation: bool = False,
) -> datetime:
    """Keep routine Graph mail in business hours and outside deterministic vacations."""
    current = value
    vacation_intervals = []
    profile_start = PROFILE_START.replace(tzinfo=UTC)
    for employee_id in employee_ids:
        if not employee_id.startswith("E") or not employee_id[1:].isdigit():
            continue
        number = int(employee_id[1:])
        if number < 1 or (number - 1) % 9 != 0:
            continue
        start = profile_start + timedelta(days=30 + number - 1)
        vacation_intervals.append((start, start + timedelta(days=7)))
    for _ in range(370):
        if current.weekday() >= 5 or current.hour >= 18:
            current = (current + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
            continue
        if current.hour < 8:
            current = current.replace(hour=8, minute=0, second=0, microsecond=0)
        vacation_window = next(
            ((start, end) for start, end in vacation_intervals if start <= current < end),
            None,
        )
        if vacation_window is not None:
            vacation_start, vacation_end = vacation_window
            if prefer_before_vacation:
                current = vacation_start - timedelta(days=1)
                while current.weekday() >= 5:
                    current -= timedelta(days=1)
                current = current.replace(hour=17, minute=0, second=0, microsecond=0)
            else:
                current = vacation_end.replace(hour=8, minute=0, second=0, microsecond=0)
            continue
        return current
    return current.replace(hour=17, minute=0, second=0, microsecond=0)


def _email_acl(employee_ids: tuple[str, ...], contact_principals: tuple[str, ...] = ()) -> Acl:
    principals = {
        *(f"user:{employee_id.casefold()}" for employee_id in employee_ids),
        *contact_principals,
    }
    return Acl.model_validate(
        {
            "allow": [{"principal": principal} for principal in sorted(principals)],
            "deny": [],
            "inherit": False,
        }
    )


def _reference_for_topic(episode: BusinessEpisode, topic: str) -> str:
    references = episode.references
    by_topic = {
        "discovery": references.deal_id,
        "support": references.ticket_id,
        "implementation": references.task_id,
        "security": references.meeting_id,
        "proposal": references.quote_id,
        "legal": references.quote_id,
        "invoice": references.invoice_id,
        "payment": references.payment_id,
        "deployment": references.deployment_id,
        "incident": references.incident_id,
        "review": references.postmortem_id,
        "renewal": references.deal_id,
        "executive": references.meeting_id,
    }
    return by_topic[topic]


def _attachment(topic: str, reference: str) -> list[dict[str, Any]]:
    if topic not in {"proposal", "invoice"}:
        return []
    extension = "pdf"
    return [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": f"attachment-{reference.casefold()}",
            "name": f"{reference}.{extension}",
            "contentType": "application/pdf",
            "size": 48_000,
            "isInline": False,
        }
    ]


def materialize_email_specs(
    chains: dict[str, list[dict[str, Any]]], episodes: list[BusinessEpisode]
) -> list[dict[str, Any]]:
    """Create varied Graph threads with explicit causal references."""
    accounts = {
        str(row.get("account_id")): row for row in chains.get("accounts", []) if row.get("account_id")
    }
    if not episodes:
        return []

    ordered_episodes = sorted(episodes, key=lambda episode: (episode.started_at, episode.episode_id))
    specs: list[dict[str, Any]] = []

    for episode_index, episode in enumerate(ordered_episodes):
        account = accounts.get(episode.account_id, {})
        account_name = str(account.get("account_name_synthetic") or account.get("name") or episode.account_id)
        customer_contact = customer_contact_identity(episode.account_id, account_name)
        customer_address = customer_contact["email"]
        for scene_index, topic in enumerate(TOPICS):
            thread_index = episode_index * THREADS_PER_EPISODE + scene_index
            thread_length = _thread_length(thread_index)
            thread_kind = _thread_kind(thread_index, thread_length)
            original = _next_working_time(
                episode_thread_start(episode_index, len(ordered_episodes), scene_index),
                episode.employee_ids,
                prefer_before_vacation=True,
            )
            conversation_id = f"rw-{episode.episode_id.casefold()}-{topic}"
            reference = _reference_for_topic(episode, topic)
            primary_employee = episode.employee_ids[scene_index % len(episode.employee_ids)]
            primary_address = f"{primary_employee.casefold()}@redwood.example"
            prior_message_id: str | None = None

            for message_index in range(1, thread_length + 1):
                if message_index > 1:
                    delay = (
                        timedelta(days=2)
                        if thread_kind == "delayed_response" and message_index == 2
                        else REPLY_DELAYS[(thread_index + message_index - 2) % len(REPLY_DELAYS)]
                    )
                    original = _next_working_time(original + delay, episode.employee_ids)
                from_customer = message_index % 2 == 1
                sender = customer_address if from_customer else primary_address
                recipient = primary_address if from_customer else customer_address
                cc_employee_ids: tuple[str, ...] = ()
                cc_bucket = (thread_index * 31 + message_index * 13) % 100
                if cc_bucket < 24 and len(episode.employee_ids) > 1:
                    cc_start = (scene_index + message_index + thread_index) % len(episode.employee_ids)
                    cc_count = 2 if cc_bucket < 7 and len(episode.employee_ids) > 2 else 1
                    cc_employee_ids = tuple(
                        episode.employee_ids[(cc_start + offset) % len(episode.employee_ids)]
                        for offset in range(cc_count)
                    )
                cc_recipients = [
                    {
                        "emailAddress": {
                            "address": f"{employee_id.casefold()}@redwood.example",
                            "name": employee_id,
                        }
                    }
                    for employee_id in cc_employee_ids
                ]
                variant = thread_index * 10 + message_index - 1
                opening = MESSAGE_OPENINGS[variant % len(MESSAGE_OPENINGS)]
                request = MESSAGE_REQUESTS[(variant // len(MESSAGE_OPENINGS)) % len(MESSAGE_REQUESTS)]
                next_step = MESSAGE_NEXT_STEPS[
                    (variant // (len(MESSAGE_OPENINGS) * len(MESSAGE_REQUESTS))) % len(MESSAGE_NEXT_STEPS)
                ]
                context = TOPIC_CONTEXT[topic][variant % len(TOPIC_CONTEXT[topic])]
                focus = " ".join(context.split()[:7]).rstrip(".,;:")
                subject = SUBJECT_FORMS[(thread_index * 5 + variant) % len(SUBJECT_FORMS)].format(
                    account=account_name,
                    topic=topic.title(),
                    focus=focus,
                )
                if len(subject) > 120:
                    subject = subject[:117].rstrip(" ,;:") + "..."
                implicit_context = _implicit_context(episode, topic, context, account_name)
                turn = DIALOGUE_TURNS[(message_index - 1 + thread_index) % len(DIALOGUE_TURNS)]
                signal = 3 + (thread_index * 17 + message_index * 11) % 97
                signal_unit = (
                    "minutes"
                    if topic in {"support", "incident", "deployment"}
                    else "percent"
                    if topic in {"discovery", "review", "renewal"}
                    else "items"
                )
                intent = _message_intent(topic, thread_index, message_index)
                is_forwarded = (thread_index * 29 + message_index * 17) % 100 < 7
                intent_text = {
                    "approval": "The sender approves the proposed treatment and asks the owner to record it.",
                    "rejection": (
                        "The sender rejects the current proposal and explains what must change "
                        "before approval."
                    ),
                    "escalation": (
                        "The customer impact is now escalated to the accountable owner for "
                        "a same-day response."
                    ),
                    "question": "The sender is asking for a concrete answer before the next checkpoint.",
                    "evidence_update": (
                        "The sender adds evidence from the latest check and narrows the "
                        "remaining uncertainty."
                    ),
                    "follow_up": (
                        "The sender confirms the next step and keeps the responsibility "
                        "visible in the thread."
                    ),
                }[intent]
                if message_index == 1:
                    body = (
                        f"{turn} {account_name} is writing about {context}. "
                        f"The initial signal is {signal} {signal_unit}; please {request} "
                        f"for the {topic} work. The expected outcome is {episode.outcome}."
                    )
                elif from_customer:
                    body = (
                        f"{turn} The customer confirms that {context} is still the relevant "
                        f"constraint and adds {signal} {signal_unit} from the latest check. "
                        f"Please {request}; the source-visible record is {reference}."
                    )
                else:
                    body = (
                        f"{turn} {COMPANY_NAME} checked {context} against {reference} and found "
                        f"{signal} {signal_unit} in the current sample. We will {request} "
                        f"and record the result in episode {episode.episode_id}."
                    )
                if is_forwarded:
                    body = f"Forwarding the relevant note so the next owner has the same context. {body}"
                body += (
                    f" {opening}; {next_step} Episode {episode.episode_id} is the "
                    "shared timeline for this conversation. "
                    f"For continuity, the team refers to {implicit_context['phrase']}. {intent_text}"
                )
                if topic == "proposal" and from_customer:
                    body += (
                        f" {account_name} approves proposal "
                        f"{episode.references.quote_id}; please advance deal "
                        f"{episode.references.deal_id} to the agreed stage."
                    )
                external_id = (
                    f"<rw-chain-{episode.episode_id.casefold()}-{topic}-m{message_index}@redwood.example>"
                )
                attachments = _attachment(topic, reference) if message_index == min(2, thread_length) else []
                headers = []
                if prior_message_id:
                    headers = [
                        {"name": "In-Reply-To", "value": prior_message_id},
                        {"name": "References", "value": prior_message_id},
                    ]
                if is_forwarded:
                    headers.append(
                        {
                            "name": "X-Redwood-Forwarded-From",
                            "value": prior_message_id or customer_address,
                        }
                    )
                message_subject = f"Fwd: {subject}" if is_forwarded else subject
                if message_index > 1 and not is_forwarded:
                    message_subject = f"Re: {subject}"
                payload = {
                    "id": external_id,
                    "internetMessageId": external_id,
                    "conversationId": conversation_id,
                    "subject": message_subject,
                    "from": {
                        "emailAddress": {
                            "address": sender,
                            "name": customer_contact["name"] if from_customer else primary_employee,
                        }
                    },
                    "toRecipients": [
                        {
                            "emailAddress": {
                                "address": recipient,
                                "name": primary_employee if from_customer else customer_contact["name"],
                            }
                        }
                    ],
                    "ccRecipients": cc_recipients,
                    "receivedDateTime": original.isoformat(),
                    "sentDateTime": original.isoformat(),
                    "inReplyTo": prior_message_id,
                    "internetMessageHeaders": headers,
                    "body": {"contentType": "text", "content": body},
                    "attachments": attachments,
                    "isRead": message_index > 1,
                    "importance": "high" if intent in {"escalation", "approval"} else "normal",
                    "redwoodThreadKind": thread_kind,
                    "redwoodMessageIntent": intent,
                    "redwoodForwarded": is_forwarded,
                    "forwardedFrom": (prior_message_id or customer_address) if is_forwarded else None,
                    "redwoodEpisodeId": episode.episode_id,
                    "redwoodTopic": topic,
                    "redwoodStage": (
                        "customer_email" if topic == "support" and message_index == 1 else "email"
                    ),
                    "redwoodReferences": {
                        "accountId": episode.account_id,
                        "dealId": episode.references.deal_id,
                        "sourceObjectId": reference,
                    },
                }
                acl_employee_ids = (primary_employee, *cc_employee_ids)
                specs.append(
                    {
                        "entity_id": immutable_id("redwood", "mail-redwood", "message", external_id),
                        "source": "mail-redwood",
                        "object_type": "message",
                        "external_id": external_id,
                        "revision": 1,
                        "original": original,
                        "calendar": "email",
                        "container": "mail",
                        "acl": _email_acl(acl_employee_ids, (customer_contact["principal"],)),
                        "payload": payload,
                    }
                )
                prior_message_id = external_id
    return specs
