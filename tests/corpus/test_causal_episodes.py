"""Cross-source support and engineering causality contract."""

from collections import defaultdict
from copy import deepcopy

from variator.corpus import acceptance
from variator.corpus.gold_links import materialize_gold_links
from variator.corpus.source_events import materialize_source_events


def _chains() -> dict[str, list[dict[str, object]]]:
    return {
        "accounts": [
            {"account_id": f"A{index:03d}", "account_name_synthetic": f"Customer {index}"}
            for index in range(1, 7)
        ],
        "employees": [{"employee_id": f"E{index:03d}", "department": "engineering"} for index in range(1, 9)],
        "opportunities": [
            {
                "opportunity_id": f"O{index:03d}",
                "account_id": f"A{index:03d}",
                "contract_value_usd": 100_000,
            }
            for index in range(1, 7)
        ],
    }


def _episode_stages(events):
    stages = defaultdict(dict)
    for event in events:
        payload = event.payload or {}
        episode_id = payload.get("redwoodEpisodeId")
        stage = payload.get("redwoodStage")
        if episode_id and stage:
            previous = stages[str(episode_id)].get(str(stage))
            if previous is None or event.revision > previous.revision:
                stages[str(episode_id)][str(stage)] = event
    return stages


def _latest_upsert_by_alias(events):
    by_alias = {}
    for event in events:
        if event.payload is None:
            continue
        for alias in event.aliases:
            previous = by_alias.get(alias)
            if previous is None or event.revision > previous.revision:
                by_alias[alias] = event
    return by_alias


def test_each_account_has_a_complete_ordered_nine_stage_chain() -> None:
    events = materialize_source_events(_chains(), seed=42)
    stages = _episode_stages(events)
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
    covered_accounts: set[str] = set()
    for episode_stages in stages.values():
        if not set(required).issubset(episode_stages):
            continue
        ordered = [episode_stages[stage] for stage in required]
        assert [event.world_at for event in ordered] == sorted(event.world_at for event in ordered)
        assert episode_stages["merge_request"].payload["state"] == "merged"
        assert episode_stages["merge_request"].payload["merged_at"]
        covered_accounts.add(ordered[1].payload["redwoodReferences"]["accountId"])
    assert covered_accounts == {"A001", "A002", "A003", "A004", "A005", "A006"}


def test_causal_links_have_source_visible_exact_or_quoted_evidence() -> None:
    chains = _chains()
    events = materialize_source_events(chains, seed=42)
    links = materialize_gold_links(events, chains)
    by_alias = _latest_upsert_by_alias(events)
    episode_links = [link for link in links if link.rule_id.startswith("episode.")]

    assert len(episode_links) >= 24 * 8
    assert {link.predicate for link in episode_links} >= {
        "informed_by",
        "decided_in",
        "closes",
        "implements",
        "caused_by",
    }
    implicit_accounts = {
        by_alias[link.source_entity_id].payload["redwoodReferences"]["accountId"]
        for link in episode_links
        if not link.explicit
    }
    assert implicit_accounts == {"A001", "A002", "A003", "A004", "A005", "A006"}
    for link in episode_links:
        source_text = str(by_alias[link.source_entity_id].payload)
        assert link.evidence_quote in source_text
        if link.explicit:
            assert by_alias[link.target_entity_id].external_id in source_text


def test_deleted_postmortem_retains_all_historical_incident_links() -> None:
    chains = _chains()
    events = materialize_source_events(chains, seed=42)
    deleted_postmortems = [
        event
        for event in events
        if event.op == "delete" and event.source == "xwiki-redwood" and event.external_id.startswith("PM-")
    ]
    assert deleted_postmortems

    links = materialize_gold_links(events, chains)
    postmortem_links = [link for link in links if link.rule_id == "episode.postmortem.incident.v1"]

    assert len(postmortem_links) == 48
    assert {link.source_entity_id for link in postmortem_links}.issuperset(
        event.aliases[0] for event in deleted_postmortems
    )


def test_distinct_semantic_rules_for_the_same_entities_survive_deduplication() -> None:
    chains = _chains()
    events = materialize_source_events(chains, seed=42)
    merge_request = next(
        event
        for event in events
        if (event.payload or {}).get("redwoodStage") == "merge_request" and event.revision == 3
    )
    task_id = merge_request.payload["redwoodReferences"]["taskId"]
    chains["pull_requests"] = [
        {
            "pr_id": merge_request.external_id,
            "linked_task_id": task_id,
        }
    ]

    links = materialize_gold_links(events, chains)
    matching_rules = {
        link.rule_id
        for link in links
        if link.source_entity_id == merge_request.aliases[0]
        and link.target_entity_id == f"jira.{task_id}"
        and link.predicate == "closes"
    }

    assert matching_rules == {
        "merge_request.closes_task.v2",
        "episode.mr.task.v1",
    }


def test_causal_acceptance_rejects_an_open_terminal_merge_request() -> None:
    chains = _chains()
    events = materialize_source_events(chains, seed=42)
    links = materialize_gold_links(events, chains)
    event_records = [event.model_dump(mode="json") for event in events if event.op != "delete"]
    link_records = [link.model_dump(mode="json") for link in links]
    assert acceptance._has_causal_episodes(event_records, link_records)

    corrupted = deepcopy(event_records)
    episode_merge_requests = [
        event for event in corrupted if (event.get("payload") or {}).get("redwoodStage") == "merge_request"
    ]
    latest = max(episode_merge_requests, key=lambda event: int(event["revision"]))
    latest["payload"]["state"] = "opened"
    latest["payload"]["merged_at"] = None
    assert not acceptance._has_causal_episodes(corrupted, link_records)
