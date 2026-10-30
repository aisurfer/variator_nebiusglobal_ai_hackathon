"""VARIATOR plugin: IT services, software outsourcing.

Template ``delivery_model``: deliver a platform with an in-house team, a nearshore
time-and-materials vendor or an offshore fixed-price vendor. Axes: money, time,
management effort and delay against the target date.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from variator.domain import (
    MONEY,
    RESOURCES,
    TIME,
    Criterion,
    DecisionModel,
    DecisionOption,
    Derived,
    Distribution,
    DistributionKind,
    MetricSpec,
    Risk,
    RiskEffect,
    Variable,
)
from variator.mcp import Plugin, load_packs_from_dir
from variator_it_services.company import company_model
from variator_it_services.jira_synth import DEFAULT_COMPONENTS
from variator_it_services.scenarios import SCENARIOS
from variator_it_services.timeline import DONE, TIMELINES, done_at

ROOT = Path(__file__).parent
HORIZON_MONTHS = 24


def _pert(low: float, mode: float, high: float) -> Distribution:
    return Distribution(kind=DistributionKind.PERT, low=low, mode=mode, high=high)


def _var(name: str, description: str, unit: str, dist: Distribution, source: str = "assumption") -> Variable:
    return Variable(name=name, description=description, unit=unit, distribution=dist, source=source)


DELAY = Criterion(
    name="delay_months",
    description="Months late against the target date",
    unit="months",
    higher_is_better=False,
    weight=0.75,
    source="it_services",
)
CRITERIA = [
    MONEY.model_copy(
        update={
            "description": f"Value over {HORIZON_MONTHS} months minus delivery cost",
            "source": "it_services",
        }
    ),
    TIME.model_copy(update={"description": "Months to go live", "source": "it_services"}),
    RESOURCES.model_copy(
        update={"description": "Internal management effort", "source": "it_services", "weight": 0.5}
    ),
    DELAY,
]

# Time: extra people add capacity from t_add (request month + onboarding); the work is done
# when the capacity before and after t_add covers it. Without extra people (add_fte = 0) the
# duration is ramp-up + work / capacity, as before the timeline.
_DONE = DONE
DERIVED = [
    Derived(name="cap", unit="points/month", description="Team throughput",
            formula="team_size * productivity"),
    Derived(name="add_cap", unit="points/month", description="Throughput of the extra people",
            formula="add_fte * productivity"),
    Derived(name="onboard", unit="months", description="Onboarding time of the extra people",
            formula="where(add_outsourced > 0.5, vendor_onboard, hire_onboard)"),
    Derived(name="t_add", unit="months", description="Month when the extra people work at full speed",
            formula="maximum(add_month + onboard, ramp_up_months)"),
    Derived(
        name="work",
        unit="points",
        description="Scope plus rework; a scope change counts when it comes before the planned go-live",
        formula="(scope_points + ev_scope_points * (ev_scope_month < ramp_up_months"
        " + scope_points * (1 + rework_share) / cap)) * (1 + rework_share)",
    ),
    Derived(name="build_time", unit="months", description="Month when all work is done",
            formula=_DONE.format(w="work")),
    Derived(
        name="duration",
        unit="months",
        description="Build time, plus 14% of the remaining time after a key person leaves, plus RAID delay;"
        " not before the load tests end (blocker: environment ready + load tests)",
        formula="maximum(build_time + ev_kp * 0.14 * maximum(build_time - ev_kp_month, 0)"
        " + risk_delay_months, env_ready + load_test_months)",
    ),
    Derived(name="beta_time", unit="months", description="Month when half of the work is done",
            formula=done_at("0.5 * work")),
    Derived(name="env_ready", unit="months", description="Month when the load-test environment is ready",
            formula="env_start + env_lead"),
    Derived(
        name="add_cost",
        unit="USD",
        description="Cost of the extra people from the request month",
        formula="add_fte * rate * (1 + add_rate_premium * add_outsourced) * maximum(duration - add_month, 0)",
    ),
    Derived(
        name="value",
        unit="USD",
        description=f"Business value until month {HORIZON_MONTHS}",
        formula=f"value_per_month * maximum({HORIZON_MONTHS} - duration, 0)",
    ),
    Derived(
        name="tm_cost",
        unit="USD",
        description="Time-and-materials cost",
        formula="team_size * rate * duration + add_cost",
    ),
]

# Timeline inputs (all neutral by default: no extra people, no events)
TIME_VARIABLES = [
    ("add_fte", "Extra people added during the project", "FTE", Distribution.fixed(0)),
    ("add_month", "Month when you request the extra people", "month", Distribution.fixed(1)),
    ("add_outsourced", "Extra people come from a vendor (1) or are hired (0)", "0/1", Distribution.fixed(0)),
    ("hire_onboard", "Months to hire and onboard a new employee", "months", _pert(2, 3, 5)),
    ("vendor_onboard", "Months to onboard people from a vendor", "months", _pert(0.5, 1, 1.5)),
    ("add_rate_premium", "Extra rate for people from a vendor", "share", Distribution.fixed(0.2)),
    ("env_start", "Month when the load-test environment work starts", "month", Distribution.fixed(1)),
    ("env_lead", "Months to build the load-test environment", "months", _pert(1.5, 2, 3.5)),
    ("load_test_months", "Load tests after the environment is ready, before the go-live", "months",
     Distribution.fixed(0.5)),
    ("ev_scope_points", "Scope added by a change event", "story points", Distribution.fixed(0)),
    ("ev_scope_month", "Month of the scope change", "month", Distribution.fixed(5)),
    ("ev_kp", "A key person leaves (1 = yes)", "0/1", Distribution.fixed(0)),
    ("ev_kp_month", "Month when the key person leaves", "month", Distribution.fixed(4)),
    ("m_beta", "Milestone: beta (half of the work done)", "month", Distribution.fixed(6)),
    ("m_env", "Milestone: load-test environment ready", "month", Distribution.fixed(4)),
    ("g_money", "Goal: money at the horizon at least", "USD", Distribution.fixed(1_500_000)),
]
# Meaningful differences (criterion -> variable, description, unit, value): forks closer than this on a
# criterion are equal on it (score and P(best)); the user tunes them like any input.
TIES = {
    "money": ("md_money", "Meaningful difference of money: one month of the budget, half a month of value",
              "USD", 100_000),
    "time": ("md_time", "Meaningful difference of the go-live: two weeks", "months", 0.5),
    "resources": ("md_resources", "Meaningful difference of effort: half a month of one manager",
                  "FTE-months", 0.5),
    "delay_months": ("md_delay_months", "Meaningful difference of months late: two weeks", "months", 0.5),
}  # fmt: skip


def delivery_model() -> DecisionModel:
    variables = [
        _var(
            "scope_points",
            "Size of the backlog",
            "story points",
            _pert(450, 600, 900),
            "data: backlog export",
        ),
        _var("productivity", "Delivered points per FTE-month", "points/FTE-month", _pert(8, 10, 12)),
        _var("team_size", "Delivery team size", "FTE", Distribution.fixed(8)),
        _var(
            "rate",
            "Fully loaded cost per FTE-month",
            "USD",
            _pert(11_000, 12_500, 14_000),
            "data: vendor_rates.csv",
        ),
        _var("ramp_up_months", "Hiring or onboarding time", "months", _pert(2, 3, 5)),
        _var("rework_share", "Extra work for defects and changes", "share", _pert(0.05, 0.08, 0.12)),
        _var("mgmt_ratio", "Internal management FTE per delivery month", "FTE", _pert(0.5, 0.7, 1.0)),
        _var(
            "fixed_quote", "Fixed-price quote", "USD", _pert(280_000, 320_000, 420_000), "data: vendor quote"
        ),
        _var("change_share", "Change requests on top of the fixed price", "share", _pert(0.05, 0.15, 0.35)),
        _var("value_per_month", "Business value when live", "USD/month", Distribution.fixed(200_000)),
        _var("target_months", "Target go-live", "months", Distribution.fixed(9)),
        _var("budget_per_month", "Monthly budget limit", "USD", Distribution.fixed(100_000)),
        _var(
            "risk_delay_months",
            "Delay from RAID items (risk events add to it)",
            "months",
            Distribution.fixed(0),
        ),
        *(_var(n, d, u, dist, "timeline") for n, d, u, dist in TIME_VARIABLES),
        *(_var(n, d, u, Distribution.fixed(x), "policy: decision") for n, d, u, x in TIES.values()),
    ]
    metrics = {
        "time": "duration",
        "resources": "mgmt_ratio * duration",
        "delay_months": "maximum(duration - target_months, 0)",
    }
    options = [
        DecisionOption(
            name="in_house",
            description="Hire and run an internal team",
            formula="value - tm_cost",
            metrics=metrics,
        ),
        DecisionOption(
            name="nearshore_tm",
            description="Nearshore vendor, time and materials",
            formula="value - tm_cost",
            metrics=metrics,
            overrides={
                "team_size": Distribution.fixed(10),
                "productivity": _pert(8, 9.5, 11),
                "rate": _pert(9_000, 10_500, 12_000),
                "ramp_up_months": _pert(0.5, 1, 2),
                "rework_share": _pert(0.08, 0.12, 0.18),
                "mgmt_ratio": _pert(0.8, 1.0, 1.3),
            },
        ),
        DecisionOption(
            name="offshore_fixed",
            description="Offshore vendor, fixed price plus change requests",
            formula="value - fixed_quote * (1 + change_share) - add_cost",
            metrics=metrics,
            overrides={
                "team_size": Distribution.fixed(16),
                "productivity": _pert(4, 6.5, 9),
                "rate": _pert(3_500, 4_200, 5_200),
                "ramp_up_months": _pert(1, 1.5, 3),
                "rework_share": _pert(0.15, 0.28, 0.6),
                "mgmt_ratio": _pert(1.0, 1.3, 1.8),
            },
        ),
    ]
    return DecisionModel(
        question="Deliver the new platform in-house, with a nearshore T&M vendor, "
        "or offshore at a fixed price?",
        metric=MetricSpec(name=f"value over {HORIZON_MONTHS} months minus delivery cost", unit="USD"),
        variables=variables,
        options=options,
        # the plugin's CRITERIA stay plain: other models that use them have no md_* variables
        criteria=[c.model_copy(update={"tie_from": TIES[c.name][0]}) for c in CRITERIA],
        derived=DERIVED,
        risks=[
            Risk(
                name="key_person_loss",
                description="A senior engineer leaves",
                probability=0.2,
                variable="productivity",
                effect=RiskEffect.MULTIPLY,
                magnitude=_pert(-0.2, -0.12, -0.05),
                options=["in_house"],
            ),
            Risk(
                name="vendor_turnover",
                description="Vendor rotates the team",
                probability=0.25,
                variable="productivity",
                effect=RiskEffect.MULTIPLY,
                magnitude=_pert(-0.25, -0.15, -0.05),
                options=["nearshore_tm", "offshore_fixed"],
            ),
            Risk(
                name="scope_creep",
                description="The business adds features",
                probability=0.35,
                variable="scope_points",
                effect=RiskEffect.MULTIPLY,
                magnitude=_pert(0.05, 0.15, 0.3),
            ),
        ],
        assumptions=["Value starts at go-live and is flat", "No discounting inside 24 months"],
    )


tools = FastMCP("it_services")


def _components(repo: Path, components: dict[str, list[str]] | None) -> dict[str, list[str]]:
    if components:
        return components
    found = {k: v for k, v in DEFAULT_COMPONENTS.items() if (repo / v[0]).is_dir()}
    if found:
        return found
    found = {
        d.name: [d.relative_to(repo).as_posix() + "/"] for d in sorted(repo.glob("src/*/*")) if d.is_dir()
    }
    if found:
        return found
    # Django/Flask style: <package>/<app>/ with Python files (for example hc/api/)
    return {
        d.name: [d.relative_to(repo).as_posix() + "/"]
        for d in sorted(repo.glob("*/*"))
        if d.is_dir() and not d.name.startswith((".", "_")) and any(d.glob("*.py"))
    }


def _history(issues: str, worklogs: str, repo: str | None, components: dict[str, list[str]] | None):
    from variator_it_services.codebase import component_metrics
    from variator_it_services.estimation import actual_hours, estimate, fit_effort, read_csv

    rows = read_csv(issues)
    hours = actual_hours(read_csv(worklogs))
    metrics = component_metrics(Path(repo), _components(Path(repo), components)) if repo else None
    model = fit_effort(rows, hours, metrics)
    return rows, model, estimate(model, rows, metrics)


@tools.tool
def rice(reach: float, impact: float, confidence: float, effort: float) -> dict[str, float]:
    """RICE score = reach * impact * confidence / effort (point estimate)."""
    if effort <= 0:
        raise ValueError("effort must be > 0")
    return {"rice": reach * impact * confidence / effort}


@tools.tool
def jira_synthetic(
    out_dir: str,
    seed: int = 7,
    sprints: int = 12,
    open_stories: int = 40,
    repo: str | None = None,
    components: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Synthetic Jira-like export ending today. With `repo`, components and effort follow its code metrics."""
    from variator_it_services.codebase import component_metrics
    from variator_it_services.jira_synth import factors_from_metrics, generate, start_for_today

    factors = None
    if repo:
        factors = factors_from_metrics(component_metrics(Path(repo), _components(Path(repo), components)))
    export = generate(
        seed=seed,
        sprints=sprints,
        open_stories=open_stories,
        components=factors,
        start=start_for_today(sprints),
    )
    return {
        "files": export.write(out_dir),
        "issues": len(export.issues),
        "raid": len(export.raid),
        **({"true_factors": factors} if factors else {}),
    }


