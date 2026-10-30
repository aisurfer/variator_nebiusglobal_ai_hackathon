"""Recovery check: the estimates against the hidden truth of the generator (``truth.json``).

Only this module reads ``truth.json``. Each row: parameter, actor, truth, estimate (point,
low = p1, high = p99), inside?, relative error, whether the history has enough data, and a
note. Kinds:

- ``estimate``: a habit or a cohort value; counts in the coverage;
- ``realised``: the truth realised in the history (``truth.realised``) vs the estimate of the same;
- ``bound``: the estimator gives a one-sided bound (working-day start); reported, not counted;
- ``direct``: the value is a record field or a calibration target; reported, not counted.

The coverage target is 90% of the ``estimate`` rows with enough data (p1-p99 intervals).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from variator.actors.stats import Param

if TYPE_CHECKING:
    from variator.actors.learn import Learned

TARGET = 0.90
MIN_EVENTS = 10  # rates: events in the history for "enough data"
MIN_OBS = 4  # levels: observations
VACATION_WEIGHTS = {6: 0.10, 7: 0.30, 8: 0.40, 9: 0.10, 12: 0.10}  # habits.VACATION_* of the generator

# Explanations of known differences between the estimate and the generator parameter.
NOTES = {
    "blockers": "The generator blocks a ticket on a day with probability blockers / 2.5 "
    "and the spell lasts 1 + Geometric(0.45) working days (mean 3.2), so the realised blocked share "
    "is about 1.3 x the nominal parameter. The estimate measures the realised share.",
    "velocity_level": "Realised throughput is about 0.8 x the planning velocity "
    "(blocked days, carry-over, review and QA waits). The card uses the realised throughput; "
    "the recovery compares the relative pace of each engineer to the engineering cohort.",
    "base_churn": "Realised churn is the base hazard plus the incident effect (k_incident_churn) "
    "and the overdue implementation term. AIC keeps the covariate only when it improves the fit.",
    "incidents_base": "The generator rate is incidents_base x load / 0.95; "
    "the realised mean load is above 0.95.",
    "mttr_h": "The restore time is 1.5 x longer while a key seat is empty; "
    "the estimate is the level with all seats filled when AIC selects the vacancy term, "
    "else the mean over the history.",
    "deal_value": "Client amounts are raw x arr_scale (the ARR calibration of the generator); the truth is "
    "compared in delivered units (raw x arr_scale).",
    "hiring_lead": "Start dates roll to the next Monday, which adds a few days to each lead time.",
    "team_load": "The truth is the planning target; the realised load also carries unplanned work.",
    "support_mult": "The window of extra tickets is approximated as 75 working days after the deal close.",
}


def _rel(est: float, truth: float) -> float | None:
    if truth == 0:
        return None
    return (est - truth) / abs(truth)


class Rows:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(
        self,
        parameter: str,
        actor: str,
        truth: float | None,
        p: Param | None,
        *,
        kind: str = "estimate",
        enough: bool = True,
        note: str = "",
        truth_name: str | None = None,
        estimate_name: str | None = None,
    ) -> None:
        if truth is None or p is None:
            return
        # a direct value is a record field or a rounded calibration target: 0.1% tolerance
        tol = 1e-9 + (1e-3 * abs(truth) if kind == "direct" else 0.0)
        inside = bool(p.low - tol <= truth <= p.high + tol)
        self.rows.append(
            {
                "parameter": parameter,
                "actor": actor,
                "truth": _r(truth),
                "truth_field": truth_name or parameter,
                "estimate": _r(p.point),
                "low": _r(p.low),
                "high": _r(p.high),
                "estimate_field": estimate_name or parameter,
                "inside": inside,
                "rel_error": _r(_rel(p.point, truth)) if _rel(p.point, truth) is not None else None,
                "n": int(p.n),
                "enough_data": bool(enough),
                "kind": kind,
                "source": p.source,
                "note": note,
            }
        )


def _r(x: float | None) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    if x == 0:
        return 0.0
    mag = int(math.floor(math.log10(abs(x))))
    return round(float(x), max(4 - mag, 0))


def load_truth(history_dir: str | Path) -> dict[str, Any] | None:
    p = Path(history_dir) / "truth.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------- check
def recovery(L: Learned, truth: dict[str, Any]) -> dict[str, Any]:
    R = Rows()
    T = truth["spec"]["truth"]
    habits = truth["habits"]
    scale = float(truth.get("calibration", {}).get("arr_scale", 1.0))
    _people(L, R, habits["people"], T)
    _clients(L, R, habits["clients"], T)
    _company(L, R, T, truth, scale)
    return _summary(L, R.rows, truth)


def _people(L: Learned, R: Rows, H: dict[str, dict], T: dict[str, Any]) -> None:
    P, info = L.people.params, L.people.info
    for pid, prm in sorted(P.items()):
        tr = H.get(pid)
        if tr is None:
            continue
        p = prm.get("sick_spells_per_year")
        if p:
            R.add(
                "sick_spells_per_year",
                pid,
                tr["sick_spells"],
                p,
                enough=p.n >= MIN_EVENTS,
                truth_name="habits.people.sick_spells",
                note=""
                if p.n >= MIN_EVENTS
                else f"{p.n} spells: the individual rate is mostly the cohort value",
            )
        p = prm.get("own_tasks_per_day")
        if p and tr["ticket_rate"] > 0:
            R.add(
                "own_tasks_per_day",
                pid,
                tr["ticket_rate"],
                p,
                enough=p.n >= MIN_EVENTS,
                truth_name="habits.people.ticket_rate",
            )
        p = prm.get("first_response_hours")
        if p:
            R.add(
                "first_response_hours",
                pid,
                tr["response_hours"],
                p,
                enough=p.n >= 20,
                truth_name="habits.people.response_hours",
            )
        p = prm.get("resignation_hazard")
        if p and tr["attrition"] > 0:
            R.add(
                "resignation_hazard",
                pid,
                tr["attrition"],
                p,
                enough=False,
                truth_name="habits.people.attrition",
                note="one-time event: at most one event per person",
            )
        p = prm.get("growth_rate")
        if p and tr["growth_rate"] > 0:
            R.add(
                "growth_rate",
                pid,
                tr["growth_rate"],
                p,
                enough=False,
                truth_name="habits.people.growth_rate",
                note="rare event: 0-1 events per person",
            )
        p = prm.get("work_start_minute")
        if p:
            R.add(
                "work_start_minute",
                pid,
                tr["start_min"],
                p,
                kind="bound",
                truth_name="habits.people.start_min",
                note="the first activity is an upper bound of the latent start",
            )
        vm = info.get(pid, {}).get("vacation_month")
        if vm is not None:
            R.rows.append(
                {
                    "parameter": "vacation_month",
                    "actor": pid,
                    "truth": tr["vacation_month"],
                    "truth_field": "habits.people.vacation_month",
                    "estimate": vm,
                    "low": vm,
                    "high": vm,
                    "estimate_field": "people.info.vacation_month",
                    "inside": vm == tr["vacation_month"],
                    "rel_error": None,
                    "n": 1,
                    "enough_data": True,
                    "kind": "estimate",
                    "source": L.ctx.tag("sim_employee_event"),
                    "note": "categorical: the month of the longest holiday",
                }
            )
    _relative_velocity(L, R, H)


def _relative_velocity(L: Learned, R: Rows, H: dict[str, dict]) -> None:
    """Pace of each engineer relative to the engineering cohort (geometric mean), estimate vs truth."""
    P, info = L.people.params, L.people.info
    ids = [pid for pid, prm in P.items() if "throughput_points_per_sprint" in prm and pid in H]
    if len(ids) < 3:
        return
    z = 2.326
    mu, var, tv = {}, {}, {}
    for pid in ids:
        p = P[pid]["throughput_points_per_sprint"]
        mu[pid] = math.log(p.point)
        var[pid] = ((math.log(p.high) - math.log(p.low)) / (2 * z)) ** 2
        g = info[pid].get("growth_events", [])
        # the truth pace over the window: velocity x 1.12 per growth event, weighted by time
        v = H[pid]["velocity"]
        mult = 1.0
        if g:
            a, b = (np.datetime64(x) for x in info[pid]["window"])
            span = max(float((b - a) / np.timedelta64(1, "D")), 1.0)
            k = 0.0
            for d in sorted(g):
                k += float((b - np.datetime64(d)) / np.timedelta64(1, "D")) / span
            mult = 1.12**k
        tv[pid] = math.log(v * mult)
    n = len(ids)
    m_est = sum(mu.values()) / n
    m_tr = sum(tv.values()) / n
    for pid in ids:
        rel = mu[pid] - m_est
        v = var[pid] * (1 - 1 / n) ** 2 + sum(var[j] for j in ids if j != pid) / n**2
        sd = math.sqrt(v)
        p = Param(
            math.exp(rel),
            math.exp(rel - z * sd),
            math.exp(rel),
            math.exp(rel + z * sd),
            P[pid]["throughput_points_per_sprint"].n,
            P[pid]["throughput_points_per_sprint"].source,
            "ratio to the cohort",
            "throughput / geometric mean of the engineers",
        )
        R.add(
            "relative_pace",
            pid,
            math.exp(tv[pid] - m_tr),
            p,
            truth_name="habits.people.velocity (relative)",
            estimate_name="throughput_points_per_sprint (relative)",
            note=NOTES["velocity_level"],
        )


def _clients(L: Learned, R: Rows, H: dict[str, dict], T: dict[str, Any]) -> None:
    C = L.clients.params
    for cid, prm in sorted(C.items()):
        tr = H.get(cid)
        if tr is None:
            continue
        p = prm.get("pay_delay_days")
        if p:
            R.add(
                "pay_delay_days",
                cid,
                tr["pay_delay_mean"],
                p,
                enough=p.n >= MIN_OBS,
                truth_name="habits.clients.pay_delay_mean",
            )
        p = prm.get("pay_delay_sd_days")
        if p:
            R.add(
                "pay_delay_sd_days",
                cid,
                tr["pay_delay_sd"],
                p,
                enough=p.n >= 6,
                truth_name="habits.clients.pay_delay_sd",
            )
        p = prm.get("tickets_per_month")
        if p:
            R.add(
                "tickets_per_month",
                cid,
                tr["support_per_month"],
                p,
                enough=p.n >= MIN_EVENTS,
                truth_name="habits.clients.support_per_month",
            )
        for name, key in (
            ("upsell_rate", "upsell_rate"),
            ("late_payment_rate", "late_payment_rate"),
            ("downgrade_rate", "downgrade_rate"),
        ):
            p = prm.get(name)
            if p:
                R.add(
                    name,
                    cid,
                    tr[key],
                    p,
                    enough=False,
                    truth_name=f"habits.clients.{key}",
                    note="rare event per client: the cohort value",
                )
        p = prm.get("notice_days")
        if p:
            R.add(
                "notice_days",
                cid,
                tr["notice_days"],
                p,
                kind="direct",
                truth_name="habits.clients.notice_days",
                note="a contract field (the generator writes int(notice_days))",
            )


def _company(L: Learned, R: Rows, T: dict[str, Any], truth: dict[str, Any], scale: float) -> None:
    co, pc, cc, coh = L.company.params, L.people.company, L.clients.company, L.clients.cohorts
    g = lambda d, k: d.get(k)  # noqa: E731
    R.add(
        "incidents_per_month",
        "company",
        T["incidents_base"],
        g(co, "incidents_per_month"),
        truth_name="incidents_base",
        note=NOTES["incidents_base"],
    )
    for j, s in enumerate(("SEV1", "SEV2", "SEV3")):
        R.add(
            f"severity_share_{s}",
            "company",
            T["severity_mix"][j],
            g(co, f"severity_share_{s}"),
            truth_name=f"severity_mix[{j}]",
        )
    R.add("incident_deploy_share", "company", T["incident_deploy_share"], g(co, "incident_deploy_share"))
    R.add(
        "mttr_hours", "company", T["mttr_h"], g(co, "mttr_hours"), truth_name="mttr_h", note=NOTES["mttr_h"]
    )
    R.add(
        "deploys_per_week", "company", T["deploy_freq"], g(co, "deploys_per_week"), truth_name="deploy_freq"
    )
    R.add("win_rate", "company", T["win_rate"], g(co, "win_rate"))
    mean_value = T["new_deal_value_median"] * math.exp(T["deal_value_sigma"] ** 2 / 2)
    R.add(
        "deals_per_month",
        "company",
        T["pipeline"] / mean_value / 12,
        g(co, "deals_per_month"),
        truth_name="pipeline / mean deal value / 12",
    )
    R.add(
        "deal_value_median",
        "company",
        T["new_deal_value_median"] * scale,
        g(co, "deal_value_median"),
        truth_name="new_deal_value_median x arr_scale",
        note=NOTES["deal_value"],
    )
    R.add(
        "deal_value_log_sd",
        "company",
        T["deal_value_sigma"],
        g(co, "deal_value_log_sd"),
        truth_name="deal_value_sigma",
    )
    p = g(co, "sales_cycle_days")
    R.add(
        "sales_cycle_days",
        "company",
        sum(T["deal_cycle_days"]) / 2,
        p,
        truth_name="mean of deal_cycle_days",
        enough=p is not None and p.n >= MIN_OBS,
    )
    R.add("upsell_win_share", "company", T["upsell_win"], g(co, "upsell_win_share"), truth_name="upsell_win")
    R.add(
        "upsell_size_share",
        "company",
        sum(T["upsell_share"]) / 2,
        g(cc, "upsell_size_share"),
        truth_name="mean of upsell_share",
    )
    p = g(cc, "downgrade_size_share")
    R.add(
        "downgrade_size_share",
        "company",
        sum(T["downgrade_share"]) / 2,
        p,
        truth_name="mean of downgrade_share",
        enough=p is not None and p.n >= MIN_OBS,
    )
    p = g(co, "hiring_lead_months")
    R.add(
        "hiring_lead_months",
        "company",
        T["hiring_lead"],
        p,
        enough=p is not None and p.n >= MIN_OBS,
        truth_name="hiring_lead",
        note=NOTES["hiring_lead"],
    )
    R.add("team_load", "company", T["team_load"], g(co, "team_load"), note=NOTES["team_load"])
    R.add(
        "blocked_time_share",
        "company",
        T["blockers"],
        g(pc, "blocked_time_share"),
        truth_name="blockers",
        note=NOTES["blockers"],
    )
    R.add(
        "support_escalation_share",
        "company",
        T["support_escalation"],
        g(co, "support_escalation_share"),
        truth_name="support_escalation",
    )
    R.add(
        "support_mult_after_deal",
        "company",
        T["upsell_support_mult"],
        g(cc, "support_mult_after_deal"),
        truth_name="upsell_support_mult",
        note=NOTES["support_mult"],
    )
    R.add("gpu_share", "company", T["gpu_share"], g(co, "gpu_share"))
    R.add(
        "cloud_cost_per_year",
        "company",
        T["infra_base"],
        g(co, "cloud_cost_per_year"),
        truth_name="infra_base",
    )
    R.add(
        "pay_delay_days (cohort median)",
        "clients",
        T["ar_days"],
        g(cc, "pay_delay_days"),
        truth_name="ar_days",
    )
    R.add(
        "pay_delay_spread",
        "clients",
        T["habit_sd"],
        g(cc, "pay_delay_spread"),
        truth_name="habit_sd",
        note="6 key clients have log-sd key_habit_sd = 0.35; the clip at 20 and 120 days",
    )
    R.add(
        "churn_hazard_base",
        "clients",
        T["base_churn"],
        g(cc, "churn_hazard_base"),
        truth_name="base_churn",
        note=NOTES["base_churn"],
    )
    realised = truth.get("realised", {})
    if realised.get("realised_churn_notice_rate") is not None:
        R.add(
            "churn_hazard_realised",
            "clients",
            realised["realised_churn_notice_rate"],
            g(cc, "churn_hazard_realised"),
            kind="realised",
            truth_name="realised.realised_churn_notice_rate",
            note="notices / clients at the start vs the realised annual hazard",
        )
    R.add(
        "resignation_hazard (cohort median)",
        "staff",
        T["attrition"],
        g(pc, "resignation_hazard"),
        truth_name="attrition",
        note="3 resignations in the history (one burnout)",
    )
    R.add(
        "growth_rate (cohort median)",
        "staff",
        T["growth_rate"],
        g(pc, "growth_rate"),
        truth_name="growth_rate",
    )
    R.add(
        "rotation_rate",
        "staff",
        T["rotation_rate"],
        g(pc, "rotation_rate"),
        enough=False,
        note="one rotation in the history",
    )
    R.add(
        "raise_grant_share",
        "staff",
        T["raise_grant"],
        g(pc, "raise_grant_share"),
        truth_name="raise_grant",
        enough=False,
        note="7 salary reviews; declined reviews repeat with a grant probability of 0.5",
    )
    R.add(
        "raise_ask_share",
        "staff",
        0.115,
        g(pc, "raise_ask_share"),
        truth_name="mean of U(0.08, 0.15) (processes.people._grow)",
        enough=False,
        note="4 growth events",
    )
    R.add(
        "sick_spells_per_year (cohort median)",
        "staff",
        T["sick_spells_per_year"],
        g(pc, "sick_spells_per_year"),
        truth_name="sick_spells_per_year",
    )
    for m, w in VACATION_WEIGHTS.items():
        R.add(
            f"vacation_month_share_{m:02d}",
            "staff",
            w,
            g(pc, f"vacation_month_share_{m:02d}"),
            truth_name="habits.VACATION_WEIGHTS",
        )
    R.add(
        "tickets_per_month (standard cohort median)",
        "clients:standard",
        T["support_per_month"],
        g(coh.get("standard", {}), "tickets_per_month"),
        truth_name="support_per_month",
    )
    R.add(
        "tickets_per_month (key cohort median)",
        "clients:key",
        T["support_per_month_key"],
        g(coh.get("key", {}), "tickets_per_month"),
        truth_name="support_per_month_key",
        note="6 key clients",
    )
    R.add(
        "tickets_per_month spread",
        "clients:standard",
        T["habit_sd"],
        g(coh.get("standard", {}), "tickets_per_month_spread"),
        truth_name="habit_sd",
    )
    R.add(
        "upsell_rate (standard cohort median)",
        "clients:standard",
        T["upsell_rate"],
        g(coh.get("standard", {}), "upsell_rate"),
        truth_name="upsell_rate",
    )
    R.add(
        "upsell_rate (key cohort median)",
        "clients:key",
        T["upsell_rate_key"],
        g(coh.get("key", {}), "upsell_rate"),
        truth_name="upsell_rate_key",
        enough=False,
        note="6 key clients, 4 requests",
    )
    R.add(
        "downgrade_rate (cohort median)",
        "clients",
        T["downgrade_rate"],
        g(coh.get("all", {}), "downgrade_rate"),
        truth_name="downgrade_rate",
        enough=False,
        note="one downgrade in the history",
    )
    R.add(
        "late_payment_rate (cohort median)",
        "clients",
        T["late_payment_rate"],
        g(coh.get("all", {}), "late_payment_rate"),
        truth_name="late_payment_rate",
    )
    R.add(
        "late_extra_days",
        "clients",
        sum(T["late_extra_days"]) / 2,
        g(cc, "late_extra_days_observed") or g(cc, "late_extra_days"),
        truth_name="mean of late_extra_days",
    )
    R.add(
        "late_spell_days",
        "clients",
        sum(T["late_spell_days"]) / 2,
        g(cc, "late_spell_days"),
        truth_name="mean of late_spell_days",
    )
    R.add(
        "renewal_uplift",
        "clients",
        T["renewal_uplift_max"] / 2,
        g(cc, "renewal_uplift"),
        truth_name="renewal_uplift_max / 2 (uplift ~ U(0, max))",
    )
    R.add(
        "payroll_tax_rate",
        "TA-UK",
        T["payroll_tax_rate"],
        L.company.tax.get("payroll_tax_rate"),
        kind="direct",
    )
    R.add(
        "employee_deduction_rate",
        "TA-UK",
        T["employee_deduction_rate"],
        L.company.tax.get("employee_deduction_rate"),
        kind="direct",
    )
    R.add(
        "arr_at_t",
        "company",
        T["arr_target"],
        g(co, "arr_at_t"),
        kind="direct",
        note="the generator scales the client amounts so that ARR at T is the target",
    )
    R.add(
        "cash_at_t",
        "company",
        T["cash_target"],
        g(co, "cash_at_t"),
        kind="direct",
        note="the generator sets the opening cash so that the cash at T is the target",
    )
    vend = {v["id"]: v for v in truth["spec"].get("vendors", [])}
    for vid, prm in sorted(L.company.vendors.items()):
        v = vend.get(vid)
        p = prm.get("monthly_fee")
        if v is None or p is None or v.get("kind") == "cloud":
            continue
        noisy = v.get("kind") in ("nearshore", "offshore")
        R.add(
            "vendor_monthly_fee",
            vid,
            float(v["monthly_fee"]),
            p,
            kind="estimate" if noisy else "direct",
            truth_name="spec.vendors.monthly_fee",
            note="the bills carry log-normal noise (sd 0.05)" if noisy else "a fixed fee",
        )
    # v2 datasets: compared only when the truth and the estimate both exist
    _v2(L, R, T, truth)


def _v2(L: Learned, R: Rows, T: dict[str, Any], truth: dict[str, Any]) -> None:
    co = L.company.params
    levels = truth.get("nps_levels") or {}
    answers = L.company.info.get("nps_answers") or []
    if levels and answers:
        per_client: dict[str, list[float]] = {}
        per_survey: dict[str, list[float]] = {}
        for cid, s in answers:
            lv = levels.get(s, {}).get(cid)
            if lv is None:
                continue
            per_client.setdefault(cid, []).append(float(lv["level"]))
            per_survey.setdefault(s, []).append(float(lv["level"]))
        for cid, xs in sorted(per_client.items()):
            p = L.clients.params.get(cid, {}).get("nps_level")
            R.add(
                "nps_level",
                cid,
                float(np.mean(xs)),
                p,
                enough=len(xs) >= MIN_OBS,
                truth_name="nps_levels[survey][client].level (mean over the answered surveys)",
                note="" if len(xs) >= MIN_OBS else f"{len(xs)} answers",
            )
        for s, xs in sorted(per_survey.items()):
            R.add(
                f"nps_survey_{s}",
                "company",
                float(np.mean(xs)),
                co.get(f"nps_survey_{s}"),
                truth_name="mean level of the answering clients (expected NPS of the survey)",
                note="the realised NPS of a survey is a sample of about 100 answers "
                "around the expected value",
            )
    factors = (truth.get("monitoring") or {}).get("latency_factor") or {}
    for svc, f in sorted(factors.items()):
        R.add(
            f"latency_p95_ms_{svc}",
            svc,
            T["latency_base"] * f,
            co.get(f"latency_p95_ms_{svc}"),
            truth_name="latency_base x monitoring.latency_factor",
            note="quiet days; the traffic level of the year (clients 105 -> 95) "
            "and the day of the week shift the mean by about 1-2%",
        )


# ---------------------------------------------------------------------------------------------- summary
def _explain(row: dict[str, Any], n_rows: int) -> str:
    if row["note"]:
        return row["note"]
    side = "below" if row["truth"] < row["low"] else "above"
    return (
        f"truth {side} the interval; with {n_rows} rows at p1-p99, about {0.02 * n_rows:.0f} misses are "
        "expected by chance"
    )


def _summary(L: Learned, rows: list[dict[str, Any]], truth: dict[str, Any]) -> dict[str, Any]:
    est = [r for r in rows if r["kind"] == "estimate" and r["enough_data"]]
    allest = [r for r in rows if r["kind"] == "estimate"]
    cov = sum(r["inside"] for r in est) / len(est) if est else float("nan")
    cov_all = sum(r["inside"] for r in allest) / len(allest) if allest else float("nan")
    misses = [r for r in est if not r["inside"]]
    for r in rows:
        if not r["inside"]:
            r["explanation"] = _explain(r, len(est))
    by_param: dict[str, dict[str, int]] = {}
    for r in est:
        key = r["parameter"].split(" (")[0]
        d = by_param.setdefault(key, {"rows": 0, "inside": 0})
        d["rows"] += 1
        d["inside"] += int(r["inside"])
    worst = sorted(misses, key=lambda r: -abs(r["rel_error"] or 0))[:12]
    other = {
        k: {
            "rows": sum(1 for r in rows if r["kind"] == k),
            "inside": sum(1 for r in rows if r["kind"] == k and r["inside"]),
        }
        for k in ("realised", "bound", "direct")
    }
    return {
        "schema": "variator.actors.recovery/1",
        "as_of": L.as_of.isoformat(),
        "truth": {
            "generator": truth.get("generator"),
            "seed": truth.get("seed"),
            "anchor": truth.get("anchor"),
        },
        "target_coverage": TARGET,
        "summary": {
            "rows": len(rows),
            "estimate_rows_enough_data": len(est),
            "inside": sum(r["inside"] for r in est),
            "coverage": round(cov, 4),
            "meets_target": bool(cov >= TARGET),
            "estimate_rows_all": len(allest),
            "coverage_all_estimate_rows": round(cov_all, 4),
            "by_parameter": by_param,
            "other_kinds": other,
        },
        "worst_misses": [
            {
                k: r[k]
                for k in (
                    "parameter",
                    "actor",
                    "truth",
                    "estimate",
                    "low",
                    "high",
                    "rel_error",
                    "explanation",
                )
            }
            for r in worst
        ],
        "rows": rows,
    }


def shrinkage_table(L: Learned) -> list[dict[str, Any]]:
    """Partial pooling report: the own-data weight of each pooled per-actor parameter."""
    out = []
    for kind, block in (("person", L.people.params), ("client", L.clients.params)):
        for aid, prm in sorted(block.items()):
            for name, p in sorted(prm.items()):
                w = p.extra.get("shrinkage_weight")
                if w is None:
                    continue
                out.append(
                    {
                        "actor": aid,
                        "kind": kind,
                        "param": name,
                        "weight": round(float(w), 3),
                        "n": p.n,
                        "exposure": _r(p.extra.get("exposure")) if p.extra.get("exposure") else None,
                        "own": _r(p.extra.get("own")) if p.extra.get("own") is not None else None,
                        "pooled": _r(p.point),
                        "source": p.source,
                    }
                )
    return out
