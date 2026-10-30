"""Case B (company_best_year): the Monte Carlo on actors with a vectorised period step.

Arrays have the leading axes ``(F, S)``: forks x scenarios (the month Monte Carlo) or 1 x tree nodes
(the week tree). Random draws have the leading axis ``S`` and broadcast over the forks, so all forks
share them (common random numbers). One step covers a period ``[d0, d1)`` in days from T: a calendar
month or a week. Hazards are per year; the calendar items (invoices on the first day of a month,
quarterly invoices, corporation tax) fall into the period that holds their day.

Processes, each from the learned habits (``company_params`` tags each parameter with its source):

- clients: notice (cumulative-hazard inversion, a residual ``Exp(1)`` per client), the end of the
  service at a billing boundary after the contract notice, renewal uplift, upsell (it adds
  implementation work), downgrade, late-payment spells, invoices paid after the client's delay;
- new business: the open pipeline at T and new deals (arrival rate, sales cycle, win rate, value);
- people: resignation (burnout above a load threshold, a declined raise), a backfill after the
  hiring lead time with a ramp-up, growth with a raise request, new hires of a fork;
- reliability: incidents per severity (debt, load, vendor share), downtime, availability, the
  downtime of each client (the churn covariate), NPS;
- money: payroll with the payroll tax, vendor bills (paid a month later), cloud, corporation tax.
"""

# ruff: noqa: E501 - parameter notes and event texts, one per line

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

import numpy as np

from variator.future.forks import Knobs, Shock, case_shocks, var_param
from variator.future.params import (
    Habits,
    Param,
    aic_weight,
    draw_theta,
    exponentials,
    normals,
    pert_ppf,
    uniforms,
)
from variator.future.params import learned as _learned
from variator.future.state import DAYS_PER_MONTH, World

# Values for a parameter that the history before T cannot give (for example no hire before the cut of
# a backtest): generic values or case values, tagged ``assumption:``.
FALLBACK = {
    "churn_p": 0.17, "churn_p_cov": 0.17, "churn_k": 0.0, "upsell_size": 0.25, "upsell_win": 0.7, "downgrade_size": 0.2,
    "late_extra_days": 40.0, "late_spell_days": 90.0, "renewal_uplift": 0.03, "deals_per_month": 2.0,
    "win_rate": 0.25, "deal_value_median": 100_000.0, "deal_value_log_sd": 0.5, "sales_cycle_days": 70.0,
    "growth_rate": 0.2, "raise_ask": 0.1, "raise_grant": 0.5, "hiring_lead_months": 3.0, "ramp_1": 0.4,
    "ramp_2": 0.8, "pace_new": 13.0, "salary_ratio": 1.0, "blocked_share": 0.15, "impl_mult": 1.5,
    "inc_SEV1": 0.25, "inc_SEV2": 1.3, "inc_SEV3": 2.0, "affected_SEV1": 20.0, "affected_SEV2": 6.0,
    "affected_SEV3": 2.0, "availability": 0.9994, "vendor_inc_share": 0.15, "latency_now": 180.0,
    "nps_now": 30.0, "payroll_tax": 0.15, "gpu_share": 0.35,
}  # fmt: skip


def learned(name: str, raw: Mapping[str, Any] | None, **kw: Any) -> Param:
    """``params.learned`` with the fallback of ``FALLBACK`` when the habits have no value."""
    if "default" not in kw and name in FALLBACK:
        kw["default"] = Param.fixed(
            name, FALLBACK[name], "assumption:", note="not in the learned habits at T"
        )
    return _learned(name, raw, **kw)


KEY_SEGMENT_SD = (
    0.7  # assumption: sd of the log hazard of a client segment around the cohort (a factor 2 is 1 sd)
)


def key_churn_factor(habits: Habits, world: World, churn_p: Param) -> Param:
    """Partial pooling of the key clients toward their own history: the hazard of the key segment is the
    cohort hazard x exp(b), b ~ N(0, KEY_SEGMENT_SD^2); the notices of key clients over their exposure in
    the history update b (Poisson likelihood, a grid posterior). The factor exp(b): p1, mode, p99, median."""
    t = world.as_of
    start = world.history_start or date(t.year - 1, t.month, t.day)
    events, exposure = 0, 0.0
    for a in habits.actor_list("client"):
        if not a.get("key"):
            continue
        first = max(date.fromisoformat(a["start"]), start) if a.get("start") else start
        stop = min(date.fromisoformat(a["end"]), t) if a.get("end") else t
        if a.get("notice_at") and date.fromisoformat(a["notice_at"]) < t:
            events += 1
            stop = min(stop, date.fromisoformat(a["notice_at"]))
        exposure += max((stop - first).days, 0) / 365.25
    lam0 = -math.log1p(-churn_p.point)
    b = np.linspace(-4, 3, 2801)
    logp = -0.5 * (b / KEY_SEGMENT_SD) ** 2 + events * b - lam0 * np.exp(b) * exposure
    w = np.exp(logp - logp.max())
    cdf = np.cumsum(w) / w.sum()
    f = np.exp(b)
    q = lambda x: float(f[np.searchsorted(cdf, x)])  # noqa: E731
    return Param(
        "key_churn_factor", q(0.5), q(0.01), float(f[np.argmax(w)]), q(0.99),
        f"data:sim_contract ({events} notices of key clients over {exposure:.1f} client-years), pooled to the cohort",
        "x", f"key-client hazard = cohort hazard x factor; segment sd {KEY_SEGMENT_SD} (assumption)",
    )  # fmt: skip


def poisson_inv(u: np.ndarray, lam: np.ndarray, kmax: int) -> np.ndarray:
    """Inverse CDF of Poisson(lam) at the uniforms ``u``, capped at ``kmax`` (monotone in lam for a fixed u)."""
    u, lam = np.broadcast_arrays(
        np.asarray(u, dtype=np.float64), np.maximum(np.asarray(lam, dtype=np.float64), 0.0)
    )
    p = np.exp(-lam)
    cdf = p.copy()
    n = np.zeros(u.shape, dtype=np.int64)
    for k in range(1, kmax + 1):
        n += u > cdf  # the count is at least k when u is above the CDF at k - 1
        p = p * lam / k
        cdf = cdf + p
    return n


SEVERITIES = ("SEV1", "SEV2", "SEV3")
NEW_SLOTS = 30  # client slots for new business in the horizon
MAX_DEALS = 48  # deal slots (open pipeline + new deals)
DEALS_PER_PERIOD = 4  # most new deals in one period (a month gives about 2)
INC_PER_PERIOD = 14  # most incidents of one severity in one period
HIRE_SLOTS = 5
DAY_NEVER = 1e9
PAY_JITTER_DAYS = 3.0  # history: a bill is paid on its due day or up to 3 days late (weekends), never early
BIG_DEAL = 150_000.0  # a new deal at least this large is a material event when it is won or lost
MAX_INCIDENT_HOURS = (
    72.0  # guard: one incident takes at most 3 days of downtime (no learned value reaches it)
)
YEAR = 365.25
METRICS = (
    "cash", "runway", "churn", "sla", "nps", "arr", "revenue", "cost", "collections", "infra_cost",
    "gpu_hit", "latency", "incidents", "availability", "load", "capacity", "headcount", "new_arr",
    "clients", "debt",
)  # fmt: skip
CHECKS = ("cash_negative", "runway_low", "over_budget", "sla_breach", "churn_high", "burnout")


