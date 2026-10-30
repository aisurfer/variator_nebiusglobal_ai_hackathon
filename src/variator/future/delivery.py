"""Case A (delivery_best_way) on actors: an in-house team, the nearshore vendor (T&M) or the offshore vendor
(fixed price plus change requests) delivers the new platform; 24 months.

Each fork is a team of FTE actors (slots) with a pace, a join month, a ramp-up and a cost:

- in-house (10-MVP-PRD §4: "own team of 8 FTE, the rest of the work slows down"): the 8 engineers of the
  company with the highest learned pace move to the project at T. No hiring lead time; a ramp-up on the new
  codebase (the codebase part of the learned ramp of hires); their own learned pace, vacation days, sickness
  and resignation hazard; a backfill from the hiring cohort after a resignation. ``in_house="hire"`` gives the
  first version (8 new hires from the cohort ``future:backend``) for the comparison;
- vendors (Vistula Code Works VN-01, Kaveri Tech Services VN-02): cohorts with ``assumption:`` tags (the case
  overrides); the month-to-month variation of their bills is learned (risk R19); vendor turnover is a case shock.

The slowdown of the other work is a cost line: the case B engine runs the company with the 8 engineers away
for the project months (``slowdown_cost``).
"""

# ruff: noqa: E501 - parameter notes, one per line

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from variator.future import export
from variator.future.forks import case_config, var_param
from variator.future.params import Habits, Param, draw_theta, learned, normals, uniforms
from variator.future.state import month_start
from variator.metrics.cube import CriterionSpec, MetricCube, MetricInfo, save_cube, to_tensor_run
from variator.orgsim import risk

CASE = "delivery_best_way"
H = 24
SLOTS = 16  # most FTE of a fork (offshore)
CREW = 8  # the in-house team
UPFRONT = 0.2  # share of the fixed price paid at signing (as in the preview of the page)
ENGINEERING = "Engineering"
MONTH_METRICS = [
    (
        "money",
        "Money to date",
        "USD",
        True,
        "Value earned after the go-live minus the delivery cost paid; month 24 is the money criterion.",
        None,
    ),
    ("progress", "Work done", "share", True, "Share of the work done by the end of each month.", 4),
    ("spend", "Spend to date", "USD", False, "Delivery cost paid to the end of each month.", None),
    ("burn", "Spend in the month", "USD", False, "Delivery cost paid in the month.", None),
    (
        "fte",
        "Delivery FTE",
        "FTE",
        True,
        "FTE at full pace on the project (ramp-up, absences and vacancies included).",
        2,
    ),
    ("mgmt", "Management effort to date", "FTE-months", False, "Internal management FTE-months to date.", 2),
    (
        "other_loss",
        "Cash lost on other work to date",
        "USD",
        False,
        "In-house: the cash the company loses while its engineers work on the project (inside money).",
        None,
    ),
]


@dataclass(frozen=True)
class Team:
    id: str
    label: str
    size: int
    vendor: str | None  # actor id of the vendor, None = in-house
    fixed_price: bool


TEAMS = (
    Team("in_house", "In-house team", CREW, None, False),
    Team("nearshore_tm", "Nearshore T&M", 10, "VN-01", False),
    Team("offshore_fixed", "Offshore fixed price", 16, "VN-02", True),
)


def crew(habits: Habits, size: int = CREW) -> dict[str, Any]:
    """The in-house team: the engineers active at T with the highest learned pace (individual habits)."""
    hp = habits.habits.get("people", {})
    rows = []
    for a in habits.actor_list("person"):
        if a.get("department") != ENGINEERING or a.get("status_at_t") != "active":
            continue
        h = hp.get(a["id"], {})
        pace = float(h.get("throughput_points_per_sprint", {}).get("point", np.nan))
        rows.append((a["id"], a.get("name", a["id"]), pace, float(a.get("salary_at_t") or 0.0), h))
    paces = np.array([r[2] for r in rows])
    mean = float(np.nanmean(paces)) if len(rows) else 1.0
    rows.sort(key=lambda r: (-np.nan_to_num(r[2], nan=0.0), r[0]))
    team = rows[:size]

    def pt(h: Mapping[str, Any], key: str, default: float) -> float:
        return float(h.get(key, {}).get("point", default))

    return {
        "ids": [r[0] for r in team],
        "names": [r[1] for r in team],
        "pace": np.array([np.nan_to_num(r[2], nan=mean) / mean for r in team]),
        "salary": np.array([r[3] for r in team]),
        "vacation": np.array([pt(r[4], "vacation_days_per_year", 16.0) for r in team]),
        "sick": np.array([pt(r[4], "sick_days_per_year", 4.5) for r in team]),
        "resign_p": np.array([pt(r[4], "resignation_hazard", 0.18) for r in team]),
        "engineers": len(rows),
        "pace_share": float(np.nansum([r[2] for r in team]) / max(np.nansum(paces), 1e-9)),
        "source": "data:jira (pace), data:sim_employee_event (vacation), group:staff (sickness, resignation)",
    }


def _ov(cfg: Mapping[str, Any], option: str, name: str) -> Param | None:
    for o in cfg.get("options", []):
        if o["name"] == option and name in o.get("overrides", {}):
            d = o["overrides"][name]
            tag = "assumption:vendor cohort (case)"
            if d.get("kind") == "pert":
                return Param.pert(f"{option}.{name}", d["low"], d["mode"], d["high"], tag)
            return Param.fixed(f"{option}.{name}", d["value"], tag)
    return None


def _median_people(hb: Mapping[str, Any], key: str, default: float) -> float:
    xs = [float(p[key]["point"]) for p in hb.get("people", {}).values() if key in p]
    return float(np.median(xs)) if xs else default


