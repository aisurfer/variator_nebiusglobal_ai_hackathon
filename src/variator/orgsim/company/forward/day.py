"""Day data of the case page (S11): daily series and the event lane of each day example.

Schema: ``PreviewFuture.dayMetrics`` and ``DayExample`` of ``workspace/src/variator/engine/types.ts``,
with ``preview: false`` and ``source: "actors"``. Each example adds ``tenant`` (the KB branch that
holds its records), ``scenario`` (the stream index of the run) and ``percentile`` (its rank of cash at
the end of the window among the fork's three leaves or in its small ensemble; not a rank in the fan).
The pipeline adds ``percentileAtWindowEnd`` and ``windowEndFan``: its place among WP18's 1,000 scenarios
of the fork (``fan.Cube``). ``DayEvent.entityId`` is an entity of that branch
(``sim.INC-0047``) or of the shared directory (``person.lw-e24``, ``org.CL-001``).
"""

from __future__ import annotations

from typing import Any

DAY_METRICS = [
    ("cash", "Cash", "USD", True, "Cash balance at the end of the day (bank entries of the branch)."),
    (
        "load",
        "Team load",
        "ratio",
        False,
        "Planned and unplanned engineering work / capacity on the day (1 = full).",
    ),
    ("incidents", "Incidents", "count", False, "Incidents that started on the day."),
    ("arr", "ARR", "USD", True, "Annual value of the contracts in force on the day."),
    ("headcount", "Headcount", "people", True, "Employees on the payroll on the day."),
    ("availability", "Availability", "share", True, "Mean availability of the seven production services."),
    ("p95_ms", "p95 latency", "ms", False, "Mean p95 latency of the production services."),
]
KIND_LABEL = {"pessimistic": "Pessimistic", "median": "Median", "optimistic": "Optimistic"}
RANK_OF_THREE = {"pessimistic": "Lowest", "median": "Middle", "optimistic": "Highest"}


def metrics() -> list[dict[str, Any]]:
    return [
        {"id": i, "label": lb, "unit": u, "higher": h, "description": d} for i, lb, u, h, d in DAY_METRICS
    ]


def example(
    run: Any,
    kind: str,
    tenant: str,
    percentile: int,
    ensemble_n: int,
    leaf: str | None = None,
    tree_kind: str | None = None,
) -> dict[str, Any]:
    """One day example. With a week-tree leaf, ``kind`` is its rank of cash at the end of the window
    among the fork's three leaves; ``treeKind`` is the leaf's rank after 52 weeks in the tree."""
    s = run.summary()
    fk = run.fork
    head = (
        f"{RANK_OF_THREE[kind]} cash of the fork's three week-tree examples (leaf {leaf}) at the end of "
        "the window"
        if leaf
        else f"{KIND_LABEL[kind]} of {ensemble_n} day runs (scenario {run.k}, p{percentile} of their cash "
        "at the end of the window)"
    )
    summary = (
        f"{head}: cash on "
        f"{run.series[-1]['date']} ${s['cash_end'] / 1e6:.2f}M, {s['incidents']} incidents, "
        f"{s['client_notices']} client notices, availability {100 * s['availability']:.2f}%"
        + (f", NPS {s['nps_last']:.0f}" if s["nps_last"] is not None else "")
        + "."
    )
    return {
        "preview": False,
        "source": "actors",
        "option": fk.id,
        "kind": kind,
        "label": KIND_LABEL[kind],
        "summary": summary,
        "tenant": tenant,
        "scenario": run.k,
        "percentile": percentile,
        "leaf": leaf,
        "treeKind": tree_kind,
        "yearEnd": {
            "cash": s["cash_end"],
            "runway": s["runway_months"],
            "churn": s["churn_annual"],
            "sla": s["availability"],
            "nps": s["nps_last"],
            "incidents": s["incidents"],
        },
        "horizonEnd": run.series[-1]["date"],
        "days": run.series,
        "events": sorted(run.lane, key=lambda e: (e["date"], e["type"])),
    }


def day_file(
    case_id: str,
    anchor: str,
    months: int,
    seed: int,
    examples: dict[str, list[dict[str, Any]]],
    note: str,
    params_sources: dict[str, str],
    extra_metrics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "preview": False,
        "caseId": case_id,
        "source": "actors",
        "note": note,
        "seed": seed,
        "anchor": anchor,
        "horizonMonths": months,
        "dayMetrics": metrics() + list(extra_metrics or []),
        "dayExamples": examples,
        "parameterSources": params_sources,
    }
