"""Preview data of case A "Best way to deliver" (delivery_best_way, delivery_model): workspace/config/cases/
delivery_best_way.preview.json. Written by ``scripts/mvp/proto_future.py`` (``--case delivery_best_way``).

A stand-in until the month Monte Carlo on actors exists; every container says ``"preview": true``. Deterministic:
the results at month 24 come from the L0 formula model (``simulate_tensor``, n = 4096, seed 42); every other random
number comes from preview streams whose labels never name the fork (common random numbers).

- Monthly fans (months 0..24) per fork: spend to date (the fork's cost, paid month by month until the go-live; the
  offshore fixed price pays 20% at signing), work done (from the end of the ramp-up to the build time) and money to
  date (value earned after the go-live minus the spend). Month 24 of the money fan equals the L0 money distribution.
- Risk calendar per fork (``orgsim.risk``), P(failed by month m) and the median first-failure month: the spend
  passes the project budget (the monthly budget times the target months), a milestone is late (beta, load-test
  environment, go-live: it fails at its marker month when the L0 condition fails), the money goal is missed
  (month 24).
- Week tree (k = 3, at most 60 nodes per level, 52 weeks) for every fork: money to date (the tree colour: each
  branch goes live when its work is done plus the fork's gap to the go-live), work done, spend to date and defects.
- Day examples for every fork: the L0 scenarios at the 10%, 50% and 90% points of money; daily work done, spend and
  defects for the first 60 working days after T, and events with knowledge-base ids (org.VN-01 Vistula Code Works,
  org.VN-02 Kaveri Tech Services, person.lw-e03 ...).
"""

# ruff: noqa: E501 - event and summary texts, one per line

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np
import proto_common as pc
from case_delivery import ITEMS

from variator.orgsim.company.spec import default_spec
from variator.orgsim.company.ukcal import company_calendar
from variator.orgsim.tree import grow_tree

CASE_ID = "delivery_best_way"
SEED, N, ANCHOR, DAYS, WEEKS = pc.SEED, pc.N, pc.ANCHOR, pc.DAYS, pc.WEEKS
MONTHS = 24
PROBES = [
    "duration", "build_time", "ramp_up_months", "value", "value_per_month", "team_size", "rate", "cap", "work",
    "rework_share", "env_start", "env_ready", "budget_per_month", "target_months", "change_share", "fixed_quote",
]  # fmt: skip
FIXED_PRICE = {"offshore_fixed"}  # pays 20% at signing, the rest with the progress
UPFRONT = 0.2
VENDOR = {
    "nearshore_tm": ("Vistula Code Works", "org.VN-01"),
    "offshore_fixed": ("Kaveri Tech Services", "org.VN-02"),
}
METRICS = [
    (
        "money",
        "Money to date",
        "USD",
        True,
        "Business value earned after the go-live minus the delivery cost paid; month 24 is the money criterion.",
    ),
    ("progress", "Work done", "share", True, "Share of the work done by the end of each month."),
    ("spend", "Spend to date", "USD", False, "Delivery cost paid to the end of each month."),
]
# the first week metric colours the tree: money varies between the branches at week 52 (work done is 100% on all)
WEEK_METRICS = [
    (
        "money",
        "Money to date",
        "USD",
        True,
        "Business value earned after the go-live of the branch minus the delivery cost paid, at the end of the week.",
    ),
    ("progress", "Work done", "share", True, "Share of the work done at the end of the week."),
    ("spend", "Spend to date", "USD", False, "Delivery cost paid to the end of the week."),
    ("defects", "Defects", "count", False, "Defects found in the week."),
]
DAY_METRICS = [
    ("progress", "Work done", "share", True, "Share of the work done at the end of the day."),
    ("spend", "Spend to date", "USD", False, "Delivery cost paid to the end of the day."),
    ("defects", "Defects", "count", False, "Defects found on the day."),
]
BUDGET_ROW = (
    "budget_overrun", "Spend above the project budget", "The project needs more money than approved", "threshold", None,
)  # fmt: skip
TIMELINE_TEXT = {
    "beta": ("Beta late", "Half of the work is not done by the beta date: the client sees less, later"),
    "load_test_env": ("Load-test environment late", "The load tests start late and can block the go-live"),
    "go_live": ("Go-live late", "You miss the promised go-live: penalties and lost trust"),
    "money_goal": ("Money goal missed", "The project earns less than its goal by month 24"),
}
COMPONENTS = ["API", "web app", "billing module", "data import", "reporting"]
# fork -> (start text, entity)
START = {
    "in_house": ("Hiring starts: 8 engineer requisitions open for the platform team", "person.lw-e03"),
    "nearshore_tm": ("Contract signed with Vistula Code Works: 10 people on time and materials", "org.VN-01"),
    "offshore_fixed": (
        "Fixed-price contract signed with Kaveri Tech Services: 16 people plus change requests",
        "org.VN-02",
    ),
}
# preview shocks: model risk, probability, options it applies to (None = all)
SHOCKS = [
    ("key_person_loss", 0.2, {"in_house"}),
    ("vendor_turnover", 0.25, {"nearshore_tm", "offshore_fixed"}),
    ("scope_creep", 0.35, None),
]


