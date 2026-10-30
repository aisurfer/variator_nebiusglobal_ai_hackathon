"""Quality gates for evaluation cases derived from causal episodes."""

import re
from collections import Counter

from variator.corpus.episode_cases import make_episode_cases
from variator.corpus.gold_links import materialize_gold_links
from variator.corpus.source_events import materialize_source_events


def _chains() -> dict[str, list[dict[str, object]]]:
    return {
        "accounts": [
            {"account_id": f"A{index:03d}", "account_name_synthetic": f"Customer {index}"}
            for index in range(1, 7)
        ],
        "employees": [
            {"employee_id": f"E{index:03d}", "department": "engineering"} for index in range(1, 13)
        ],
        "opportunities": [
            {
                "opportunity_id": f"O{index:03d}",
                "account_id": f"A{index:03d}",
                "contract_value_usd": 100_000 + index * 5_000,
            }
            for index in range(1, 7)
        ],
    }


def _case_data():
    chains = _chains()
    events = materialize_source_events(chains, seed=42)
    links = materialize_gold_links(events, chains)
    cases = make_episode_cases(events, links)
    by_reference = {reference: event for event in events for reference in [event.entity_id, *event.aliases]}
    return events, cases, by_reference, links


def test_episode_cases_have_exact_quotas_and_short_facts() -> None:
    _, cases, _, _ = _case_data()
    assert Counter(case.category for case in cases) == {
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
    assert len(cases) == 100
    assert all(
        fact.get("key") != "source_excerpt" and all(len(str(option)) <= 120 for option in fact.get("any", []))
        for case in cases
        for fact in case.answer_facts
    )


def test_multi_hop_why_history_and_as_of_cases_are_structural() -> None:
    events, cases, by_reference, links = _case_data()
    revisions = {(event.entity_id, event.revision): event for event in events}
    revision_by_reference = {
        (reference, event.revision): event
        for event in events
        for reference in [event.entity_id, *event.aliases]
    }
    causal_pairs = {
        (link.source_entity_id, link.target_entity_id)
        for link in links
        if link.rule_id and link.rule_id.startswith("episode.")
    }
    for case in cases:
        if case.category == "multi_hop":
            assert len({by_reference[item["entity_id"]].source for item in case.evidence}) >= 3
        elif case.category == "why":
            stages = [
                (by_reference[item["entity_id"]].payload or {}).get("redwoodStage") for item in case.evidence
            ]
            assert stages == [
                "incident",
                "deployment",
                "merge_request",
                "task",
                "decision",
                "meeting",
                "slack",
                "ticket",
                "customer_email",
            ]
            references = [item["entity_id"] for item in case.evidence]
            assert all(pair in causal_pairs for pair in zip(references[:6], references[1:6], strict=False))
            assert 3 <= len(case.roots) <= 5
        elif case.category == "history":
            assert len(case.evidence) >= 3
            assert len({item["entity_id"] for item in case.evidence}) == 1
        elif case.category == "as_of":
            item = case.evidence[0]
            assert len(case.evidence) >= 2
            event = revision_by_reference[(item["entity_id"], item["version"])]
            next_event = revisions[(event.entity_id, event.revision + 1)]
            assert event.observed_at <= case.known_at < next_event.world_at


def test_why_cases_cover_accounts_and_as_of_cases_cover_each_required_object() -> None:
    _, cases, by_reference, _ = _case_data()
    why = [case for case in cases if case.category == "why"]
    why_accounts = {
        (by_reference[item["entity_id"]].payload or {}).get("redwoodReferences", {}).get("accountId")
        for case in why
        for item in case.evidence
        if (by_reference[item["entity_id"]].payload or {}).get("redwoodStage") == "incident"
    }
    assert len(why_accounts) >= 6
    assert len({re.sub(r"\b[A-Z][A-Z0-9-]*\d[A-Z0-9-]*\b", "ID", case.question) for case in why}) >= 10

    as_of_stages = Counter(
        (by_reference[case.evidence[0]["entity_id"]].payload or {}).get("redwoodStage")
        or by_reference[case.evidence[0]["entity_id"]].object_type
        for case in cases
        if case.category == "as_of"
    )
    assert as_of_stages == {"task": 2, "deal": 2, "invoice": 2, "decision": 2}


def test_numeric_cases_include_historical_partial_or_overdue_balances() -> None:
    _, cases, by_reference, _ = _case_data()
    numeric = [case for case in cases if case.category == "numeric"]
    nonzero = [
        case
        for case in numeric
        if float(next(fact for fact in case.answer_facts if fact["key"] == "outstanding_total")["any"][0]) > 0
    ]
    invoice_statuses = {
        fact["any"][0]
        for case in numeric
        for fact in case.answer_facts
        if fact["key"].startswith("invoice_") and fact["key"].endswith("_status")
    }
    assert len(nonzero) >= 6
    assert {"partial", "overdue"}.issubset(invoice_statuses)
    assert all(
        len(
            {
                by_reference[item["entity_id"]].entity_id
                for item in case.evidence
                if by_reference[item["entity_id"]].object_type in {"deal", "invoice", "payment"}
            }
        )
        >= 6
        for case in numeric
    )


def test_acl_cases_mix_redaction_and_expected_not_found_outcomes() -> None:
    _, cases, _, _ = _case_data()
    acl = [case for case in cases if case.category == "acl"]
    assert 2 <= sum(case.expect_not_found for case in acl) <= 6
    assert 2 <= sum(not case.expect_not_found for case in acl) <= 6
    assert len({case.principal for case in acl}) >= 4


def test_cancellation_cases_do_not_dominate_the_case_set() -> None:
    _, cases, _, _ = _case_data()
    cancellation_cases = [
        case for case in cases if any(fact["key"] == "close_reason" for fact in case.answer_facts)
    ]
    assert len(cancellation_cases) <= 15


def test_numeric_cases_reconcile_multiple_deals_invoices_and_payments() -> None:
    events, cases, by_reference, _ = _case_data()
    revision_by_reference = {
        (reference, event.revision): event
        for event in events
        for reference in [event.entity_id, *event.aliases]
    }
    for case in cases:
        if case.category != "numeric":
            continue
        evidence_events = [
            revision_by_reference[(item["entity_id"], item["version"])] for item in case.evidence
        ]
        by_type = {
            object_type: [event for event in evidence_events if event.object_type == object_type]
            for object_type in ("deal", "invoice", "payment")
        }
        assert all(len({event.entity_id for event in selected}) >= 2 for selected in by_type.values())
        expected = {
            "invoice_total": sum(
                event.payload["properties"]["amount"]
                for event in by_type["invoice"]
                if event.payload["properties"].get("status") != "voided"
            ),
            "paid_total": sum(
                event.payload["properties"]["cumulative_amount"] for event in by_type["payment"]
            ),
            "outstanding_total": sum(event.payload["properties"]["balance"] for event in by_type["invoice"]),
        }
        actual = {
            fact["key"]: float(fact["any"][0])
            for fact in case.answer_facts
            if fact["key"] in {"invoice_total", "paid_total", "outstanding_total"}
        }
        assert actual == expected


def test_acl_cases_cover_finance_and_commercial_secrets() -> None:
    _, cases, by_reference, _ = _case_data()
    acl_cases = [case for case in cases if case.category == "acl"]
    forbidden_types: set[str] = set()
    forbidden_keys: set[str] = set()
    assert {case.acl_scenario for case in acl_cases} == {
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
        if case.acl_scenario in {
            "sales_margin_redacted",
            "engineering_commercial_redacted",
            "unrelated_not_found",
            "membership_change",
            "finance_secret_denied",
        }:
            assert case.forbidden
        for forbidden in case.forbidden:
            event = by_reference[forbidden["entity_id"]]
            forbidden_types.add(event.object_type)
            forbidden_keys.update(fact["key"] for fact in forbidden["facts"])
            assert event.external_id in case.question
    assert {"invoice", "deal", "issue"}.issubset(forbidden_types)
    assert {"amount", "discount", "forecast"}.intersection(forbidden_keys)