# ---------------------------------------------------------------------------------------------- parameters
def company_params(habits: Habits, world: World, cfg: Mapping[str, Any]) -> dict[str, Param]:
    """Every parameter of case B with its source tag (``data:``, ``group:``, ``case:``, ``assumption:``)."""
    hb = habits.habits
    co, cb, st = hb.get("company", {}), hb.get("client_base", {}), hb.get("staff", {})
    std = habits.cohort("clients:standard").get("params", {})
    fb = habits.cohort("future:backend").get("params", {})
    models = hb.get("models", {})
    ps: list[Param] = []

    def ratio(name: str, raw: Mapping[str, Any] | None, source_note: str) -> Param:
        if not raw:
            return Param.fixed(name, 1.0, "assumption:", note="no learned range")
        pt = float(raw["point"])
        return Param.pert(
            name,
            raw["low"] / pt,
            raw.get("mode", pt) / pt,
            raw["high"] / pt,
            str(raw["source"]),
            "x",
            source_note,
        )

    # clients
    churn_m = models.get("churn", {})
    selected = churn_m.get("selected", "none") != "none"
    none_base = churn_m.get("base_without_covariate") or cb.get("churn_hazard_base")
    ps.append(
        learned("churn_p", none_base, note="annual probability to give notice (cohort base, no covariate)")
    )
    ps.append(
        learned(
            "churn_p_cov",
            cb.get("churn_hazard_base"),
            note="base of the covariate model (at no downtime when selected)",
        )
    )
    ps.append(
        Param.fixed(
            "churn_cov_selected", float(selected), "data:AIC", "0/1", "the learner selected the covariate"
        )
    )
    ps.append(learned("churn_k", cb.get("churn_incident_k"), note="hazard x (1 + k x downtime effect)"))
    ps.append(key_churn_factor(habits, world, next(p for p in ps if p.name == "churn_p")))
    w = aic_weight(churn_m.get("aic", {}), "downtime_decay_120d")
    ps.append(
        Param.fixed(
            "churn_cov_weight",
            w,
            "data:AIC weight of the downtime covariate",
            "share",
            f"selected model: {churn_m.get('selected', '?')}; per scenario the covariate is on with this probability",
        )
    )
    ps.append(
        Param.fixed(
            "churn_tau_days",
            float(churn_m.get("tau_days", 120.0)),
            "data:sim_contract+incident_impact",
            "days",
        )
    )
    ps.append(ratio("upsell_scale", std.get("upsell_rate"), "scale of the upsell rates of all clients"))
    ps.append(learned("upsell_size", cb.get("upsell_size_share"), lo=0.0))
    ps.append(learned("upsell_win", co.get("upsell_win_share")))
    ps.append(learned("downgrade_size", cb.get("downgrade_size_share"), lo=0.05, hi=0.5, note="n = 1"))
    ps.append(
        ratio(
            "late_scale",
            habits.risk("late_payment_spell").get("base_rate_per_month"),
            "scale of the late-payment rates",
        )
    )
    ps.append(learned("late_extra_days", cb.get("late_extra_days")))
    ps.append(learned("late_spell_days", cb.get("late_spell_days")))
    pd = cb.get("pay_delay_days")
    if pd:
        pt = float(pd["point"])
        ps.append(
            Param.pert(
                "pay_delay_shift",
                pd["low"] - pt,
                pd.get("mode", pt) - pt,
                pd["high"] - pt,
                str(pd["source"]),
                "days",
                "shift of the payment delays of all clients",
            )
        )
    ps.append(learned("renewal_uplift", cb.get("renewal_uplift"), lo=0.0))
    # new business
    ps.append(learned("deals_per_month", co.get("deals_per_month")))
    ps.append(learned("win_rate", co.get("win_rate")))
    ps.append(learned("deal_value_median", co.get("deal_value_median")))
    ps.append(learned("deal_value_log_sd", co.get("deal_value_log_sd")))
    ps.append(learned("sales_cycle_days", co.get("sales_cycle_days")))
    # people
    ps.append(ratio("resign_scale", st.get("resignation_hazard"), "scale of the resignation hazards"))
    ps.append(learned("growth_rate", st.get("growth_rate")))
    ps.append(learned("raise_ask", st.get("raise_ask_share")))
    ps.append(learned("raise_grant", st.get("raise_grant_share")))
    ps.append(
        Param.pert(
            "raise_declined_mult",
            1.5,
            2.0,
            3.0,
            "assumption:",
            "x",
            "resignation hazard after a declined raise, for a year (risk R13 has no size)",
        )
    )
    ps.append(
        Param.fixed(
            "growth_pace", 1.1, "assumption:", "x", "pace after growth in the role (risk R14 has no size)"
        )
    )
    ps.append(learned("hiring_lead_months", fb.get("hiring_lead_months"), note="log-normal median; n = 3"))
    ps.append(
        Param.fixed(
            "hiring_lead_log_sd",
            float(fb.get("hiring_lead_months", {}).get("extra", {}).get("log_sd", 0.25)),
            "group:hires",
            "log-sd",
        )
    )
    ps.append(learned("ramp_1", fb.get("ramp_share_first_20_bdays")))
    ps.append(learned("ramp_2", fb.get("ramp_share_next_20_bdays")))
    ps.append(learned("pace_new", fb.get("pace_throughput_points_per_sprint")))
    ps.append(
        Param.fixed(
            "pace_spread",
            float(st.get("throughput_spread", {}).get("point", 0.34)),
            "group:engineers",
            "log-sd",
        )
    )
    ps.append(learned("salary_ratio", fb.get("salary_vs_predecessor")))
    ps.append(
        Param.fixed(
            "team_load_now",
            float(co.get("team_load_now", {}).get("point", 0.95)),
            "data:sprint_load",
            "ratio",
        )
    )
    sd = float(co.get("team_load", {}).get("extra", {}).get("sd", 0.074))
    ps.append(
        Param.fixed("load_sd", sd / math.sqrt(2), "data:sprint_load", "log-sd", "sprint sd / sqrt(2 sprints)")
    )
    ps.append(var_param(cfg, "burnout_load", 1.05, "03-REQUIREMENTS §8 fix 2: the load after the hires"))
    ps.append(
        replace(
            var_param(cfg, "burnout_attrition", 0.05, "rule burnout: attrition + this share per year"),
            name="burnout_add",
        )
    )
    ps.append(learned("blocked_share", st.get("blocked_time_share")))
    ps.append(
        Param.fixed(
            "demand_client_share",
            0.5,
            "assumption:",
            "share",
            "part of the engineering demand that follows the ARR",
        )
    )
    ps.append(
        learned(
            "impl_mult",
            cb.get("support_mult_after_deal"),
            note="tickets after a deal; used for the engineering work of 3 months after a deal (assumption)",
        )
    )
    # reliability
    for s in SEVERITIES:
        ps.append(learned(f"inc_{s}", co.get(f"incidents_per_month_{s}")))
        # only log_mu and log_sd of the incident downtime: the point, mode and high of the learned mean
        # (and the registry's downtime_hours_per_client) are not read; SEV1 has mode = high = 1.86M hours there
        ex = co.get(f"downtime_hours_{s}", {}).get("extra", {})
        ps.append(
            Param.fixed(
                f"dt_mu_{s}",
                min(float(ex.get("log_mu", 0.0)), math.log(MAX_INCIDENT_HOURS)),
                "data:sim_incident",
                "log-hours",
            )
        )
        ps.append(
            Param.fixed(
                f"dt_sd_{s}",
                min(float(ex.get("log_sd", 0.5)), 1.5),
                "data:sim_incident",
                "log-sd",
                "guard: at most 1.5",
            )
        )
        ps.append(learned(f"affected_{s}", co.get(f"affected_clients_{s}"), lo=1.0))
    ps.append(learned("availability", co.get("availability")))
    ps.append(learned("vendor_inc_share", co.get("incident_vendor_share")))
    el = co.get("incident_load_elasticity", {})
    ps.append(
        Param.pert(
            "load_elasticity",
            0.0,
            0.5,
            1.0,
            "assumption:",
            "elasticity",
            f"learned {el.get('point', float('nan')):.3g} [{el.get('low', float('nan')):.3g}, {el.get('high', float('nan')):.3g}] "
            "is not significant and has the wrong sign",
        )
    )
    ps.append(var_param(cfg, "tech_debt", 0.45))
    ps.append(var_param(cfg, "k_inc_debt", 1.2))
    ps.append(var_param(cfg, "debt_lead", 3.0))
    ps.append(var_param(cfg, "gpu_setup", 2.0))
    r11 = habits.risk("key_person_absence").get("size", {}).get("mttr_multiplier", {})
    ps.append(
        learned(
            "mttr_vacancy",
            next(iter(r11.values()), None),
            lo=1.0,
            default=Param.fixed("mttr_vacancy", 1.0, "assumption:"),
            note="the seat of LW-E12 is empty",
        )
    )
    ps.append(learned("latency_now", co.get("latency_p95_ms_now")))
    ps.append(var_param(cfg, "latency_slo", 250.0))
    ps.append(var_param(cfg, "k_lat_gpu", 0.6))
    ps.append(var_param(cfg, "k_lat_debt", 0.5))
    ps.append(
        Param.fixed("gpu_inc_mult", 2.0, "assumption:", "x", "more vendor incidents per unit of GPU shortage")
    )
    ps.append(learned("nps_now", co.get("nps_now")))
    ps.append(
        Param.pert(
            "nps_k_downtime",
            1.0,
            3.0,
            6.0,
            "assumption:",
            "points per hour",
            "NPS loss per hour of decayed client downtime",
        )
    )
    ps.append(
        Param.pert(
            "nps_k_price",
            12.0,
            40.0,
            75.0,
            "assumption:",
            "points per unit",
            "NPS loss per unit of price rise",
        )
    )
    # price
    ps.append(
        Param.pert(
            "price_elasticity",
            1.5,
            3.5,
            8.0,
            "assumption:",
            "x per unit",
            "relative churn hazard per unit of price rise for 12 months; no price change in the history; "
            "centred on the case link k_churn_price = 0.6",
        )
    )
    ps.append(
        Param.pert(
            "k_win_price",
            0.5,
            1.0,
            2.0,
            "assumption:",
            "x",
            "win-rate loss per unit of price rise (case link k_win_price; price is the reason of 7 of 20 lost deals)",
        )
    )
    # money
    tax = hb.get("regulators", {}).get("TA-UK", {}).get("payroll_tax_rate")
    ps.append(learned("payroll_tax", tax, default=Param.fixed("payroll_tax", 0.15, "assumption:")))
    ps.append(learned("gpu_share", co.get("gpu_share")))
    ps.append(var_param(cfg, "k_infra_gpu", 0.8))
    ps.append(var_param(cfg, "budget", 11_200_000.0))
    ps.append(var_param(cfg, "sla_target", 0.995))
    ps.append(Param.fixed("churn_limit", 0.2, "case:rule churn_high", "share"))
    ps.append(replace(var_param(cfg, "runway_red", 6.0, "rule runway_low"), name="runway_limit"))
    ps.append(var_param(cfg, "runway_cap", 60.0, "03-REQUIREMENTS §8 fix 4"))
    return {p.name: p for p in ps}


# ---------------------------------------------------------------------------------------------- periods
@dataclass(frozen=True)
class Periods:
    """Period bounds in days from T and the calendar items in each period."""

    d0: np.ndarray  # (P,) start day of period p = 1..P at index p - 1
    d1: np.ndarray
    month_bounds: np.ndarray  # day of the first day of month m (m = 0..)
    calendar_month: np.ndarray  # calendar month of month m
    grain: str  # month | week

    @property
    def n(self) -> int:
        return len(self.d0)

    def period_of_day(self, days: np.ndarray) -> np.ndarray:
        """1-based period index of the days (days before period 1 count as period 1)."""
        return np.maximum(np.searchsorted(self.d0, days, side="right"), 1)

    def months_in(self, p: int) -> list[int]:
        """Month indices whose first day lies in period p (1-based)."""
        lo, hi = self.d0[p - 1], self.d1[p - 1]
        return [m for m, d in enumerate(self.month_bounds) if lo <= d < hi]

    def months_elapsed(self, p: int) -> float:
        return float(self.d1[p - 1]) / DAYS_PER_MONTH


