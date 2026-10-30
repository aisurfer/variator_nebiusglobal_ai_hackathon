"""VARIATOR plugin: logistics (warehousing, 3PL, capacity).

Template ``warehouse``: the demo distributor case with four axes: money, time,
management effort and service level. ``extend`` adds the service-level axis to any
other model.
"""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP

from variator.domain import (
    MONEY,
    RESOURCES,
    TIME,
    Criterion,
    DecisionModel,
    Distribution,
    DistributionKind,
    Variable,
)
from variator.domain.examples import warehouse_decision
from variator.mcp import Plugin, load_packs_from_dir

ROOT = Path(__file__).parent
DATA_DIR = ROOT.parents[3] / "data" / "demo" / "warehouse"  # repository demo data


def _pert(low: float, mode: float, high: float) -> Distribution:
    return Distribution(kind=DistributionKind.PERT, low=low, mode=mode, high=high)


SERVICE_LEVEL = Criterion(
    name="service_level",
    description="Share of orders shipped on time",
    unit="share",
    higher_is_better=True,
    weight=0.5,
    source="logistics",
)
CRITERIA = [
    MONEY.model_copy(update={"description": "5-year profit contribution", "source": "logistics"}),
    TIME.model_copy(update={"description": "Months until the new capacity works", "source": "logistics"}),
    RESOURCES.model_copy(update={"description": "Management effort", "source": "logistics", "weight": 0.5}),
    SERVICE_LEVEL,
]


def warehouse() -> DecisionModel:
    base = warehouse_decision()
    data = base.model_dump(mode="python")
    data["variables"] += [
        Variable(
            name="setup_months",
            description="Months to set up the option",
            unit="months",
            distribution=_pert(1, 2, 4),
            source="assumption: project plans",
        ).model_dump(),
        Variable(
            name="mgmt_fte",
            description="Management FTE during setup",
            unit="FTE",
            distribution=_pert(0.3, 0.5, 1.0),
            source="assumption",
        ).model_dump(),
        Variable(
            name="service_level",
            description="On-time shipping share",
            unit="share",
            distribution=_pert(0.93, 0.95, 0.97),
            source="data: monthly_orders.csv",
        ).model_dump(),
    ]
    overrides = {
        "second_warehouse": {
            "setup_months": _pert(9, 12, 18),
            "mgmt_fte": _pert(1.5, 2.0, 3.0),
            "service_level": _pert(0.95, 0.97, 0.985),
        },
        "expand_current": {
            "setup_months": _pert(4, 6, 9),
            "mgmt_fte": _pert(1.0, 1.5, 2.0),
            "service_level": _pert(0.94, 0.96, 0.975),
        },
        "outsource_3pl": {
            "setup_months": _pert(1, 2, 3),
            "mgmt_fte": _pert(0.3, 0.5, 0.8),
            "service_level": _pert(0.90, 0.94, 0.96),
        },
    }
    for option in data["options"]:
        option["overrides"].update({k: v.model_dump() for k, v in overrides[option["name"]].items()})
        option["metrics"] = {
            "time": "setup_months",
            "resources": "setup_months * mgmt_fte",
            "service_level": "service_level",
        }
    data["criteria"] = [c.model_dump() for c in CRITERIA]
    return DecisionModel.model_validate(data)


def extend(model: DecisionModel) -> DecisionModel:
    """Add the service-level axis to another model (same distribution for all options)."""
    data = model.model_dump(mode="python")
    if not data["criteria"]:
        data["criteria"] = [c.model_dump() for c in model.effective_criteria()]
    if not any(v["name"] == "service_level" for v in data["variables"]):
        data["variables"].append(
            Variable(
                name="service_level",
                description="On-time delivery share",
                unit="share",
                distribution=_pert(0.92, 0.95, 0.97),
                source="assumption: logistics plugin",
            ).model_dump()
        )
    data["criteria"].append(SERVICE_LEVEL.model_dump())
    for option in data["options"]:
        option.setdefault("metrics", {})["service_level"] = "service_level"
    return DecisionModel.model_validate(data)


tools = FastMCP("logistics")


@tools.tool
def capacity_gap(
    base_orders: float, growth: float, capacity: float, years: int = 5
) -> dict[str, list[float]]:
    """Yearly orders above capacity for a growth rate (local arithmetic)."""
    volumes = [base_orders * (1 + growth) ** t for t in range(1, years + 1)]
    return {"volume": [round(v) for v in volumes], "overflow": [round(max(v - capacity, 0)) for v in volumes]}


plugin = Plugin(
    name="logistics",
    description="Warehousing, 3PL and capacity decisions",
    criteria=CRITERIA,
    rule_packs=load_packs_from_dir(ROOT / "rules", "logistics"),
    templates={"warehouse": warehouse},
    extend=extend,
    server=tools,
    data_dir=DATA_DIR,
)
