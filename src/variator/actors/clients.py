"""Habits of clients (09 §2.2-2.4): ARR at T, payment delay, tickets, life-event hazards.

- ARR, status, notice and term: the contract version in force at T.
- Payment delay (DSO per client): days from the invoice to the payment, normal with right
  censoring (invoices not paid at T), invoices inside a known late-payment spell excluded;
  per-client means pooled on the log scale (``stats.pool_normal``).
- Ticket rate: base support tickets per month (no incident tickets, no tickets caused by an
  upsell or onboarding deal), Poisson-log-normal pooling per cohort (key, standard).
- Upsell, downgrade, late-payment spells: life events per client-year, pooled per cohort.
- Churn: the realised hazard and the base hazard without the incident effect
  (``stats.covariate_hazard`` with the recent downtime of the client as the covariate,
  decay chosen by AIC).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
from scipy import stats as _st

from variator.actors import stats
from variator.actors.context import Context
from variator.actors.history import ClientInfo, parse_date
from variator.actors.stats import Param

TAUS_DAYS = (30.0, 60.0, 120.0)  # candidate decays of the incident effect on churn


def st_norm_ppf(q: float) -> float:
    return float(_st.norm.ppf(q))


def st_t_ppf(q: float, df: float) -> float:
    return float(_st.t.ppf(q, df))


def st_chi2_ppf(q: float, df: float) -> float:
    return float(_st.chi2.ppf(q, df))


@dataclass
class ClientsResult:
    params: dict[str, dict[str, Param]] = field(default_factory=dict)  # cid -> name -> Param
    info: dict[str, dict[str, Any]] = field(default_factory=dict)
    cohorts: dict[str, dict[str, Param]] = field(default_factory=dict)  # cohort -> name -> Param
    company: dict[str, Param] = field(default_factory=dict)
    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # kind -> rows with kb ids
    evidence: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    churn_model: dict[str, Any] = field(default_factory=dict)


def cohort_of(c: ClientInfo) -> str:
    return "key" if c.key else "standard"


def learn_clients(ctx: Context) -> ClientsResult:
    h = ctx.h
    res = ClientsResult()
    clients = sorted(h.clients.values(), key=lambda c: c.id)
    for c in clients:
        res.params[c.id] = {}
        res.evidence[c.id] = defaultdict(list)
        res.info[c.id] = _state(ctx, c)
        res.params[c.id]["arr"] = stats.fixed(
            c.acv, ctx.tag("sim_contract"), "USD per year", "contract in force at T"
        )
        res.evidence[c.id]["arr"] = [h.kb("sim", c.contract)] if c.contract else []
        if c.notice_days is not None:
            nd = float(c.notice_days)
            res.params[c.id]["notice_days"] = Param(
                nd,
                nd,
                nd,
                nd + 1.0,
                1,
                ctx.tag("sim_contract"),
                "days",
                "contract term in whole days (the habit lies in [days, days + 1))",
            )
    _life_events(ctx, res)
    _payments(ctx, res, clients)
    _tickets(ctx, res, clients)
    _hazards(ctx, res, clients)
    _churn(ctx, res, clients)
    _renewals(ctx, res)
    res.evidence = {k: dict(v) for k, v in res.evidence.items()}
    return res


# ---------------------------------------------------------------------------------------------- state
def _active_span(ctx: Context, c: ClientInfo, *, until_notice: bool = False) -> tuple[date, date]:
    """[a, b) of the service inside the history window (to the notice date when ``until_notice``)."""
    h = ctx.h
    a = max(c.start or h.start, h.start)
    b = h.as_of
    if c.end is not None and c.end < b:
        b = c.end
    if until_notice and c.notice_at is not None and c.notice_at < b:
        b = c.notice_at
    return a, max(a, b)


def _state(ctx: Context, c: ClientInfo) -> dict[str, Any]:
    a, b = _active_span(ctx, c)
    return {
        "status_at_t": c.status,
        "arr_at_t": c.acv,
        "start": c.start.isoformat() if c.start else None,
        "end": c.end.isoformat() if c.end else None,
        "notice_at": c.notice_at.isoformat() if c.notice_at else None,
        "term_end": c.term_end.isoformat() if c.term_end else None,
        "term_months": c.term_months,
        "billing": c.billing,
        "key": c.key,
        "new": c.new,
        "account_manager": c.account_manager,
        "active_years_in_window": round(ctx.h.years(a, b), 3),
    }


def _life_events(ctx: Context, res: ClientsResult) -> None:
    h = ctx.h
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for le in h.records.get("life_event", []):
        typ = le.get("event_type")
        if typ and le.get("client"):
            out[typ].append({**le, "kb": h.kb("sim", le["id"])})
            res.evidence.setdefault(le["client"], defaultdict(list))[typ].append(h.kb("sim", le["id"]))
    res.events = dict(out)


# ---------------------------------------------------------------------------------------------- payments
def _spells(res: ClientsResult) -> dict[str, list[tuple[date, date, float, str]]]:
    out: dict[str, list[tuple[date, date, float, str]]] = defaultdict(list)
    for le in res.events.get("late_payment", []):
        a = parse_date(le["effective_date"])
        b = parse_date(le.get("end_date")) or a
        out[le["client"]].append((a, b, float(le.get("extra_days") or 0.0), le["kb"]))
    return out


def _payments(ctx: Context, res: ClientsResult, clients: list[ClientInfo]) -> None:
    h = ctx.h
    tag = ctx.tag("sim_invoice")
    first: dict[str, dict] = {}
    for r in h.records.get("invoice", []):
        if r.get("direction") != "receivable":
            continue
        first.setdefault(r["id"], r)
    paid: dict[str, date] = {}
    pay_id: dict[str, str] = {}
    for r in h.records.get("payment", []):
        if r.get("direction") == "incoming" and r.get("settles"):
            paid[r["settles"]] = parse_date(r.get("paid_at")) or h.local(r["ts"]).date()
            pay_id[r["settles"]] = r["id"]
    spells = _spells(res)
    rows: dict[str, list[tuple[float, bool, str]]] = defaultdict(list)
    spell_rows: dict[str, list[tuple[float, str, str]]] = defaultdict(list)
    for inv, r in first.items():
        issued = parse_date(r.get("issued_at"))
        if issued is None or issued < h.start:
            continue  # opened before the history: only the slow ones are delivered (truncation)
        cid = r["client"]
        if inv in paid:
            val, cens = float((paid[inv] - issued).days), False
        else:
            val, cens = float((h.as_of - issued).days), True
        sp = next((s for s in spells.get(cid, []) if s[0] <= issued <= s[1]), None)
        if sp is not None:
            if not cens:
                spell_rows[cid].append((val, inv, sp[3]))
            continue
        rows[cid].append((val, cens, inv))
    fits: dict[str, tuple[float, float, float, float, int]] = {}
    for c in clients:
        obs = rows.get(c.id, [])
        n_paid = sum(1 for x in obs if not x[1])
        res.info[c.id]["invoices_used"] = len(obs)
        res.info[c.id]["invoices_paid"] = n_paid
        if n_paid < 2:
            continue
        vals = np.array([x[0] for x in obs])
        cens = np.array([x[1] for x in obs])
        fits[c.id] = stats.censored_normal(vals, cens, sd_floor=1.0)
        paid_inv = [x[2] for x in obs if not x[1]]
        res.evidence[c.id]["pay_delay_days"] = [h.kb("sim", i) for i in paid_inv[-4:]] + [
            h.kb("sim", pay_id[i]) for i in paid_inv[-2:] if i in pay_id
        ]
    if not fits:
        return
    # the spread of the delays: each client's sd shrinks to the cohort sd (conjugate, NU0 prior df)
    nu0 = 4.0
    sds = [f[1] for f in fits.values() if f[4] >= 5]
    sigma0 = float(np.median(sds)) if sds else 7.0
    ids, y, v = [], [], []
    post_sd: dict[str, tuple[float, float]] = {}
    z = st_norm_ppf(stats.P_HI)
    for cid, (mu, sd, _se_mu, _se_ls, n_obs) in fits.items():
        df = max(n_obs - 1, 0) + nu0
        s2 = (max(n_obs - 1, 0) * sd * sd + nu0 * sigma0 * sigma0) / df
        post_sd[cid] = (math.sqrt(s2), df)
        se = math.sqrt(s2 / n_obs) * st_t_ppf(stats.P_HI, df) / z  # t tails carried into the normal pooling
        ids.append(cid)
        y.append(math.log(max(mu, 1.0)))
        v.append((se / max(mu, 1.0)) ** 2)
    pooled = stats.pool_normal(
        ids, np.array(y), np.array(v), tag, ctx.group("clients"), unit="days", label="pay_delay"
    )
    res.company["pay_delay_days"] = pooled.group
    res.company["pay_delay_spread"] = pooled.spread
    res.company["pay_delay_sd_days"] = Param(
        sigma0,
        float(np.quantile(sds, 0.1)) if sds else sigma0,
        sigma0,
        float(np.quantile(sds, 0.9)) if sds else sigma0,
        len(sds),
        tag,
        "days",
        "median of the client sds (low/high: p10/p90 of the clients)",
    )
    for cid in ids:
        prm = pooled.actors[cid]
        prm.n = fits[cid][4]
        res.params[cid]["pay_delay_days"] = prm
        s, df = post_sd[cid]
        lo, hi = (s * math.sqrt(df / st_chi2_ppf(q, df)) for q in (stats.P_HI, stats.P_LO))
        res.params[cid]["pay_delay_sd_days"] = Param(
            s,
            lo,
            s,
            hi,
            fits[cid][4],
            tag if fits[cid][4] >= nu0 else ctx.group("clients"),
            "days",
            "scaled inverse chi2 posterior, shrunk to the cohort sd",
            {"own": fits[cid][1], "prior_df": nu0},
        )
        res.info[cid]["pay_delay_own"] = round(fits[cid][0], 2)
    # late-payment spells: extra days observed on the invoices of a spell
    extra = []
    for cid, items in spell_rows.items():
        base = res.params.get(cid, {}).get("pay_delay_days")
        if base is None:
            continue
        for val, inv, _le in items:
            extra.append(val - base.point)
            res.evidence[cid]["late_payment"].append(h.kb("sim", inv))
    if len(extra) >= 3:
        res.company["late_extra_days_observed"] = stats.normal_mean(np.array(extra), tag, "days")
    declared = [
        float(le.get("extra_days")) for le in res.events.get("late_payment", []) if le.get("extra_days")
    ]
    if len(declared) >= 2:
        res.company["late_extra_days"] = stats.normal_mean(
            np.array(declared), ctx.tag("sim_life_event"), "days"
        )
    lengths = []
    for le in res.events.get("late_payment", []):
        a, b = parse_date(le["effective_date"]), parse_date(le.get("end_date"))
        if a and b:
            lengths.append(float((b - a).days))
    if len(lengths) >= 2:
        res.company["late_spell_days"] = stats.normal_mean(
            np.array(lengths), ctx.tag("sim_life_event"), "days"
        )


# ---------------------------------------------------------------------------------------------- tickets
def _tickets(ctx: Context, res: ClientsResult, clients: list[ClientInfo]) -> None:
    h = ctx.h
    tag = ctx.tag("sim_support_ticket")
    base: dict[str, list[str]] = defaultdict(list)
    extra: dict[str, int] = defaultdict(int)
    incident: dict[str, int] = defaultdict(int)
    seen: set[str] = set()
    for r in h.records.get("support_ticket", []):
        if r["kind"] != "ticket_opened" or r["id"] in seen:
            continue
        seen.add(r["id"])
        causes = r.get("causes") or []
        if r.get("category") == "incident" or any(x.startswith("process:INC") for x in causes):
            incident[r["client"]] += 1
        elif any(x.startswith("process:DEAL") for x in causes):
            extra[r["client"]] += 1
        else:
            base[r["client"]].append(r["id"])
    for name in ("key", "standard"):
        members = [c for c in clients if cohort_of(c) == name]
        ids, cnt, months = [], [], []
        for c in members:
            a, b = _active_span(ctx, c)
            m = h.bdays(a, b) / 21.0
            res.info[c.id]["tickets_base"] = len(base.get(c.id, []))
            res.info[c.id]["tickets_after_deal"] = extra.get(c.id, 0)
            res.info[c.id]["tickets_incident"] = incident.get(c.id, 0)
            if m <= 0:
                continue
            ids.append(c.id)
            cnt.append(len(base.get(c.id, [])))
            months.append(m)
            res.evidence[c.id]["tickets_per_month"] = [h.kb("sim", t) for t in base.get(c.id, [])[-5:]]
        if not ids:
            continue
        pooled = stats.pool_poisson(
            ids,
            np.array(cnt),
            np.array(months),
            tag,
            ctx.group(f"clients:{name}"),
            unit="tickets per month",
            label=f"tickets:{name}",
        )
        res.cohorts.setdefault(name, {})["tickets_per_month"] = pooled.group
        res.cohorts[name]["tickets_per_month_spread"] = pooled.spread
        for cid, prm in pooled.actors.items():
            res.params[cid]["tickets_per_month"] = prm
    # extra tickets after an upsell or an onboarding: the multiplier of the base rate for ~3 months
    expected = 0.0
    for vers in h.versions("deal").values():
        last = vers[-1]
        if last.get("status") != "won":
            continue
        cid = last.get("client")
        closed = parse_date(last.get("closed_at"))
        prm = res.params.get(cid, {}).get("tickets_per_month")
        if closed is None or prm is None:
            continue
        end = min(h.cal.add_business_days(closed, 60 + 15), h.as_of)
        expected += prm.point * max(h.bdays(closed, end), 0) / 21.0
    n_extra = sum(extra.values())
    if expected > 0:
        r = stats.rate(n_extra, expected, tag, "extra tickets per base ticket")
        res.company["support_mult_after_deal"] = Param(
            1 + r.point,
            1 + r.low,
            1 + r.mode,
            1 + r.high,
            n_extra,
            tag,
            "multiplier of the base rate",
            "1 + tickets caused by a won deal / base tickets expected in ~3 months after the deal",
        )


# ---------------------------------------------------------------------------------------------- hazards
def _busy(res: ClientsResult, ctx: Context) -> dict[str, list[tuple[date, date]]]:
    """Upsell windows: from the request to the close of its deal (no new request in between)."""
    h = ctx.h
    deals = h.versions("deal")
    out: dict[str, list[tuple[date, date]]] = defaultdict(list)
    for vers in deals.values():
        if vers[0].get("deal_type") != "upsell":
            continue
        a = h.local(vers[0]["ts"]).date()
        last = vers[-1]
        b = parse_date(last.get("closed_at")) or h.as_of
        out[last["client"]].append((a, b))
    return out


def _hazards(ctx: Context, res: ClientsResult, clients: list[ClientInfo]) -> None:
    h = ctx.h
    tag = ctx.tag("sim_life_event")
    busy = _busy(res, ctx)
    spells = _spells(res)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for typ in ("upsell_request", "downgrade", "late_payment"):
        for le in res.events.get(typ, []):
            counts[typ][le["client"]] += 1
    for typ, name, cohorts in (
        ("upsell_request", "upsell_rate", ("key", "standard")),
        ("downgrade", "downgrade_rate", ("all",)),
        ("late_payment", "late_payment_rate", ("all",)),
    ):
        for coh in cohorts:
            members = [c for c in clients if coh == "all" or cohort_of(c) == coh]
            ids, cnt, yrs = [], [], []
            for c in members:
                a, b = _active_span(ctx, c, until_notice=True)
                y = h.years(a, b)
                if typ == "upsell_request":
                    y -= sum(h.years(max(x, a), min(z, b)) for x, z in busy.get(c.id, []) if z > a and x < b)
                if typ == "late_payment":
                    y -= sum(
                        h.years(max(x, a), min(z, b))
                        for x, z, _, _ in spells.get(c.id, [])
                        if z > a and x < b
                    )
                if y <= 0.02:
                    continue
                ids.append(c.id)
                cnt.append(counts[typ].get(c.id, 0))
                yrs.append(y)
            if not ids:
                continue
            group = ctx.group("clients" if coh == "all" else f"clients:{coh}")
            pooled = stats.pool_rate(
                ids, np.array(cnt), np.array(yrs), tag, group, unit="1/year", label=f"{typ}:{coh}"
            )
            res.cohorts.setdefault(coh, {})[name] = pooled.group
            res.cohorts[coh][f"{name}_mean"] = pooled.group_mean
            res.cohorts[coh][f"{name}_crude"] = stats.rate(sum(cnt), sum(yrs), tag, "1/year")
            for cid, prm in pooled.actors.items():
                res.params[cid][name] = prm
    sizes = [float(le["size_share"]) for le in res.events.get("upsell_request", []) if le.get("size_share")]
    if len(sizes) >= 2:
        res.company["upsell_size_share"] = stats.normal_mean(np.array(sizes), tag, "share of ACV")
    sizes = [float(le["size_share"]) for le in res.events.get("downgrade", []) if le.get("size_share")]
    if sizes:
        res.company["downgrade_size_share"] = stats.normal_mean(
            np.array(sizes), tag, "share of ACV", sd_floor=0.05
        )


# ---------------------------------------------------------------------------------------------- churn
def _churn(ctx: Context, res: ClientsResult, clients: list[ClientInfo]) -> None:
    """Realised and base churn hazards; the incident effect via the recent downtime of the client."""
    h = ctx.h
    tag = ctx.tag("sim_contract+incident_impact")
    notice = {le["client"]: le for le in res.events.get("churn", [])}
    hits: dict[str, list[tuple[int, float]]] = defaultdict(list)
    days = h.window_days
    idx = {d: i for i, d in enumerate(days)}
    for r in h.table("incident_impact"):
        d = r["date"]
        if d in idx:
            hits[r["client"]].append((idx[d], float(r.get("downtime_minutes") or 0.0) / 60.0))
    cells_ev, cells_ex, cells_c = [], [], []
    for c in clients:
        a, b = _active_span(ctx, c, until_notice=True)
        if b <= a:
            continue
        first = idx.get(h.cal.roll_forward(a))
        ndays = h.bdays(a, b)
        if first is None or ndays <= 0:
            continue
        ev = np.zeros(ndays)
        le = notice.get(c.id)
        if le is not None:
            d = parse_date(le["effective_date"])
            k = h.bdays(a, d)
            if 0 <= k < ndays + 1:
                ev[min(k, ndays - 1)] = 1.0
        cells_ev.append(ev)
        cells_ex.append(np.full(ndays, 1.0 / 252.0))
        cells_c.append((c.id, first, ndays))
    events = np.concatenate(cells_ev) if cells_ev else np.zeros(0)
    expo = np.concatenate(cells_ex) if cells_ex else np.zeros(0)
    n_ev = float(events.sum())
    years = float(expo.sum())
    if years <= 0:
        return
    realised = stats.hazard(n_ev, years, ctx.tag("sim_contract"))
    res.company["churn_hazard_realised"] = realised
    best = None
    aic: dict[str, float] = {}
    base0 = stats.hazard(n_ev, years, tag)
    ll0 = n_ev * math.log(max(n_ev, 1e-9) / years) - n_ev
    aic["none"] = 2 * 1 - 2 * ll0
    for tau in TAUS_DAYS:
        tau_b = tau * 252 / 365
        xs = []
        for cid, first, ndays in cells_c:
            x = np.zeros(ndays)
            for k, dt in hits.get(cid, []):
                j = k - first
                if j >= ndays:
                    continue
                t = np.arange(max(j, 0), ndays)
                x[t] += dt * np.exp(-(t - j) / tau_b)
            xs.append(x)
        xx = np.concatenate(xs)
        base, kp, llmax = stats.covariate_hazard(events, expo, xx, tag)
        aic[f"downtime_decay_{int(tau)}d"] = 2 * 2 - 2 * llmax
        if best is None or aic[f"downtime_decay_{int(tau)}d"] < best[0]:
            best = (aic[f"downtime_decay_{int(tau)}d"], tau, base, kp, xs)
    res.churn_model = {"aic": {k: round(v, 3) for k, v in aic.items()}}
    choose_cov = best is not None and best[0] < aic["none"]
    if best is not None:
        _, tau, base, kp, xs = best
        res.company["churn_hazard_base"] = base if choose_cov else base0
        res.company["churn_incident_k"] = kp
        res.churn_model.update(
            selected=f"downtime_decay_{int(tau)}d" if choose_cov else "none",
            tau_days=tau,
            covariate="client downtime hours (incident_impact), exponential decay",
            base_without_covariate=base0.to_dict(),
        )
        # the hazard of each client as now: base x (1 + k x(T)) with x at the last day
        for (cid, _first, _ndays), x in zip(cells_c, xs, strict=True):
            c = h.clients[cid]
            if c.status != "active":
                continue
            xt = float(x[-1]) if x.size else 0.0
            b = res.company["churn_hazard_base"]
            mult = 1.0 + (kp.point if choose_cov else 0.0) * xt
            lam = [-math.log(1 - p) * mult for p in (b.point, b.low, b.mode, b.high)]
            f = lambda r: 1 - math.exp(-r)  # noqa: E731
            res.params[cid]["churn_hazard"] = Param(
                f(lam[0]),
                f(lam[1]),
                f(lam[2]),
                f(lam[3]),
                int(n_ev),
                ctx.group("clients"),
                "probability per year",
                "cohort base hazard x (1 + k x downtime effect at T)",
                {"downtime_effect_at_t": xt, "multiplier": mult},
            )
    for le in res.events.get("churn", []):
        res.evidence[le["client"]]["churn"].append(le["kb"])
        c = h.clients.get(le["client"])
        if c and c.contract:
            res.evidence[le["client"]]["churn"].append(h.kb("sim", c.contract))
    res.info["_churn"] = {"notices": int(n_ev), "client_years": round(years, 3)}


def _renewals(ctx: Context, res: ClientsResult) -> None:
    h = ctx.h
    tag = ctx.tag("sim_contract")
    ups = []
    for r in h.records.get("contract", []):
        if r["kind"] == "contract_renewed" and r.get("uplift") is not None:
            ups.append(float(r["uplift"]))
            res.evidence.setdefault(r["client"], defaultdict(list))["renewal"].append(h.kb("sim", r["id"]))
            res.info.setdefault(r["client"], {}).setdefault("renewals", []).append(
                {"date": r.get("renewed_at"), "uplift": r["uplift"]}
            )
    if len(ups) >= 2:
        res.company["renewal_uplift"] = stats.normal_mean(np.array(ups), tag, "share of ACV")
        res.company["renewal_uplift"].extra["max_seen"] = max(ups)