def month_periods(world: World, horizon: int, extra: int) -> Periods:
    b = world.month_bounds
    n = horizon + extra
    d0 = np.array([0.0] + [b[m] for m in range(2, n + 1)])
    d1 = np.array([b[m + 1] for m in range(1, n + 1)])
    return Periods(d0, d1, b, world.calendar_month, "month")


def week_periods(world: World, weeks: int, extra: int) -> Periods:
    n = weeks + extra
    d0 = 7.0 * np.arange(n)
    return Periods(d0, d0 + 7.0, world.month_bounds, world.calendar_month, "week")


# ---------------------------------------------------------------------------------------------- engine
@dataclass
class Result:
    """Values per period: ``metrics[name]`` (F, S, P + 1) with period 0 = T; ``checks[id]`` (F, S, P)."""

    forks: list[str]
    periods: Periods
    metrics: dict[str, np.ndarray]
    checks: dict[str, np.ndarray]
    milestones: dict[str, np.ndarray]  # id -> done month (F, S), float; NaN = not this fork
    theta: dict[str, np.ndarray]
    events: list[list[list[dict[str, Any]]]] | None = None  # [period][row] -> events (tree mode)
    notes: list[str] = field(default_factory=list)
    churn_parts: dict[str, np.ndarray] = field(default_factory=dict)  # (F, S) shares of the ARR at T
    noticed_at_t: list[dict[str, Any]] = field(
        default_factory=list
    )  # clients on notice at T (known end dates)


