"""Habits of the company processes, the vendors and the tax authority (04 §1.4.2).

| Parameter | Estimator |
|---|---|
| incidents per month, deploys per week, deal arrivals | Gamma-Poisson rate per working-day exposure |
| incident load effect | Poisson regression of sprint incidents on the sprint load, AIC vs none |
| severity mix, deploy-caused share, win rate, upsell win | Beta |
| MTTR | log-normal regression on the severity and a seat-vacancy covariate, AIC selects the seat |
| deal value, sales cycle, hiring lead time | log-normal |
| team load, GPU share, cloud cost | Student t |
| vendor fee | median of the bills with an order-statistic interval |
| NPS, latency, availability (v2 datasets) | Dirichlet draws, log-normal, Beta; only when the datasets exist |
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import numpy as np
from scipy import optimize
from scipy import stats as st

from variator.actors import stats
from variator.actors.context import Context
from variator.actors.history import parse_date, parse_ts
from variator.actors.people import _median_ci
from variator.actors.stats import Param

SEVERITIES = ("SEV1", "SEV2", "SEV3")
BDAYS_PER_MONTH = 21.0


@dataclass
class CompanyResult:
    params: dict[str, Param] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    vendors: dict[str, dict[str, Param]] = field(default_factory=dict)
    vendor_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    tax: dict[str, Param] = field(default_factory=dict)
    tax_info: dict[str, Any] = field(default_factory=dict)
    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)
    vacancies: list[dict[str, Any]] = field(default_factory=list)


def learn_company(ctx: Context) -> CompanyResult:
    res = CompanyResult()
    _vacancies(ctx, res)
    _incidents(ctx, res)
    _deploys(ctx, res)
    _sales(ctx, res)
    _hiring(ctx, res)
    _load(ctx, res)
    _support(ctx, res)
    _finance(ctx, res)
    _vendors(ctx, res)
    _tax(ctx, res)
    _optional(ctx, res)
    return res


def _window_bdays(ctx: Context) -> int:
    return len(ctx.h.window_days)


# ---------------------------------------------------------------------------------------------- vacancies
def _vacancies(ctx: Context, res: CompanyResult) -> None:
    """Seats without their holder: from the last day of a leaver to the start of the hire (or T)."""
    h = ctx.h
    for p in h.people.values():
        if p.last_day is None or p.last_day >= h.as_of:
            continue
        hire = next(
            (q for q in h.people.values() if q.seat == p.seat and q.id != p.id and q.start > p.last_day), None
        )
        end = hire.start if hire else h.as_of
        res.vacancies.append(
            {
                "seat": p.seat,
                "leaver": p.id,
                "title": p.title,
                "role": p.role,
                "from": p.last_day + timedelta(days=1),
                "to": end,
                "hire": hire.id if hire else None,
                "senior": p.title.startswith(("Senior", "Lead", "Head", "Chief")),
            }
        )


# ---------------------------------------------------------------------------------------------- incidents
def _incidents(ctx: Context, res: CompanyResult) -> None:
    h = ctx.h
    tag = ctx.tag("sim_incident")
    latest = h.latest("incident")
    inc = [r for r in latest.values()]
    inc.sort(key=lambda r: r["started_at"])
    res.events["incident"] = [{**r, "kb": h.kb("sim", r["id"])} for r in inc]
    n = len(inc)
    bd = _window_bdays(ctx)
    res.params["incidents_per_month"] = stats.rate(n, bd, tag, "incidents per month", per=BDAYS_PER_MONTH)
    res.evidence["incidents_per_month"] = [h.kb("sim", r["id"]) for r in inc[-6:]]
    sev = Counter(r.get("severity") for r in inc)
    for s in SEVERITIES:
        res.params[f"severity_share_{s}"] = stats.share(sev.get(s, 0), n, tag)
        res.params[f"incidents_per_month_{s}"] = stats.rate(
            sev.get(s, 0), bd, tag, "incidents per month", per=BDAYS_PER_MONTH
        )
    dep = sum(1 for r in inc if r.get("cause_kind") == "deploy")
    res.params["incident_deploy_share"] = stats.share(dep, n, tag)
    vendor = [r for r in inc if r.get("cause_kind") == "cloud provider capacity"]
    res.params["incident_vendor_share"] = stats.share(len(vendor), n, tag)
    # MTTR: log restore minutes ~ severity + vacancy of a seat (AIC picks the seat or none)
    resolved = [r for r in inc if r.get("restore_minutes")]
    if len(resolved) >= 5:
        _mttr(ctx, res, resolved, tag)
    # the incident rate and the sprint load (company_model: incidents grow with the load)
    _incident_load(ctx, res, inc, tag)
    # affected clients and downtime per severity
    for s in SEVERITIES:
        rows = [r for r in inc if r.get("severity") == s]
        if len(rows) >= 2:
            res.params[f"affected_clients_{s}"] = stats.normal_mean(
                np.array([float(r.get("affected_count") or 0) for r in rows]), tag, "clients"
            )
            dts = [float(r["downtime_minutes"]) / 60 for r in rows if r.get("downtime_minutes")]
            if len(dts) >= 2:
                res.params[f"downtime_hours_{s}"] = stats.lognormal(
                    np.array(dts), tag, "hours", f"down:{s}"
                ).mean


def _mttr(ctx: Context, res: CompanyResult, rows: list[dict], tag: str) -> None:
    h = ctx.h
    y = np.log(np.array([float(r["restore_minutes"]) / 60.0 for r in rows]))
    days = [h.local(parse_ts(r["started_at"])).date() for r in rows]
    present = [s for s in SEVERITIES if any(r.get("severity") == s for r in rows)]
    ns = len(present)
    sev = np.array([[1.0 if r.get("severity") == s else 0.0 for s in present] for r in rows])
    cands: dict[str, np.ndarray] = {"none": np.zeros(len(rows))}
    responders = {r.get("assignee") for r in rows}
    for v in res.vacancies:
        if v["leaver"] not in responders:
            continue  # only the seats of incident responders can change the restore time
        x = np.array([1.0 if v["from"] <= d < v["to"] else 0.0 for d in days])
        if 0 < x.sum() < len(rows):
            cands[f"vacancy:{v['seat']}"] = x
    fits = {}
    for name, x in cands.items():
        X = sev if name == "none" else np.column_stack([sev, x])
        if np.linalg.matrix_rank(X) < X.shape[1] or X.shape[0] <= X.shape[1] + 1:
            continue  # the vacancy coincides with one severity: not identifiable
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ beta
        nobs, k = X.shape
        s2 = float(resid @ resid / nobs)
        ll = -0.5 * nobs * (math.log(2 * math.pi * s2) + 1)
        fits[name] = (2 * (k + 1) - 2 * ll, X, beta, resid)
    if not fits:
        return
    best = min(fits, key=lambda k: fits[k][0])
    res.evidence["mttr_hours"] = [h.kb("sim", r["id"]) for r in rows[-6:]]
    # the effect of each vacancy, also when AIC does not select it (for the key-person risk)
    effects = {}
    for name, (_, Xc, bc, rc) in fits.items():
        if name == "none":
            continue
        nb, kb = Xc.shape
        s2c = float(rc @ rc / max(nb - kb, 1))
        se = math.sqrt(max(s2c * np.linalg.inv(Xc.T @ Xc)[ns, ns], 1e-12))
        tq = st.t.ppf(stats.P_HI, max(nb - kb, 1))
        effects[name.split(":", 1)[1]] = Param(
            math.exp(bc[ns]),
            math.exp(bc[ns] - tq * se),
            math.exp(bc[ns]),
            math.exp(bc[ns] + tq * se),
            int(cands[name].sum()),
            tag,
            "multiplier of the restore time",
            "log-normal regression (vacancy term)",
        )
    res.info["mttr_vacancy_effects"] = effects
    aic, X, beta, resid = fits[best]
    nobs, k = X.shape
    dof = max(nobs - k, 1)
    s2 = float(resid @ resid / dof)
    cov = s2 * np.linalg.inv(X.T @ X)
    r = stats.rng("mttr")
    sig2 = dof * s2 / r.chisquare(dof, stats.DRAWS)
    z = r.multivariate_normal(np.zeros(k), cov / s2, stats.DRAWS) * np.sqrt(sig2)[:, None]
    coef = beta[None, :] + z
    sev_n = np.array([sum(1 for row in rows if row.get("severity") == s) for s in present], float)
    mix = r.dirichlet(sev_n + 0.5, stats.DRAWS)
    base_mean = np.sum(mix * np.exp(coef[:, :ns] + sig2[:, None] / 2), axis=1)
    res.params["mttr_hours"] = stats.from_draws(
        base_mean,
        nobs,
        tag,
        "hours",
        "log-normal regression on severity" + ("" if best == "none" else " + vacancy"),
        model=best,
        aic={kk: round(v[0], 2) for kk, v in fits.items()},
    )
    res.params["mttr_hours_realised"] = stats.lognormal(np.exp(y), tag, "hours", "mttr_all").mean
    for j, s in enumerate(present):
        res.params[f"restore_hours_{s}"] = stats.from_draws(
            np.exp(coef[:, j] + sig2 / 2), int(sev_n[j]), tag, "hours", "log-normal regression (mean)"
        )
    if best != "none":
        res.params["mttr_vacancy_multiplier"] = stats.from_draws(
            np.exp(coef[:, ns]),
            int(cands[best].sum()),
            tag,
            "multiplier",
            "log-normal regression",
            seat=best.split(":", 1)[1],
        )
    res.info["mttr_model"] = {"selected": best, "aic": {kk: round(v[0], 2) for kk, v in fits.items()}}


def _incident_load(ctx: Context, res: CompanyResult, inc: list[dict], tag: str) -> None:
    h = ctx.h
    loads = {r["sprint"]: r["load"] for r in h.table("sprint_load")}
    if len(loads) < 5:
        return
    cnt, expo, lo = [], [], []
    for n, a, b in h.sprints:
        if n not in loads or b > h.as_of:
            continue
        k = sum(1 for r in inc if a <= h.local(parse_ts(r["started_at"])).date() < b)
        cnt.append(k)
        expo.append(h.bdays(a, b) / BDAYS_PER_MONTH)
        lo.append(loads[n])
    cnt_a, expo_a, lo_a = np.array(cnt, float), np.array(expo), np.array(lo)
    ref = float(np.mean(lo_a))
    xl = np.log(lo_a / ref)

    def nll(th: np.ndarray) -> float:
        lam = np.exp(th[0] + th[1] * xl) * expo_a
        return -float(np.sum(cnt_a * np.log(lam) - lam))

    r0 = optimize.minimize(
        lambda t: nll(np.array([t[0], 0.0])), [math.log(cnt_a.sum() / expo_a.sum())], method="Nelder-Mead"
    )
    r1 = optimize.minimize(nll, [r0.x[0], 0.0], method="Nelder-Mead")
    aic0, aic1 = 2 * 1 + 2 * r0.fun, 2 * 2 + 2 * r1.fun
    hess = stats._num_hessian(nll, r1.x)
    try:
        se = math.sqrt(max(np.linalg.inv(hess)[1, 1], 1e-12))
    except np.linalg.LinAlgError:
        se = 1.0
    z = st.norm.ppf(stats.P_HI)
    b = float(r1.x[1])
    res.params["incident_load_elasticity"] = Param(
        b,
        b - z * se,
        b,
        b + z * se,
        int(cnt_a.sum()),
        tag,
        "elasticity",
        "Poisson regression of sprint incidents on log(load / mean load)",
        {
            "aic_without": round(aic0, 2),
            "aic_with": round(aic1, 2),
            "selected": "load" if aic1 < aic0 else "none",
            "mean_load": ref,
        },
    )
    # the base rate at the target-free reference: incidents per month at the mean load
    res.info["incident_load"] = {"sprints": len(cnt), "mean_load": ref}


# ---------------------------------------------------------------------------------------------- deploys
def _deploys(ctx: Context, res: CompanyResult) -> None:
    h = ctx.h
    tag = ctx.tag("sim_deploy")
    dep = [r for r in h.latest("deploy").values()]
    res.params["deploys_per_week"] = stats.rate(
        len(dep), _window_bdays(ctx), tag, "deploys per 5 working days", per=5.0
    )
    rolled = sum(1 for r in dep if r.get("status") == "rolled_back")
    res.params["deploy_rollback_share"] = stats.share(rolled, len(dep), tag)
    dep.sort(key=lambda r: r["ts"])
    res.evidence["deploys_per_week"] = [h.kb("sim", r["id"]) for r in dep[-5:]]


# ---------------------------------------------------------------------------------------------- sales
def _sales(ctx: Context, res: CompanyResult) -> None:
    h = ctx.h
    tag = ctx.tag("sim_deal")
    deals = h.versions("deal")
    new = {k: v for k, v in deals.items() if v[0].get("deal_type") == "new"}
    up = {k: v for k, v in deals.items() if v[0].get("deal_type") == "upsell"}
    won = [k for k, v in new.items() if v[-1].get("status") == "won"]
    lost = [k for k, v in new.items() if v[-1].get("status") == "lost"]
    res.params["win_rate"] = stats.share(len(won), len(won) + len(lost), tag)
    res.evidence["win_rate"] = [h.kb("sim", k) for k in won]
    created = [k for k, v in new.items() if v[0]["kind"] == "deal_created"]
    res.evidence["deals_per_month"] = [h.kb("sim", k) for k in sorted(created)[-5:]]
    res.params["deals_per_month"] = stats.rate(
        len(created), _window_bdays(ctx), tag, "new deals per month", per=BDAYS_PER_MONTH
    )
    amounts = np.array([float(v[0]["amount"]) for v in new.values() if v[0].get("amount")])
    if amounts.size >= 3:
        fit = stats.lognormal(amounts, tag, "USD", "deal_value")
        res.params["deal_value_median"] = fit.median
        res.params["deal_value_mean"] = fit.mean
        res.params["deal_value_log_sd"] = fit.sigma
    # sales cycle: deals that ran the full cycle (won, or lost after the negotiation)
    cyc = []
    for v in new.values():
        if v[0]["kind"] != "deal_created":
            continue
        last = v[-1]
        kinds = {x["kind"] for x in v}
        if last.get("status") == "won" or (last.get("status") == "lost" and "deal_negotiation" in kinds):
            c0 = h.local(v[0]["ts"]).date()
            c1 = parse_date(last.get("closed_at"))
            if c1:
                cyc.append(float((c1 - c0).days))
    res.info["sales_cycle_deals"] = len(cyc)
    if len(cyc) >= 3:
        fit = stats.lognormal(np.array(cyc), tag, "days", "cycle")
        res.params["sales_cycle_days"] = fit.mean
        res.params["sales_cycle_days_median"] = fit.median
    uw = [k for k, v in up.items() if v[-1].get("status") == "won"]
    ul = [
        k
        for k, v in up.items()
        if v[-1].get("status") == "lost" and v[-1].get("lost_reason") != "the client gave notice"
    ]
    if uw or ul:
        res.params["upsell_win_share"] = stats.share(len(uw), len(uw) + len(ul), tag)
    res.info["deals"] = {
        "new": len(new),
        "new_won": len(won),
        "new_lost": len(lost),
        "new_open": len(new) - len(won) - len(lost),
        "upsell": len(up),
        "upsell_won": len(uw),
    }
    res.events["deal_lost"] = [{**v[-1], "kb": h.kb("sim", k)} for k, v in new.items() if k in lost]
    res.events["deal_won"] = [
        {**v[-1], "kb": h.kb("sim", k)} for k, v in deals.items() if v[-1].get("status") == "won"
    ]
    open_deals = [v[-1] for v in new.values() if v[-1].get("status") not in ("won", "lost")]
    res.info["open_pipeline"] = {
        "deals": len(open_deals),
        "amount": round(sum(float(d.get("amount") or 0) for d in open_deals), 2),
        "ids": [h.kb("sim", d["id"]) for d in open_deals],
    }


# ---------------------------------------------------------------------------------------------- hiring
def _hiring(ctx: Context, res: CompanyResult) -> None:
    h = ctx.h
    tag = ctx.tag("sim_requisition")
    leads, ev = [], []
    for rid, vers in h.versions("requisition").items():
        last = vers[-1]
        a = parse_date(vers[0].get("opened_at"))
        b = parse_date(last.get("filled_at")) if last.get("status") == "filled" else None
        if a and b:
            leads.append((b - a).days / 30.4)
            ev.append(h.kb("sim", rid))
    res.info["requisitions"] = len(h.versions("requisition"))
    if len(leads) >= 2:
        fit = stats.lognormal(np.array(leads), tag, "months", "hiring_lead")
        res.params["hiring_lead_months"] = fit.median
        res.params["hiring_lead_months_mean"] = fit.mean
    elif leads:
        res.params["hiring_lead_months"] = stats.fixed(leads[0], tag, "months", "one requisition")
    res.evidence["hiring_lead_months"] = ev


# ---------------------------------------------------------------------------------------------- load
def _load(ctx: Context, res: CompanyResult) -> None:
    h = ctx.h
    tag = ctx.tag("sprint_load")
    loads = np.array([r["load"] for r in h.table("sprint_load")], float)
    if loads.size >= 3:
        res.params["team_load"] = stats.normal_mean(loads, tag, "assigned / capacity")
        res.evidence["team_load"] = ["dataset:sprint_load.csv"]
        res.params["team_load_now"] = stats.fixed(
            float(loads[-1]), tag, "assigned / capacity", "the last full sprint"
        )
        over = int(np.sum(loads > 1.05))
        res.params["overload_sprint_share"] = stats.share(over, loads.size, tag)
        res.info["load_series"] = [round(float(x), 4) for x in loads]


# ---------------------------------------------------------------------------------------------- support
def _support(ctx: Context, res: CompanyResult) -> None:
    h = ctx.h
    tag = ctx.tag("sim_support_ticket")
    vers = h.versions("support_ticket")
    tickets = [v for v in vers.values() if v[0]["kind"] == "ticket_opened"]
    non_inc = [v for v in tickets if v[0].get("category") != "incident"]
    esc = sum(1 for v in non_inc if any(x["kind"] == "ticket_escalated" for x in v))
    if non_inc:
        res.params["support_escalation_share"] = stats.share(esc, len(non_inc), tag)
    res.params["support_tickets_per_month"] = stats.rate(
        len(tickets), _window_bdays(ctx), tag, "tickets per month", per=BDAYS_PER_MONTH
    )
    csat = [float(v[-1]["csat"]) for v in tickets if v[-1].get("csat") is not None]
    if len(csat) >= 3:
        res.params["csat"] = stats.normal_mean(np.array(csat), tag, "score 1-5")
    res_h = []
    for v in tickets:
        last = v[-1]
        if last.get("status") == "resolved" and last.get("resolved_at"):
            t0, t1 = parse_ts(last["opened_at"]), parse_ts(last["resolved_at"])
            res_h.append(max((t1 - t0).total_seconds() / 3600, 0.05))
    if len(res_h) >= 3:
        res.params["support_resolve_hours"] = stats.lognormal(np.array(res_h), tag, "hours", "resolve").median


# ---------------------------------------------------------------------------------------------- finance
def _finance(ctx: Context, res: CompanyResult) -> None:
    h = ctx.h
    arr = sum(
        c.acv
        for c in h.clients.values()
        if c.status in ("active", "notice") and (c.start or h.start) <= h.as_of
    )
    res.params["arr_at_t"] = stats.fixed(
        arr, ctx.tag("sim_contract"), "USD per year", "sum of the contracts in force"
    )
    top = sorted((c for c in h.clients.values() if c.contract and c.acv > 0), key=lambda c: -c.acv)[:5]
    res.evidence["arr_at_t"] = [h.kb("sim", c.contract) for c in top]
    res.info["clients_at_t"] = sum(1 for c in h.clients.values() if c.status in ("active", "notice"))
    bank = sorted(h.records.get("bank_entry", []), key=lambda r: r["ts"])
    if bank:
        res.params["cash_at_t"] = stats.fixed(
            float(bank[-1]["balance_after"]),
            ctx.tag("sim_bank_entry"),
            "USD",
            "the last bank balance before T",
        )
        res.evidence["cash_at_t"] = [h.kb("sim", bank[-1]["id"])]
    # receivables at T and DSO (receivables / revenue of the last 90 days x 90)
    first: dict[str, dict] = {}
    for r in h.records.get("invoice", []):
        if r.get("direction") == "receivable":
            first.setdefault(r["id"], r)
    status = h.latest("invoice")
    open_amt = sum(
        float(r["amount"])
        for k, r in status.items()
        if r.get("direction") == "receivable" and r.get("status") != "paid"
    )
    rev90 = sum(
        float(r["amount"]) for r in first.values() if (h.as_of - parse_date(r["issued_at"])).days <= 90
    )
    if rev90 > 0:
        res.params["dso_days_at_t"] = stats.fixed(
            open_amt / (rev90 / 90),
            ctx.tag("sim_invoice"),
            "days",
            "receivables / revenue of the last 90 days x 90",
        )
    overdue = sum(
        1 for r in status.values() if r.get("direction") == "receivable" and r.get("status") == "overdue"
    )
    opened = sum(
        1 for r in status.values() if r.get("direction") == "receivable" and r.get("status") != "paid"
    )
    res.info["receivables_at_t"] = {
        "open_invoices": opened,
        "overdue_invoices": overdue,
        "amount": round(open_amt, 2),
    }
    # cloud
    tag = ctx.tag("cloud_cost_daily")
    rows = h.table("cloud_cost_daily").rows
    if len(rows) >= 30:
        tot = np.array([float(r["total"]) for r in rows])
        gpu = np.array([float(r["gpu"]) for r in rows])
        res.params["gpu_share"] = stats.normal_mean(gpu / tot, tag, "share of the cloud cost")
        months: dict[str, float] = defaultdict(float)
        mdays: dict[str, int] = defaultdict(int)
        for r, x in zip(rows, tot, strict=True):
            key = r["date"].isoformat()[:7]
            months[key] += x
            mdays[key] += 1
        full = [months[k] for k in sorted(months) if mdays[k] >= 28]
        if len(full) >= 3:
            res.params["cloud_cost_per_year"] = stats.normal_mean(np.array(full) * 12, tag, "USD per year")
        last30 = tot[-30:].sum() * 365 / 30
        res.params["cloud_cost_per_year_now"] = stats.fixed(
            last30, tag, "USD per year", "the last 30 days x 365 / 30"
        )
        t = np.arange(tot.size) / 365.0
        slope, icpt = np.polyfit(t, np.log(tot), 1)
        resid = np.log(tot) - (icpt + slope * t)
        se = math.sqrt(float(resid @ resid) / max(tot.size - 2, 1) / float(np.sum((t - t.mean()) ** 2)))
        z = st.norm.ppf(stats.P_HI)
        g = lambda b: math.exp(b) - 1  # noqa: E731
        res.params["cloud_cost_growth_per_year"] = Param(
            g(slope),
            g(slope - z * se),
            g(slope),
            g(slope + z * se),
            int(tot.size),
            tag,
            "share per year",
            "log-linear trend",
        )


# ---------------------------------------------------------------------------------------------- vendors
def _vendors(ctx: Context, res: CompanyResult) -> None:
    h = ctx.h
    tag = ctx.tag("sim_invoice")
    contracts = {c["vendor"]: c for c in h.latest("contract").values() if c.get("contract_type") == "vendor"}
    bills: dict[str, list[dict]] = defaultdict(list)
    for r in h.latest("invoice").values():
        if r.get("direction") == "payable" and r.get("vendor"):
            bills[r["vendor"]].append(r)
    cloud_bills = sorted(h.latest("cloud_bill").values(), key=lambda r: r["period"])
    for vid, c in sorted(contracts.items()):
        res.vendors[vid] = {}
        items = sorted(bills.get(vid, []), key=lambda r: r["period"])
        info = {
            "name": c.get("vendor_name"),
            "contract": h.kb("sim", c["id"]),
            "annual_amount": c.get("amount"),
            "payment_terms_days": c.get("payment_terms_days"),
            "bills": len(items),
            "title": c.get("title"),
        }
        if vid == "VN-03" or (c.get("title") or "").lower().find("cloud") >= 0:
            items = [{"amount": r["amount"], "period": r["period"], "id": r["id"]} for r in cloud_bills]
            info["bills"] = len(items)
            info["kind"] = "cloud"
        amounts = np.array([float(r["amount"]) for r in items])
        if amounts.size >= 3:
            med = float(np.median(amounts))
            lo, hi = _median_ci(amounts)
            res.vendors[vid]["monthly_fee"] = Param(
                med,
                lo,
                med,
                hi,
                int(amounts.size),
                tag,
                "USD per month",
                "median of the bills (order-statistic interval)",
                {"cv": float(np.std(amounts) / max(np.mean(amounts), 1e-9))},
            )
            res.vendors[vid]["monthly_fee_now"] = stats.fixed(
                float(amounts[-1]), tag, "USD per month", "the last bill"
            )
            if amounts.size >= 4:
                t = np.arange(amounts.size) / 12.0
                slope = float(np.polyfit(t, np.log(amounts), 1)[0])
                info["growth_per_year"] = round(math.exp(slope) - 1, 4)
        info["evidence"] = [h.kb("sim", r["id"]) for r in items[-3:]]
        res.vendor_info[vid] = info
    caps = [le for le in h.records.get("life_event", []) if le.get("event_type") == "vendor_capacity"]
    res.events["vendor_capacity"] = [{**le, "kb": h.kb("sim", le["id"])} for le in caps]
    if caps:
        res.params["vendor_capacity_events_per_month"] = stats.rate(
            len(caps), _window_bdays(ctx), ctx.tag("sim_life_event"), "events per month", per=BDAYS_PER_MONTH
        )


# ---------------------------------------------------------------------------------------------- tax authority
def _tax(ctx: Context, res: CompanyResult) -> None:
    h = ctx.h
    tag = ctx.tag("sim_payroll")
    pay = [r for r in h.latest("payroll").values() if r.get("amount")]
    res.tax_info["payroll_evidence"] = [h.kb("sim", r["id"]) for r in sorted(pay, key=lambda r: r["id"])[-3:]]
    gross = sum(float(r["amount"]) for r in pay)
    if gross > 0:
        res.tax["payroll_tax_rate"] = stats.fixed(
            sum(float(r.get("employer_tax") or 0) for r in pay) / gross,
            tag,
            "share of gross pay",
            "employer tax / gross pay",
        )
        res.tax["employee_deduction_rate"] = stats.fixed(
            sum(float(r.get("deductions") or 0) for r in pay) / gross,
            tag,
            "share of gross pay",
            "deductions / gross pay",
        )
    taxes = sorted(h.latest("tax_payment").values(), key=lambda r: r.get("due_date") or "")
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for r in taxes:
        by_kind[r.get("tax", "tax")].append(r)
    res.tax_info["schedule"] = {}
    for kind, rows in by_kind.items():
        amts = np.array([float(r["amount"]) for r in rows])
        late = sum(1 for r in rows if r.get("paid_at") and r.get("due_date") and r["paid_at"] > r["due_date"])
        res.tax_info["schedule"][kind] = {
            "payments": len(rows),
            "median_amount": round(float(np.median(amts)), 2) if amts.size else None,
            "late_payments": late,
            "evidence": [h.kb("sim", r["id"]) for r in rows[-3:]],
            "authority": rows[0].get("authority_name"),
        }
    res.tax_info["authority"] = taxes[0].get("authority") if taxes else None
    res.tax_info["authority_name"] = taxes[0].get("authority_name") if taxes else None


# ------------------------------------------------------------------------------------- v2: NPS and monitoring
def _first_key(row: dict, *names: str, contains: str | None = None) -> str | None:
    for n in names:
        if n in row:
            return n
    if contains:
        for k in row:
            if contains in k.lower():
                return k
    return None


def _rows(ctx: Context, name: str) -> list[dict]:
    h = ctx.h
    if name.startswith("sim_"):
        recs = h.records.get(name[4:], [])
        latest: dict[str, dict] = {}
        for r in recs:
            latest[r.get("id", str(len(latest)))] = r
        return list(latest.values())
    return h.table(name).rows


def _optional(ctx: Context, res: CompanyResult) -> None:
    """NPS and monitoring habits: active only when the v2 datasets exist."""
    h = ctx.h
    res.info["optional_datasets"] = dict(h.optional)
    name = h.optional.get("monitoring")
    if not name:
        return
    rows = _rows(ctx, name)
    if not rows:
        return
    tag = ctx.tag(name)
    pk = _first_key(rows[0], "p95_ms", "latency_p95_ms", "p95", contains="p95")
    ak = _first_key(rows[0], "availability", "uptime", contains="availab")
    dk = _first_key(rows[0], "downtime_minutes", contains="downtime")
    ek = _first_key(rows[0], "error_rate", contains="error")
    sk = _first_key(rows[0], "service", "component")
    tk = _first_key(rows[0], "date", "day", "ts")
    targets = [float(c["sla_target"]) for c in h.latest("contract").values() if c.get("sla_target")]
    target = float(np.median(targets)) if targets else 0.995
    caps = sorted(
        parse_date(le["effective_date"])
        for le in h.records.get("life_event", [])
        if le.get("event_type") == "vendor_capacity"
    )
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[str(r.get(sk)) if sk else "platform"].append(r)

    def num(r: dict, k: str | None) -> float | None:
        v = r.get(k) if k else None
        return None if v in (None, "") else float(v)

    def quiet(r: dict) -> bool:
        d = parse_date(r.get(tk)) if tk else None
        if (num(r, ak) is not None and num(r, ak) < 1.0) or d is None:
            return False
        return not any(0 <= (d - c).days <= 21 for c in caps)

    plat_lat: dict[str, list[float]] = defaultdict(list)
    plat_av: dict[str, list[float]] = defaultdict(list)
    for svc, rs in sorted(by.items()):
        rs.sort(key=lambda r: str(r.get(tk)))
        if pk:
            q = np.array([num(r, pk) for r in rs if quiet(r) and num(r, pk) is not None])
            allv = np.array([num(r, pk) for r in rs if num(r, pk) is not None])
            if q.size >= 10:
                res.params[f"latency_p95_ms_{svc}"] = stats.normal_mean(q, tag, "ms")
                res.params[
                    f"latency_p95_ms_{svc}"
                ].method = "mean p95 of the quiet days (no downtime, no GPU shortage)"
                res.params[f"latency_p95_ms_{svc}_now"] = stats.normal_mean(q[-63:], tag, "ms")
            if allv.size >= 10:
                res.params[f"latency_p95_ms_{svc}_all_days"] = stats.normal_mean(allv, tag, "ms")
        if ak:
            av = np.array([num(r, ak) for r in rs if num(r, ak) is not None])
            if av.size >= 10:
                if av.max() > 1.0:
                    av = av / 100.0
                res.params[f"availability_{svc}"] = stats.normal_mean(av, tag, "share of the day")
                res.params[f"sla_breach_day_share_{svc}"] = stats.share(
                    int(np.sum(av < target)), int(av.size), tag
                )
        if dk:
            dm = np.array([num(r, dk) for r in rs if num(r, dk) is not None])
            if dm.size >= 10:
                res.params[f"downtime_minutes_per_month_{svc}"] = stats.normal_mean(
                    dm * 30.44, tag, "minutes per month"
                )
        if ek:
            er = np.array([num(r, ek) for r in rs if num(r, ek) is not None])
            if er.size >= 10:
                res.params[f"error_rate_{svc}"] = stats.normal_mean(er, tag, "share of requests")
        for r in rs:
            key = str(r.get(tk))
            if num(r, pk) is not None:
                plat_lat[key].append(num(r, pk))
            if num(r, ak) is not None:
                plat_av[key].append(num(r, ak))
    for k in ("latency_p95_ms", "availability"):
        res.evidence[k] = [f"dataset:{name}.csv" if name in h.tables else f"dataset:{name}"]
    if plat_lat:
        daily = np.array([float(np.mean(v)) for _, v in sorted(plat_lat.items())])
        res.params["latency_p95_ms"] = stats.normal_mean(daily, tag, "ms")
        res.params["latency_p95_ms"].method = "mean over the services and days (unweighted)"
        res.params["latency_p95_ms_now"] = stats.normal_mean(daily[-90:], tag, "ms")
    if plat_av:
        daily = np.array([float(np.mean(v)) for _, v in sorted(plat_av.items())])
        res.params["availability"] = stats.normal_mean(daily, tag, "share")
        res.params["availability"].method = "mean over the services and days (unweighted)"
        res.params["sla_breach_day_share"] = stats.share(int(np.sum(daily < target)), int(daily.size), tag)
        res.info["sla_target"] = target
        days = [k for k, _ in sorted(plat_av.items())]
        res.info["sla_breach_days"] = [d for d, x in zip(days, daily, strict=True) if x < target]
