"""Targeted regressions for numeric, ACL, and atomic answer facts."""

from test_episode_cases import _chains

from variator.corpus.episode_cases import make_episode_cases
from variator.corpus.gold_links import materialize_gold_links
from variator.corpus.source_events import materialize_source_events


def _case_data():
    chains = _chains()
    events = materialize_source_events(chains, seed=42)
    cases = make_episode_cases(events, materialize_gold_links(events, chains))
    by_revision = {
        (reference, event.revision): event
        for event in events
        for reference in [event.entity_id, *event.aliases]
    }
    return events, cases, by_revision


def test_numeric_cases_recompute_applied_payments_and_cover_financial_families():
    _, cases, by_revision = _case_data()
    numeric = [case for case in cases if case.category == "numeric"]
    assert len(numeric) == 10
    assert len(
        {
            next(fact["any"][0] for fact in case.answer_facts if fact["key"] == "financial_family")
            for case in numeric
        }
    ) >= 5

    non_zero = 0
    for case in numeric:
        evidence = [by_revision[(item["entity_id"], item["version"])] for item in case.evidence]
        invoices = [event for event in evidence if event.object_type == "invoice"]
        payments = [event for event in evidence if event.object_type == "payment"]
        assert len({event.entity_id for event in invoices}) >= 2
        assert len({event.entity_id for event in payments}) >= 2
        invoice_total = sum(
            float(event.payload["properties"].get("amount") or 0)
            for event in invoices
            if event.payload["properties"].get("status") != "voided"
        )
        applied_total = sum(
            float(event.payload["properties"].get("applied_amount") or 0)
            for event in payments
            if event.payload["properties"].get("application_status") == "completed"
        )
        credit_total = sum(
            float(event.payload["properties"].get("applicable_credit_note_amount") or 0)
            for event in invoices
        )
        expected = {
            "invoice_total": invoice_total,
            "paid_total": applied_total,
            "credit_note_total": credit_total,
            "outstanding_total": round(invoice_total - applied_total - credit_total, 2),
        }
        actual = {
            fact["key"]: float(fact["any"][0])
            for fact in case.answer_facts
            if fact["key"] in expected
        }
        assert actual == expected
        non_zero += expected["outstanding_total"] > 0
    assert non_zero >= 6


def test_finance_histories_have_void_reissue_and_credit_note_links():
    events, _, _ = _case_data()
    links = materialize_gold_links(events, _chains())
    assert any(
        (event.payload or {}).get("properties", {}).get("record_type") == "invoice"
        and (event.payload or {}).get("properties", {}).get("replaces_invoice_id")
        for event in events
    )
    assert any(
        (event.payload or {}).get("properties", {}).get("record_type") == "credit_note"
        for event in events
    )
    assert any(link.rule_id == "invoice_reissue.replaces_invoice.v1" for link in links)
    assert any(link.rule_id == "credit_note.applies_to_invoice.v1" for link in links)


def test_acl_cases_use_real_principals_departments_and_protected_objects():
    events, cases, by_revision = _case_data()
    acl = [case for case in cases if case.category == "acl"]
    by_reference = {reference: event for event in events for reference in [event.entity_id, *event.aliases]}
    assert len(acl) == 8
    assert len({case.principal for case in acl}) >= 6
    assert len(
        {
            (by_reference[case.evidence[0]["entity_id"]].payload or {}).get("redwoodAclDepartment")
            for case in acl
        }
    ) >= 4
    assert {case.acl_scenario for case in acl} == {
        "finance_full",
        "sales_margin_redacted",
        "engineering_commercial_redacted",
        "unrelated_not_found",
        "manager_inherited",
        "membership_change",
        "historical_as_of",
        "finance_secret_denied",
    }
    assert all(case.evidence for case in acl)
    assert all(case.acl_context for case in acl)
    for case in acl:
        visible = by_revision[(case.evidence[0]["entity_id"], case.evidence[0]["version"])]
        assert case.known_at is not None and case.known_at >= visible.world_at
        assert all(
            item["entity_id"] in {evidence["entity_id"] for evidence in case.evidence}
            for item in case.forbidden
        )


def test_answer_facts_are_atomic_and_decisions_use_structured_fields():
    events, cases, by_revision = _case_data()
    for case in cases:
        assert all(fact["key"] != "source_excerpt" for fact in case.answer_facts)
        assert all(len(str(value)) <= 120 for fact in case.answer_facts for value in fact["any"])
    for case in cases:
        decision_facts = [fact for fact in case.answer_facts if fact["key"] == "decision"]
        if not decision_facts:
            continue
        decision_events = [
            by_revision[(item["entity_id"], item["version"])]
            for item in case.evidence
            if (
                (by_revision[(item["entity_id"], item["version"])].payload or {}).get("decision")
                or (
                    by_revision[(item["entity_id"], item["version"])].payload or {}
                ).get("protocol", {}).get("decision")
            )
        ]
        assert decision_events
        assert decision_facts[0]["any"][0] in {
            str(
                event.payload.get("decision")
                or event.payload.get("protocol", {}).get("decision")
            )
            for event in decision_events
        }
