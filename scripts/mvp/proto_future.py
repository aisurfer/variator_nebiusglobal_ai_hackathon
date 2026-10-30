"""Write the preview data of the future sections (WP13a, WP14a): workspace/config/cases/<case>.preview.json.

Run with VARIATOR's venv:
    .venv/bin/python scripts/mvp/proto_future.py                       # every case
    .venv/bin/python scripts/mvp/proto_future.py --case company_best_year

A stand-in until the month Monte Carlo on actors exists; every container says ``"preview": true``.
Deterministic: the results at the horizon come from the L0 formula model (``simulate_tensor``, n = 4096, seed 42);
all other randomness comes from ``synthetic.company.derived_rng`` streams whose labels never name the fork, so
the forks share their random numbers (common random numbers). Case A (delivery_best_way) is in proto_delivery.py.

Case B (company_best_year):
- Monthly fans (months 0..12) of cash, churn, SLA, NPS and runway per fork: per L0 scenario a path from the start
  to the L0 year-end value (Brownian bridge for cash and NPS; year-to-date shares for churn and SLA). Month 12
  equals the L0 year-end distribution exactly.
- Risk calendar per fork (``orgsim.risk``), P(failed by month m) and the median first-failure month: cash below
  zero, runway below 6 months, a month below the SLA target, churn to date above 20%, cost to date above the
  budget to date, and each milestone of the fork late (it fails at its marker month when the L0 condition fails).
- Week tree (``orgsim.tree.grow_tree``, k = 3, at most 60 nodes per level, 52 weeks) for every fork: weekly cash,
  load and incidents, calibrated to the fork's L0 results.
- Day examples for every fork: the L0 scenarios at the 10%, 50% and 90% points of cash at year end; daily cash,
  load and incidents for the first 60 working days after T, and events with knowledge-base entity ids of the
  generated company in the exact KB form (``org.CL-001``, ``person.lw-e06``).
"""

# ruff: noqa: E501 - event and summary texts, one per line

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proto_common as pc  # noqa: E402
from case_company import G_CHURN, ITEMS  # noqa: E402
from export_case import CASE_ID, CASES, INDEX_ORDER  # noqa: E402

from variator.orgsim.company.spec import default_spec  # noqa: E402
from variator.orgsim.company.ukcal import company_calendar  # noqa: E402
from variator.orgsim.tree import grow_tree  # noqa: E402

SEED, N, ANCHOR, DAYS = pc.SEED, pc.N, pc.ANCHOR, pc.DAYS
MONTHS = 12
WEEKS = pc.WEEKS
PROBES = [
    "cash0", "cash_end", "burn", "revenue", "cost", "budget", "sla_target", "load", "incidents", "mttr_eff", "attrition",
    "ar_days", "debt_ready", "hire_ready", "gpu_ready", "price_live", "hire_count", "lever_hire", "gpu_shortage", "vendor_price", "fx_shock",
    "regulation_cost", "competitor_cut", "key_person_exit", "key_account_lost", "reputation_hit",
    "engineer_cost", "runway_cap", "runway_red", "burnout_load", "g_churn",
]  # fmt: skip
METRICS = [
    ("cash", "Cash", "USD", True, "Cash balance at the end of each month."),
    ("churn", "Churn, year to date", "share", False, "Share of ARR lost since the start of the year."),
    ("sla", "Availability, year to date", "share", True, "Availability since the start of the year."),
    ("nps", "NPS", "points", True, "Net promoter score at the end of each month."),
    ("runway", "Runway", "months", True, "Months of cash left at the burn rate of the year (capped at 60 months)."),
]
# id, label, consequence, kind, ref
CHECKS = [
    ("cash_negative", "Cash below zero", "You need a credit line or emergency cuts", "threshold", None),
    (
        "runway_low",
        "Runway below 6 months",
        "Cash runs out within 6 months: the plan is not acceptable then",
        "check",
        "runway_low",
    ),
    (
        "sla_breach",
        "SLA breach",
        "A month below the SLA: penalties and angry clients are possible",
        "flag",
        "sla_breach",
    ),
    (
        "churn_high",
        f"Churn above {G_CHURN:.0%}",
        "You lose more clients and their recurring revenue",
        "flag",
        "churn_high",
    ),
    (
        "over_budget",
        "Cost above budget",
        "Spending runs ahead of the budget: cuts or a new budget are needed",
        "flag",
        "over_budget",
    ),
]
MILESTONES = {
    "price_live": ("New prices late", "The higher prices start later: less revenue this year"),
    "debt_done": ("Debt work late", "Incidents and latency stay high for longer"),
    "hires_ready": ("Hires late", "The team stays overloaded for longer"),
    "gpu_ready": ("GPU capacity late", "A GPU shortage can still hit before the second cloud is ready"),
}
WEEK_METRICS = [
    ("cash", "Cash", "USD", True, "Cash balance at the end of the week."),
    ("load", "Team load", "ratio", False, "Planned work / capacity in the week (1 = full)."),
    ("incidents", "Incidents", "count", False, "Incidents in the week."),
]
DAY_METRICS = [
    ("cash", "Cash", "USD", True, "Cash balance at the end of the day."),
    ("load", "Team load", "ratio", False, "Planned work / capacity on the day (1 = full)."),
    ("incidents", "Incidents", "count", False, "Incidents on the day."),
]
SERVICES = ["API gateway", "inference cluster", "billing service", "client portal", "data pipeline"]
# fork -> (start text, plan item (its owner starts it), derived "ready" month, ready text)
FORK_EVENTS = {
    "pay_tech_debt": (
        "Debt work starts: part of the engineering capacity goes to technical debt",
        "debt_start",
        "debt_ready",
        "The debt work shows an effect: fewer incidents and lower latency",
    ),
    "hire_5": (
        "Hiring starts: 5 engineer requisitions open",
        "hire_start",
        "hire_ready",
        "The new engineers join and start work; their cost starts",
    ),
    "reserve_gpu": (
        "GPU reservation ordered on a second cloud",
        "gpu_start",
        "gpu_ready",
        "GPU capacity on the second cloud is ready",
    ),
    "raise_prices_8": (
        "New prices announced to clients (+8%)",
        "price_start",
        "price_live",
        "The new prices apply",
    ),
}