def _cfg_value(cfg: Mapping[str, Any], name: str, default: float) -> float:
    for v in cfg.get("variables", []):
        if v["name"] == name:
            return float(v["dist"].get("value", v["dist"].get("mode", default)))
    return default


def delivery_params(habits: Habits, cfg: Mapping[str, Any]) -> dict[str, Param]:
    hb = habits.habits
    st, fb = hb.get("staff", {}), habits.cohort("future:backend").get("params", {})
    ps: list[Param] = [
        var_param(cfg, "scope_points", 600.0),
        var_param(cfg, "env_lead", 2.0),
        var_param(cfg, "env_start", 1.0),
        var_param(cfg, "load_test_months", 0.5),
        var_param(cfg, "value_per_month", 200_000.0),
        var_param(cfg, "target_months", 9.0),
        var_param(cfg, "budget_per_month", 100_000.0),
        var_param(cfg, "fixed_quote", 320_000.0),
        var_param(cfg, "change_share", 0.15),
        var_param(cfg, "g_money", 1_500_000.0),
        var_param(cfg, "m_beta", 6.0),
        var_param(cfg, "m_env", 4.0),
        Param.fixed(
            "engineer_cost", _cfg_value(cfg, "engineer_cost", 150_000.0), "case:data: HR", "USD per year"
        ),
        var_param(cfg, "productivity", 10.0, "points per FTE-month at full pace (the scale of the backlog)"),
        var_param(cfg, "rework_share", 0.08),
        var_param(cfg, "mgmt_ratio", 0.7),
        learned(
            "hiring_lead_months",
            fb.get("hiring_lead_months"),
            default=Param.fixed("hiring_lead_months", 3.0, "assumption:"),
            note="log-normal median; n = 3",
        ),
        Param.fixed(
            "hiring_lead_log_sd",
            float(fb.get("hiring_lead_months", {}).get("extra", {}).get("log_sd", 0.25)),
            "group:hires",
            "log-sd",
        ),
        learned(
            "ramp_1", fb.get("ramp_share_first_20_bdays"), default=Param.fixed("ramp_1", 0.4, "assumption:")
        ),
        learned(
            "ramp_2", fb.get("ramp_share_next_20_bdays"), default=Param.fixed("ramp_2", 0.8, "assumption:")
        ),
        Param.fixed(
            "codebase_ramp",
            float(fb.get("ramp_share_next_20_bdays", {}).get("point", 0.8)),
            "group:hires",
            "share",
            "pace in the first month on the new codebase: the second month of the learned ramp of hires (the company part is known; risk R15 says about a month)",
        ),
        Param.fixed(
            "pace_spread",
            float(st.get("throughput_spread", {}).get("point", 0.34)),
            "group:engineers",
            "log-sd",
        ),
        learned(
            "resign_p", st.get("resignation_hazard"), default=Param.fixed("resign_p", 0.18, "assumption:")
        ),
        Param.fixed(
            "sick_days",
            _median_people(hb, "sick_days_per_year", 4.5),
            "group:staff",
            "days per year",
            "median of the people",
        ),
        Param.fixed(
            "vacation_days",
            _median_people(hb, "vacation_days_per_year", 16.0),
            "group:staff",
            "days per year",
            "median of the people",
        ),
        Param.fixed(
            "payroll_tax",
            float(hb.get("regulators", {}).get("TA-UK", {}).get("payroll_tax_rate", {}).get("point", 0.15)),
            "data:sim_payroll",
            "share",
        ),
    ]
    shares = [float(st.get(f"vacation_month_share_{m:02d}", {}).get("point", 1 / 12)) for m in range(1, 13)]
    total = sum(shares) or 1.0
    for m, s in enumerate(shares, start=1):
        ps.append(
            Param.fixed(
                f"vacation_share_{m:02d}",
                s / total,
                "data:sim_employee_event",
                "share",
                "the vacation calendar of the staff",
            )
        )
    for t in TEAMS[1:]:
        for name in ("productivity", "rate", "ramp_up_months", "rework_share", "mgmt_ratio"):
            p = _ov(cfg, t.id, name)
            if p is not None:
                ps.append(p)
    r19 = habits.risk("vendor_cost_change").get("size", {}).get("bill_variability_cv", {})
    for vid in ("VN-01", "VN-02"):
        ps.append(
            Param.fixed(f"{vid}.bill_cv", float(r19.get(vid, 0.05)), "data:sim_invoice (risk R19)", "cv")
        )
    for r in cfg.get("risks", []):
        if r["name"] == "key_person_loss":
            continue  # the in-house people carry their own resignation hazards
        m = r["magnitude"]
        ps.append(Param.fixed(f"shock.{r['name']}.p", float(r["p"]), "assumption:case shock", "probability"))
        ps.append(
            Param.pert(f"shock.{r['name']}.size", m["low"], m["mode"], m["high"], "assumption:case shock")
        )
    return {p.name: p for p in ps}


@dataclass
class DeliveryResult:
    forks: list[str]
    metrics: dict[str, np.ndarray]  # (F, S, H + 1)
    checks: dict[str, np.ndarray]  # (F, S, H)
    criteria: dict[str, np.ndarray]  # (F, S)
    done: dict[str, np.ndarray]  # milestone months (F, S)
    theta: dict[str, np.ndarray]
    crew: dict[str, Any] | None = None
    events: list[list[list[tuple[int, dict[str, Any]]]]] | None = None  # [fork][scenario] -> (month, event)


