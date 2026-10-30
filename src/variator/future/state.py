"""The state of the company at T: clients and contracts, people, vendors, open invoices and bills, pipeline.

The structure comes from the history (``variator.actors.history.load_history`` cut at T) and the
individual habits from the learned documents (``params.Habits``). Months count from T: month 1 is the
first calendar month after the month of T (T = 2026-10-27 gives month 1 = November 2026).
"""

# ruff: noqa: E501 - one field per line

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from variator.actors.history import History, load_history, parse_date
from variator.future.params import HISTORY_DIR, Habits

DAYS_PER_MONTH = 365.25 / 12
ENGINEERING = "Engineering"
MAX_LAG = 8  # months after the horizon in which late payments still arrive


def month_start(t: date, m: int) -> date:
    """First day of month m (m = 0 is the month of T)."""
    k = t.month - 1 + m
    return date(t.year + k // 12, k % 12 + 1, 1)


def month_index(t: date, d: date) -> int:
    """Month index of a date (0 = the month of T, negative before)."""
    return (d.year - t.year) * 12 + d.month - t.month


@dataclass
class Clients:
    ids: list[str]
    names: list[str]
    kb: list[str]
    acv0: np.ndarray  # ACV in force at T (signed: the ACV from the start month)
    start_month: np.ndarray  # int: 0 = active at T, >= 1 = starts in that month (signed contracts)
    end_month: np.ndarray  # int: 0 = no end known at T, >= 1 = first month without the service (notice given)
    quarterly: np.ndarray  # bool
    key: np.ndarray  # bool
    term_end_month: np.ndarray  # int: month of the next renewal (0 = none in the horizon)
    term_months: np.ndarray  # int
    notice_months: np.ndarray  # int: months from a notice to the end of the service
    churn_p: np.ndarray  # annual probability to give notice (point)
    x0: np.ndarray  # downtime effect at T (hours, decayed)
    churn_mult: np.ndarray  # the covariate multiplier at T inside churn_p (1 when the model has no covariate)
    upsell_rate: np.ndarray  # 1/year
    downgrade_rate: np.ndarray  # 1/year
    late_rate: np.ndarray  # late-payment spells per year
    pay_delay: np.ndarray  # days
    pay_sd: np.ndarray  # days
    nps: np.ndarray  # NPS level of the client
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.ids)


@dataclass
class People:
    ids: list[str]
    names: list[str]
    titles: list[str]
    kb: list[str]
    role: list[str]
    engineer: np.ndarray  # bool
    salary: np.ndarray  # USD per year, gross
    pace: np.ndarray  # throughput points per sprint (engineers), else 0
    resign_p: np.ndarray  # annual probability to resign (point)
    devops_seat: np.ndarray  # bool: the seat whose vacancy raised the restore time (risk R11)

    @property
    def n(self) -> int:
        return len(self.ids)


@dataclass
class World:
    """Everything the engine needs at T."""

    as_of: date
    anchor: date  # the fork date of the case (the last day of the history)
    clients: Clients
    people: People
    vendors: dict[str, dict[str, Any]]  # id -> fee_now, cv, cloud, offshore, name
    cash0: float
    arr0: float
    open_receivables: dict[str, np.ndarray]  # client index, amount, issue day (days from T)
    open_payables: np.ndarray  # amount per month index 1..H+MAX_LAG (index 0 unused)
    pipeline: list[dict[str, Any]]  # open deals: amount, close month, client, id
    corp_tax: dict[int, float]  # calendar month -> amount
    month_bounds: np.ndarray  # day offset from T of the start of month m (m = 0..H + MAX_LAG + 1)
    calendar_month: np.ndarray  # calendar month number (1..12) of month m
    history_root: str = ""
    notes: list[str] = field(default_factory=list)
    history_start: date | None = None  # the first day of the history (the exposure window of the habits)

    def month_of_day(self, days: np.ndarray) -> np.ndarray:
        """Month index of day offsets from T (days before month 1 count as month 1)."""
        return np.maximum(np.searchsorted(self.month_bounds, days, side="right") - 1, 1)


def _p(d: dict[str, Any] | None, key: str, default: float) -> float:
    if not d or key not in d or not isinstance(d[key], dict):
        return default
    return float(d[key].get("point", default))


