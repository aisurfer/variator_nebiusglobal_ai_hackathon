"""Scenario presets for the it_services templates: typical situations of an IT services company.

Each preset is a set of relative changes (``domain.ScenarioOp``), so presets combine.
The numbers are assumptions for a quick what-if, not calibrated values.
"""

from __future__ import annotations

from variator.domain import Scenario, ScenarioOp


def _op(target: str, kind: str = "variable", **kw: object) -> ScenarioOp:
    return ScenarioOp(target=target, kind=kind, **kw)  # type: ignore[arg-type]


DELIVERY = [
    Scenario(
        name="requirements_change",
        label="Requirements change often",
        emoji="🔁",
        description="The client changes requirements: the chance of scope creep rises from 35% to 61%, "
        "and it is 1.5 times larger; more change requests and rework.",
        ops=[
            _op("scope_creep", "risk", probability=0.4),
            _op("scope_creep", "risk", scale=1.5),
            _op("change_share", scale=1.5),
            _op("rework_share", scale=1.15),
        ],
    ),
    Scenario(
        name="priorities_shift",
        label="Priorities shift",
        emoji="🔀",
        description="Priorities move during the release: context switches cut productivity by 10%, "
        "rework grows by 20%, and the go-live moves by about half a month.",
        ops=[
            _op("productivity", scale=0.9),
            _op("rework_share", scale=1.2),
            _op("risk_delay_months", shift=0.5),
        ],
    ),
    Scenario(
        name="key_people_leave",
        label="Key people leave",
        emoji="🚪",
        description="A tight labour market: the chance of a key-person loss in-house rises from 20% to 44%, "
        "and of a vendor team rotation from 25% to 48%.",
        ops=[
            _op("key_person_loss", "risk", probability=0.3),
            _op("vendor_turnover", "risk", probability=0.3),
        ],
    ),
    Scenario(
        name="tight_budget",
        label="Tight budget",
        emoji="💸",
        description="The monthly budget is cut by 15%; the budget check fails more often.",
        ops=[_op("budget_per_month", scale=0.85)],
    ),
    Scenario(
        name="fixed_deadline",
        label="Fixed deadline",
        emoji="⏰",
        description="The go-live date moves 2 months earlier, and delay counts twice as much.",
        ops=[_op("target_months", shift=-2), _op("delay_months", "weight", scale=2)],
    ),
    Scenario(
        name="vendor_rates_rise",
        label="Vendor rates rise",
        emoji="📈",
        description="Vendors raise rates and quotes by 10% (in-house cost stays).",
        ops=[
            _op("rate", scale=1.1, options=["nearshore_tm", "offshore_fixed"]),
            _op("fixed_quote", scale=1.1),
        ],
    ),
]

COMPANY = [
    Scenario(
        name="price_war",
        label="Price war",
        emoji="⚔️",
        description="The chance of a competitor price cut rises from 30% to 65%, "
        "and the cut is 1.5 times deeper.",
        ops=[_op("competitor_event", "risk", probability=0.5), _op("competitor_event", "risk", scale=1.5)],
    ),
    Scenario(
        name="gpu_crunch",
        label="GPU shortage",
        emoji="🖥️",
        description="The chance of a GPU shortage rises from 25% to 62%, "
        "and of a supplier price rise from 20% to 44%.",
        ops=[
            _op("gpu_shortage_event", "risk", probability=0.5),
            _op("vendor_price_event", "risk", probability=0.3),
        ],
    ),
    Scenario(
        name="big_client_leaves",
        label="Big client may leave",
        emoji="🧲",
        # no numbers: the model's key-account chance is an assumption, the history learns one per client
        description="A key account gives notice with a much higher chance, and the account it takes is 1.5 "
        "times larger.",
        ops=[_op("key_account_event", "risk", probability=0.6), _op("key_account_event", "risk", scale=1.5)],
    ),
    Scenario(
        name="client_requirements_churn",
        label="Clients change requirements",
        emoji="🔁",
        description="Clients change requirements and priorities often: more blockers, higher load, "
        "more late implementations.",
        ops=[_op("blockers", scale=1.5), _op("team_load", scale=1.08), _op("overdue_base", scale=1.3)],
    ),
    Scenario(
        name="hiring_freeze",
        label="Hard to hire",
        emoji="🧊",
        description="Hiring takes 4 months longer, and attrition is 20% higher.",
        ops=[_op("hiring_lead", shift=4), _op("attrition", scale=1.2)],
    ),
    Scenario(
        name="cash_squeeze",
        label="Cash squeeze",
        emoji="💰",
        description="Funding is late: 30% less cash at the start, and clients pay 30 days later.",
        ops=[_op("cash0", scale=0.7), _op("ar_days", shift=30)],
    ),
    Scenario(
        name="downturn",
        label="Market downturn",
        emoji="📉",
        description="The chance of a macro downturn rises from 20% to 60%, "
        "and demand growth is 5 points lower.",
        ops=[_op("macro_event", "risk", probability=0.5), _op("demand_growth", shift=-0.05)],
    ),
    Scenario(
        name="payroll_tax_july",  # the same id as the world hypothesis of the actor engine (future.forks)
        label="Payroll tax +5 pp from July",
        emoji="🧾",
        description="A new employer payroll tax of 5 more points applies from July: the cost of the gross "
        "payroll rises by 5% for the rest of the year.",
        ops=[_op("payroll_tax_add", shift=0.05)],
    ),
]

SCENARIOS = {"delivery_model": DELIVERY, "company_model": COMPANY}