def _bridge(n: int, purpose: str) -> np.ndarray:
    """Standard Brownian bridge on months 0..12 (n, 13): 0 at both ends."""
    w = np.concatenate(
        [np.zeros((n, 1)), np.cumsum(pc.rng("fan", purpose).standard_normal((n, MONTHS)), axis=1)], axis=1
    )
    return w - np.arange(MONTHS + 1) / MONTHS * w[:, -1:]


def l0_run(n: int = N) -> dict[str, Any]:
    """The L0 run of case B with the probes of the preview and the timeline conditions."""
    return pc.l0_run(CASE_ID, {p: p for p in PROBES}, n)


def monthly_paths(l0: dict[str, Any]) -> dict[str, np.ndarray]:
    """Per metric: (n, options, 13) monthly values; month 12 = the L0 year-end value. Also the monthly SLA and
    the cost to date (months 1..12, for the calendar)."""
    run = l0["run"]
    n = run.n_scenarios
    crit = {c.name: run.values[:, :, k] for k, c in enumerate(run.criteria)}
    pr = run.probes
    t = np.arange(MONTHS + 1) / MONTHS
    cash0 = pr["cash0"][:, :, None]
    sigma = (0.03 * pr["revenue"] / MONTHS)[:, :, None]
    cash = cash0 + (crit["cash"][:, :, None] - cash0) * t + sigma * _bridge(n, "cash")[:, None, :]
    gam = pc.rng("fan", "churn").gamma(4.0, size=(n, MONTHS))
    share = np.concatenate(
        [np.zeros((n, 1)), np.cumsum(gam, axis=1) / gam.sum(axis=1, keepdims=True)], axis=1
    )
    churn = crit["churn"][:, :, None] * share[:, None, :]
    e = pc.rng("fan", "sla").standard_normal((n, MONTHS))
    e = e - e.mean(axis=1, keepdims=True)
    sla_y = crit["sla"][:, :, None]
    monthly = np.minimum(sla_y + 0.25 * (1 - sla_y) * e[:, None, :], 1.0)
    ytd = np.cumsum(monthly, axis=2) / np.arange(1, MONTHS + 1)
    sla = np.concatenate([ytd[:, :, :1], ytd], axis=2)
    nps0 = float(np.median(crit["nps"][:, 0]))
    nps = nps0 + (crit["nps"][:, :, None] - nps0) * t + 1.5 * _bridge(n, "nps")[:, None, :]
    burn = pr["burn"][:, :, None]
    runway = np.minimum(np.maximum(cash, 0) / np.maximum(burn, 1), pr["runway_cap"][:, :, None])
    # cost: the new hires are paid from the month they join (the model's engineer_cost a year each, hire_cost),
    # the rest evenly with noise; the months add up to the year's cost
    months = np.arange(1, MONTHS + 1)
    hired = np.clip(months[None, None, :] - pr["hire_ready"][:, :, None], 0, 1)
    hire_month = (pr["engineer_cost"] * pr["hire_count"] * pr["lever_hire"])[:, :, None] / MONTHS * hired
    xi = pc.rng("fan", "cost").standard_normal((n, MONTHS))
    xi = xi - xi.mean(axis=1, keepdims=True)
    rest = pr["cost"] - hire_month.sum(axis=2)
    cost_month = rest[:, :, None] / MONTHS * (1 + 0.04 * xi[:, None, :]) + hire_month
    return {
        "cash": cash, "churn": churn, "sla": sla, "nps": nps, "runway": runway,
        "sla_monthly": monthly, "cost_to_date": np.cumsum(cost_month, axis=2),
    }  # fmt: skip


