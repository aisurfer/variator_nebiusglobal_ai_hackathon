"""Tests for deterministic causal business episode planning."""

from collections import Counter

from variator.corpus.episodes import plan_business_episodes


def _chains() -> dict[str, list[dict[str, object]]]:
    accounts = [
        {"account_id": f"A{index:03d}", "account_name_synthetic": f"Account {index}"} for index in range(1, 7)
    ]
    employees = [
        {"employee_id": f"E{index:03d}", "department": department}
        for index, department in enumerate(
            ("sales", "engineering", "support", "finance", "product", "leadership"),
            start=1,
        )
    ]
    return {
        "accounts": accounts,
        "employees": employees,
        "opportunities": [
            {
                "opportunity_id": "O001",
                "account_id": "A001",
                "owner_employee_id": "E001",
            }
        ],
        "tasks": [
            {
                "task_id": "TASK-001",
                "linked_account_id": "A001",
                "assignee_employee_id": "E002",
            }
        ],
        "support_tickets": [{"ticket_id": "TKT-001", "account_id": "A001"}],
    }


def test_episode_planner_is_deterministic_and_balanced() -> None:
    first = plan_business_episodes(_chains(), seed=42)
    second = plan_business_episodes(_chains(), seed=42)
    different_seed = plan_business_episodes(_chains(), seed=43)

    assert first == second
    assert first != different_seed
    assert {episode.account_id for episode in first} == {
        "A001",
        "A002",
        "A003",
        "A004",
        "A005",
        "A006",
    }
    counts = Counter(episode.kind for episode in first)
    assert counts["commercial"] >= 24
    assert counts["support_engineering"] >= 24
    assert all(episode.employee_ids for episode in first)


def test_episode_references_resolve_for_sparse_accounts() -> None:
    episodes = plan_business_episodes(_chains(), seed=42)

    for episode in episodes:
        assert episode.episode_id
        assert episode.references.account_id == episode.account_id
        assert episode.references.deal_id
        assert episode.references.ticket_id
        assert episode.references.task_id
        assert episode.references.meeting_id
        assert episode.references.merge_request_id
        assert episode.references.deployment_id
        assert episode.references.invoice_id


def test_episode_schedule_contains_quiet_and_busy_weeks() -> None:
    episodes = plan_business_episodes(_chains(), seed=42)
    weekly_counts = Counter(episode.week_index for episode in episodes)

    assert min(weekly_counts) >= 0
    assert max(weekly_counts) < 26
    assert len(set(weekly_counts.values())) >= 3
    assert len(weekly_counts) < 26