CODE_REFS = ["code_nloc", "code_cyclomatic_complexity", "code_churn", "code_fan_in", "code_fan_out",
             "code_instability"]
ARCH_REFS = [*CODE_REFS, "code_dependency_cycles", "code_hot_spot"]


@tools.tool
def code_metrics(repo: str = ".", components: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Per component: files, NLOC, complexity, churn, fan-in/out, instability. Local, no code sent."""
    from variator_it_services.codebase import component_metrics
    from variator_it_services.metrics_catalog import references

    return {
        "components": component_metrics(Path(repo), _components(Path(repo), components)),
        "references": references(CODE_REFS),
    }


@tools.tool
def architecture(repo: str = ".", prefixes: list[str] = ["src/"]) -> dict[str, Any]:  # noqa: B006
    """Package coupling, instability, import cycles and churn x complexity hot spots."""
    from variator_it_services.codebase import architecture_report
    from variator_it_services.metrics_catalog import references

    return {**architecture_report(Path(repo), prefixes=prefixes), "references": references(ARCH_REFS)}


@tools.tool
def metric_catalog(
    text: str = "", source: str | None = None, group: str | None = None, used_by: str | None = None
) -> dict[str, Any]:
    """Offline SDLC metric catalog (DevLake, DORA, Scorecard, CHAOSS, classic): search by text and filters."""
    from variator_it_services.metrics_catalog import load_catalog, search

    found = search(text, source=source, group=group, used_by=used_by)
    return {"count": len(found), "metrics": found, "sources": load_catalog()["sources"]}


@tools.tool
def metric_ref(metric_id: str) -> dict[str, Any]:
    """Full definition of one metric (calculation, benchmarks, SQL) + the citation to show to people."""
    from variator_it_services.metrics_catalog import cite, metric

    return {**metric(metric_id), "cite": cite(metric_id)}


@tools.tool
def estimate_backlog(
    issues: str,
    worklogs: str,
    repo: str | None = None,
    out: str | None = None,
    components: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Effort ranges (p10/p50/p90 hours) for open issues from history (+ code metrics if repo)."""
    import csv

    _, model, estimates = _history(issues, worklogs, repo, components)
    rows = [e.row() for e in estimates]
    if out:
        with Path(out).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["key"])
            writer.writeheader()
            writer.writerows(rows)
    widest = sorted(rows, key=lambda r: r["p90"] / max(r["p10"], 1e-9), reverse=True)[:5]
    return {
        "model": model.summary(),
        "open_issues": len(rows),
        "sum_p50_hours": round(sum(r["p50"] for r in rows)),
        "without_points": sum(1 for r in rows if r["points"] == ""),
        "without_component": sum(1 for r in rows if not r["component"]),
        "widest_ranges": [{k: r[k] for k in ("key", "p10", "p50", "p90")} for r in widest],
        **({"file": out} if out else {}),
    }