def _round(x: float, metric: str) -> float:
    return round(float(x)) if metric == "cash" else round(float(x), 6 if metric in ("churn", "sla") else 3)


def fans(paths: dict[str, np.ndarray], j: int) -> dict[str, list[dict[str, float]]]:
    return {
        m: pc.fan(paths[m][:, j, :], MONTHS, None if m == "cash" else 6 if m in ("churn", "sla") else 3)
        for m, *_ in METRICS
    }


def risk_calendar(l0: dict[str, Any], paths: dict[str, np.ndarray]) -> list[list[dict[str, Any]]]:
    """Per option: the rows of the calendar (months 1..12)."""
    pr = l0["run"].probes
    months = np.arange(1, MONTHS + 1)
    masks = {
        "cash_negative": paths["cash"][:, :, 1:] < 0,
        "runway_low": paths["runway"][:, :, 1:] < pr["runway_red"][:, :, None],
        "sla_breach": paths["sla_monthly"] < pr["sla_target"][:, :, None],
        "churn_high": paths["churn"][:, :, 1:] > pr["g_churn"][:, :, None],  # the goal and the red flag
        "over_budget": paths["cost_to_date"] > pr["budget"][:, :, None] * months / MONTHS,
    }
    rows = [
        ({"id": cid, "label": label, "consequence": cons, "kind": kind, "ref": ref}, masks[cid], None)
        for cid, label, cons, kind, ref in CHECKS
    ]
    rows += pc.timeline_rows(l0, ("milestone",), MILESTONES, MONTHS)
    return pc.calendar(rows, l0["options"])


def _week_summary(
    week: int, cash: float, delta: float, load: float, inc: int, sd: float, burnout: float = 1.05
) -> str:
    if week == 0:
        return f"Today: cash {pc.money(cash)}, load {load:.2f}."
    notes = []
    if cash < 0:
        notes.append("cash below zero")
    elif delta < -2 * sd:
        notes.append("a costly week")
    if load > burnout:  # the burnout limit of the model (burnout_load)
        notes.append("team over capacity")
    if inc >= 3:
        notes.append("a rough week for reliability")
    text = f"Week {week}: cash {pc.money(cash)} ({pc.signed_money(delta)}), load {load:.2f}, {inc} incident{'' if inc == 1 else 's'}"
    return text + (f"; {', '.join(notes)}." if notes else ".")


def week_tree(l0: dict[str, Any], j: int) -> dict[str, Any]:
    run, option = l0["run"], l0["options"][j]
    cash_end = run.values[:, j, run.criterion_index("cash")]
    cash0 = float(run.probes["cash0"][:, j].mean())
    mu, sd = (float(cash_end.mean()) - cash0) / WEEKS, float(cash_end.std()) / np.sqrt(WEEKS)
    lm = float(run.probes["load"][:, j].mean())
    inc_week = float(run.probes["incidents"][:, j].mean()) * MONTHS / WEEKS

    def step(state: dict[str, float], rng: np.random.Generator, node: Any) -> dict[str, float]:
        z = rng.standard_normal(2)
        load = lm + 0.7 * (state["load"] - lm) + 0.03 * z[1]
        return {
            "cash": state["cash"] + mu + sd * z[0],
            "load": load,
            "incidents": int(rng.poisson(max(inc_week * load / lm, 0))),
        }

    root = {"cash": cash0, "load": lm, "incidents": 0}
    tree = grow_tree(
        root, step, levels=WEEKS, k=pc.K, max_nodes=pc.MAX_NODES, seed=SEED, root_key="week", with_node=True
    )

    burnout = float(np.median(run.probes["burnout_load"][:, j]))

    def summary(w: int, s: dict[str, Any], prev: dict[str, Any]) -> str:
        return _week_summary(w, s["cash"], s["cash"] - prev["cash"], s["load"], s["incidents"], sd, burnout)

    label = l0["labels"].get(option, option)
    return pc.export_tree(
        tree,
        option,
        label,
        {"cash": 0, "load": 3, "incidents": 0},
        summary,
        {"cash": 0, "load": 3, "incidents": 3},
    )


