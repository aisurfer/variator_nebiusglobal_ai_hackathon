"""Vendor bill timing (WP19 audit): monthly in arrears on the 1st, paid on the due day or 0-3 days late."""

from __future__ import annotations

import numpy as np
import pytest

from variator.future.company import DAYS_PER_MONTH, PAY_JITTER_DAYS, CompanyModel, month_periods, week_periods
from variator.future.forks import Fork, compile_knobs
from variator.future.params import draw_theta

N = 20


class Recorder(CompanyModel):
    """Records the vendor bills that the step puts into the payables ledger: (period, ledger index)."""

    calls: list

    @staticmethod
    def _ledger_add(ledger, idx, amount):  # type: ignore[override]
        if idx.shape[-1] == 1:  # vendor bills (receivables have one column per client)
            Recorder.calls.append(np.asarray(idx)[..., 0].copy())
        CompanyModel._ledger_add(ledger, idx, amount)


def _run(company, grain: str, periods_n: int):
    m = Recorder(company.habits, company.world, company.cfg)
    knobs = compile_knobs([Fork("status_quo", "x")])
    per = month_periods(m.world, periods_n, 2) if grain == "month" else week_periods(m.world, periods_n, 30)
    th = draw_theta(m.params, N, 3)
    draws = m.month_draws(3, N, periods_n)
    st = m.init(knobs, th, draws.first(), per)
    return m, per, st, draws, th, knobs


@pytest.mark.parametrize("grain,periods_n", [("month", 6), ("week", 26)])
def test_no_bill_is_paid_before_its_due_day(company, grain, periods_n):
    m, per, st, draws, th, knobs = _run(company, grain, periods_n)
    b = per.month_bounds
    for p in range(1, periods_n + 1):
        Recorder.calls = []
        m.step(st, p, per, knobs, th, draws.period(p))
        mid = 0.5 * (per.d0[p - 1] + per.d1[p - 1])
        issue = b[np.searchsorted(b, mid, side="right")]  # the first day of the month after the usage
        if grain == "month":
            assert issue == pytest.approx(per.d1[p - 1])
        for vid, idx in zip(m.vendor_ids, Recorder.calls, strict=True):
            terms = m.world.vendors[vid]["terms"]
            earliest = per.period_of_day(np.array([issue + terms]))[0]
            latest = per.period_of_day(np.array([issue + terms + PAY_JITTER_DAYS]))[0]
            assert (idx >= earliest).all() and (idx <= latest).all(), (grain, p, vid)


def test_the_month_of_t_is_billed_only_for_the_days_before_day_0(company):
    m, per, st, *_ = _run(company, "month", 6)
    w = m.world
    open_bills = w.open_payables.sum()
    share = max(-float(w.month_bounds[0]), 0.0) / DAYS_PER_MONTH
    expected = sum(w.vendors[v]["fee_now"] for v in m.vendor_ids) * share
    assert st["payb"][0, 0].sum() == pytest.approx(open_bills + expected)