def _ramp(th: Mapping[str, np.ndarray], age: np.ndarray) -> np.ndarray:
    return np.where(
        age < 0, 0.0, np.where(age < 1, th["ramp_1"][:, None], np.where(age < 2, th["ramp_2"][:, None], 1.0))
    )


def simulate(
    params: Mapping[str, Param],
    n: int,
    seed: int,
    *,
    as_of: date,
    layer: Mapping[str, float] | None = None,
    forks: Sequence[str] | None = None,
    crew_: Mapping[str, Any] | None = None,
    in_house: str = "existing",
) -> DeliveryResult:
    """The month Monte Carlo of case A: (forks, n scenarios, 24 months). ``crew_``: ``crew(habits)``."""
    layer = dict(layer or {})
    th = draw_theta(params, n, seed)
    teams = [t for t in TEAMS if forks is None or t.id in forks]
    F = len(teams)
    cal = np.array([month_start(as_of, m).month for m in range(H + 1)])
    # the same world in every fork: scope, environment, shocks
    scope = th["scope_points"] * layer.get("scope_scale", 1.0)
    creep_on = uniforms(seed, n, "delivery", "scope_creep", "occur") < 1 - (1 - th["shock.scope_creep.p"]) * (
        1 - layer.get("scope_creep_p", 0.0)
    )
    creep_month = 1 + np.floor(uniforms(seed, n, "delivery", "scope_creep", "month") * 12)
    creep_size = th["shock.scope_creep.size"] * layer.get("scope_creep_scale", 1.0)
    env_ready = th["env_start"] + th["env_lead"]
    turn_u = uniforms(seed, n, "delivery", "vendor_turnover", "occur")
    turn_month = 1 + np.floor(uniforms(seed, n, "delivery", "vendor_turnover", "month") * 12)
    out = {m[0]: np.zeros((F, n, H + 1)) for m in MONTH_METRICS}
    budget_fail = np.zeros((F, n, H), bool)
    events: list[list[list[tuple[int, dict[str, Any]]]]] = [[[] for _ in range(n)] for _ in teams]
    live = np.full((F, n), np.inf)
    beta = np.full((F, n), np.inf)
    done_at = np.full((F, n), np.inf)
    tax = th["payroll_tax"][:, None]
    for j, team in enumerate(teams):

        def p(name: str, t: Team = team) -> np.ndarray:
            key = f"{t.id}.{name}"
            return th[key] if key in th else th[name]

        prod = p("productivity") * layer.get("productivity_scale", 1.0)
        rework = np.minimum(p("rework_share") * layer.get("rework_scale", 1.0), 0.9)
        mgmt = p("mgmt_ratio")
        spread = th["pace_spread"][:, None]
        z_pace = normals(seed, n, "delivery", "slot", "pace", shape=(SLOTS,))
        pace = np.exp(spread * z_pace - spread**2 / 2)
        size = np.arange(SLOTS) < team.size
        bf_pace = np.exp(
            spread * normals(seed, n, "delivery", "slot", "backfill-pace", shape=(SLOTS,)) - spread**2 / 2
        )
        existing = team.vendor is None and in_house == "existing" and crew_ is not None
        if team.vendor is None:
            lead = th["hiring_lead_months"][:, None] * np.exp(
                th["hiring_lead_log_sd"][:, None]
                * normals(seed, n, "delivery", "slot", "lead", shape=(SLOTS,))
            )
            e = -np.log(np.maximum(uniforms(seed, n, "delivery", "slot", "resign", shape=(SLOTS,)), 1e-12))
            if existing:
                k = len(crew_["ids"])
                size = np.arange(SLOTS) < k
                pace = np.zeros((n, SLOTS))
                pace[:, :k] = crew_["pace"]
                rp = np.full(SLOTS, 0.18)
                rp[:k] = crew_["resign_p"]
                join = np.zeros((n, SLOTS))
                salary = np.zeros(SLOTS)
                salary[:k] = crew_["salary"]
                rate = np.broadcast_to(salary / 12, (n, SLOTS)) * (1 + tax)
                vac = np.full(SLOTS, 16.0)
                vac[:k] = crew_["vacation"]
                sick = np.full(SLOTS, 4.5)
                sick[:k] = crew_["sick"]
            else:
                rp = np.full(SLOTS, 1.0) * th["resign_p"][:, None]
                join = lead + layer.get("hiring_lead_shift", 0.0)
                rate = np.full((n, SLOTS), th["engineer_cost"][0] / 12)
                vac = np.full(SLOTS, float(th["vacation_days"][0]))
                sick = np.full(SLOTS, float(th["sick_days"][0]))
            h = -np.log1p(-np.minimum(rp * layer.get("resign_scale", 1.0), 0.95))
            leave = join + e / h * 12  # months after T (hazard per year)
            bf_lead = th["hiring_lead_months"][:, None] * np.exp(
                th["hiring_lead_log_sd"][:, None]
                * normals(seed, n, "delivery", "slot", "backfill-lead", shape=(SLOTS,))
            )
            bf_join = leave + bf_lead + layer.get("hiring_lead_shift", 0.0)
            bf_rate = th["engineer_cost"][0] / 12
        else:  # vendor people start at T and reach full pace after the onboarding
            join = np.zeros((n, SLOTS))
            leave = np.full((n, SLOTS), np.inf)
            bf_join = np.full((n, SLOTS), np.inf)
            rate = np.broadcast_to(p("rate")[:, None], (n, SLOTS)) * layer.get("vendor_rate_scale", 1.0)
            bf_rate = 0.0
        work_total = scope * (1 + rework)
        done = np.zeros(n)
        spend = np.zeros(n)
        mgmt_sum = np.zeros(n)
        earned = np.zeros(n)
        bill_cv = th.get(f"{team.vendor}.bill_cv", np.zeros(n)) if team.vendor else np.zeros(n)
        z_bill = normals(seed, n, "delivery", "bill", shape=(H,))
        for m in range(1, H + 1):
            mid = m - 0.5
            if team.vendor is None:
                inc = (join <= mid) & (leave > mid)
                if existing:
                    r_inc = np.where(mid - join < 1, th["codebase_ramp"][:, None], 1.0)
                else:
                    r_inc = _ramp(th, mid - join)
                bf = bf_join <= mid
                present = (inc * r_inc * pace + bf * _ramp(th, mid - bf_join) * bf_pace) * size
                paid_inc, paid_bf = inc & size, bf & size
                absent = (sick / 12 + vac * th[f"vacation_share_{cal[m]:02d}"][:, None]) / 21
                fte = (present * (1 - absent)).sum(-1)
                pay_team = (rate * paid_inc).sum(-1) + bf_rate * paid_bf.sum(-1)
            else:
                r = np.clip(mid / np.maximum(p("ramp_up_months"), 1e-6), 0, 1)[:, None]
                fte = (r * pace * size).sum(-1)
                turned = (
                    turn_u
                    < 1 - (1 - th["shock.vendor_turnover.p"]) * (1 - layer.get("vendor_turnover_p", 0.0))
                ) & (m >= turn_month)
                fte = fte * (1 + np.where(turned, th["shock.vendor_turnover.size"], 0.0))
                pay_team = (rate * size).sum(-1) * (1 + bill_cv * z_bill[:, m - 1])
            active = done < work_total
            creep = creep_on & (creep_month == m) & active
            work_total = work_total + np.where(creep, scope * creep_size * (1 + rework), 0.0)
            gain = np.where(active, fte * prod, 0.0)
            prev = done
            done = np.minimum(done + gain, work_total)
            just = (done >= work_total) & (prev < work_total)
            done_at[j] = np.where(
                just, m - 1 + np.clip((work_total - prev) / np.maximum(gain, 1e-9), 0, 1), done_at[j]
            )
            half = (done >= 0.5 * work_total) & (prev < 0.5 * work_total)
            beta[j] = np.where(
                half, m - 1 + np.clip((0.5 * work_total - prev) / np.maximum(gain, 1e-9), 0, 1), beta[j]
            )
            live[j] = np.where(
                np.isfinite(done_at[j]), np.maximum(done_at[j], env_ready + th["load_test_months"]), np.inf
            )
            working = np.isinf(done_at[j]) | (done_at[j] > m - 1)
            if team.fixed_price:
                quote = th["fixed_quote"] * layer.get("vendor_rate_scale", 1.0)
                pay = (UPFRONT * quote if m == 1 else 0.0) + (1 - UPFRONT) * quote * np.maximum(
                    done / work_total - prev / np.maximum(work_total, 1e-9), 0
                )
                pay = pay + np.where(just, quote * th["change_share"] * layer.get("change_scale", 1.0), 0.0)
            else:
                pay = np.where(working, pay_team, 0.0)
            spend = spend + pay
            mgmt_sum = mgmt_sum + np.where(working, mgmt, 0.0)
            earned = earned + th["value_per_month"] * np.clip(m - live[j], 0, 1)
            out["money"][j, :, m] = earned - spend
            out["progress"][j, :, m] = done / work_total
            out["spend"][j, :, m] = spend
            out["burn"][j, :, m] = pay
            out["fte"][j, :, m] = fte
            out["mgmt"][j, :, m] = mgmt_sum
            budget_fail[j, :, m - 1] = (
                (pay > th["budget_per_month"] * layer.get("budget_scale", 1.0))
                & working
                & (not team.fixed_price)
            )
        # the events of each scenario (the month event tree)
        turned_on = turn_u < 1 - (1 - th["shock.vendor_turnover.p"]) * (
            1 - layer.get("vendor_turnover_p", 0.0)
        )
        for s in range(n):
            ev: list[tuple[int, dict[str, Any]]] = []
            end = min(float(live[j, s]), H)
            if team.vendor is None:
                for k in range(SLOTS):
                    if not size[k]:
                        continue
                    who = (
                        (crew_["ids"][k], crew_["names"][k])
                        if existing
                        else (f"hire-slot-{k + 1}", f"New engineer {k + 1}")
                    )
                    if leave[s, k] < end:
                        ev.append(
                            (
                                max(int(np.ceil(leave[s, k])), 1),
                                {
                                    "type": "people",
                                    "code": "resign",
                                    "actor": who[0],
                                    "text": f"{who[1]} hands in notice",
                                    **({"entityId": f"person.{who[0].lower()}"} if existing else {}),
                                },
                            )
                        )
                        if bf_join[s, k] < H:
                            ev.append(
                                (
                                    max(int(np.ceil(bf_join[s, k])), 1),
                                    {
                                        "type": "people",
                                        "code": "backfill_join",
                                        "text": f"A backfill for {who[1]} joins",
                                    },
                                )
                            )
            elif turned_on[s] and turn_month[s] < end:
                ev.append(
                    (
                        int(turn_month[s]),
                        {
                            "type": "vendor",
                            "code": "vendor_turnover",
                            "actor": team.vendor,
                            "entityId": f"org.{team.vendor}",
                            "text": f"{VENDOR_NAMES.get(team.vendor, team.vendor)} rotates the team",
                        },
                    )
                )
            if creep_on[s] and creep_month[s] < end:
                ev.append(
                    (
                        int(creep_month[s]),
                        {
                            "type": "client",
                            "code": "scope_creep",
                            "text": f"The business adds features (+{float(creep_size[s]):.0%} scope)",
                        },
                    )
                )
            over = np.nonzero(budget_fail[j, s])[0]
            if over.size:
                ev.append(
                    (
                        int(over[0]) + 1,
                        {"type": "money", "code": "over_budget", "text": "Monthly spend above the budget"},
                    )
                )
            tgt = float(th["target_months"][s] + layer.get("target_shift", 0.0))
            if live[j, s] > tgt and tgt < H:
                ev.append(
                    (
                        max(int(np.ceil(tgt)), 1),
                        {"type": "milestone", "code": "late", "text": "The go-live target passes"},
                    )
                )
            if live[j, s] <= H:
                ev.append(
                    (
                        max(int(np.ceil(live[j, s])), 1),
                        {"type": "milestone", "code": "go_live", "text": "The platform goes live"},
                    )
                )
            events[j][s] = sorted(ev, key=lambda x: x[0])
    target = th["target_months"] + layer.get("target_shift", 0.0)
    duration = np.minimum(live, H + 6)
    crit = {
        "money": out["money"][:, :, H],
        "time": duration,
        "resources": out["mgmt"][:, :, H],
        "delay_months": np.maximum(duration - target, 0),
    }
    months = np.arange(1, H + 1)
    late = (live[..., None] > target[None, :, None]) & (
        months[None, None, :] >= np.ceil(target - 1e-9)[None, :, None]
    )
    checks = {"burn_over_budget": budget_fail, "go_live_late": late}
    done_ms = {"beta": beta, "load_test_env": np.broadcast_to(env_ready, (F, n)), "go_live": live}
    res = DeliveryResult(
        [t.id for t in teams], out, checks, crit, done_ms, th, dict(crew_) if crew_ else None
    )
    res.events = events
    return res


