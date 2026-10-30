"""State and financial invariants for causal commercial histories."""

from collections import defaultdict
from copy import deepcopy

from variator.corpus import acceptance
from variator.corpus.business_events import materialize_business_specs
from variator.corpus.episodes import plan_business_episodes
from variator.corpus.source_events import materialize_source_events


def _chains() -> dict[str, list[dict[str, object]]]:
    return {
        "accounts": [
            {"account_id": f"A{index:03d}", "account_name_synthetic": f"Customer {index}"}
            for index in range(1, 7)
        ],
        "employees": [{"employee_id": f"E{index:03d}", "department": "sales"} for index in range(1, 7)],
        "opportunities": [
            {
                "opportunity_id": f"O{index:03d}",
                "account_id": f"A{index:03d}",
                "contract_value_usd": 100_000 + index * 5_000,
            }
            for index in range(1, 7)
        ],
    }


def _by_entity(specs: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for spec in specs:
        grouped[str(spec["entity_id"])].append(spec)
    return {
        entity_id: sorted(rows, key=lambda row: int(row["revision"])) for entity_id, rows in grouped.items()
    }


def test_commercial_objects_have_complete_ordered_histories() -> None:
    episodes = plan_business_episodes(_chains(), seed=42)
    specs = materialize_business_specs(_chains(), episodes)
    grouped = _by_entity(specs)

    deals = [rows for rows in grouped.values() if rows[0]["object_type"] == "deal"]
    quotes = [rows for rows in grouped.values() if rows[0]["object_type"] == "quote"]
    assert len(deals) == 48
    assert len(quotes) == 48
    assert all(
        [row["payload"]["properties"]["status"] for row in rows][:-1]
        == ["new", "qualified", "proposal_sent", "negotiation"]
        for rows in deals
    )
    assert all(
        row["original"] < later["original"]
        for rows in grouped.values()
        for row, later in zip(rows, rows[1:], strict=False)
    )
    assert any(len({row["payload"]["properties"]["amount"] for row in rows}) > 1 for rows in quotes)


def test_invoice_paths_cover_early_late_and_partial_payment() -> None:
    episodes = plan_business_episodes(_chains(), seed=42)
    grouped = _by_entity(materialize_business_specs(_chains(), episodes))
    invoice_statuses = [
        [row["payload"]["properties"]["status"] for row in rows]
        for rows in grouped.values()
        if rows[0]["object_type"] == "invoice"
    ]

    assert any(statuses == ["draft", "issued", "paid"] for statuses in invoice_statuses)
    assert any(statuses == ["draft", "issued", "overdue", "paid"] for statuses in invoice_statuses)
    assert any("partial" in statuses for statuses in invoice_statuses)


def test_final_invoice_balances_reconcile_to_payments() -> None:
    episodes = plan_business_episodes(_chains(), seed=42)
    specs = materialize_business_specs(_chains(), episodes)
    grouped = _by_entity(specs)
    payments_by_invoice: dict[str, float] = defaultdict(float)
    for rows in grouped.values():
        if rows[0]["object_type"] != "payment":
            continue
        final = rows[-1]["payload"]["properties"]
        payments_by_invoice[str(final["invoice_id"])] = float(final["cumulative_amount"])

    for rows in grouped.values():
        if rows[0]["object_type"] != "invoice":
            continue
        final = rows[-1]["payload"]["properties"]
        amount = float(final["amount"])
        paid = payments_by_invoice[str(final["hs_object_id"])]
        assert float(final["paid_amount"]) == paid
        if final["status"] == "voided":
            assert float(final["balance"]) == 0
        else:
            assert float(final["balance"]) == amount - paid
        if final["status"] in {"paid", "voided"}:
            assert float(final["balance"]) == 0
        else:
            assert float(final["balance"]) > 0


def test_financial_acceptance_rejects_a_corrupted_balance() -> None:
    records = [event.model_dump(mode="json") for event in materialize_source_events(_chains(), seed=42)]
    assert acceptance._has_financial_histories(records)

    corrupted = deepcopy(records)
    invoice_events = [event for event in corrupted if event["object_type"] == "invoice"]
    latest = max(invoice_events, key=lambda event: int(event["revision"]))
    latest["payload"]["properties"]["balance"] = 1.0
    assert not acceptance._has_financial_histories(corrupted)