@tools.tool
def release_forecast(
    issues: str,
    worklogs: str,
    capacity_hours: float,
    repo: str | None = None,
    keys: list[str] | None = None,
) -> dict[str, Any]:
    """Monte Carlo: hours and sprints to finish the open issues (or `keys`)."""
    from variator_it_services.estimation import release_forecast as forecast

    _, _, estimates = _history(issues, worklogs, repo, None)
    if keys:
        estimates = [e for e in estimates if e.key in set(keys)]
    return forecast(estimates, capacity_hours)


@tools.tool
def rice_ranking(
    issues: str, worklogs: str, top_k: int = 5, repo: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """Probabilistic RICE: effort as a distribution; expected rank and P(top k)."""
    from variator_it_services.estimation import rice_ranking as ranking

    rows, _, estimates = _history(issues, worklogs, repo, None)
    return {"ranking": ranking(rows, estimates, top_k=top_k)[:limit]}


@tools.tool
def raid_summary(path: str) -> dict[str, Any]:
    """RAID log: counts, overdue, exposure (probability x impact days)."""
    from variator_it_services.estimation import read_csv
    from variator_it_services.raid import raid_summary as summary

    return summary(read_csv(path))


@tools.tool
def raid_patch(path: str) -> dict[str, Any]:
    """case_patch ops that add open RAID items as delay risks (send them to case_patch)."""
    from variator_it_services.estimation import read_csv
    from variator_it_services.raid import raid_patch as patch

    return {"ops": patch(read_csv(path))}


plugin = Plugin(
    name="it_services",
    description="Software outsourcing: delivery models, vendors, rates, delivery risk",
    criteria=CRITERIA,
    rule_packs=load_packs_from_dir(ROOT / "rules", "it_services"),
    templates={"delivery_model": delivery_model, "company_model": company_model},
    server=tools,
    data_dir=ROOT / "data",
    scenarios=SCENARIOS,
    timelines=TIMELINES,
)
