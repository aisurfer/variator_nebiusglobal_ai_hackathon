"""Expanded commercial metrics and report reconciliation."""

from collections import defaultdict

from variator.corpus.cli import _dashboard_metrics
from variator.corpus.source_events import materialize_source_events


def _chains() -> dict[str, list[dict[str, object]]]:
    return {
        "accounts": [
            {
                "account_id": f"A{index:03d}",
                "account_name_synthetic": f"Customer {index}",
                "status": "customer",
            }
            for index in range(1, 7)
        ],
        "employees": [{"employee_id": f"E{index:03d}", "department": "sales"} for index in range(1, 13)],
        "opportunities": [
            {
                "opportunity_id": f"O{index:03d}",
                "account_id": f"A{index:03d}",
                "contract_value_usd": 100_000 + index * 5_000,
            }
            for index in range(1, 7)
        ],
        "account_health_daily": [
            {
                "account_health_id": f"H{index:03d}",
                "account_id": f"A{index:03d}",
                "timestamp": "2026-01-02T09:00:00Z",
                "health_score": 0.8,
            }
            for index in range(1, 7)
        ],
    }


def test_contracts_costs_and_amendments_have_histories() -> None:
    events = materialize_source_events(_chains(), seed=42)
    by_type: dict[str, list] = defaultdict(list)
    for event in events:
        if event.payload and event.payload.get("properties", {}).get("redwood_episode_id"):
            by_type[event.object_type].append(event)

    for object_type in ("contract", "amendment", "supplier_invoice", "operating_cost"):
        assert by_type[object_type]
        assert len({event.entity_id for event in by_type[object_type]}) >= 12
    assert all(
        len({event.revision for event in by_type[object_type] if event.entity_id == entity_id}) >= 2
        for object_type in ("contract", "amendment", "supplier_invoice", "operating_cost")
        for entity_id in {event.entity_id for event in by_type[object_type]}
    )


def test_dashboard_contains_reconciled_financial_indicators() -> None:
    chains = _chains()
    events = materialize_source_events(chains, seed=42)
    metrics = {metric["id"]: metric for metric in _dashboard_metrics(events, chains)}
    required = {
        "arr_usd",
        "mrr_usd",
        "churn_rate",
        "expansion_usd",
        "dso_days",
        "discount_usd",
        "gross_margin_pct",
        "forecast_revenue_usd",
        "actual_revenue_usd",
        "supplier_cost_usd",
        "operating_cost_usd",
    }
    assert required.issubset(metrics)
    assert metrics["arr_usd"]["value"] == metrics["mrr_usd"]["value"] * 12
    assert 0 < metrics["actual_revenue_usd"]["value"] <= metrics["closed_won_revenue_usd"]["value"]
    assert 0 <= metrics["gross_margin_pct"]["value"] <= 1
    assert all(metrics[metric_id]["evidence"] for metric_id in required)


def test_weekly_reports_include_financial_totals() -> None:
    events = materialize_source_events(_chains(), seed=42)
    reports = [event for event in events if event.external_id.startswith("north-star-")]
    assert reports
    assert all(
        all(
            token in event.payload["body"]["atlas_doc_format"]["value"]
            for token in ("ARR", "MRR", "Forecast", "Actual")
        )
        for event in reports
    )
