"""Stateful HubSpot-style commercial histories for Redwood episodes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from variator.corpus.canonical import immutable_id
from variator.corpus.episodes import BusinessEpisode
from variator.corpus.models import Acl


def _acl(chains: dict[str, list[dict[str, Any]]], episode: BusinessEpisode, container: str) -> Acl:
    departments = {"sales"} if container == "COMMERCIAL" else {"finance", "ops"}
    employee_ids = [
        str(row["employee_id"])
        for row in chains.get("employees", [])
        if row.get("employee_id")
        and (
            str(row.get("department", "")).casefold() in departments
            or container == "FINANCE"
            and "revenue" in str(row.get("role_title", "")).casefold()
        )
    ]
    if not employee_ids:
        employee_ids = list(episode.employee_ids[:1])
    principals = {f"user:{employee_id.casefold()}" for employee_id in employee_ids}
    principals.update(
        {"grp:sales", "grp:management"}
        if container == "COMMERCIAL"
        else {"grp:finance", "grp:management"}
    )
    return Acl.model_validate(
        {
            "allow": [{"principal": principal} for principal in sorted(principals)],
            "deny": [],
            "inherit": False,
        }
    )


def _payload(
    *,
    external_id: str,
    episode: BusinessEpisode,
    properties: dict[str, Any],
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    return {
        "id": external_id,
        "redwoodEpisodeId": episode.episode_id,
        "properties": {
            "hs_object_id": external_id,
            "redwood_episode_id": episode.episode_id,
            "account_id": episode.account_id,
            **properties,
        },
        "archived": False,
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


def _working_time(value: Any, employee_ids: tuple[str, ...]) -> Any:
    """Keep finance snapshots on workdays and within local work hours."""
    current = value
    for _ in range(370):
        if current.weekday() >= 5 or current.hour >= 18:
            current = (current + timedelta(days=1)).replace(
                hour=8, minute=0, second=0, microsecond=0
            )
            continue
        if current.hour < 8:
            current = current.replace(hour=8, minute=0, second=0, microsecond=0)
        return current
    return current.replace(hour=17, minute=0, second=0, microsecond=0)


def _snapshots(
    episode: BusinessEpisode,
    object_type: str,
    external_id: str,
    states: list[tuple[int, dict[str, Any]]],
    container: str,
    acl: Acl,
) -> list[dict[str, Any]]:
    created_at = _working_time(episode.started_at, episode.employee_ids)
    anchor_at = episode.started_at
    created = created_at.isoformat()
    entity_id = immutable_id("redwood", "hubspot-redwood", object_type, external_id)
    snapshots = []
    previous_at = None
    for revision, (day_offset, properties) in enumerate(states, start=1):
        occurred_at = _working_time(anchor_at + timedelta(days=day_offset), episode.employee_ids)
        if previous_at is not None and occurred_at <= previous_at:
            occurred_at = _working_time(previous_at + timedelta(minutes=10), episode.employee_ids)
        snapshots.append(
            {
                "entity_id": entity_id,
                "source": "hubspot-redwood",
                "object_type": object_type,
                "external_id": external_id,
                "revision": revision,
                "original": occurred_at,
                "calendar": "business",
                "container": container,
                "acl": acl,
                "payload": _payload(
                    external_id=external_id,
                    episode=episode,
                    properties=properties,
                    created_at=created,
                    updated_at=occurred_at.isoformat(),
                ),
            }
        )
        previous_at = occurred_at
    return snapshots


def _base_amount(chains: dict[str, list[dict[str, Any]]], episode: BusinessEpisode) -> float:
    amounts = [
        float(row.get("contract_value_usd") or 0)
        for row in chains.get("opportunities", [])
        if str(row.get("account_id") or "") == episode.account_id
    ]
    source_amount = amounts[0] if amounts and amounts[0] > 0 else 100_000.0
    sequence = int(episode.episode_id.rsplit("-", maxsplit=1)[-1])
    return round(source_amount * (0.85 + sequence * 0.05), 2)


def materialize_business_specs(
    chains: dict[str, list[dict[str, Any]]], episodes: list[BusinessEpisode]
) -> list[dict[str, Any]]:
    """Emit complete deal, quote, invoice, and payment snapshots."""
    specs: list[dict[str, Any]] = []
    for episode in episodes:
        if episode.kind != "commercial":
            continue
        refs = episode.references
        initial_amount = _base_amount(chains, episode)
        accepted_amount = round(initial_amount * 0.98, 2)
        won = episode.outcome != "lost"
        commercial_acl = _acl(chains, episode, "COMMERCIAL")
        finance_acl = _acl(chains, episode, "FINANCE")
        final_deal_status = "closed_won" if won else "closed_lost"
        sequence = int(episode.episode_id.rsplit("-", maxsplit=1)[-1])
        financial_family = (
            "voided_reissued"
            if episode.episode_id.endswith("-04")
            else "recurring_revenue"
            if episode.outcome in {"renewed", "churned"}
            else "forecast_actual"
            if sequence % 7 == 0
            else "credit_adjustment"
            if sequence % 6 == 0
            else "margin_discount"
            if sequence % 5 == 0
            else "dso_ar"
            if sequence % 4 == 0
            else "supplier_costs"
            if sequence % 3 == 0
            else "invoice_collections"
        )
        credit_note_id = f"CN-{refs.invoice_id}"
        reissued_invoice_id = f"{refs.invoice_id}-R1"
        credit_note_amount = round(accepted_amount * 0.025, 2)
        deal_description = (
            f"Episode {episode.episode_id}: deal {refs.deal_id} follows customer messages, "
            f"meeting {refs.meeting_id}, and proposal {refs.quote_id}."
        )
        deal_states = [
            (
                0,
                {
                    "status": "new",
                    "amount": initial_amount,
                    "discount": 0.0,
                    "forecast_amount": initial_amount,
                    "financial_family": financial_family,
                    "margin": 0.768,
                    "arr": 0.0,
                    "mrr": 0.0,
                },
            ),
            (
                1,
                {
                    "status": "qualified",
                    "amount": initial_amount,
                    "discount": 0.0,
                    "forecast_amount": initial_amount,
                    "financial_family": financial_family,
                    "margin": 0.768,
                    "arr": 0.0,
                    "mrr": 0.0,
                },
            ),
            (
                3,
                {
                    "status": "proposal_sent",
                    "amount": initial_amount,
                    "discount": 0.0,
                    "forecast_amount": initial_amount,
                    "financial_family": financial_family,
                    "margin": 0.768,
                    "arr": 0.0,
                    "mrr": 0.0,
                },
            ),
            (
                6,
                {
                    "status": "negotiation",
                    "amount": accepted_amount,
                    "discount": round(initial_amount - accepted_amount, 2),
                    "forecast_amount": round(accepted_amount * 1.08, 2),
                    "financial_family": financial_family,
                    "margin": 0.768,
                    "arr": accepted_amount if won else 0.0,
                    "mrr": round(accepted_amount / 12, 2) if won else 0.0,
                },
            ),
            (
                12,
                {
                    "status": final_deal_status,
                    "amount": accepted_amount,
                    "discount": round(initial_amount - accepted_amount, 2),
                    "forecast_amount": round(accepted_amount * 1.08, 2),
                    "financial_family": financial_family,
                    "margin": 0.768,
                    "arr": accepted_amount if won else 0.0,
                    "mrr": round(accepted_amount / 12, 2) if won else 0.0,
                },
            ),
        ]
        specs.extend(
            _snapshots(
                episode,
                "deal",
                refs.deal_id,
                [
                    (
                        offset,
                        {
                            **properties,
                            "quote_id": refs.quote_id,
                            "approval_email_id": (
                                f"<rw-chain-{episode.episode_id.casefold()}-proposal-m1@redwood.example>"
                            ),
                            "description": (
                                f"{deal_description} Forecast amount "
                                f"{properties['forecast_amount']:.2f}."
                            ),
                        },
                    )
                    for offset, properties in deal_states
                ],
                "COMMERCIAL",
                commercial_acl,
            )
        )

        quote_final_status = "accepted" if won else "rejected"
        if not won and episode.episode_id.endswith("-04"):
            quote_final_status = "revoked"
        quote_states = [
            (2, {"status": "draft", "amount": initial_amount}),
            (3, {"status": "sent", "amount": initial_amount}),
            (5, {"status": "revised", "amount": accepted_amount}),
            (8, {"status": quote_final_status, "amount": accepted_amount}),
        ]
        specs.extend(
            _snapshots(
                episode,
                "quote",
                refs.quote_id,
                [
                    (
                        offset,
                        {
                            **properties,
                            "deal_id": refs.deal_id,
                            "currency": "USD",
                            "description": (
                                f"Episode {episode.episode_id}: proposal {refs.quote_id} answers "
                                f"the customer request and prices deal {refs.deal_id}."
                            ),
                        },
                    )
                    for offset, properties in quote_states
                ],
                "COMMERCIAL",
                commercial_acl,
            )
        )
        if not won:
            continue

        if episode.episode_id.endswith("-04"):
            invoice_transitions = ((9, "draft", 0.0), (10, "issued", 0.0), (16, "voided", 0.0))
            payment_transitions = ((17, "voided", 0.0),)
        elif episode.outcome == "paid":
            invoice_transitions = ((9, "draft", 0.0), (10, "issued", 0.0), (18, "paid", accepted_amount))
            payment_transitions = ((19, "completed", accepted_amount),)
        elif episode.outcome == "paid_late":
            sequence = int(episode.episode_id.rsplit("-", maxsplit=1)[-1])
            if sequence % 2 == 1:
                invoice_transitions = (
                    (9, "draft", 0.0),
                    (10, "issued", 0.0),
                    (42, "overdue", 0.0),
                )
                payment_transitions = ((43, "pending", 0.0),)
            else:
                invoice_transitions = (
                    (9, "draft", 0.0),
                    (10, "issued", 0.0),
                    (42, "overdue", 0.0),
                    (49, "paid", accepted_amount),
                )
                payment_transitions = (
                    (43, "pending", 0.0),
                    (50, "completed", accepted_amount),
                )
        else:
            half = round(accepted_amount / 2, 2)
            invoice_transitions = (
                (9, "draft", 0.0),
                (10, "issued", 0.0),
                (20, "partial", half),
                (30, "paid", accepted_amount),
            )
            payment_transitions = ((21, "partial", half), (31, "completed", accepted_amount))
        specs.extend(
            _snapshots(
                episode,
                "invoice",
                refs.invoice_id,
                [
                    (
                        offset,
                        {
                            "status": status,
                            "deal_id": refs.deal_id,
                            "quote_id": refs.quote_id,
                            "contract_id": f"CONTRACT-{refs.deal_id}",
                            "payment_id": refs.payment_id,
                            "amount": accepted_amount,
                            "paid_amount": paid_amount,
                            "financial_family": financial_family,
                            "active_invoice": status != "voided",
                            "applied_payment_ids": [refs.payment_id] if paid_amount else [],
                            "applied_payment_amount": paid_amount,
                            "application_status": (
                                "completed"
                                if status in {"partial", "paid"}
                                else "voided"
                                if status == "voided"
                                else "pending"
                            ),
                            "credit_note_id": credit_note_id,
                            "credit_note_amount": credit_note_amount,
                            "credit_note_status": "voided",
                            "applicable_credit_note_amount": 0.0,
                            "reissued_invoice_id": reissued_invoice_id if status == "voided" else None,
                            "reissue_of": refs.invoice_id if status == "voided" else None,
                            "invoice_lifecycle": (
                                "voided then reissued" if status == "voided" else "active invoice revision"
                            ),
                            "days_to_collect": (
                                max(0, offset - 10) if status in {"paid", "voided"} else None
                            ),
                            "supplier_invoice_id": f"SUP-{refs.deal_id}",
                            "supplier_cost_amount": round(accepted_amount * 0.18, 2),
                            "operating_cost_id": f"COST-{refs.deal_id}",
                            "operating_cost_forecast": round(accepted_amount * 0.05, 2),
                            "operating_cost_actual": round(accepted_amount * 0.05 * 1.04, 2),
                            "balance": (
                                0.0 if status == "voided" else round(accepted_amount - paid_amount, 2)
                            ),
                            "currency": "USD",
                            "description": (
                                f"Episode {episode.episode_id}: invoice {refs.invoice_id} bills "
                                f"accepted proposal {refs.quote_id}. Amount {accepted_amount}; "
                                f"paid {paid_amount}; balance "
                                f"{round(accepted_amount - paid_amount, 2)}. "
                                f"Payment is {refs.payment_id}."
                            ),
                        },
                    )
                    for offset, status, paid_amount in invoice_transitions
                ],
                "FINANCE",
                finance_acl,
            )
        )
        specs.extend(
            _snapshots(
                episode,
                "payment",
                refs.payment_id,
                [
                    (
                        offset,
                        {
                            "status": status,
                            "invoice_id": refs.invoice_id,
                            "deal_id": refs.deal_id,
                            "amount": cumulative_amount,
                            "cumulative_amount": cumulative_amount,
                            "applied_amount": cumulative_amount,
                            "application_status": (
                                "completed"
                                if status in {"partial", "completed"}
                                else "voided"
                                if status == "voided"
                                else "pending"
                            ),
                            "financial_family": financial_family,
                            "applied_credit_note_amount": 0.0,
                            "currency": "USD",
                            "description": (
                                f"Episode {episode.episode_id}: payment {refs.payment_id} settles "
                                f"invoice {refs.invoice_id}. Cumulative amount {cumulative_amount}."
                            ),
                        },
                    )
                    for offset, status, cumulative_amount in payment_transitions
                ],
                "FINANCE",
                finance_acl,
            )
        )
        if episode.episode_id.endswith("-04"):
            specs.extend(
                _snapshots(
                    episode,
                    "crm_object",
                    reissued_invoice_id,
                    [
                        (
                            17,
                            {
                                "record_type": "invoice",
                                "status": "reissued",
                                "hs_object_id": reissued_invoice_id,
                                "replaces_invoice_id": refs.invoice_id,
                                "voided_invoice_id": refs.invoice_id,
                                "amount": accepted_amount,
                                "currency": "USD",
                                "financial_family": "voided_reissued",
                            },
                        ),
                        (
                            18,
                            {
                                "record_type": "invoice",
                                "status": "issued",
                                "hs_object_id": reissued_invoice_id,
                                "replaces_invoice_id": refs.invoice_id,
                                "voided_invoice_id": refs.invoice_id,
                                "amount": accepted_amount,
                                "currency": "USD",
                                "financial_family": "voided_reissued",
                            },
                        ),
                    ],
                    "FINANCE",
                    finance_acl,
                )
            )
        if sequence % 4 == 0:
            specs.extend(
                _snapshots(
                    episode,
                    "crm_object",
                    credit_note_id,
                    [
                        (
                            12,
                            {
                                "record_type": "credit_note",
                                "status": "proposed",
                                "hs_object_id": credit_note_id,
                                "applies_to_invoice_id": refs.invoice_id,
                                "amount": credit_note_amount,
                                "applicable": False,
                                "currency": "USD",
                                "financial_family": "credit_adjustment",
                            },
                        ),
                        (
                            13,
                            {
                                "record_type": "credit_note",
                                "status": "voided",
                                "hs_object_id": credit_note_id,
                                "applies_to_invoice_id": refs.invoice_id,
                                "amount": credit_note_amount,
                                "applicable": False,
                                "currency": "USD",
                                "financial_family": "credit_adjustment",
                            },
                        ),
                    ],
                    "FINANCE",
                    finance_acl,
                )
            )
        annual_recurring = accepted_amount if won else 0.0
        amendment_delta = round(accepted_amount * 0.10, 2) if episode.outcome == "renewed" else 0.0
        contract_states = [
            (
                11,
                {
                    "status": "draft",
                    "deal_id": refs.deal_id,
                    "quote_id": refs.quote_id,
                    "arr": 0.0,
                    "mrr": 0.0,
                    "discount": round(initial_amount - accepted_amount, 2),
                    "forecast_amount": accepted_amount,
                    "margin": 0.768,
                    "lost_arr": 0.0,
                },
            ),
            (
                14,
                {
                    "status": "active",
                    "deal_id": refs.deal_id,
                    "quote_id": refs.quote_id,
                    "arr": annual_recurring,
                    "mrr": annual_recurring / 12,
                    "discount": round(initial_amount - accepted_amount, 2),
                    "forecast_amount": accepted_amount,
                    "margin": 0.768,
                    "lost_arr": 0.0,
                },
            ),
        ]
        if episode.outcome == "renewed":
            expanded_arr = round(annual_recurring + amendment_delta, 2)
            contract_states.append(
                (
                    20,
                    {
                        "status": "active",
                        "deal_id": refs.deal_id,
                        "quote_id": refs.quote_id,
                        "arr": expanded_arr,
                        "mrr": expanded_arr / 12,
                        "discount": round(initial_amount - accepted_amount, 2),
                        "forecast_amount": accepted_amount,
                        "margin": 0.768,
                        "lost_arr": 0.0,
                    },
                )
            )
        elif episode.outcome == "churned":
            contract_states.append(
                (
                    55,
                    {
                        "status": "churned",
                        "deal_id": refs.deal_id,
                        "quote_id": refs.quote_id,
                        "arr": 0.0,
                        "mrr": 0.0,
                        "discount": round(initial_amount - accepted_amount, 2),
                        "forecast_amount": accepted_amount,
                        "margin": 0.768,
                        "lost_arr": annual_recurring,
                    },
                )
            )
        specs.extend(
            _snapshots(
                episode,
                "contract",
                f"CONTRACT-{refs.deal_id}",
                contract_states,
                "FINANCE",
                finance_acl,
            )
        )
        specs.extend(
            _snapshots(
                episode,
                "amendment",
                f"AMEND-{refs.deal_id}",
                [
                    (
                        15,
                        {
                            "status": "proposed",
                            "deal_id": refs.deal_id,
                            "contract_id": f"CONTRACT-{refs.deal_id}",
                            "delta_arr": 0.0,
                        },
                    ),
                    (
                        19,
                        {
                            "status": "approved" if episode.outcome == "renewed" else "rejected",
                            "deal_id": refs.deal_id,
                            "contract_id": f"CONTRACT-{refs.deal_id}",
                            "delta_arr": amendment_delta,
                        },
                    ),
                ],
                "FINANCE",
                finance_acl,
            )
        )
        supplier_amount = round(accepted_amount * 0.18, 2)
        specs.extend(
            _snapshots(
                episode,
                "supplier_invoice",
                f"SUP-{refs.deal_id}",
                [
                    (20, {"status": "draft", "amount": supplier_amount, "currency": "USD"}),
                    (21, {"status": "issued", "amount": supplier_amount, "currency": "USD"}),
                    (28, {"status": "paid", "amount": supplier_amount, "currency": "USD"}),
                ],
                "FINANCE",
                finance_acl,
            )
        )
        operating_forecast = round(accepted_amount * 0.05, 2)
        operating_actual = round(operating_forecast * 1.04, 2)
        specs.extend(
            _snapshots(
                episode,
                "operating_cost",
                f"COST-{refs.deal_id}",
                [
                    (20, {"status": "forecast", "amount": operating_forecast, "currency": "USD"}),
                    (29, {"status": "actual", "amount": operating_actual, "currency": "USD"}),
                ],
                "FINANCE",
                finance_acl,
            )
        )
    return specs
