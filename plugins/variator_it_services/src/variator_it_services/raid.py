"""RAID log (risks, assumptions, issues, dependencies): summary and link to the simulation.

``raid_patch`` turns open items into ``case_patch`` operations: each item is a risk
event that adds months to ``risk_delay_months`` (part of ``duration`` in the
delivery template). Probability: the item's value for risks; defaults for the
other types (an open issue already happens; an assumption or a dependency can fail).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

DEFAULT_PROBABILITY = {"risk": None, "issue": 1.0, "assumption": 0.2, "dependency": 0.3}
WORK_DAYS_PER_MONTH = 21.0
DELAY_VARIABLE = "risk_delay_months"
TOP = 5


def _probability(item: Mapping[str, str]) -> float:
    default = DEFAULT_PROBABILITY.get(item["type"])
    return float(item["probability"]) if default is None else default


def raid_summary(items: Sequence[Mapping[str, str]], *, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    open_items = [i for i in items if i.get("status") == "open"]
    exposure = sorted(
        (
            {
                "id": i["id"],
                "type": i["type"],
                "title": i["title"],
                "exposure_days": round(_probability(i) * float(i["impact_days"]), 2),
            }
            for i in open_items
        ),
        key=lambda r: r["exposure_days"],
        reverse=True,
    )
    overdue = [i["id"] for i in open_items if i.get("due") and date.fromisoformat(i["due"]) < today]
    by_component = Counter(i.get("component", "") for i in open_items)
    return {
        "counts": {
            f"{t}/{s}": n for (t, s), n in sorted(Counter((i["type"], i["status"]) for i in items).items())
        },
        "open": len(open_items),
        "overdue": overdue,
        "expected_delay_days": round(sum(r["exposure_days"] for r in exposure), 1),
        "top_exposure": exposure[:TOP],
        "open_by_component": dict(by_component.most_common(TOP)),
    }


def raid_patch(items: Sequence[Mapping[str, str]], *, variable: str = DELAY_VARIABLE) -> list[dict[str, Any]]:
    """``case_patch`` ops: one ``set_risk`` per open item (impact as PERT around impact_days)."""
    ops = []
    for item in items:
        if item.get("status") != "open":
            continue
        months = float(item["impact_days"]) / WORK_DAYS_PER_MONTH
        ops.append(
            {
                "op": "set_risk",
                "value": {
                    "name": item["id"].lower().replace("-", "_"),
                    "description": f"{item['type']}: {item['title']}",
                    "probability": _probability(item),
                    "variable": variable,
                    "effect": "add",
                    "magnitude": {
                        "kind": "pert",
                        "low": round(0.5 * months, 4),
                        "mode": round(months, 4),
                        "high": round(1.5 * months, 4),
                    },
                },
            }
        )
    return ops