def build_world(
    habits: Habits,
    history: History | str | Path = HISTORY_DIR,
    *,
    horizon: int = 12,
    anchor: date | None = None,
) -> World:
    """The state at T. ``history`` is a loaded ``History`` or its root directory (cut at the habits' as-of)."""
    h = history if isinstance(history, History) else load_history(history, habits.as_of, timeline=False)
    t = h.as_of
    n_months = horizon + MAX_LAG + 2
    bounds = np.array([(month_start(t, m) - t).days for m in range(n_months)], dtype=np.float64)
    cal = np.array([month_start(t, m).month for m in range(n_months)])
    notes: list[str] = []

    # ---- clients: active, on notice and signed at T
    hb = habits.habits
    contracts = h.latest("contract")
    std = habits.cohort("clients:standard").get("params", {})
    base = hb.get("client_base", {})
    rows = []
    for c in h.clients.values():
        if c.status not in ("active", "notice", "signed"):
            continue
        acv = c.acv
        start_m = 0
        if c.status == "signed":
            ctr = next((x for x in contracts.values() if x.get("client") == c.id), None)
            acv = float(ctr.get("amount", 0.0)) if ctr else 0.0
            start_m = max(month_index(t, c.start), 1) if c.start else 1
        if acv <= 0:
            continue
        rows.append((c, acv, start_m))
    hc = hb.get("clients", {})
    std_upsell = _p(std, "upsell_rate", 0.13)
    sources = {}

    def cp(cid: str, key: str, default: float) -> float:
        own = hc.get(cid, {})
        if key in own:
            sources.setdefault(key, str(own[key].get("source", "data:")))
            return float(own[key]["point"])
        return default

    ids = [c.id for c, _, _ in rows]
    churn_base = _p(base, "churn_hazard_base", 0.17)
    clients = Clients(
        ids=ids,
        names=[c.name for c, _, _ in rows],
        kb=[f"org.{c.id}" for c, _, _ in rows],
        acv0=np.array([a for _, a, _ in rows]),
        start_month=np.array([s for _, _, s in rows], dtype=np.int64),
        end_month=np.array(
            [max(month_index(t, c.end), 1) if c.end and c.end > t else 0 for c, _, _ in rows], dtype=np.int64
        ),
        quarterly=np.array([c.billing == "quarterly" for c, _, _ in rows]),
        key=np.array([bool(c.key) for c, _, _ in rows]),
        term_end_month=np.array(
            [
                month_index(t, c.term_end) if c.term_end and 1 <= month_index(t, c.term_end) <= horizon else 0
                for c, _, _ in rows
            ],
            dtype=np.int64,
        ),
        term_months=np.array([c.term_months or 12 for c, _, _ in rows], dtype=np.int64),
        notice_months=np.array(
            [max(1, math.ceil((c.notice_days or 60) / DAYS_PER_MONTH)) for c, _, _ in rows], dtype=np.int64
        ),
        churn_p=np.array([cp(i, "churn_hazard", churn_base) for i in ids]),
        x0=np.array(
            [
                float(hc.get(i, {}).get("churn_hazard", {}).get("extra", {}).get("downtime_effect_at_t", 0.0))
                for i in ids
            ]
        ),
        churn_mult=np.array(
            [
                max(
                    float(hc.get(i, {}).get("churn_hazard", {}).get("extra", {}).get("multiplier", 1.0)), 1e-6
                )
                for i in ids
            ]
        ),
        upsell_rate=np.array([cp(i, "upsell_rate", std_upsell) for i in ids]),
        downgrade_rate=np.array([cp(i, "downgrade_rate", 0.012) for i in ids]),
        late_rate=np.array([cp(i, "late_payment_rate", 0.13) for i in ids]),
        pay_delay=np.array([cp(i, "pay_delay_days", _p(base, "pay_delay_days", 62.0)) for i in ids]),
        pay_sd=np.array([cp(i, "pay_delay_sd_days", _p(base, "pay_delay_sd_days", 6.7)) for i in ids]),
        nps=np.array([cp(i, "nps_level", _p(std, "nps_mean_client_level", 32.0)) for i in ids]),
        sources=sources,
    )
    arr0 = float(clients.acv0[(clients.start_month == 0)].sum())

    # ---- people active at T
    hp = hb.get("people", {})
    staff = hb.get("staff", {})
    ppl = [p for p in h.people.values() if not p.left and (p.last_day is None or p.last_day >= t)]
    ppl.sort(key=lambda p: p.id)
    eng_mean = _p(staff, "throughput_engineers", 13.0)
    people = People(
        ids=[p.id for p in ppl],
        names=[p.name for p in ppl],
        titles=[p.title for p in ppl],
        kb=[f"person.{p.id.lower()}" for p in ppl],
        role=[p.role for p in ppl],
        engineer=np.array([p.department == ENGINEERING for p in ppl]),
        salary=np.array([float(p.salary or 0.0) for p in ppl]),
        pace=np.array(
            [
                _p(hp.get(p.id), "throughput_points_per_sprint", eng_mean)
                if p.department == ENGINEERING
                else 0.0
                for p in ppl
            ]
        ),
        resign_p=np.array(
            [_p(hp.get(p.id), "resignation_hazard", _p(staff, "resignation_hazard", 0.18)) for p in ppl]
        ),
        devops_seat=np.array([p.seat == "LW-E12" for p in ppl]),
    )
    if not people.devops_seat.any():
        notes.append("no person holds the seat LW-E12: the vacancy effect on the restore time is off")

    # ---- vendors (fees as now; the cloud separately)
    risk19 = habits.risk("vendor_cost_change").get("size", {})
    cvs = risk19.get("bill_variability_cv", {})
    vendors = {}
    for a in habits.actor_list("vendor"):
        vid = a["id"]
        cloud = a.get("vendor_kind") == "cloud"
        fee = _p(hb.get("vendors", {}).get(vid), "monthly_fee_now", 0.0)
        if cloud:
            fee = _p(hb.get("company"), "cloud_cost_per_year_now", fee * 12) / 12
        vendors[vid] = {
            "name": a.get("name", vid),
            "fee_now": fee,
            "cv": float(cvs.get(vid, 0.0)),
            "cloud": cloud,
            "offshore": vid in ("VN-01", "VN-02"),
            "terms": float(a.get("payment_terms_days") or 30),
        }

    # ---- open invoices and bills at T
    inv = list(h.latest("invoice").values())
    idx = {c: i for i, c in enumerate(clients.ids)}
    rec = [
        x
        for x in inv
        if x.get("direction") != "payable" and x.get("status") != "paid" and x.get("client") in idx
    ]
    open_rec = {
        "client": np.array([idx[x["client"]] for x in rec], dtype=np.int64),
        "amount": np.array([float(x["amount"]) for x in rec]),
        "issue_day": np.array(
            [(parse_date(x.get("issued_at") or x.get("due_date")) - t).days for x in rec], dtype=np.float64
        ),
    }
    payables = np.zeros(n_months)
    for x in inv:
        if x.get("direction") == "payable" and x.get("status") != "paid":
            m = max(month_index(t, parse_date(x.get("due_date") or x.get("issued_at"))), 1)
            payables[min(m, n_months - 1)] += float(x["amount"])

    # ---- pipeline: open new-business deals
    pipeline = []
    for d in h.latest("deal").values():
        if d.get("status") in ("won", "lost") or d.get("deal_type", "new") != "new":
            continue
        close = parse_date(d.get("expected_close")) or month_start(t, 2)
        pipeline.append(
            {
                "id": d["id"],
                "client": d.get("client", d["id"]),
                "name": d.get("client_name", ""),
                "amount": float(d.get("amount", 0.0)),
                "close_month": max(month_index(t, close), 1),
            }
        )
    pipeline.sort(key=lambda d: d["id"])

    # ---- corporation tax: the instalments of the last year, by calendar month (as now)
    corp: dict[int, float] = {}
    for x in h.latest("tax_payment").values():
        if x.get("tax") == "Corporation tax":
            d = parse_date(x.get("paid_at") or x.get("due_date"))
            if d and (t - d).days <= 366:
                corp[d.month] = float(x["amount"])
    # a quarterly cadence (all instalments 3 months apart) continues in every quarter (as now)
    months = sorted(corp)
    if len(months) >= 2 and all((b - a) % 3 == 0 for a, b in zip(months, months[1:], strict=False)):
        amount = float(np.median(list(corp.values())))
        corp = {m: corp.get(m, amount) for m in range(1, 13) if (m - months[0]) % 3 == 0}
        if len(months) < 4:
            notes.append(
                f"corporation tax: {len(months)} instalments seen; the quarterly cadence fills the others"
            )

    cash0 = _p(hb.get("company"), "cash_at_t", 0.0)
    return World(
        as_of=t,
        anchor=anchor or t,
        clients=clients,
        people=people,
        vendors=vendors,
        cash0=cash0,
        arr0=arr0,
        open_receivables=open_rec,
        open_payables=payables,
        pipeline=pipeline,
        corp_tax=corp,
        month_bounds=bounds,
        calendar_month=cal,
        history_root=str(h.root),
        notes=notes,
        history_start=h.start,
    )
