"""Dashboard and access reconciliation against final source snapshots."""

from variator.corpus.cli import _dashboard_metrics
from variator.corpus.redwood_delivery import build_directory
from variator.corpus.source_events import materialize_source_events


def _chains() -> dict[str, list[dict[str, object]]]:
    departments = (
        "engineering",
        "sales",
        "ops",
        "support",
        "security",
        "customer_success",
    )
    return {
        "accounts": [
            {
                "account_id": f"A{index:03d}",
                "account_name_synthetic": f"Customer {index}",
                "status": "customer",
            }
            for index in range(1, 7)
        ],
        "employees": [
            {
                "employee_id": f"E{index:03d}",
                "department": departments[(index - 1) % len(departments)],
                "role_title": "Lead",
            }
            for index in range(1, 13)
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


def test_dashboard_financial_metrics_equal_final_snapshots() -> None:
    chains = _chains()
    events = materialize_source_events(chains, seed=42)
    metrics = {metric["id"]: metric for metric in _dashboard_metrics(events, chains)}
    latest = {}
    for event in events:
        if event.source != "hubspot-redwood":
            continue
        previous = latest.get(event.entity_id)
        if previous is None or event.revision > previous.revision:
            latest[event.entity_id] = event
    episode_events = [
        event for event in latest.values() if event.payload.get("properties", {}).get("redwood_episode_id")
    ]
    won = [
        event
        for event in episode_events
        if event.object_type == "deal" and event.payload["properties"]["status"] == "closed_won"
    ]
    invoices = [event for event in episode_events if event.object_type == "invoice"]
    payments = [event for event in episode_events if event.object_type == "payment"]

    assert metrics["closed_won_revenue_usd"]["value"] == sum(
        event.payload["properties"]["amount"] for event in won
    )
    assert metrics["accounts_receivable_usd"]["value"] == sum(
        event.payload["properties"]["balance"] for event in invoices
    )
    assert metrics["paid_amount_usd"]["value"] == sum(
        event.payload["properties"]["cumulative_amount"] for event in payments
    )
    assert set(metrics["paid_amount_usd"]["evidence"]) == {event.aliases[0] for event in payments}


def test_commercial_acl_separates_sales_finance_and_engineering() -> None:
    events = materialize_source_events(_chains(), seed=42)
    quote = next(
        event
        for event in events
        if event.object_type == "quote"
        and event.revision == 1
        and event.payload["properties"].get("redwood_episode_id")
    )
    invoice = next(
        event
        for event in events
        if event.object_type == "invoice"
        and event.revision == 1
        and event.payload["properties"].get("redwood_episode_id")
    )
    payment = next(
        event
        for event in events
        if event.object_type == "payment"
        and event.revision == 1
        and event.payload["properties"].get("redwood_episode_id")
    )
    quote_acl = {rule.principal for rule in quote.acl.allow}
    invoice_acl = {rule.principal for rule in invoice.acl.allow}
    payment_acl = {rule.principal for rule in payment.acl.allow}

    assert "user:e002" in quote_acl
    assert "user:e003" in invoice_acl
    assert invoice_acl == payment_acl
    assert "user:e001" not in quote_acl | invoice_acl | payment_acl


def test_directory_has_customer_role_contacts() -> None:
    directory = build_directory(_chains())
    roles_by_organization = {}
    for contact in directory["contacts"]:
        organization_id = contact.get("organization_id")
        role = contact.get("role")
        if organization_id and role:
            roles_by_organization.setdefault(organization_id, set()).add(role)

    for index in range(1, 7):
        assert roles_by_organization[f"org-a{index:03d}"] == {
            "Customer stakeholder",
            "Finance contact",
            "Security contact",
            "Support contact",
            "Executive sponsor",
        }


def test_directory_marks_redwood_internal_and_exposes_named_customer_contacts() -> None:
    directory = build_directory(_chains())
    organizations = {organization["id"]: organization for organization in directory["organizations"]}
    assert organizations["org-redwood"]["kind"] == "internal"
    assert organizations["org-redwood-partners"]["kind"] == "partner"
    assert all(
        organization_id != "org-redwood"
        for organization_id in (
            organization["id"]
            for organization in directory["organizations"]
            if organization.get("kind") in {"client", "prospect"}
        )
    )

    named_contacts = [
        contact
        for contact in directory["contacts"]
        if contact.get("role") == "Customer stakeholder"
    ]
    assert len(named_contacts) == 6
    assert all(contact["principal"].startswith("contact:") for contact in named_contacts)
    assert all("@" in contact["email"] for contact in named_contacts)
    assert all(
        contact["name"] != f"{contact['organization_id']} Customer stakeholder"
        for contact in named_contacts
    )
