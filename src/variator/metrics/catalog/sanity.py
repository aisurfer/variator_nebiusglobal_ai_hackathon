"""Sanity check: the catalogue values at T against the generator's own numbers.

``summary.json`` (``stats``) and ``truth.json`` (``realised``, the hidden ``spec.truth`` targets) sit next to
the delivery. The generator computes its stats from its internal state at T; the catalogue computes from the
delivered records with the as-of rule. A difference is reported, never hidden.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any

from variator.metrics.catalog.asof import bdays
from variator.metrics.catalog.datasets import HistoryData
from variator.metrics.catalog.engine import Evaluation, run
from variator.metrics.catalog.params import merge_params
from variator.metrics.catalog.registry import BY_ID, default_params


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _row(
    check: str, ours: float | None, truth: float | None, basis: str, source: str, note: str = ""
) -> dict[str, Any]:
    diff = None if ours is None or truth is None else round(ours - truth, 4)
    pct = None if diff is None or not truth else round(diff / truth * 100, 2)
    return {
        "check": check,
        "ours": None if ours is None else round(ours, 4),
        "truth": truth,
        "diff": diff,
        "diffPct": pct,
        "oursBasis": basis,
        "truthSource": source,
        "note": note,
    }


def _year(data: HistoryData, metric_id: str) -> float | None:
    """The metric at T over the whole history (window = every day from the start)."""
    days = (data.anchor - data.start).days + 1
    params = merge_params(default_params(), {"metrics": {metric_id: {"window_days": days}}})
    return run(BY_ID[metric_id], data, params, data.anchor).value


def _v2_rows(data: HistoryData, evals: dict[str, Evaluation], stats: dict[str, Any]) -> list[dict[str, Any]]:
    """NPS, SLA, latency and late implementations against ``stats.nps`` and ``stats.sla`` (history v2)."""
    rows: list[dict[str, Any]] = []
    nps, sla = stats.get("nps") or {}, stats.get("sla") or {}
    if nps and data.has("nps_responses"):
        scores = [r.score for r in data.nps if r.date <= data.anchor]
        ours = (
            (sum(s >= 9 for s in scores) - sum(s <= 6 for s in scores)) / len(scores) * 100
            if scores
            else None
        )
        rows.append(
            _row("NPS, year", ours, nps.get("nps_year"), "all answers to T", "summary.stats.nps.nps_year")
        )
        ev = evals.get("cust.nps")
        by = {b.key: b.value for b in ev.kb.breakdown} if ev else {}
        for s in nps.get("surveys") or []:
            rows.append(
                _row(
                    f"NPS, survey {s.get('survey')}",
                    by.get(s.get("survey")),
                    s.get("nps"),
                    "cust.nps breakdown at T",
                    "summary.stats.nps.surveys[].nps",
                )
            )
        inputs = (nps.get("company_model") or {}).get("realised_inputs") or {}
        if inputs.get("implementations") and inputs.get("overdue_share") is not None:
            late = sum(
                1
                for _, v in data.table("contract").versions(data.clock.end_of_day(data.anchor))
                if v.kind == "implementation_completed" and int(v.f.get("go_live_late_days") or 0) > 0
            )
            rows.append(
                _row(
                    "Implementations late, year",
                    float(late),
                    round(inputs["implementations"] * inputs["overdue_share"], 3),
                    "completed implementations with go_live_late_days > 0",
                    "summary.stats.nps.company_model.realised_inputs (implementations x overdue_share)",
                    "ops.overdue_implementations at T (90 days):"
                    f" {evals['ops.overdue_implementations'].kb.value}"
                    if "ops.overdue_implementations" in evals
                    else "",
                )
            )
    if sla and data.has("monitoring_daily"):
        ours = _year(data, "ops.sla")
        rows.append(
            _row(
                "SLA, year, %",
                ours,
                None if sla.get("sla_series") is None else sla["sla_series"] * 100,
                "ops.sla over the whole history (traffic weights, latency penalty)",
                "summary.stats.sla.sla_series",
                f"30-day value at T {evals['ops.sla'].kb.value}" if "ops.sla" in evals else "",
            )
        )
        rows.append(
            _row(
                "Platform p95, year, ms",
                _year(data, "ops.latency"),
                sla.get("p95_platform_mean_ms"),
                "ops.latency over the whole history",
                "summary.stats.sla.p95_platform_mean_ms",
                f"7-day value at T {evals['ops.latency'].kb.value}" if "ops.latency" in evals else "",
            )
        )
    return rows


def sanity(data: HistoryData, evals: dict[str, Evaluation]) -> dict[str, Any]:
    stats = (_read(data.root / "summary.json").get("stats")) or {}
    truth = _read(data.root / "truth.json")
    targets = stats.get("truth_targets") or {}
    cut = data.clock.end_of_day(data.anchor)

    def val(mid: str) -> float | None:
        e = evals.get(mid)
        return None if e is None else e.kb.value

    incidents = [v for _, v in data.table("incident").known(cut)]
    restore = [
        float(v.f["restore_minutes"]) / 60 for v in incidents if v.f.get("restore_minutes") is not None
    ]
    deploys = [v for _, v in data.table("deploy").known(cut)]
    days = (data.anchor - data.start).days
    workdays = bdays(data.start, data.anchor)
    sprints = [s.load for s in data.sprints if s.load is not None and s.end <= cut]
    sprints_all = [s.load for s in data.sprints if s.load is not None]
    contracts = [v for _, v in data.table("contract").known(cut) if v.f.get("contract_type") != "vendor"]
    ended = sum(1 for v in contracts if v.f.get("status") == "terminated")
    arr_e = evals.get("rev.arr")
    rows = [
        _row(
            "ARR at T, USD",
            val("rev.arr"),
            stats.get("arr_at_t"),
            "rev.arr at T",
            "summary.stats.arr_at_t",
            f"plan target {targets.get('arr')}",
        ),
        _row(
            "Cash at T, USD",
            val("fin.cash"),
            stats.get("cash_at_t"),
            "fin.cash at T",
            "summary.stats.cash_at_t",
            f"plan target {targets.get('cash')}",
        ),
        _row(
            "DSO at T, days",
            val("fin.dso"),
            stats.get("dso_days_at_t"),
            "fin.dso at T (90-day billing)",
            "summary.stats.dso_days_at_t",
            f"habit target ar_days {targets.get('ar_days')}",
        ),
        _row(
            "Incidents per month (year)",
            len(incidents) / 12,
            stats.get("incidents_per_month"),
            "incident records known at T / 12",
            "summary.stats.incidents_per_month",
            f"30-day value at T {val('ops.incidents')}; habit base {targets.get('incidents_per_month')}",
        ),
        _row(
            "MTTR, hours (year mean)",
            mean(restore) if restore else None,
            stats.get("mttr_h_mean"),
            "mean restore_minutes / 60 of all resolved incidents",
            "summary.stats.mttr_h_mean",
            f"90-day value at T {val('ops.mttr')}; habit {targets.get('mttr_h')}",
        ),
        _row(
            "Deploys per week (year)",
            len(deploys) / (workdays / 5),
            stats.get("deploys_per_week"),
            f"deploy records / ({workdays} Mon–Fri days / 5)",
            "summary.stats.deploys_per_week",
            f"per calendar week {len(deploys) / (days / 7):.2f}; the generator divides by UK working days "
            f"(bank holidays out); 28-day value at T {val('ops.deploys')};"
            f" habit {targets.get('deploys_per_week')}",
        ),
        _row(
            "Headcount at T",
            val("ppl.headcount"),
            stats.get("headcount_at_t"),
            "ppl.headcount at T",
            "summary.stats.headcount_at_t",
        ),
        _row(
            "Clients in force at T",
            float(arr_e.kb.items_total) if arr_e else None,
            stats.get("clients_at_t"),
            "contracts in force in rev.arr",
            "summary.stats.clients_at_t",
            "one contract per client",
        ),
        _row(
            "Contracts ended by T",
            float(ended),
            stats.get("churned_by_t"),
            "client contracts terminated, known at T",
            "summary.stats.churned_by_t",
            f"rev.churn at T {val('rev.churn')}% of ARR; habit base churn {targets.get('churn')}",
        ),
        _row(
            "Open requisitions at T",
            val("ops.open_requisitions"),
            stats.get("open_requisitions_at_t"),
            "ops.open_requisitions at T",
            "summary.stats.open_requisitions_at_t",
        ),
        _row(
            "Team load (mean of sprints)",
            mean(sprints) if sprints else None,
            stats.get("team_load_mean"),
            "mean sprint load of the sprints finished by T",
            "summary.stats.team_load_mean",
            f"with the sprint that runs at T: {mean(sprints_all):.3f}; last finished {val('ops.team_load')}%"
            if sprints_all
            else "",
        ),
    ]
    rows += _v2_rows(data, evals, stats)
    return {
        "asOf": data.anchor.isoformat(),
        "history": str(data.root),
        "runId": data.run_id,
        "truthSeed": truth.get("seed"),
        "rows": rows,
        "note": "ours: the catalogue on the delivered records with the as-of rule;"
        " truth: the generator stats at T.",
    }
