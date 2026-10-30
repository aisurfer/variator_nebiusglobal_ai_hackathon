"""Deterministic planning for cross-source Redwood business episodes."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from variator.corpus.redwood_profile import PROFILE_START

EpisodeKind = Literal["commercial", "support_engineering"]


@dataclass(frozen=True, slots=True)
class EpisodeReference:
    """Stable source-object identifiers shared by one causal episode."""

    account_id: str
    deal_id: str
    quote_id: str
    invoice_id: str
    payment_id: str
    ticket_id: str
    task_id: str
    meeting_id: str
    merge_request_id: str
    deployment_id: str
    incident_id: str
    postmortem_id: str


@dataclass(frozen=True, slots=True)
class BusinessEpisode:
    """One deterministic commercial or support-to-engineering storyline."""

    episode_id: str
    kind: EpisodeKind
    account_id: str
    started_at: datetime
    outcome: str
    employee_ids: tuple[str, ...]
    references: EpisodeReference
    week_index: int


def _first_by_account(
    rows: list[dict[str, Any]], account_id: str, account_fields: tuple[str, ...]
) -> dict[str, Any]:
    for row in rows:
        if any(str(row.get(field) or "") == account_id for field in account_fields):
            return row
    return {}


def _identifier(row: dict[str, Any], fields: tuple[str, ...], fallback: str) -> str:
    for field in fields:
        if row.get(field):
            return str(row[field])
    return fallback


def _references(
    chains: dict[str, list[dict[str, Any]]], account_id: str, episode_slug: str
) -> EpisodeReference:
    opportunity = _first_by_account(chains.get("opportunities", []), account_id, ("account_id",))
    ticket = _first_by_account(
        chains.get("support_tickets", []) + chains.get("tickets", []),
        account_id,
        ("account_id", "linked_account_id"),
    )
    task = _first_by_account(chains.get("tasks", []), account_id, ("linked_account_id", "account_id"))
    meeting = _first_by_account(chains.get("meetings", []), account_id, ("linked_account_id", "account_id"))
    base_deal_id = _identifier(opportunity, ("opportunity_id", "id"), "DEAL")
    base_ticket_id = _identifier(ticket, ("ticket_id", "id"), "TKT")
    base_task_id = _identifier(task, ("task_id", "id"), "TASK")
    base_meeting_id = _identifier(meeting, ("meeting_id", "id"), "MTG")
    return EpisodeReference(
        account_id=account_id,
        deal_id=f"{base_deal_id}-{episode_slug}",
        quote_id=f"QUOTE-{episode_slug}",
        invoice_id=f"INV-{episode_slug}",
        payment_id=f"PAY-{episode_slug}",
        ticket_id=f"{base_ticket_id}-{episode_slug}",
        task_id=f"{base_task_id}-{episode_slug}",
        meeting_id=f"{base_meeting_id}-{episode_slug}",
        merge_request_id=f"MR-{episode_slug}",
        deployment_id=f"DEP-{episode_slug}",
        incident_id=f"INC-{episode_slug}",
        postmortem_id=f"PM-{episode_slug}",
    )


def plan_business_episodes(chains: dict[str, list[dict[str, Any]]], seed: int) -> list[BusinessEpisode]:
    """Plan balanced, non-uniform episodes over the shared 180-day calendar."""
    accounts = sorted(
        (row for row in chains.get("accounts", []) if row.get("account_id")),
        key=lambda row: str(row["account_id"]),
    )[:6]
    employees = sorted(
        str(row["employee_id"]) for row in chains.get("employees", []) if row.get("employee_id")
    )
    if not accounts or not employees:
        return []

    templates = [
        (str(account["account_id"]), kind, sequence)
        for account in accounts
        for kind in ("commercial", "support_engineering")
        for sequence in range(1, 9)
    ]
    rng = random.Random(seed)
    rng.shuffle(templates)

    frequencies = (6, 6, 5, 5, 4, 4, 3, 3, 3, 2, 2, 2, 2, 1)
    week_pool = [week for week, count in enumerate(frequencies) for _ in range(count)]
    week_offset = rng.randrange(0, 26)
    if len(templates) <= len(week_pool):
        week_pool = week_pool[: len(templates)]
    else:
        week_pool = [week_pool[index % len(week_pool)] for index in range(len(templates))]
    weeks = [((week * 2) + week_offset) % 26 for week in week_pool]
    rng.shuffle(weeks)

    commercial_outcomes = ("paid", "paid_late", "lost", "renewed", "churned")
    engineering_outcomes = ("resolved", "resolved_after_incident", "workaround", "backlog")
    episodes: list[BusinessEpisode] = []
    profile_start = PROFILE_START.replace(tzinfo=UTC)
    for ordinal, ((account_id, kind, sequence), week_index) in enumerate(zip(templates, weeks, strict=True)):
        episode_slug = f"{account_id}-{kind[:3].upper()}-{sequence:02d}"
        participant_count = 2 + ((ordinal + seed) % min(4, len(employees)))
        employee_start = (ordinal * 3 + seed) % len(employees)
        employee_ids = tuple(
            employees[(employee_start + offset) % len(employees)] for offset in range(participant_count)
        )
        outcome_options = commercial_outcomes if kind == "commercial" else engineering_outcomes
        outcome = outcome_options[(ordinal + seed) % len(outcome_options)]
        day_offset = (ordinal * 3 + seed) % 7
        hour = 8 + ((ordinal + seed) % 9)
        started_at = profile_start + timedelta(weeks=week_index, days=day_offset, hours=hour)
        episodes.append(
            BusinessEpisode(
                episode_id=f"EP-{episode_slug}",
                kind=kind,  # type: ignore[arg-type]
                account_id=account_id,
                started_at=started_at,
                outcome=outcome,
                employee_ids=employee_ids,
                references=_references(chains, account_id, episode_slug),
                week_index=week_index,
            )
        )
    return sorted(episodes, key=lambda episode: (episode.started_at, episode.episode_id))