class CompanyModel:
    """Case B on actors. ``run`` makes the month Monte Carlo; ``tree.py`` uses ``init`` and ``step``."""

    def __init__(self, habits: Habits, world: World, cfg: Mapping[str, Any], horizon: int = 12):
        self.habits, self.world, self.cfg, self.horizon = habits, world, cfg, horizon
        self.params = company_params(habits, world, cfg)
        self.shocks: list[Shock] = case_shocks(cfg)
        self.notes: list[str] = list(world.notes)
        c = world.clients
        self.c0 = c.n
        self.n_clients = c.n + NEW_SLOTS
        self.p0 = world.people.n
        self.n_people = self.p0 + HIRE_SLOTS
        std = habits.cohort("clients:standard").get("params", {})

        def pt(d: Mapping[str, Any] | None, default: float) -> float:
            return float(d["point"]) if d and "point" in d else default

        # static attributes of the client rows (existing clients, then new-client slots with the cohort habits)
        k = NEW_SLOTS
        self.cl = {
            "key": np.concatenate([c.key, np.zeros(k, bool)]),
            "quarterly": np.concatenate([c.quarterly, np.zeros(k, bool)]),
            "notice_days": np.concatenate([c.notice_months * DAYS_PER_MONTH, np.full(k, 2 * DAYS_PER_MONTH)]),
            # the individual deviation of each client from the cohort base (partial pooling); the learned
            # covariate multiplier at T is divided out: the engine applies the covariate month by month
            "churn_dev": np.concatenate(
                [
                    -np.log1p(-np.minimum(c.churn_p, 0.99))
                    / c.churn_mult
                    / -math.log1p(-self.params["churn_p_cov"].point),
                    np.ones(k),
                ]
            ),
            "upsell": np.concatenate([c.upsell_rate, np.full(k, pt(std.get("upsell_rate"), 0.13))]),
            "downgrade": np.concatenate([c.downgrade_rate, np.full(k, float(np.median(c.downgrade_rate)))]),
            "late": np.concatenate([c.late_rate, np.full(k, float(np.median(c.late_rate)))]),
            "pay_delay": np.concatenate([c.pay_delay, np.full(k, pt(std.get("pay_delay_days"), 62.0))]),
            "pay_sd": np.concatenate([c.pay_sd, np.full(k, float(np.median(c.pay_sd)))]),
            "nps": np.concatenate([c.nps, np.full(k, pt(std.get("nps_mean_client_level"), 32.0))]),
            "renew_day": np.array(
                [world.month_bounds[m] if m > 0 else DAY_NEVER for m in c.term_end_month] + [DAY_NEVER] * k
            ),
        }
        self.cl_ids = list(c.ids) + [f"new-client-{i + 1}" for i in range(k)]
        self.cl_kb = list(c.kb) + [""] * k
        p = world.people
        self.pp = {
            "engineer": np.concatenate([p.engineer, np.ones(HIRE_SLOTS, bool)]),
            "devops": np.concatenate([p.devops_seat, np.zeros(HIRE_SLOTS, bool)]),
            "slot": np.concatenate([np.zeros(p.n, bool), np.ones(HIRE_SLOTS, bool)]),
            "resign_h": -np.log1p(
                -np.concatenate([p.resign_p, np.full(HIRE_SLOTS, float(np.median(p.resign_p)))])
            ),
        }
        self.pp_ids = list(p.ids) + [f"hire-slot-{i + 1}" for i in range(HIRE_SLOTS)]
        self.pp_kb = list(p.kb) + [""] * HIRE_SLOTS
        self.pp_names = list(p.names) + [f"New engineer {i + 1}" for i in range(HIRE_SLOTS)]
        self.vendor_ids = [v for v in world.vendors if not world.vendors[v]["cloud"]]
        self.cloud_ids = [v for v in world.vendors if world.vendors[v]["cloud"]]
        self.deal_ids = [d["id"] for d in world.pipeline]
        # the week ensemble: no SEV3 events (many, never material) and lost-deal events; the v1 tree keeps its events
        self.ensemble_events = False

    # ------------------------------------------------------------------ draws
    def month_draws(self, seed: int, n: int, n_periods: int) -> MonthDraws:
        return MonthDraws(self, seed, n, n_periods)

    # ------------------------------------------------------------------ init
    def init(
        self, knobs: Knobs, theta: Mapping[str, np.ndarray], first: Mapping[str, np.ndarray], periods: Periods
    ) -> dict:
        """The state at T: arrays (F, S, ...). ``first``: the draws of the start (residuals, open invoices)."""
        F, S = knobs.n, len(next(iter(theta.values())))
        w, c, p = self.world, self.world.clients, self.world.people
        C, P, k = self.n_clients, self.n_people, NEW_SLOTS
        z = np.zeros
        st: dict[str, Any] = {"F": F, "S": S}
        acv = np.concatenate([c.acv0, z(k)])
        start = np.where(c.start_month > 0, w.month_bounds[np.maximum(c.start_month, 0)], -1.0)
        st["acv"] = np.broadcast_to(acv, (F, S, C)).copy()
        st["start_day"] = np.broadcast_to(np.concatenate([start, np.full(k, DAY_NEVER)]), (F, S, C)).copy()
        end = np.where(c.end_month > 0, w.month_bounds[np.maximum(c.end_month, 0)], DAY_NEVER)
        st["end_day"] = np.broadcast_to(np.concatenate([end, np.full(k, DAY_NEVER)]), (F, S, C)).copy()
        st["noticed"] = st["end_day"] < DAY_NEVER
        st["resid_c"] = np.broadcast_to(first["cl_notice_e"], (F, S, C)).copy()
        st["x"] = np.broadcast_to(np.concatenate([c.x0, z(k)]), (F, S, C)).copy()
        st["spell"] = z((F, S, C))
        st["price_done"] = z((F, S, C), bool)
        st["renewed"] = z((F, S, C), bool)
        st["next_slot"] = z((F, S), np.int64)
        # people: incumbents (the staff at T and the hire slots) and one backfill per seat
        st["join"] = np.broadcast_to(
            np.concatenate([np.full(p.n, -1e6), np.full(HIRE_SLOTS, DAY_NEVER)]), (F, S, P)
        ).copy()
        st["leave"] = np.full((F, S, P), DAY_NEVER)
        st["salary"] = np.broadcast_to(np.concatenate([p.salary, z(HIRE_SLOTS)]), (F, S, P)).copy()
        st["pace"] = np.broadcast_to(np.concatenate([p.pace, z(HIRE_SLOTS)]), (F, S, P)).copy()
        st["resid_p"] = np.broadcast_to(first["pp_resign_e"], (F, S, P)).copy()
        st["declined_until"] = np.full((F, S, P), -1.0)
        st["bf_join"] = np.full((F, S, P), DAY_NEVER)
        st["bf_salary"] = z((F, S, P))
        st["bf_pace"] = z((F, S, P))
        st["hired"] = z((F, S), bool)
        st["hire_ready"] = np.full((F, S), np.nan)
        # deals: the open pipeline at T, then new deals
        D = MAX_DEALS
        st["deal_close"] = np.full((F, S, D), DAY_NEVER)
        st["deal_value"] = z((F, S, D))
        st["deal_win_u"] = np.ones((F, S, D))
        for i, d in enumerate(w.pipeline[:D]):
            st["deal_close"][:, :, i] = w.month_bounds[min(d["close_month"], len(w.month_bounds) - 1)] + 14.0
            st["deal_value"][:, :, i] = d["amount"]
            st["deal_win_u"][:, :, i] = first["pipeline_win_u"][:, i]
        st["next_deal"] = np.full((F, S), min(len(w.pipeline), D), np.int64)
        # money: ledgers of receivables and payables by period, cash
        NP = periods.n + 1
        st["recv"] = z((F, S, NP + 1))
        st["payb"] = z((F, S, NP + 1))
        orc = w.open_receivables
        if len(orc["amount"]):
            delay = c.pay_delay[orc["client"]] + c.pay_sd[orc["client"]] * first["open_pay_z"]
            delay = delay + theta["pay_delay_shift"][:, None] + knobs.pay_delay_shift
            pay_day = np.maximum(orc["issue_day"] + delay, 1.0)
            idx = np.minimum(periods.period_of_day(pay_day), NP)  # (S, R)
            per_s = np.zeros((S, NP + 1))
            np.add.at(
                per_s,
                (np.repeat(np.arange(S), idx.shape[1]), idx.ravel()),
                np.broadcast_to(orc["amount"], idx.shape).ravel(),
            )
            st["recv"] += per_s[None]
        for m, amount in enumerate(w.open_payables):
            if amount:
                st["payb"][:, :, min(int(periods.period_of_day(np.array([w.month_bounds[m]]))[0]), NP)] += (
                    amount
                )
        # the bill of the month of T (in arrears, issued on the first day of month 1): only the days before
        # day 0; the step bills the usage from day 0 on (WP19 audit M1)
        for vid in self.vendor_ids:
            vv = w.vendors[vid]
            share = max(-float(w.month_bounds[0]), 0.0) / DAYS_PER_MONTH
            due = w.month_bounds[1] + vv["terms"]
            st["payb"][:, :, min(int(periods.period_of_day(np.array([due]))[0]), NP)] += vv["fee_now"] * share
        st["cash"] = np.full((F, S), w.cash0 * knobs.cash_scale)
        st["cost_ytd"] = z((F, S))
        st["churn_lost"] = z((F, S))
        st["downgrade_lost"] = z((F, S))
        st["new_arr"] = z((F, S))
        st["avail_sum"] = z((F, S))
        st["impl"] = z((F, S, 3))  # ACV won in the last 3 periods of a month (implementation work)
        st["net_hist"] = []  # monthly net outflow (cost - revenue) for the burn rate
        st["shock"] = z((F, S, len(self.shocks)))
        st["shock_on"] = z((F, S, len(self.shocks)), bool)
        st["arr_prev"] = (st["acv"] * self._active(st, 0.0)).sum(-1)
        st["arr0"] = float(w.arr0)
        # engineering capacity and demand at T
        cap0 = self._capacity(st, theta, knobs, 0.0, blocked=False)
        st["cap0"] = cap0
        st["demand0"] = theta["team_load_now"] * cap0
        st["load"] = np.broadcast_to(theta["team_load_now"], (F, S)).copy()
        x_ref = self._x_ref(theta)
        st["x_ref"] = x_ref
        act0 = self._active(st, 0.0) & (st["start_day"] <= 0)
        st["nps_mix0"] = (np.broadcast_to(self.cl["nps"], (F, S, C)) * act0).sum(-1) / np.maximum(
            act0.sum(-1), 1
        )
        st["xbar0"] = (st["x"] * act0).sum(-1) / np.maximum(act0.sum(-1), 1)
        return st

    # ------------------------------------------------------------------ helpers
    def _x_ref(self, th: Mapping[str, np.ndarray]) -> np.ndarray:
        """Steady-state downtime effect of a client at the learned incident rates (the churn reference)."""
        n0 = max(self.c0, 1)
        dt = sum(
            th[f"inc_{s}"]
            * np.minimum(th[f"affected_{s}"] / n0, 1.0)
            * np.exp(th[f"dt_mu_{s}"] + th[f"dt_sd_{s}"] ** 2 / 2)
            for s in SEVERITIES
        )
        decay = np.exp(-DAYS_PER_MONTH / th["churn_tau_days"])
        return dt / (1 - decay)

    @staticmethod
    def _active(st: Mapping[str, Any], day: float | np.ndarray) -> np.ndarray:
        return (st["start_day"] <= day) & (st["end_day"] > day)

    @staticmethod
    def _ramp(th: Mapping[str, np.ndarray], months: np.ndarray) -> np.ndarray:
        r1, r2 = th["ramp_1"][:, None], th["ramp_2"][:, None]
        return np.where(months < 0, 0.0, np.where(months < 1, r1, np.where(months < 2, r2, 1.0)))

    def _capacity(self, st, th, knobs: Knobs, day: float, blocked: bool = True) -> np.ndarray:
        eng = self.pp["engineer"]
        inc = (st["join"] <= day) & (st["leave"] > day)
        m_inc = (day - st["join"]) / DAYS_PER_MONTH
        ramp_inc = np.where(st["join"] < -1e5, 1.0, self._ramp(th, m_inc))
        bf = st["bf_join"] <= day
        ramp_bf = self._ramp(th, (day - st["bf_join"]) / DAYS_PER_MONTH)
        if knobs.seconded and day > 0:
            # people who work on another project (case A in-house team) until a month (per scenario or one
            # for all): their seat gives no capacity here
            until = np.asarray(knobs.seconded_until, dtype=np.float64) * DAYS_PER_MONTH
            away = np.isin(np.array(self.pp_ids), knobs.seconded) & (day < until)[..., None]
            eng = eng & ~away
        cap = ((st["pace"] * ramp_inc * inc + st["bf_pace"] * ramp_bf * bf) * eng).sum(-1)
        if blocked and knobs.blocked_scale != 1.0:
            b = th["blocked_share"]
            cap = cap * (1 - np.minimum(b * knobs.blocked_scale, 0.9)) / (1 - b)
        return cap

    def _payroll_year(self, st, day: float) -> tuple[np.ndarray, np.ndarray]:
        """(salaries per year of the present staff, cost per year of the present new hires)."""
        inc = (st["join"] <= day) & (st["leave"] > day)
        slot = self.pp["slot"]
        sal = (st["salary"] * inc * ~slot).sum(-1) + (st["bf_salary"] * (st["bf_join"] <= day)).sum(-1)
        hires = (st["salary"] * inc * slot).sum(-1)
        return sal, hires

    # ------------------------------------------------------------------ step
    def step(
        self,
        st: dict,
        p: int,
        periods: Periods,
        knobs: Knobs,
        th: Mapping[str, np.ndarray],
        d: Mapping[str, np.ndarray],
        events: list[list[dict]] | None = None,
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
        """One period. ``d``: the draws of the period, leading axis S. Returns (metrics, check masks) (F, S)."""
        F, S = st["F"], st["S"]
        d0, d1 = float(periods.d0[p - 1]), float(periods.d1[p - 1])
        days = d1 - d0
        dty = days / YEAR
        mid = 0.5 * (d0 + d1)
        cl, pp = self.cl, self.pp
        f1 = lambda x: np.asarray(x)[:, None]  # noqa: E731 - fork knob (F,) -> (F, 1)
        f2 = lambda x: np.asarray(x)[:, None, None]  # noqa: E731 - fork knob (F,) -> (F, 1, 1)
        s1 = lambda x: np.asarray(x)[:, None]  # noqa: E731 - scenario value (S,) -> (S, 1)

        # ---- shocks of the case (external events): occur once, persist
        for i, sh in enumerate(self.shocks):
            p_year = 1 - (1 - sh.p) * (1 - knobs.shock_p_union.get(sh.name, 0.0))
            h = -math.log(max(1 - p_year, 1e-9))
            occur = (d["shock_u"][:, i] < 1 - math.exp(-h * dty)) & ~st["shock_on"][:, :, i]
            size = (
                d["shock_size"][:, i]
                if "shock_size" in d
                else pert_ppf(d["shock_size_u"][:, i], sh.size.low, sh.size.mode, sh.size.high)
            )
            size = size * knobs.shock_size_scale.get(sh.name, 1.0)
            st["shock"][:, :, i] = np.where(occur, size, st["shock"][:, :, i])
            st["shock_on"][:, :, i] |= occur
            if events is not None:
                for f, s in zip(*np.nonzero(occur), strict=True):
                    events[f][s].append(
                        {
                            "type": "shock",
                            "code": f"shock:{sh.variable}",
                            "text": f"{sh.label}: size {float(size[s]):.3g}",
                            "id": sh.name,
                            "size": float(size[s]),
                        }
                    )
        # placed events of the page (certain, in every scenario and fork): the larger size wins
        for var, month, size_p in knobs.placed_shocks:
            if d0 <= month * DAYS_PER_MONTH < d1 or (p == 1 and month * DAYS_PER_MONTH < d0):
                for i, sh in enumerate(self.shocks):
                    if sh.variable == var:
                        st["shock"][:, :, i] = np.maximum(st["shock"][:, :, i], size_p)
                        st["shock_on"][:, :, i] = True
        shock = {sh.variable: st["shock"][:, :, i] for i, sh in enumerate(self.shocks)}
        z0 = np.zeros((F, S))
        gpu_shock, comp_cut = shock.get("gpu_shortage", z0), shock.get("competitor_cut", z0)
        rep, macro = shock.get("reputation_hit", z0), shock.get("demand_growth", z0)

        # ---- fork state: debt, GPU reserve, price
        debt_ready = f1(knobs.debt_month) * DAYS_PER_MONTH + th["debt_lead"] * DAYS_PER_MONTH
        debt_on = f1(knobs.debt_month) * DAYS_PER_MONTH <= mid
        debt_years = np.maximum(mid - debt_ready, 0) / YEAR
        debt = th["tech_debt"] * np.maximum(1 - f1(knobs.debt_cut) * debt_years * debt_on, 0.0)
        gpu_ready = (f1(knobs.gpu_month) + th["gpu_setup"]) * DAYS_PER_MONTH <= mid
        reserve_on = f1(knobs.gpu_month) * DAYS_PER_MONTH <= mid
        relief = f1(knobs.gpu_relief) * gpu_ready
        gpu_hit = gpu_shock * (1 - relief)
        price_day = f1(knobs.price_month) * DAYS_PER_MONTH
        price_on = price_day <= mid
        dprice = f1(knobs.price_change)
        dprice3 = f2(knobs.price_change)

        # ---- people: capacity, demand, load
        cap = self._capacity(st, th, knobs, mid)
        arr_ratio = st["arr_prev"] / st["arr0"]
        w = th["demand_client_share"]
        impl = st["impl"].sum(-1) / st["arr0"] * (th["impl_mult"] - 1) * w
        demand = st["demand0"] * knobs.demand_scale * (1 - w + w * arr_ratio) * (1 + impl)
        sig = th["load_sd"]
        load = demand / np.maximum(cap, 1e-6) * np.exp(sig * d["load_z"] - sig**2 / 2)
        debt_share = f1(knobs.debt_share) * debt_on
        roadmap = np.minimum(1.0, (1 - debt_share) / np.maximum(load, 1e-6))
        st["load"] = load

        # resignations (cumulative hazard), growth and raises
        present = (st["join"] <= mid) & (st["leave"] > mid)
        base_p = 1 - np.exp(-pp["resign_h"] * s1(th["resign_scale"]) * knobs.resign_scale)
        burn = (load > th["burnout_load"])[:, :, None] & pp["engineer"]
        p_eff = np.minimum(base_p + burn * s1(th["burnout_add"]), 0.95)
        h = -np.log1p(-p_eff) * np.where(st["declined_until"] > mid, s1(th["raise_declined_mult"]), 1.0)
        st["resid_p"] = st["resid_p"] - h * dty * present
        for pid, month in knobs.placed_quits:
            if pid in self.pp_ids and (
                d0 <= month * DAYS_PER_MONTH < d1 or (p == 1 and month * DAYS_PER_MONTH < d0)
            ):
                st["resid_p"][:, :, self.pp_ids.index(pid)] = -1.0
        quit_ = present & (st["resid_p"] <= 0) & (st["leave"] >= DAY_NEVER)
        notice_day = d0 + d["pp_quit_frac"] * days
        st["leave"] = np.where(quit_, notice_day + DAYS_PER_MONTH, st["leave"])
        lead = th["hiring_lead_months"][:, None] * np.exp(
            th["hiring_lead_log_sd"][:, None] * d["pp_bf_lead_z"]
        )
        lead = (lead + knobs.hiring_lead_shift) * DAYS_PER_MONTH
        st["bf_join"] = np.where(quit_, notice_day + lead, st["bf_join"])
        new_sal = np.where(pp["slot"], st["salary"], st["salary"] * s1(th["salary_ratio"]))
        st["bf_salary"] = np.where(quit_, new_sal, st["bf_salary"])
        bf_pace = th["pace_new"][:, None] * np.exp(
            th["pace_spread"][:, None] * d["pp_pace_z"] - th["pace_spread"][:, None] ** 2 / 2
        )
        st["bf_pace"] = np.where(quit_ & pp["engineer"], bf_pace, st["bf_pace"])
        grow = present & (d["pp_grow_u"] < 1 - np.exp(-s1(th["growth_rate"]) * dty)) & ~pp["slot"]
        st["pace"] = np.where(grow, st["pace"] * s1(th["growth_pace"]), st["pace"])
        grant = grow & (d["pp_grant_u"] < s1(th["raise_grant"]))
        st["salary"] = np.where(grant, st["salary"] * (1 + s1(th["raise_ask"])), st["salary"])
        st["declined_until"] = np.where(grow & ~grant, mid + YEAR, st["declined_until"])
        # new hires of the fork: the requisitions open in the period of the hiring month
        hire_day = f1(knobs.hire_month) * DAYS_PER_MONTH
        open_now = (hire_day >= d0) & (hire_day < d1) & ~st["hired"]
        if open_now.any():
            slots = np.arange(HIRE_SLOTS)
            want = (slots < np.asarray(knobs.hire_count)[:, None])[:, None, :] & open_now[:, :, None]
            lead_h = th["hiring_lead_months"][:, None] * np.exp(
                th["hiring_lead_log_sd"][:, None] * d["hire_lead_z"]
            )
            join = hire_day[:, :, None] + (lead_h + knobs.hiring_lead_shift)[None] * DAYS_PER_MONTH
            sl = slice(self.p0, self.n_people)
            st["join"][:, :, sl] = np.where(want, join, st["join"][:, :, sl])
            pace_h = th["pace_new"][:, None] * np.exp(
                th["pace_spread"][:, None] * d["hire_pace_z"] - th["pace_spread"][:, None] ** 2 / 2
            )
            st["pace"][:, :, sl] = np.where(want, pace_h[None], st["pace"][:, :, sl])
            st["salary"][:, :, sl] = np.where(want, f2(knobs.hire_cost), st["salary"][:, :, sl])
            st["hired"] |= open_now
            last = np.where(want, join, -np.inf).max(-1)
            st["hire_ready"] = np.where(open_now, (last + DAYS_PER_MONTH) / DAYS_PER_MONTH, st["hire_ready"])
        if events is not None:
            self._people_events(events, quit_, st, d0, d1)

        # ---- reliability: incidents, downtime, availability, latency
        load_f = (np.maximum(load, 0.05) / th["team_load_now"]) ** th["load_elasticity"]
        debt_f = (1 + th["k_inc_debt"] * debt) / (1 + th["k_inc_debt"] * th["tech_debt"])
        v = th["vendor_inc_share"]
        vendor_f = (1 - relief) * (1 + th["gpu_inc_mult"] * gpu_shock)
        inc_f = (1 - v) * debt_f * load_f + v * vendor_f
        vacancy = (pp["devops"] & ~((st["join"] <= mid) & (st["leave"] > mid)) & (st["bf_join"] > mid)).any(
            -1
        )
        mttr = np.where(vacancy, th["mttr_vacancy"], 1.0)
        n0 = max(self.c0, 1)
        dt_total = np.zeros((F, S))
        client_dt = np.zeros((F, S, self.n_clients))
        n_inc = np.zeros((F, S))
        for j, sev in enumerate(SEVERITIES):
            lam = th[f"inc_{sev}"] * inc_f * days / DAYS_PER_MONTH
            n = poisson_inv(d["inc_u"][:, j], lam, INC_PER_PERIOD)
            n = np.minimum(n, INC_PER_PERIOD)
            hours = np.exp(s1(th[f"dt_mu_{sev}"]) + s1(th[f"dt_sd_{sev}"]) * d["inc_dt_z"][:, j, :])  # (S, K)
            hours = np.minimum(hours, MAX_INCIDENT_HOURS)
            csum = np.concatenate([np.zeros((S, 1)), np.cumsum(hours, -1)], -1)
            down = (
                np.take_along_axis(np.broadcast_to(csum, (F, S, INC_PER_PERIOD + 1)), n[..., None], -1)[
                    ..., 0
                ]
                * mttr
            )
            a = np.minimum(th[f"affected_{sev}"] / n0, 1.0)
            dt_total += a * down
            hit = d["cl_hit_u"][:, :, j] < 1 - (1 - s1(a)) ** n[..., None]
            client_dt += hit * (down / np.maximum(n, 1))[..., None]
            n_inc += n
            if events is not None and (sev != "SEV3" or not self.ensemble_events):
                for f, s in zip(*np.nonzero(n), strict=True):
                    for _ in range(int(n[f, s])):
                        events[f][s].append(
                            {
                                "type": "incident",
                                "code": "incident",
                                "severity": sev,
                                "text": f"{sev} incident",
                                "hours": float(down[f, s] / n[f, s]),
                            }
                        )
                    if j == 0:
                        for i in np.nonzero(hit[f, s] & self._active(st, mid)[f, s] & self.cl["key"])[0][:3]:
                            events[f][s].append(
                                {
                                    "type": "client",
                                    "code": "sev1_hit",
                                    "actor": self.cl_ids[i],
                                    "text": f"{self.world.clients.names[i]} hit by a SEV1 outage",
                                    "entityId": self.cl_kb[i],
                                }
                            )
        lat = (
            th["latency_now"]
            * (1 + th["k_lat_gpu"] * gpu_hit)
            * (1 + th["k_lat_debt"] * debt)
            / (1 + th["k_lat_debt"] * th["tech_debt"])
        )
        exp_dt = sum(
            th[f"inc_{s}"]
            * np.minimum(th[f"affected_{s}"] / n0, 1.0)
            * np.exp(th[f"dt_mu_{s}"] + th[f"dt_sd_{s}"] ** 2 / 2)
            for s in SEVERITIES
        )
        resid = np.maximum((1 - th["availability"]) - exp_dt / (DAYS_PER_MONTH * 24), 0.0)
        avail = 1 - dt_total / (days * 24) - resid - 0.002 * np.maximum(lat - th["latency_slo"], 0) / 50
        decay = np.exp(-days / s1(th["churn_tau_days"]))
        st["x"] = st["x"] * decay + client_dt

        # ---- clients: price, renewals, notices, upsell, downgrade, late spells
        active = self._active(st, mid)
        open_ = active & ~st["noticed"]
        live_day = np.maximum(price_day[:, :, None] + cl["notice_days"], st["start_day"])
        apply_price = (
            (live_day < d1)
            & ~st["price_done"]
            & (dprice3 > 0)
            & (st["start_day"] < f2(knobs.price_month * DAYS_PER_MONTH))
        )
        st["acv"] = np.where(apply_price, st["acv"] * (1 + dprice3), st["acv"])
        st["price_done"] |= apply_price
        renew = open_ & (cl["renew_day"] >= d0) & (cl["renew_day"] < d1) & ~st["renewed"]
        st["acv"] = np.where(renew, st["acv"] * (1 + s1(th["renewal_uplift"])[None]), st["acv"])
        st["renewed"] |= renew
        # the churn base: the covariate model (with probability = its AIC weight) or the model without it
        h_none = -math.log1p(-self.params["churn_p"].point)
        h_cov = -math.log1p(-self.params["churn_p_cov"].point)
        k = th["churn_k"]
        norm = np.where(th["churn_cov_selected"] > 0, 1.0, 1 + k * st["x_ref"])
        cov_h = h_cov * (1 + s1(k) * st["x"]) / s1(norm)
        base_h = np.where(s1(d["churn_cov_on"]), cov_h, h_none)
        reacts = (
            (st["start_day"] < price_day[:, :, None])
            & (mid >= price_day[:, :, None])
            & (mid < price_day[:, :, None] + YEAR)
        )
        price_f = 1 + reacts * dprice3 * s1(th["price_elasticity"])
        comp_f = 1 + (comp_cut * th["price_elasticity"])[..., None]
        key_mult = self._key_mult(knobs)
        h = cl["churn_dev"] * s1(th["churn_p"] / self.params["churn_p"].point) * base_h * price_f * comp_f
        h = (
            h * np.where(cl["key"], key_mult * s1(th["key_churn_factor"]), 1.0)
            + (-np.log1p(-np.minimum(rep, 0.5)))[..., None]
        )
        st["resid_c"] = st["resid_c"] - h * dty * open_
        for cid, month in knobs.placed_notices:
            if cid in self.cl_ids and (
                d0 <= month * DAYS_PER_MONTH < d1 or (p == 1 and month * DAYS_PER_MONTH < d0)
            ):
                st["resid_c"][:, :, self.cl_ids.index(cid)] = -1.0
        notice = open_ & (st["resid_c"] <= 0)
        nday = d0 + d["cl_notice_frac"] * days + cl["notice_days"]
        b = periods.month_bounds
        mi = np.searchsorted(b, nday)  # first month start on or after the notice end
        mi = np.where(
            cl["quarterly"], mi + (-(periods.calendar_month[np.minimum(mi, len(b) - 1)] - 1) % 3), mi
        )
        end = b[np.minimum(mi, len(b) - 1)]
        st["end_day"] = np.where(notice, end, st["end_day"])
        st["noticed"] |= notice
        lost = (st["acv"] * notice).sum(-1)
        st["churn_lost"] += lost
        up_req = open_ & (d["cl_up_u"] < 1 - np.exp(-cl["upsell"] * s1(th["upsell_scale"]) * dty))
        up_p = th["upsell_win"] * np.minimum(1.0, 1.0 / np.maximum(load, 1e-6)) ** 2
        up_win = up_req & (d["cl_upwin_u"] < up_p[..., None])
        gain = st["acv"] * s1(th["upsell_size"]) * up_win
        dg = open_ & ~up_win & (d["cl_down_u"] < 1 - np.exp(-cl["downgrade"] * dty))
        loss = st["acv"] * s1(th["downgrade_size"]) * dg
        st["churn_lost"] += loss.sum(-1)
        st["downgrade_lost"] += loss.sum(-1)
        start_spell = (
            (st["spell"] <= 0)
            & active
            & (d["cl_late_u"] < 1 - np.exp(-cl["late"] * s1(th["late_scale"]) * dty))
        )
        st["spell"] = np.where(start_spell, s1(th["late_spell_days"]), np.maximum(st["spell"] - days, 0))
        if events is not None:
            self._client_events(events, notice, up_win, gain, dg, start_spell, st)

        # ---- new business: deals close in this period; new deals arrive
        win_f = (
            (1 - np.minimum(th["k_win_price"] * dprice * price_on, 0.9))
            * (1 - comp_cut)
            * (0.8 + 0.2 * roadmap)
        )
        closing = (st["deal_close"] >= d0) & (st["deal_close"] < d1)
        won = closing & (st["deal_win_u"] < (th["win_rate"][:, None] * win_f[..., None]))
        if won.any():
            self._open_slots(st, won, price_on, dprice, d1, events)
        if events is not None and self.ensemble_events:  # lost deals of the pipeline at T and big new deals
            lost = closing & ~won
            for ff, ss, ii in zip(*np.nonzero(lost), strict=True):
                v = float(st["deal_value"][ff, ss, ii])
                if ii < len(self.world.pipeline) or v >= BIG_DEAL:
                    events[ff][ss].append(self._deal_event("deal_lost", int(ii), v))
        lam_d = th["deals_per_month"] * knobs.deals_scale * np.maximum(1 + macro, 0.0) * days / DAYS_PER_MONTH
        n_new = poisson_inv(d["deal_u"], lam_d, DEALS_PER_PERIOD)
        if n_new.any():
            cyc = th["sales_cycle_days"][:, None] * np.exp(0.32 * d["deal_cycle_z"] - 0.32**2 / 2)
            val = th["deal_value_median"][:, None] * np.exp(
                th["deal_value_log_sd"][:, None] * d["deal_value_z"]
            )
            for k in range(DEALS_PER_PERIOD):
                put = (n_new > k) & (st["next_deal"] < MAX_DEALS)
                if not put.any():
                    break
                idx = np.minimum(st["next_deal"], MAX_DEALS - 1)
                ff, ss = np.nonzero(put)
                ii = idx[ff, ss]
                st["deal_close"][ff, ss, ii] = d0 + d["deal_arrive_frac"][ss, k] * days + cyc[ss, k]
                st["deal_value"][ff, ss, ii] = val[ss, k]
                st["deal_win_u"][ff, ss, ii] = d["deal_win_u"][ss, k]
                st["next_deal"] += put

        # ---- money
        active = self._active(st, mid)
        revenue = (st["acv"] * active).sum(-1) * dty
        months_here = periods.months_in(p)
        for m in months_here:
            day = float(periods.month_bounds[m])
            quarter = periods.calendar_month[m] in (1, 4, 7, 10)
            billed = (st["start_day"] <= day) & (st["end_day"] > day)
            amount = st["acv"] * billed * np.where(cl["quarterly"], 0.25 * quarter, 1 / 12)
            delay = cl["pay_delay"] + s1(th["pay_delay_shift"]) + knobs.pay_delay_shift
            delay = delay + cl["pay_sd"] * d["cl_pay_z"] + (st["spell"] > 0) * s1(th["late_extra_days"])
            pidx = np.minimum(periods.period_of_day(day + np.maximum(delay, 0.0)), st["recv"].shape[-1] - 1)
            self._ledger_add(st["recv"], pidx, amount)
        collections = st["recv"][:, :, p].copy()
        st["recv"][:, :, p] = 0.0
        sal, hires = self._payroll_year(st, mid)
        tax = th["payroll_tax"] + np.where(
            mid >= knobs.payroll_tax_month * DAYS_PER_MONTH, knobs.payroll_tax_shift, 0.0
        )
        payroll = (sal * (1 + tax) + hires) * dty
        offshore = shock.get("fx_shock", z0)
        vend = np.zeros((F, S))
        last = st["payb"].shape[-1] - 1
        b = periods.month_bounds
        issue = float(
            b[min(int(np.searchsorted(b, mid, side="right")), len(b) - 1)]
        )  # the 1st after the usage
        for i, vid in enumerate(self.vendor_ids):
            vv = self.world.vendors[vid]
            bill = vv["fee_now"] * (1 + vv["cv"] * d["vendor_z"][:, i]) * (days / DAYS_PER_MONTH)
            bill = bill * (1 + offshore * vv["offshore"])
            vend = vend + bill
            # vendors bill monthly in arrears: the usage of the period is billed on the first day of the next
            # month (at month grain: d1) and paid on the due day or up to 3 days late, never early (WP19 audit)
            due = issue + vv["terms"] + PAY_JITTER_DAYS * d["vendor_pay_u"][:, i]
            idx = np.broadcast_to(np.minimum(periods.period_of_day(due), last)[None, :, None], (F, S, 1))
            self._ledger_add(st["payb"], idx, np.broadcast_to(bill, (F, S))[..., None])
        cloud = sum(self.world.vendors[v]["fee_now"] for v in self.cloud_ids) * (days / DAYS_PER_MONTH)
        cloud = (
            cloud
            * (1 + 0.04 * d["cloud_z"])
            * (1 + shock.get("vendor_price", z0))
            * (1 + th["k_infra_gpu"] * gpu_hit * th["gpu_share"] / 0.35)
        )
        reserve = f1(knobs.gpu_cost) * reserve_on * dty
        regulation = shock.get("regulation_cost", z0) * dty
        cost = payroll + vend + cloud + reserve + regulation
        corp = sum(
            amt
            for m in months_here
            for cm, amt in self.world.corp_tax.items()
            if periods.calendar_month[m] == cm
        )
        out = payroll + cloud + reserve + regulation + st["payb"][:, :, p] + corp
        st["payb"][:, :, p] = 0.0
        st["cash"] = st["cash"] + collections - out
        st["cost_ytd"] += cost
        corp_month = sum(self.world.corp_tax.values()) / 12
        st["net_hist"].append((cost + corp_month * days / DAYS_PER_MONTH - revenue) / (days / DAYS_PER_MONTH))
        window = st["net_hist"][-max(1, round(3 * DAYS_PER_MONTH / days)) :]
        burn = np.maximum(np.mean(window, axis=0), 0.0)
        cap_r = th["runway_cap"]
        runway = np.where(st["cash"] <= 0, 0.0, np.minimum(st["cash"] / np.maximum(burn, 1.0), cap_r))
        runway = np.where(burn <= 0, np.where(st["cash"] > 0, cap_r, 0.0), runway)
        # upsell and won deals add ACV from the next period; their implementation adds work for 3 months
        st["acv"] = st["acv"] + gain - loss
        st["impl"] = (
            np.concatenate([st["impl"][:, :, 1:], gain.sum(-1)[..., None]], -1)
            if periods.grain == "month"
            else self._impl_week(st, gain)
        )
        arr = (st["acv"] * self._active(st, d1)).sum(-1)
        st["arr_prev"] = arr
        st["avail_sum"] += avail * days
        sla = st["avail_sum"] / d1
        act_now = self._active(st, d1)
        mix = (np.broadcast_to(cl["nps"], st["acv"].shape) * act_now).sum(-1) / np.maximum(act_now.sum(-1), 1)
        xbar = (st["x"] * act_now).sum(-1) / np.maximum(act_now.sum(-1), 1)
        nps = (
            th["nps_now"]
            + (mix - st["nps_mix0"])
            - th["nps_k_downtime"] * (xbar - st["xbar0"])
            - th["nps_k_price"] * dprice * price_on
        )
        churn = st["churn_lost"] / st["arr0"]
        headcount = ((st["join"] <= d1) & (st["leave"] > d1)).sum(-1) + (st["bf_join"] <= d1).sum(-1)
        elapsed = d1 / YEAR
        metrics = {
            "cash": st["cash"].copy(), "runway": runway, "churn": churn.copy(), "sla": sla, "nps": nps,
            "arr": arr, "revenue": revenue, "cost": cost, "collections": collections,
            "infra_cost": (cloud + reserve) * YEAR / days, "gpu_hit": gpu_hit, "latency": lat,
            "incidents": n_inc, "availability": avail, "load": load, "capacity": cap / st["cap0"],
            "headcount": headcount.astype(float), "new_arr": st["new_arr"].copy(), "clients": act_now.sum(-1).astype(float),
            "debt": np.broadcast_to(debt, (F, S)).astype(float),
        }  # fmt: skip
        checks = {
            "cash_negative": st["cash"] < 0,
            "runway_low": runway < th["runway_limit"],
            "over_budget": st["cost_ytd"] > th["budget"] * elapsed,
            "sla_breach": avail < th["sla_target"],
            "churn_high": churn > th["churn_limit"],
            "burnout": load > th["burnout_load"],
        }
        return metrics, checks

    # ------------------------------------------------------------------ parts of the step
    def _key_mult(self, knobs: Knobs) -> float:
        """Hazard multiplier of the key clients: P(a key client gives notice in the horizon) = union with q."""
        q = knobs.key_notice_union
        if q <= 0:
            return 1.0
        h_none = -math.log1p(-self.params["churn_p"].point)
        factor = self.params["key_churn_factor"].point
        h = float(self.cl["churn_dev"][self.cl["key"]].sum()) * h_none * factor * self.horizon / 12
        p0 = 1 - math.exp(-h)
        target = 1 - (1 - p0) * (1 - q)
        return -math.log(max(1 - target, 1e-9)) / max(h, 1e-9)

    @staticmethod
    def _ledger_add(ledger: np.ndarray, idx: np.ndarray, amount: np.ndarray) -> None:
        F, S, NP = ledger.shape
        flat = (np.arange(F * S).reshape(F, S, 1) * NP + idx).ravel()
        ledger += np.bincount(
            flat, weights=np.broadcast_to(amount, idx.shape).ravel(), minlength=F * S * NP
        ).reshape(F, S, NP)

    @staticmethod
    def _impl_week(st: dict, gain: np.ndarray) -> np.ndarray:
        """Week grain: the implementation work decays over about 13 weeks (3 months)."""
        return st["impl"] * np.array([1.0, 1.0, 12 / 13]) + np.concatenate(
            [np.zeros(st["impl"].shape[:-1] + (2,)), gain.sum(-1)[..., None] / 13], -1
        )

    def _open_slots(self, st, won, price_on, dprice, d1, events) -> None:
        """Each won deal takes the next free new-client slot of its row (vectorised; deal order in a row)."""
        slot = st["next_slot"][..., None] + np.cumsum(won, axis=-1) - 1
        ok = won & (slot < NEW_SLOTS)
        ff, ss, ii = np.nonzero(ok)
        c = self.c0 + slot[ff, ss, ii]
        on = np.broadcast_to(np.asarray(price_on), (won.shape[0], 1))[ff, 0]
        value = st["deal_value"][ff, ss, ii] * (1 + np.asarray(dprice)[ff, 0] * on)
        b = self.world.month_bounds
        mi = np.minimum(np.searchsorted(b, st["deal_close"][ff, ss, ii], side="right"), len(b) - 1)
        st["acv"][ff, ss, c] = value
        st["start_day"][ff, ss, c] = b[mi]
        st["price_done"][ff, ss, c] = True
        st["next_slot"] += ok.sum(-1)
        np.add.at(st["new_arr"], (ff, ss), value)
        if events is not None:
            for f_, s_, i_, v_ in zip(ff, ss, ii, value, strict=True):
                events[f_][s_].append(self._deal_event("deal_won", int(i_), float(v_)))

    def _deal_event(self, code: str, i: int, value: float) -> dict[str, Any]:
        pipe = i < len(self.world.pipeline)
        name = self.world.pipeline[i]["name"] if pipe else "a new client"
        actor = self.world.pipeline[i]["client"] if pipe else None
        verb = "won" if code == "deal_won" else "lost"
        return {
            "type": "client",
            "code": code,
            "text": f"Deal {verb}: {name}, ACV ${value / 1e3:.0f}k",
            "value": value,
            "pipeline": pipe,
            **({"actor": actor, "entityId": f"org.{actor}"} if actor else {}),
        }

    def _people_events(self, events, quit_, st, d0: float, d1: float) -> None:
        for ff, ss, j in zip(*np.nonzero(quit_), strict=True):
            kb = self.pp_kb[j]
            events[ff][ss].append(
                {
                    "type": "people",
                    "code": "resign",
                    "actor": self.pp_ids[j],
                    "text": f"{self.pp_names[j]} hands in notice",
                    **({"entityId": kb} if kb else {}),
                }
            )
        joins = (st["join"] >= d0) & (st["join"] < d1) & self.pp["slot"]
        for ff, ss, j in zip(*np.nonzero(joins), strict=True):
            events[ff][ss].append(
                {
                    "type": "decision",
                    "code": "hire_join",
                    "actor": self.pp_ids[j],
                    "text": f"{self.pp_names[j]} joins (hire of the fork)",
                }
            )
        bf = (st["bf_join"] >= d0) & (st["bf_join"] < d1)
        for ff, ss, j in zip(*np.nonzero(bf), strict=True):
            events[ff][ss].append(
                {
                    "type": "people",
                    "code": "backfill_join",
                    "actor": self.pp_ids[j],
                    "text": f"A backfill for {self.pp_names[j]} joins",
                }
            )

    def _client_events(self, events, notice, up_win, gain, dg, spell, st) -> None:
        names = self.world.clients.names
        for kind, mask in (("notice", notice), ("upsell", up_win), ("downgrade", dg), ("late", spell)):
            for ff, ss, i in zip(*np.nonzero(mask), strict=True):
                if i >= self.c0:
                    label = "A new client"
                    kb = ""
                else:
                    label, kb = names[i], self.cl_kb[i]
                acv = float(st["acv"][ff, ss, i])
                text = {
                    "notice": f"{label} gives notice: ACV ${acv / 1e3:.0f}k",
                    "upsell": f"{label} buys more: +${float(gain[ff, ss, i]) / 1e3:.0f}k ACV",
                    "downgrade": f"{label} reduces its scope",
                    "late": f"{label} starts to pay late",
                }[kind]
                events[ff][ss].append(
                    {
                        "type": "client" if kind != "late" else "payment",
                        "code": {"late": "late_spell"}.get(kind, kind),
                        "actor": self.cl_ids[i],
                        "acv": acv,
                        "key": bool(self.cl["key"][i]),
                        "text": text,
                        **({"entityId": kb} if kb else {}),
                    }
                )

    # ------------------------------------------------------------------ the month Monte Carlo
    def run(
        self,
        knobs: Knobs,
        n: int,
        seed: int,
        *,
        horizon: int | None = None,
        point: bool = False,
        theta: Mapping[str, np.ndarray] | None = None,
    ) -> Result:
        H = horizon or self.horizon
        periods = month_periods(self.world, H, extra=2)
        th = dict(theta) if theta is not None else draw_theta(self.params, n, seed, point=point)
        draws = self.month_draws(seed, n, H)
        st = self.init(knobs, th, draws.first(), periods)
        F = knobs.n
        out = {m: np.zeros((F, n, H + 1)) for m in METRICS}
        chk = {c: np.zeros((F, n, H), bool) for c in CHECKS}
        out["cash"][:, :, 0] = st["cash"]
        out["arr"][:, :, 0] = st["arr_prev"]
        out["sla"][:, :, 0] = th["availability"]
        out["availability"][:, :, 0] = th["availability"]
        out["nps"][:, :, 0] = th["nps_now"]
        out["load"][:, :, 0] = th["team_load_now"]
        out["capacity"][:, :, 0] = 1.0
        out["runway"][:, :, 0] = th["runway_cap"]
        out["latency"][:, :, 0] = th["latency_now"]
        out["debt"][:, :, 0] = th["tech_debt"]
        out["headcount"][:, :, 0] = self.p0
        out["clients"][:, :, 0] = self._active(st, 0.0).sum(-1)
        out["infra_cost"][:, :, 0] = sum(self.world.vendors[v]["fee_now"] for v in self.cloud_ids) * 12
        for p in range(1, H + 1):
            m, c = self.step(st, p, periods, knobs, th, draws.period(p))
            for k, v in m.items():
                out[k][:, :, p] = v
            for k, v in c.items():
                chk[k][:, :, p - 1] = v
        miles = self._milestones(knobs, th, st, n)
        res = Result(knobs.forks, periods, out, chk, miles, th, notes=self.notes)
        res.churn_parts, res.noticed_at_t = self._churn_parts(st, periods, H)
        return res

    def _churn_parts(
        self, st: Mapping[str, Any], periods: Periods, H: int
    ) -> tuple[dict[str, np.ndarray], list[dict[str, Any]]]:
        """Year-1 churn in parts (QA m2): the clients on notice at T (their end dates are known), the new
        notices after T, the downgrades, and the ARR of all contracts that end within the horizon (the
        definition of the history metric rev.churn: terminations + downgrades)."""
        arr0, end = st["arr0"], float(periods.d1[H - 1])
        c, w = self.world.clients, self.world
        known = [
            {"id": c.ids[i], "name": c.names[i], "arr": float(c.acv0[i]), "endDate": (w.as_of + timedelta(days=int(w.month_bounds[c.end_month[i]]))).isoformat()}
            for i in range(c.n)
            if c.end_month[i] > 0 and c.start_month[i] == 0
        ]  # fmt: skip
        ended = (st["end_day"] <= end) & (st["start_day"] <= end)
        return {
            "new_notices": (st["churn_lost"] - st["downgrade_lost"]) / arr0,
            "downgrades": st["downgrade_lost"] / arr0,
            "terminations": (st["acv"] * ended).sum(-1) / arr0,
        }, known

    def run_weeks(
        self,
        knobs: Knobs,
        n: int,
        seed: int,
        *,
        weeks: int = 52,
        theta: Mapping[str, np.ndarray] | None = None,
    ) -> Result:
        """The week ensemble: n paths per fork, one week a step, with the parameter draws of the month
        Monte Carlo (the same theta, the same notice and resignation thresholds). ``events[f][s]``: the
        events of path s as (week, event) pairs; metrics cash, load, incidents, arr (F, n, weeks + 1)."""
        periods = week_periods(self.world, weeks, extra=30)
        th = dict(theta) if theta is not None else draw_theta(self.params, n, seed)
        draws = self.month_draws(seed, n, weeks)
        st = self.init(knobs, th, draws.first(), periods)
        F = knobs.n
        names = ("cash", "load", "incidents", "arr")
        out = {m: np.zeros((F, n, weeks + 1)) for m in names}
        out["cash"][:, :, 0], out["arr"][:, :, 0] = st["cash"], st["arr_prev"]
        out["load"][:, :, 0] = th["team_load_now"]
        events: list[list[list[tuple[int, dict[str, Any]]]]] = [[[] for _ in range(n)] for _ in range(F)]
        self.ensemble_events = True
        try:
            for p in range(1, weeks + 1):
                ev: list[list[list[dict[str, Any]]]] = [[[] for _ in range(n)] for _ in range(F)]
                m, _ = self.step(st, p, periods, knobs, th, draws.period(p), ev)
                for k in names:
                    out[k][:, :, p] = m[k]
                for f in range(F):
                    for s in range(n):
                        if ev[f][s]:
                            events[f][s].extend((p, e) for e in ev[f][s])
        finally:
            self.ensemble_events = False
        res = Result(knobs.forks, periods, out, {}, {}, th, notes=self.notes)
        res.events = events  # type: ignore[assignment]
        return res

    def _milestones(self, knobs: Knobs, th, st, n: int) -> dict[str, np.ndarray]:
        F = knobs.n
        nan = np.full((F, n), np.nan)
        price_end = float(self.cl["notice_days"][: self.c0].max()) / DAYS_PER_MONTH
        out = {
            "price_live": np.where(
                (knobs.price_change > 0)[:, None], knobs.price_month[:, None] + price_end, nan
            ),
            "debt_done": np.where(
                (knobs.debt_share > 0)[:, None], knobs.debt_month[:, None] + th["debt_lead"][None], nan
            ),
            "hires_ready": np.where((knobs.hire_count > 0)[:, None], st["hire_ready"], nan),
            "gpu_ready": np.where(
                (knobs.gpu_cost > 0)[:, None], knobs.gpu_month[:, None] + th["gpu_setup"][None], nan
            ),
        }
        return out


class MonthDraws:
    """The random numbers of the month Monte Carlo from keyed streams per entity and purpose.

    Scenario s takes row s of each stream, so all forks share the draws (common random numbers) and
    the first n scenarios do not change when n grows.
    """

    def __init__(self, model: CompanyModel, seed: int, n: int, n_periods: int):
        self.m, self.seed, self.n, self.H = model, seed, n, n_periods
        H, s = n_periods, seed
        cl, pp = model.cl_ids, model.pp_ids

        def per(ids: list[str], kind: str, purpose: str, fn=uniforms) -> np.ndarray:
            return np.stack([fn(s, n, kind, i, purpose, shape=(H,)) for i in ids], axis=-1)  # (n, H, N)

        self.a = {
            "cl_up_u": per(cl, "client", "upsell"),
            "cl_upwin_u": per(cl, "client", "upsell-win"),
            "cl_down_u": per(cl, "client", "downgrade"),
            "cl_late_u": per(cl, "client", "late-spell"),
            "cl_pay_z": per(cl, "client", "pay", normals),
            "cl_notice_frac": per(cl, "client", "notice-day"),
            "pp_grow_u": per(pp, "person", "growth"),
            "pp_grant_u": per(pp, "person", "raise"),
            "pp_quit_frac": per(pp, "person", "notice-day"),
            "pp_bf_lead_z": per(pp, "person", "backfill-lead", normals),
            "pp_pace_z": per(pp, "person", "backfill-pace", normals),
            "load_z": normals(s, n, "team", "load", shape=(H,)),
            "deal_u": uniforms(s, n, "deals", "arrivals", shape=(H,)),
            "cloud_z": normals(s, n, "vendor", "cloud", "bill", shape=(H,)),
        }
        self.cl_hit = np.stack(
            [uniforms(s, n, "client", i, "hit", shape=(H, 3)) for i in cl], axis=2
        )  # (n, H, C, 3)
        self.inc_u = np.stack(
            [uniforms(s, n, "incidents", sev, "count", shape=(H,)) for sev in SEVERITIES], -1
        )
        self.inc_z = np.stack(
            [normals(s, n, "incidents", sev, "downtime", shape=(H, INC_PER_PERIOD)) for sev in SEVERITIES], 2
        )
        self.shock_u = (
            np.stack([uniforms(s, n, "shock", sh.name, "occur", shape=(H,)) for sh in model.shocks], -1)
            if model.shocks
            else np.zeros((n, H, 0))
        )
        self.shock_size = (
            np.stack([uniforms(s, n, "shock", sh.name, "size", shape=(H,)) for sh in model.shocks], -1)
            if model.shocks
            else np.zeros((n, H, 0))
        )
        # the sizes once per shock (one PERT inverse per shock, not per month)
        self.shock_size_v = np.stack(
            [pert_ppf(self.shock_size[..., i], sh.size.low, sh.size.mode, sh.size.high) for i, sh in enumerate(model.shocks)], -1
        ) if model.shocks else np.zeros((n, H, 0))  # fmt: skip
        self.vendor_z = np.stack(
            [normals(s, n, "vendor", v, "bill", shape=(H,)) for v in model.vendor_ids], -1
        )
        self.vendor_pay_u = np.stack(
            [uniforms(s, n, "vendor", v, "pay-day", shape=(H,)) for v in model.vendor_ids], -1
        )
        self.deal = {
            k: (fn(s, n, "deals", k, shape=(H, DEALS_PER_PERIOD)))
            for k, fn in (("cycle", normals), ("value", normals), ("win", uniforms), ("arrive", uniforms))
        }
        self.hire_lead = np.stack([normals(s, n, "hire-slot", i + 1, "lead") for i in range(HIRE_SLOTS)], -1)
        self.hire_pace = np.stack([normals(s, n, "hire-slot", i + 1, "pace") for i in range(HIRE_SLOTS)], -1)
        cov_w = model.params["churn_cov_weight"].point
        self.cov_on = uniforms(s, n, "theta", "churn-covariate") < cov_w

    def first(self) -> dict[str, np.ndarray]:
        m, s, n = self.m, self.seed, self.n
        orc = m.world.open_receivables
        return {
            "cl_notice_e": np.stack([exponentials(s, n, "client", i, "notice") for i in m.cl_ids], -1),
            "pp_resign_e": np.stack([exponentials(s, n, "person", i, "resign") for i in m.pp_ids], -1),
            "pipeline_win_u": np.stack([uniforms(s, n, "deal", i, "win") for i in m.deal_ids], -1)
            if m.deal_ids
            else np.zeros((n, 0)),
            "open_pay_z": normals(s, n, "receivables", "open", shape=(len(orc["amount"]),)),
        }

    def period(self, p: int) -> dict[str, np.ndarray]:
        i = p - 1
        d = {k: v[:, i] for k, v in self.a.items()}
        d["cl_hit_u"] = self.cl_hit[:, i]
        d["inc_u"] = self.inc_u[:, i]
        d["inc_dt_z"] = self.inc_z[:, i]
        d["shock_u"] = self.shock_u[:, i]
        d["shock_size_u"] = self.shock_size[:, i]
        d["shock_size"] = self.shock_size_v[:, i]
        d["vendor_z"] = self.vendor_z[:, i]
        d["vendor_pay_u"] = self.vendor_pay_u[:, i]
        d["deal_cycle_z"] = self.deal["cycle"][:, i]
        d["deal_value_z"] = self.deal["value"][:, i]
        d["deal_win_u"] = self.deal["win"][:, i]
        d["deal_arrive_frac"] = self.deal["arrive"][:, i]
        d["hire_lead_z"] = self.hire_lead
        d["hire_pace_z"] = self.hire_pace
        d["churn_cov_on"] = self.cov_on
        return d