def l0_run(n: int = N) -> dict[str, Any]:
    return pc.l0_run(CASE_ID, {p: p for p in PROBES}, n)


def _share(option: str, t: np.ndarray, duration: np.ndarray) -> np.ndarray:
    """Share of the fork's whole cost paid by month t (paid until the go-live, all of it by month 24)."""
    end = np.minimum(duration, MONTHS)
    run = np.clip(t / np.maximum(end, 1e-9), 0, 1)
    if option in FIXED_PRICE:
        return np.where(t > 0, np.minimum(UPFRONT + (1 - UPFRONT) * run, 1), 0.0)
    return run


def _progress(t: np.ndarray, ramp: np.ndarray, build: np.ndarray) -> np.ndarray:
    return np.clip((t - ramp) / np.maximum(build - ramp, 1e-9), 0, 1)


def monthly_paths(l0: dict[str, Any]) -> dict[str, np.ndarray]:
    """Per metric: (n, options, 25) monthly values; month 24 of money = the L0 money value."""
    run = l0["run"]
    pr = run.probes
    total = pr["value"] - run.values[:, :, run.criterion_index("money")]  # the fork's whole delivery cost
    t = np.arange(MONTHS + 1, dtype=float)[None, None, :]
    spend = np.stack(
        [
            total[:, j, None] * _share(o, t[0], pr["duration"][:, j, None])
            for j, o in enumerate(l0["options"])
        ],
        axis=1,
    )
    earned = pr["value_per_month"][:, :, None] * np.maximum(t - pr["duration"][:, :, None], 0)
    progress = _progress(t, pr["ramp_up_months"][:, :, None], pr["build_time"][:, :, None])
    return {"money": earned - spend, "progress": progress, "spend": spend}


def fans(paths: dict[str, np.ndarray], j: int) -> dict[str, list[dict[str, float]]]:
    return {m: pc.fan(paths[m][:, j, :], MONTHS, 4 if m == "progress" else None) for m, *_ in METRICS}


def risk_calendar(l0: dict[str, Any], paths: dict[str, np.ndarray]) -> list[list[dict[str, Any]]]:
    pr = l0["run"].probes
    budget = (pr["budget_per_month"] * pr["target_months"])[:, :, None]
    cid, label, cons, kind, ref = BUDGET_ROW
    rows = [
        (
            {"id": cid, "label": label, "consequence": cons, "kind": kind, "ref": ref},
            paths["spend"][:, :, 1:] > budget,
            None,
        )
    ]
    rows += pc.timeline_rows(l0, ("milestone", "goal"), TIMELINE_TEXT, MONTHS)
    return pc.calendar(rows, l0["options"])


def _week_summary(week: int, s: dict[str, Any], prev: dict[str, Any], lam: float) -> str:
    if week == 0:
        return "Today: the work starts; nothing done yet."
    notes = []
    if s["progress"] <= 0:
        notes.append("the team is still ramping up")
    elif s["progress"] >= 1 > prev["progress"]:
        notes.append("all work done")
    if s["earned"] > 0 >= prev["earned"]:
        notes.append("the platform is live and earns")
    if s["defects"] >= max(3, 2 * lam):
        notes.append("a rough week for quality")
    d = s["defects"]
    text = f"Week {week}: {s['progress']:.0%} done, spent {pc.money(s['spend'])} ({pc.signed_money(s['spend'] - prev['spend'])}), {d} defect{'' if d == 1 else 's'}"
    return text + (f"; {', '.join(notes)}." if notes else ".")