# ---------------------------------------------------------------------------------------------- event tree
TREE_METRICS = ("money", "progress", "spend", "fte")
VENDOR_NAMES = {"VN-01": "Vistula Code Works", "VN-02": "Kaveri Tech Services"}  # actors.json
MATERIAL = {"resign", "scope_creep", "vendor_turnover", "over_budget", "late"}


def event_rows(events: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    """The events of one path with material flags and keys (the first occurrence of each key)."""
    rows, seen = [], set()
    for month, e in events:
        code = e.get("code", "")
        key = (f"resign:{e['actor']}" if code == "resign" else code) if code in MATERIAL else None
        mat = key is not None and key not in seen
        if mat:
            seen.add(key)
        row = {"week": int(month), "type": e.get("type", "event"), "text": e.get("text", ""), "material": mat}
        if mat:
            row["key"] = key
        for k in ("entityId", "actor"):
            if e.get(k):
                row[k] = e[k]
        rows.append(row)
    return rows


def event_trees(res: DeliveryResult, seed: int, keep: dict[str, Any] | None = None) -> dict[str, Any]:
    """The month event tree of each fork of case A (``unit: "month"``; the split outcome is the money)."""
    from variator.future import events as evt

    labels = {t.id: t.label for t in TEAMS}
    out = {}
    n = res.metrics["money"].shape[1]
    for j, f in enumerate(res.forks):
        rows = [event_rows(res.events[j][s]) for s in range(n)] if res.events else [[] for _ in range(n)]
        out[f] = evt.event_tree(
            option=f, label=labels[f], seed=seed, rows=rows, series={m: res.metrics[m][j] for m in TREE_METRICS},
            unit="month", metrics=TREE_METRICS, outcome_metric="money", keep=keep.setdefault(f, {}) if keep is not None else None,
        )  # fmt: skip
    return out


# ---------------------------------------------------------------------------------------------- cost line
def slowdown_cost(
    habits: Habits,
    world: Any,
    cfg_b: Mapping[str, Any],
    ids: Sequence[str],
    months: float | np.ndarray,
    n: int,
    seed: int,
    *,
    with_path: bool = False,
) -> dict[str, Any]:
    """The other work without the in-house team: the case B engine runs "keep the plan" twice, with the
    team at work and with the team away for ``months`` (one value or per scenario). The differences are
    the cost of the in-house fork; ``with_path`` adds ``_loss`` (n, 13): the cash lost to each month 0..12."""
    from variator.future.company import CompanyModel
    from variator.future.forks import Fork, compile_knobs

    model = CompanyModel(habits, world, cfg_b)
    base = compile_knobs([Fork("status_quo", "Keep the plan")])
    away = compile_knobs([Fork("status_quo", "Keep the plan")])
    away.seconded, away.seconded_until = list(ids), months
    a = model.run(base, n, seed).metrics
    b = model.run(away, n, seed).metrics
    m_med = float(np.median(months))
    end = lambda m, x: float(np.median(x[m][0, :, -1]))  # noqa: E731
    mean_window = lambda m, x: float(np.median(x[m][0, :, 1 : int(np.ceil(m_med)) + 1].mean(-1)))  # noqa: E731
    loss_path = a["cash"][0] - b["cash"][0]
    cash_loss = loss_path[:, -1]
    out = {
        "_loss": loss_path if with_path else None,
        "months_away": round(m_med, 2),
        "engineers_away": list(ids),
        "capacity_left_share": round(mean_window("capacity", b), 3),
        "load": {
            "with_team": round(mean_window("load", a), 3),
            "team_away": round(mean_window("load", b), 3),
        },
        "cash_loss_12m": {
            "p10": round(float(np.quantile(cash_loss, 0.1))),
            "p50": round(float(np.median(cash_loss))),
            "p90": round(float(np.quantile(cash_loss, 0.9))),
        },
        "churn_12m": {"with_team": round(end("churn", a), 4), "team_away": round(end("churn", b), 4)},
        "sla_12m": {"with_team": round(end("sla", a), 6), "team_away": round(end("sla", b), 6)},
        "nps_12m": {"with_team": round(end("nps", a), 2), "team_away": round(end("nps", b), 2)},
        "incidents_12m": {
            "with_team": round(float(np.median(a["incidents"][0, :, 1:].sum(-1))), 1),
            "team_away": round(float(np.median(b["incidents"][0, :, 1:].sum(-1))), 1),
        },
        "new_arr_12m": {"with_team": round(end("new_arr", a)), "team_away": round(end("new_arr", b))},
        "note": "Case B engine, keep the plan: the client work keeps its demand, so the load on the 2 remaining engineers rises; "
        "fewer upsells are won (assumption: win share x min(1, 1/load)^2), fewer deals (roadmap link), more incidents (load elasticity, assumption) and burnout.",
    }
    if not with_path:
        out.pop("_loss")
    return out


def charge_slowdown(res: DeliveryResult, habits: Habits, world: Any, seed: int) -> dict[str, Any] | None:
    """Count the cash lost on the other work inside the in-house money, per scenario (product decision of
    WP18b): money = value - delivery cost - the loss. The team is away until its own go-live in each
    scenario (at most the 12 months of the case B engine); after month 12 the loss stays at its month-12
    value (later effects are not modelled). Returns the breakdown (the cost line) or None without in-house."""
    if "in_house" not in res.forks or not res.crew:
        return None
    j = res.forks.index("in_house")
    n = res.metrics["money"].shape[1]
    until = np.minimum(np.nan_to_num(res.done["go_live"][j], posinf=12.0), 12.0)
    line = slowdown_cost(
        habits, world, case_config("company_best_year"), res.crew["ids"], until, n, seed, with_path=True
    )
    loss = line.pop("_loss")  # (n, 13)
    path = np.concatenate(
        [loss, np.repeat(loss[:, -1:], H + 1 - loss.shape[1], axis=1)], axis=1
    )  # (n, H + 1)
    res.metrics["other_loss"][j] = path
    res.metrics["money"][j] = res.metrics["money"][j] - path
    res.criteria["money"][j] = res.metrics["money"][j, :, H]
    line["charged"] = "inside the in-house money criterion (per scenario); the fan other_loss shows it"
    line["loss_to_month_24"] = {
        k: round(float(np.quantile(path[:, -1], q))) for k, q in (("p10", 0.1), ("p50", 0.5), ("p90", 0.9))
    }
    return line


# ---------------------------------------------------------------------------------------------- export
CAL_ROWS = {
    "burn_over_budget": (
        "Monthly spend above the budget",
        "The team costs more per month than approved: the plan is not acceptable",
        "check",
        "burn_over_budget",
    ),
    "go_live_late": (
        "Go-live late",
        "You miss the promised go-live: penalties and lost trust",
        "milestone",
        "go_live",
    ),
}


def _by(mask: np.ndarray) -> dict[str, Any]:
    m = mask[:, None, :]
    med = risk.median_first_failure(m)[0]
    return {
        "rowType": "risk",
        "pByMonth": [round(float(x), 5) for x in risk.p_failed_by(m)[0]],
        "pEver": round(float(risk.p_ever(m)[0]), 5),
        "medianFirstMonth": None if np.isnan(med) else float(med) + 1,
    }


def calendar(res: DeliveryResult, th: Mapping[str, np.ndarray]) -> list[list[dict[str, Any]]]:
    out = []
    months = np.arange(1, H + 1)
    for j in range(len(res.forks)):
        rows = [
            {
                "id": cid,
                "label": lab,
                "consequence": cons,
                "kind": kind,
                "ref": ref,
                **_by(res.checks[cid][j]),
            }
            for cid, (lab, cons, kind, ref) in CAL_ROWS.items()
        ]
        for ms, var, label, cons in (
            (
                "beta",
                "m_beta",
                "Beta late",
                "Half of the work is not done by the beta date: the client sees less, later",
            ),
            (
                "load_test_env",
                "m_env",
                "Load-test environment late",
                "The load tests start late and can block the go-live",
            ),
        ):
            marker = float(np.median(th[var]))
            late = (res.done[ms][j] > marker)[:, None] & (months >= np.ceil(marker))[None, :]
            rows.append(
                {"id": ms, "label": label, "consequence": cons, "kind": "milestone", "ref": ms, **_by(late)}
            )
        goal = (res.metrics["money"][j, :, H] < th["g_money"])[:, None] & (months == H)[None, :]
        rows.append(
            {
                "id": "money_goal",
                "label": "Money goal missed",
                "consequence": "The project earns less than its goal by month 24",
                "kind": "goal",
                "ref": "money_goal",
                **_by(goal),
            }
        )
        live = res.done["go_live"][j]
        med = float(np.median(live))
        rows.append(
            {
                "id": "go_live_by", "label": "Live by the month", "consequence": "P(the platform is live by month m)", "kind": "progress", "rowType": "progress", "ref": "go_live",
                "pByMonth": [round(float((live <= m).mean()), 5) for m in months], "pEver": round(float((live <= H).mean()), 5),
                "medianFirstMonth": float(np.ceil(med)) if np.isfinite(med) else None,
            }
        )  # fmt: skip
        out.append(rows)
    return out


CRIT_INFO = {
    "money": ("Value over 24 months minus delivery cost", "USD", "higher"),
    "time": ("Months to go live", "months", "lower"),
    "resources": ("Internal management effort", "FTE-months", "lower"),
    "delay_months": ("Months late against the target", "months", "lower"),
}


def build_cube(res: DeliveryResult, anchor: date, seed: int) -> MetricCube:
    F, S, T = res.metrics["money"].shape
    vals = [res.metrics[m[0]] for m in MONTH_METRICS]
    vals += [
        np.broadcast_to(res.criteria[k][..., None], (F, S, T)) for k in CRIT_INFO
    ]  # the same in every period
    infos = [
        MetricInfo(id=m[0], title=m[1], unit=m[2], direction="higher" if m[3] else "lower")
        for m in MONTH_METRICS
    ]
    infos += [MetricInfo(id=f"crit_{k}", title=t, unit=u, direction=d) for k, (t, u, d) in CRIT_INFO.items()]
    pad = np.zeros((S, F, 1), bool)
    return MetricCube(
        run=f"{CASE}:actors",
        options=list(res.forks),
        metrics=infos,
        periods=[anchor.isoformat()] + [month_start(anchor, m).isoformat() for m in range(1, T)],
        values=np.transpose(np.stack(vals, axis=2), (1, 0, 2, 3)),
        checks={
            "burn_over_budget": np.concatenate(
                [pad, np.transpose(res.checks["burn_over_budget"], (1, 0, 2))], -1
            )
        },
        seed=seed,
        grain="month",
        anchor=anchor.isoformat(),
    )


def decide(cube: MetricCube, cfg: Mapping[str, Any]) -> dict[str, Any]:
    from variator.mcda import best_path, rank

    weights = cfg.get("defaults", {}).get("weights", {})
    specs = [
        CriterionSpec(
            metric=f"crit_{c['name']}",
            name=c["name"],
            higher_is_better=bool(c["higher"]),
            weight=float(weights.get(c["name"], c.get("weight", 1.0))),
            unit=c.get("unit"),
        )
        for c in cfg.get("criteria", [])
    ]
    run = export.with_case_ties(to_tensor_run(cube, specs), cfg)
    ra = float(cfg.get("defaults", {}).get("riskAversion", 0.25))
    r = rank(run, risk_aversion=ra)
    path = best_path(run, risk_aversion=ra)
    return {
        "weights": r.weights,
        "riskAversion": ra,
        "options": [
            {
                "option": o.option, "rank": o.rank, "score": round(o.score, 4), "scoreTimesFeasible": round(o.score * o.p_feasible, 4),
                "pBest": round(o.prob_best, 4), "pFeasible": round(o.p_feasible, 4), "robustness": round(o.robustness, 4),
                "stats": {k: {q: float(f"{getattr(v, q):.6g}") for q in ("mean", "p5", "p50", "p95", "cvar")} for k, v in o.stats.items()},
            }
            for o in r.options
        ],
        "tradeoffs": r.tradeoffs,
        "warnings": r.warnings,
        "bestPath": path.model_dump(),
        "pairwise": export.pairwise(run, r.options[0].option, ra, r.weights),
    }  # fmt: skip


LAYERS = {
    "requirements_change": {
        "scope_creep_p": 0.4,
        "scope_creep_scale": 1.5,
        "change_scale": 1.5,
        "rework_scale": 1.15,
    },
    "priorities_shift": {"productivity_scale": 0.9, "rework_scale": 1.2},
    "key_people_leave": {"resign_scale": 1.5, "vendor_turnover_p": 0.3},
    "tight_budget": {"budget_scale": 0.85},
    "fixed_deadline": {"target_shift": -2.0},
    "vendor_rates_rise": {"vendor_rate_scale": 1.1},
}


def rule_checks(res: DeliveryResult, j: int, fork: str, cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The actor equivalents of the flag rules of case A (``late_go_live``, ``change_requests``,
    ``high_rework``) with the thresholds of the case config; a parameter flag is known from the start."""
    th = res.theta
    target = th["target_months"]
    live = np.nan_to_num(res.done["go_live"][j], nan=np.inf)
    months = np.arange(1, H + 1)
    out = []
    late = live > target + 3  # amber: more than 3 months late; red: more than 6
    mask = late[:, None] & (months[None, :] >= np.ceil(target + 3)[:, None])
    med = risk.median_first_failure(mask[:, None, :])[0]
    out.append({"id": "late_go_live", "rule": "late_go_live", "kind": "flag", "pEver": round(float(late.mean()), 5),
                "pRed": round(float((live > target + 6).mean()), 5), "pByMonth": [round(float(x), 5) for x in risk.p_failed_by(mask[:, None, :])[0]],
                "medianFirstMonth": None if np.isnan(med) else float(med) + 1})  # fmt: skip
    if fork == "offshore_fixed":
        cs = th["change_share"]
        out.append({"id": "change_requests", "rule": "change_requests", "kind": "flag", "pEver": round(float((cs > 0.2).mean()), 5),
                    "pRed": round(float((cs > 0.3).mean()), 5), "pByMonth": [round(float((cs > 0.2).mean()), 5)] * H,
                    "medianFirstMonth": 1.0 if (cs > 0.2).any() else None, "note": "a parameter of the quote: known from the start"})  # fmt: skip
    rw = th.get(f"{fork}.rework_share", th["rework_share"])
    out.append({"id": "high_rework", "rule": "high_rework", "kind": "flag", "pEver": round(float((rw > 0.22).mean()), 5),
                "pByMonth": [round(float((rw > 0.22).mean()), 5)] * H, "medianFirstMonth": 1.0 if (rw > 0.22).any() else None,
                "note": "a parameter of the team: known from the start"})  # fmt: skip
    return out


def month_doc(
    res: DeliveryResult,
    params: Mapping[str, Param],
    cfg: Mapping[str, Any],
    *,
    anchor: date,
    seed: int,
    habits: Habits,
) -> dict[str, Any]:
    """``month.json`` of case A (the page schema) with the ranking."""
    cube = build_cube(res, anchor, seed)
    dec = decide(cube, cfg)
    cal = calendar(res, res.theta)
    labels = {t.id: t.label for t in TEAMS}
    n = res.metrics["money"].shape[1]
    return {
        "preview": False,
        "source": "actors",
        "caseId": CASE,
        "engine": export.ENGINE,
        "note": "Month Monte Carlo on actors: the in-house team is 8 engineers of the company with their learned habits; the vendors are cohorts with assumption tags.",
        "provenance": {"engine": export.ENGINE, "seed": seed, "n": n, "habits": {"path": habits.path, "as_of": habits.as_of, "sha256": habits.sha256}},
        "seed": seed,
        "n": n,
        "anchor": anchor.isoformat(),
        "horizonMonths": H,
        "metrics": export.metric_defs(MONTH_METRICS),
        "weekMetrics": [],
        "dayMetrics": [],
        "options": {
            f: {"preview": False, "option": f, "label": labels[f], "fans": {m[0]: export.fan(res.metrics[m[0]][j], m[5]) for m in MONTH_METRICS}, "riskCalendar": cal[j],
                "goals": export.goal_rows(cfg, {"money": res.metrics["money"][j]}, {k: v[j] for k, v in res.done.items()}),
                "milestones": export.milestone_rows(cfg, f, {k: v[j] for k, v in res.done.items()}, H),
                "checks": export.check_rows(cal[j]) + rule_checks(res, j, f, cfg)}
            for j, f in enumerate(res.forks)
        },
        "ranking": dec,
        "crew": {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in (res.crew or {}).items()},
        "parameters": [p.as_row() for p in params.values()],
        "weekTrees": {},
        "dayExamples": {},
        "notes": [],
    }  # fmt: skip


def _summary(doc: Mapping[str, Any], res: DeliveryResult) -> list[Any]:
    j = res.forks.index("in_house") if "in_house" in res.forks else 0
    return [
        (
            o["option"],
            o["rank"],
            o["pBest"],
            o["pFeasible"],
            o["stats"]["money"]["p50"],
            o["stats"]["time"]["p50"],
        )
        for o in doc["ranking"]["options"]
    ] + [("in_house go-live p50", round(float(np.median(res.criteria["time"][j])), 2))]


def run_cli(args: Any) -> int:
    t0 = time.perf_counter()
    habits = Habits.load(args.actors)
    cfg = case_config(CASE)
    anchor = date.fromisoformat(cfg.get("forkDate", "2026-10-26"))
    params = delivery_params(habits, cfg)
    as_of = date.fromisoformat(habits.as_of) if habits.as_of else anchor
    team = crew(habits)
    from variator.future.state import build_world

    world = build_world(habits, args.history)
    t1 = time.perf_counter()
    res = simulate(params, args.n, args.seed, as_of=as_of, crew_=team)
    t2 = time.perf_counter()
    line = charge_slowdown(res, habits, world, args.seed)
    t3 = time.perf_counter()
    doc = month_doc(res, params, cfg, anchor=anchor, seed=args.seed, habits=habits)
    if line is not None:
        doc["costLines"] = {"in_house": {"other_work_slows": line}}
    doc["eventTrees"] = event_trees(res, args.seed)  # the event tree on the month grid of case A (WP18c)
    hire = simulate(params, args.n, args.seed, as_of=as_of, forks=["in_house"], in_house="hire")
    j = res.forks.index("in_house")
    doc["inHouseVersions"] = {
        "existing_engineers": {
            "goLiveP50": round(float(np.median(res.criteria["time"][j])), 2),
            "moneyP50": round(float(np.median(res.criteria["money"][j]))),
        },
        "hire_8": {
            "goLiveP50": round(float(np.median(hire.criteria["time"][0])), 2),
            "moneyP50": round(float(np.median(hire.criteria["money"][0]))),
        },
    }
    layers = {}
    for lid, layer in LAYERS.items():
        r2 = simulate(params, args.n, args.seed, as_of=as_of, layer=layer, crew_=team)
        charge_slowdown(r2, habits, world, args.seed)
        d2 = decide(build_cube(r2, anchor, args.seed), cfg)
        layers[lid] = {
            "winner": d2["options"][0]["option"],
            "ranking": [
                {k: o[k] for k in ("option", "rank", "pBest", "pFeasible")}
                | {"moneyP50": o["stats"]["money"]["p50"]}
                for o in d2["options"]
            ],
            "knobs": layer,
        }
    doc["layers"] = layers
    t4 = time.perf_counter()
    doc["timings"] = {
        "setup_s": round(t1 - t0, 3),
        "month_mc_s": round(t2 - t1, 3),
        "cost_line_s": round(t3 - t2, 3),
        "layers_s": round(t4 - t3, 3),
    }
    doc["provenance"]["code"] = export.git_commit(Path(__file__).resolve().parents[3])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, separators=(",", ":")) + "\n"
    (out / "month.json").write_text(text, encoding="utf-8")
    save_cube(build_cube(res, anchor, args.seed), out / "cube")
    manifest = {"id": f"future:{CASE}", "schema": "variator.future/1", "timings": doc["timings"], "winner": doc["ranking"]["options"][0]["option"],
                "files": {"month.json": hashlib.sha256(text.encode()).hexdigest()}, "cube_format": "npz+json"}  # fmt: skip
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(out),
                "timings": doc["timings"],
                "ranking": _summary(doc, res),
                "inHouseVersions": doc["inHouseVersions"],
                "costLines": doc.get("costLines"),
            },
            indent=1,
        )
    )
    return 0
