"""Versioned controlled vocabulary for Redwood company-analysis factors."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final, Literal

FACTORS_VERSION: Final[str] = "factors/1"
FactorRole = Literal["outcome", "driver", "constraint", "risk", "lever"]


@dataclass(frozen=True)
class FactorCatalogEntry:
    """A stable factor definition used by deterministic extraction rules."""

    factor_id: str
    group: str
    definition: str
    supported_units: tuple[str, ...]
    permitted_roles: tuple[FactorRole, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_STANDARD_ROLES: Final[tuple[FactorRole, ...]] = ("outcome", "driver", "constraint", "risk")
_GROUP_FACTORS: Final[dict[str, tuple[str, ...]]] = {
    "financial": (
        "revenue",
        "growth",
        "gross_margin",
        "operating_expense",
        "burn_rate",
        "cash_runway",
        "accounts_receivable",
    ),
    "commercial_customer": (
        "pipeline",
        "bookings",
        "conversion",
        "retention",
        "churn",
        "expansion",
        "customer_concentration",
        "nps",
        "sla_risk",
    ),
    "product_technology": (
        "adoption",
        "roadmap_delivery",
        "quality",
        "latency",
        "availability",
        "accuracy",
        "technical_debt",
        "security",
    ),
    "operations_infrastructure": (
        "delivery_cycle_time",
        "deploy_frequency",
        "lead_time",
        "incident_rate",
        "mttr",
        "capacity",
        "utilization",
        "cloud_gpu_cost",
    ),
    "people": (
        "headcount",
        "hiring",
        "attrition",
        "skills",
        "critical_role_dependency",
        "team_capacity",
    ),
    "market_competition": (
        "demand",
        "pricing",
        "competitor_event",
        "supplier_gpu_availability",
        "macro_signal",
    ),
    "risk_governance": (
        "regulatory",
        "compliance",
        "legal",
        "security",
        "reputation",
        "foreign_exchange",
        "data_quality",
    ),
    "decision_lever": (
        "pricing",
        "budget_allocation",
        "hiring",
        "infrastructure_procurement",
        "capacity_allocation",
        "roadmap_priority",
    ),
}
_PREFIXES: Final[dict[str, str]] = {
    "financial": "financial",
    "commercial_customer": "commercial",
    "product_technology": "product",
    "operations_infrastructure": "operations",
    "people": "people",
    "market_competition": "market",
    "risk_governance": "risk",
    "decision_lever": "lever",
}
_UNIT_OVERRIDES: Final[dict[str, tuple[str, ...]]] = {
    "financial.revenue": ("currency",),
    "financial.growth": ("percent",),
    "financial.gross_margin": ("percent",),
    "financial.burn_rate": ("currency_per_month",),
    "financial.cash_runway": ("months",),
    "commercial.nps": ("score",),
    "commercial.sla_risk": ("score", "count"),
    "product.latency": ("milliseconds",),
    "product.availability": ("percent",),
    "operations.mttr": ("minutes", "hours"),
    "operations.utilization": ("percent",),
    "operations.cloud_gpu_cost": ("currency", "currency_per_gpu_hour"),
    "people.headcount": ("count",),
    "people.team_capacity": ("person_days", "hours"),
    "market.pricing": ("currency", "currency_per_unit"),
    "risk.foreign_exchange": ("percent", "currency"),
    "lever.pricing": ("currency", "percent"),
    "lever.budget_allocation": ("currency", "percent"),
    "lever.hiring": ("count",),
    "lever.infrastructure_procurement": ("currency", "gpu_hours"),
    "lever.capacity_allocation": ("percent", "gpu_hours"),
    "lever.roadmap_priority": ("rank",),
}


def _entry(group: str, name: str) -> FactorCatalogEntry:
    factor_id = f"{_PREFIXES[group]}.{name}"
    roles: tuple[FactorRole, ...] = ("lever",) if group == "decision_lever" else _STANDARD_ROLES
    return FactorCatalogEntry(
        factor_id=factor_id,
        group=group,
        definition=f"Controlled vocabulary entry for {name.replace('_', ' ')}.",
        supported_units=_UNIT_OVERRIDES.get(factor_id, ("count", "percent", "score")),
        permitted_roles=roles,
    )


FACTOR_CATALOG: Final[tuple[FactorCatalogEntry, ...]] = tuple(
    _entry(group, name) for group, names in _GROUP_FACTORS.items() for name in names
)
FACTOR_BY_ID: Final[dict[str, FactorCatalogEntry]] = {entry.factor_id: entry for entry in FACTOR_CATALOG}
FACTOR_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    group: tuple(f"{_PREFIXES[group]}.{name}" for name in names) for group, names in _GROUP_FACTORS.items()
}


def all_factor_ids() -> tuple[str, ...]:
    """Return stable catalog IDs in deterministic lexical order."""
    return tuple(sorted(FACTOR_BY_ID))


def factor_group(factor_id: str) -> str | None:
    """Return a catalog group, or ``None`` when the factor is unknown."""
    entry = FACTOR_BY_ID.get(factor_id)
    return entry.group if entry else None


def factor_catalog_records() -> tuple[dict[str, object], ...]:
    """Return the versioned factor catalog in stable delivery order."""
    return tuple(entry.as_dict() for entry in FACTOR_CATALOG)