def week_tree(l0: dict[str, Any], j: int) -> dict[str, Any]:
    run, option = l0["run"], l0["options"][j]
    pr = {k: float(v[:, j].mean()) for k, v in run.probes.items()}
    total = float((run.probes["value"][:, j] - run.values[:, j, run.criterion_index("money")]).mean())
    ramp, build, dur = pr["ramp_up_months"], pr["build_time"], pr["duration"]
    week = 12 / 52  # months
    gain = week / max(build - ramp, 1e-9)
    burn = (1 - UPFRONT if option in FIXED_PRICE else 1) * total / max(min(dur, MONTHS), 1e-9) * week
    lam = pr["rework_share"] * pr["cap"] * week / 2
    # a branch goes live when its work is done plus the fork's mean gap from the build end to the go-live
    # (load tests, the environment); then it earns the value of a month each month
    lag, vpm = max(dur - build, 0.0), pr["value_per_month"]

    def step(state: dict[str, Any], rng: np.random.Generator, node: Any) -> dict[str, Any]:
        z = rng.standard_normal(2)
        t = state["t"] + week
        working = 0 < state["progress"] < 1 or (state["progress"] == 0 and t > ramp)
        progress = min(state["progress"] + max(gain * (1 + 0.25 * z[0]), 0), 1) if t > ramp else 0.0
        pay = burn * max(1 + 0.03 * z[1], 0) if state["t"] < dur else 0.0
        if option in FIXED_PRICE and state["t"] == 0:
            pay += UPFRONT * total
        done = state["done"] if state["done"] >= 0 else (t if progress >= 1 else -1.0)
        earned = state["earned"] + (vpm * week if 0 <= done and t > done + lag else 0.0)
        spend = state["spend"] + pay
        return {
            "t": t,
            "progress": progress,
            "spend": spend,
            "defects": int(rng.poisson(lam if working else 0.2 * lam)),
            "done": done,
            "earned": earned,
            "money": earned - spend,
        }

    root = {"t": 0.0, "progress": 0.0, "spend": 0.0, "defects": 0, "done": -1.0, "earned": 0.0, "money": 0.0}
    tree = grow_tree(
        root, step, levels=WEEKS, k=pc.K, max_nodes=pc.MAX_NODES, seed=SEED, root_key="week", with_node=True
    )
    label = l0["labels"].get(option, option)
    return pc.export_tree(
        tree,
        option,
        label,
        {"money": 0, "progress": 4, "spend": 0, "defects": 0},
        lambda w, s, prev: _week_summary(w, s, prev, lam),
        {"money": 0, "progress": 4, "spend": 0, "defects": 3},
    )


