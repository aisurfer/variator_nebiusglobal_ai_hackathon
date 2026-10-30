"""The actual values of the backtest come from the full history (the run itself learns: see the CLI)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from variator.future.backtest import actuals, months_before

HISTORY = Path(os.environ.get("VARIATOR_FUTURE_HISTORY", "/tmp/variator-1000/mvp-history-mgmt"))


def test_months_before():
    assert months_before(date(2026, 10, 27), 6) == date(2026, 4, 27)
    assert months_before(date(2026, 2, 27), 6) == date(2025, 8, 27)


def test_actuals_end_at_t():
    if not (HISTORY / "datasets").is_dir():
        pytest.skip("no history")
    a = actuals(HISTORY, date(2026, 4, 27), 6)
    assert len(a["cash"]) == len(a["arr"]) == len(a["churn"]) == 6
    assert a["month_end"][-1] == "2026-10-27"
    assert a["cash"][-1] == pytest.approx(2_000_000.0)
    assert all(b >= x for x, b in zip(a["churn"], a["churn"][1:], strict=False))