def day_example(
    l0: dict[str, Any], paths: dict[str, np.ndarray], j: int, kind: str, q: float, cal: Any
) -> dict[str, Any]:
    run, option, spec = l0["run"], l0["options"][j], default_spec()
    pr = {k: v[:, j] for k, v in run.probes.items()}
    cash_end = run.values[:, j, run.criterion_index("cash")]
    s = int(np.argmin(np.abs(cash_end - np.quantile(cash_end, q))))
    days = pc.bdays(cal, DAYS)
    staff_t = pc.staff_at_t(spec)  # people who left before T are not in the future
    people = {p.id: p for p in staff_t}
    engineers = [p for p in staff_t if p.department == "Engineering"]
    rng_cash, rng_load = pc.rng("day", kind, "cash"), pc.rng("day", kind, "load")
    rng_inc, rng_people = pc.rng("day", kind, "incidents"), pc.rng("people", f"s{s}")
    load_mean = float(pr["load"][s])
    ev = pc.Events(days)
    # the fork's own decisions: its owner starts it, then it is ready
    if option in FORK_EVENTS:
        start, item, ready, ready_text = FORK_EVENTS[option]
        ev.add(0, "decision", start, f"person.{ITEMS[item]['owner'].lower()}")
        ev.add(
            pc.day_index(days, ANCHOR + timedelta(days=round(float(pr[ready][s]) * 365 / 12))),
            "decision",
            ready_text,
        )
    # client payments of the key clients (invoice on the first working day of a month, paid after ar_days)
    ar = float(pr["ar_days"][s])
    for k, cl in enumerate(spec.key_clients):
        step = 1 if cl.billing == "monthly" else 3
        for m in range(-3, 4, step):
            invoiced = cal.add_business_days(
                date(ANCHOR.year + (ANCHOR.month + m) // 12, (ANCHOR.month + m) % 12 + 1, 1), 0
            )
            paid = invoiced + timedelta(days=round(ar + 5 * (k % 3 - 1)))
            if days[0] <= paid <= days[-1]:
                ev.add(
                    pc.day_index(days, paid),
                    "payment",
                    f"{cl.name} paid an invoice: {pc.money(cl.acv * step / 12)}",
                    f"org.{cl.id}",
                )
    # shocks of this L0 scenario: one working day of the year per shock and scenario (the same in every fork)
    shocks = [
        (
            "gpu_shortage",
            "shock",
            lambda x: f"GPU shortage at Nimbus Cloud: {x:.0%} less capacity",
            "org.VN-03",
        ),
        ("vendor_price", "vendor", lambda x: f"Nimbus Cloud raises prices by {x:.0%}", "org.VN-03"),
        ("fx_shock", "shock", lambda x: f"A currency move raises offshore cost by {x:.0%}", None),
        (
            "regulation_cost",
            "shock",
            lambda x: f"New regulation: compliance work of about {pc.money(x)} a year",
            None,
        ),
        ("competitor_cut", "shock", lambda x: f"A competitor cuts prices by {x:.0%}", None),
        ("key_person_exit", "people", lambda x: "", "key_person"),
        ("key_account_lost", "client", lambda x: "", "key_account"),
        ("reputation_hit", "shock", lambda x: "A public incident report hurts the reputation", None),
    ]
    for name, etype, text, who in shocks:
        x = float(pr[name][s])
        if x <= 0:
            continue
        r = pc.rng("shock", name, f"s{s}")
        i = int(r.integers(0, 250))
        if i >= DAYS:
            continue
        if who == "key_person":
            keys = [p for p in staff_t if p.key]
            p = keys[int(r.integers(0, len(keys)))]
            ev.add(i, etype, f"{p.name} ({p.title}) hands in notice", f"person.{p.id.lower()}")
        elif who == "key_account":
            cl = spec.key_clients[int(r.integers(0, len(spec.key_clients)))]
            ev.add(i, etype, f"{cl.name} gives notice: about {x:.0%} of ARR", f"org.{cl.id}")
        else:
            ev.add(i, etype, text(x), who)
    # staff leaving (attrition) in the window
    leavers = rng_people.poisson(float(pr["attrition"][s]) * len(people) * DAYS / 250)
    staff = [p for p in staff_t if p.department != "Management" and not p.key]
    for _ in range(int(leavers)):
        p = staff[int(rng_people.integers(0, len(staff)))]
        ev.add(
            int(rng_people.integers(0, DAYS)),
            "people",
            f"{p.name} ({p.title}) hands in notice",
            f"person.{p.id.lower()}",
        )
    # daily series and incidents
    month_pos = np.arange(1, DAYS + 1) / pc.BDAYS_PER_MONTH
    cash_path = paths["cash"][s, j, :]
    noise = np.zeros(DAYS)
    load = np.zeros(DAYS)
    z_cash, z_load = rng_cash.standard_normal(DAYS), rng_load.standard_normal(DAYS)
    for d in range(DAYS):
        noise[d] = (0.6 * noise[d - 1] if d else 0.0) + 12_000 * z_cash[d]
        load[d] = load_mean + 0.6 * ((load[d - 1] if d else load_mean) - load_mean) + 0.03 * z_load[d]
    cash = np.interp(month_pos, np.arange(MONTHS + 1), cash_path) + noise
    lam = float(pr["incidents"][s]) * MONTHS / 250
    inc = np.zeros(DAYS, dtype=int)
    for d in range(DAYS):
        inc[d] = int(rng_inc.poisson(max(lam * load[d] / load_mean, 0)))
        for _ in range(inc[d]):
            hours = float(rng_inc.exponential(float(pr["mttr_eff"][s])))
            eng = engineers[int(rng_inc.integers(0, len(engineers)))]
            svc = SERVICES[int(rng_inc.integers(0, len(SERVICES)))]
            ev.add(
                d,
                "incident",
                f"Incident on the {svc}: restored in {hours:.1f} h (on call: {eng.name})",
                f"person.{eng.id.lower()}",
            )
    year_end = {
        c.name: round(float(run.values[s, j, k]), 6 if c.unit == "share" else 3)
        for k, c in enumerate(run.criteria)
    }
    label = pc.LABEL[kind]
    return {
        "preview": True,
        "option": option,
        "kind": kind,
        "label": label,
        "summary": f"{label} example (scenario {s}): cash at year end {pc.money(year_end['cash'])}, churn "
        f"{year_end['churn']:.0%}, availability {year_end['sla']:.2%}, NPS {year_end['nps']:.0f}.",
        "yearEnd": year_end,
        "days": [
            {
                "date": days[d].isoformat(),
                "values": {
                    "cash": round(float(cash[d])),
                    "load": round(float(load[d]), 3),
                    "incidents": int(inc[d]),
                },
            }
            for d in range(DAYS)
        ],
        "events": ev.sorted(),
    }


def build_company(n: int = N) -> dict[str, Any]:
    l0 = l0_run(n)
    paths = monthly_paths(l0)
    labels, options = l0["labels"], l0["options"]
    calendars = risk_calendar(l0, paths)
    cal = company_calendar(pc.TIMEZONE, ANCHOR.year, ANCHOR.year + 1)
    return {
        "preview": True,
        "caseId": CASE_ID,
        "source": f"scripts/mvp/proto_future.py: L0 year-end results (simulate_tensor, n = {n}, seed {SEED}) with "
        "monthly bridges, an orgsim.tree weekly tree and the orgsim.risk calendar",
        "note": "Preview data: a stand-in until the month Monte Carlo on actors exists. Month 12 of each fan equals the "
        "year-end distribution of the formula model.",
        "seed": SEED,
        "n": n,
        "anchor": ANCHOR.isoformat(),
        "horizonMonths": MONTHS,
        "metrics": pc.metrics(METRICS),
        "weekMetrics": pc.metrics(WEEK_METRICS),
        "dayMetrics": pc.metrics(DAY_METRICS),
        "options": {
            o: {
                "preview": True,
                "option": o,
                "label": labels.get(o, o),
                "fans": fans(paths, j),
                "riskCalendar": calendars[j],
            }
            for j, o in enumerate(options)
        },
        "weekTrees": {o: week_tree(l0, j) for j, o in enumerate(options)},
        "dayExamples": {
            o: [day_example(l0, paths, j, k, q, cal) for k, q in pc.KINDS] for j, o in enumerate(options)
        },
    }


def build_preview(case: str = CASE_ID, n: int = N) -> dict[str, Any]:
    if case == CASE_ID:
        return build_company(n)
    import proto_delivery

    return proto_delivery.build_preview(n)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--case", choices=[*INDEX_ORDER, "all"], default="all")
    args = ap.parse_args(argv)
    for cid in INDEX_ORDER if args.case == "all" else [args.case]:
        data = build_preview(cid)
        out = CASES / f"{cid}.preview.json"
        out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        nodes = sum(len(lv) for t in data["weekTrees"].values() for lv in t["levels"])
        print(out, f"({out.stat().st_size // 1024} KB, {nodes} tree nodes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