def _first_workday(cal: Any, y: int, m: int) -> date:
    return cal.add_business_days(date(y + (m - 1) // 12, (m - 1) % 12 + 1, 1), 0)


def day_example(l0: dict[str, Any], j: int, kind: str, q: float, cal: Any) -> dict[str, Any]:
    run, option, spec = l0["run"], l0["options"][j], default_spec()
    pr = {k: v[:, j] for k, v in run.probes.items()}
    money_end = run.values[:, j, run.criterion_index("money")]
    s = int(np.argmin(np.abs(money_end - np.quantile(money_end, q))))
    x = {k: float(v[s]) for k, v in pr.items()}
    total = x["value"] - float(money_end[s])
    days = pc.bdays(cal, DAYS)
    t = np.array([(d - ANCHOR).days for d in days]) * 12 / 365  # months after T
    progress = _progress(t, x["ramp_up_months"], x["build_time"])
    spend = total * _share(option, t, np.float64(x["duration"]))
    rng_def, rng_shock = pc.rng("day", kind, "defects"), pc.rng("delivery-shocks", f"s{s}")
    staff_t = pc.staff_at_t(spec)  # people who left before T are not in the future
    engineers = [p for p in staff_t if p.department == "Engineering"]
    env_owner = f"person.{ITEMS['env_work']['owner'].lower()}"
    vendor = VENDOR.get(option)
    ev = pc.Events(days)
    at = lambda months: pc.day_index(days, ANCHOR + timedelta(days=round(months * 365 / 12)))  # noqa: E731
    text, entity = START[option]
    ev.add(0, "decision", text, entity)
    ev.add(at(x["env_start"]), "decision", "Load-test environment work starts", env_owner)
    ev.add(at(x["env_ready"]), "milestone", "Load-test environment ready", env_owner)
    if vendor:
        ev.add(at(x["ramp_up_months"]), "vendor", f"{vendor[0]} team onboarded: delivery starts", vendor[1])
    else:
        ev.add(
            at(x["ramp_up_months"]),
            "people",
            "The platform team is complete and up to speed: delivery starts",
            "person.lw-e03",
        )
    # payments: payroll or the vendor's invoices on the first working day of each month; the fixed price 20% at signing
    monthly = total / max(min(x["duration"], MONTHS), 1e-9) * (1 - UPFRONT if option in FIXED_PRICE else 1)
    if option in FIXED_PRICE:
        ev.add(
            0,
            "payment",
            f"{vendor[0]} paid 20% of the fixed price at signing: {pc.money(UPFRONT * total)}",
            vendor[1],
        )
    for m in range(1, 4):
        paid = _first_workday(cal, ANCHOR.year, ANCHOR.month + m)
        if paid > days[-1] or (paid - ANCHOR).days * 12 / 365 > x["duration"]:
            continue
        if vendor and option in FIXED_PRICE:
            ev.add(
                pc.day_index(days, paid),
                "payment",
                f"{vendor[0]} progress payment: {pc.money(monthly)}",
                vendor[1],
            )
        elif vendor:
            ev.add(
                pc.day_index(days, paid),
                "payment",
                f"{vendor[0]} invoice paid: {pc.money(monthly)}",
                vendor[1],
            )
        else:
            ev.add(pc.day_index(days, paid), "payment", f"Payroll of the platform team: {pc.money(monthly)}")
    # preview shocks of this scenario (one working day of the year each; the same in every fork it applies to)
    for name, p, only in SHOCKS:
        r = pc.rng("shock", name, f"s{s}")
        hit, i, size = r.random() < p, int(r.integers(0, 250)), float(r.triangular(0.05, 0.15, 0.3))
        if not hit or i >= DAYS or (only is not None and option not in only):
            continue
        if name == "key_person_loss":
            keys = [p for p in staff_t if p.key]
            who = keys[int(rng_shock.integers(0, len(keys)))]
            ev.add(i, "people", f"{who.name} ({who.title}) hands in notice", f"person.{who.id.lower()}")
        elif name == "vendor_turnover":
            ev.add(i, "vendor", f"{vendor[0]} rotates two developers off the team", vendor[1])
        else:
            ev.add(i, "client", f"The client asks for more features: about {size:.0%} more scope", None)
    # defects while the work runs
    lam = x["rework_share"] * x["cap"] / pc.BDAYS_PER_MONTH / 2
    defects = np.zeros(DAYS, dtype=int)
    for d in range(DAYS):
        defects[d] = int(rng_def.poisson(lam if 0 < progress[d] < 1 else 0.0))
        for _ in range(defects[d]):
            comp = COMPONENTS[int(rng_def.integers(0, len(COMPONENTS)))]
            hours = float(rng_def.exponential(6.0))
            if vendor:
                ev.add(
                    d, "incident", f"Defect in the {comp}: {vendor[0]} fixes it in {hours:.1f} h", vendor[1]
                )
            else:
                eng = engineers[int(rng_def.integers(0, len(engineers)))]
                ev.add(
                    d,
                    "incident",
                    f"Defect in the {comp}: fixed in {hours:.1f} h by {eng.name}",
                    f"person.{eng.id.lower()}",
                )
    end = {c.name: round(float(run.values[s, j, k]), 3) for k, c in enumerate(run.criteria)}
    label = pc.LABEL[kind]
    return {
        "preview": True,
        "option": option,
        "kind": kind,
        "label": label,
        "summary": f"{label} example (scenario {s}): money at month 24 {pc.money(end['money'])}, go-live in month "
        f"{end['time']:.1f}, {end['delay_months']:.1f} months late, management effort {end['resources']:.1f} FTE-months.",
        "yearEnd": end,
        "days": [
            {
                "date": days[d].isoformat(),
                "values": {
                    "progress": round(float(progress[d]), 4),
                    "spend": round(float(spend[d])),
                    "defects": int(defects[d]),
                },
            }
            for d in range(DAYS)
        ],
        "events": ev.sorted(),
    }


def build_preview(n: int = N) -> dict[str, Any]:
    l0 = l0_run(n)
    paths = monthly_paths(l0)
    labels, options = l0["labels"], l0["options"]
    calendars = risk_calendar(l0, paths)
    cal = company_calendar(pc.TIMEZONE, ANCHOR.year, ANCHOR.year + 1)
    return {
        "preview": True,
        "caseId": CASE_ID,
        "source": f"scripts/mvp/proto_delivery.py: L0 results at month 24 (simulate_tensor, n = {n}, seed {SEED}) with "
        "monthly spend, work and value paths, an orgsim.tree weekly tree and the orgsim.risk calendar",
        "note": "Preview data: a stand-in until the month Monte Carlo on actors exists. Month 24 of the money fan equals "
        "the money distribution of the formula model.",
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
            o: [day_example(l0, j, k, q, cal) for k, q in pc.KINDS] for j, o in enumerate(options)
        },
    }
