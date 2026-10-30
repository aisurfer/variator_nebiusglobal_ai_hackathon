"""A day example's place among the scenarios of WP18's month cube (``forward.fan``), on a small cube."""

from __future__ import annotations

import json
from datetime import date

import numpy as np

from variator.orgsim.company.forward.fan import Cube, WeekFan


def _cube(tmp_path, cash):
    """S scenarios x 2 forks x 2 metrics x 3 periods; cash of fork ``b`` in April = ``cash``."""
    S = len(cash)
    values = np.zeros((S, 2, 2, 3))
    values[:, 1, 1, 2] = cash
    values[:, 0, 1, 2] = 1e9  # the other fork must not count
    (tmp_path / "cube.json").write_text(
        json.dumps(
            {
                "run": "test",
                "options": ["a", "b"],
                "metrics": [{"id": "arr"}, {"id": "cash"}],
                "periods": ["2027-02-26", "2027-03-01", "2027-04-01"],
            }
        )
    )
    np.savez(tmp_path / "arrays.npz", values=values)
    return Cube(tmp_path)


def test_percentile_among_the_scenarios_of_the_fork_and_month(tmp_path):
    cube = _cube(tmp_path, np.arange(1.0, 1001.0))  # 1 .. 1000
    pos = cube.position("b", date(2027, 4, 26), 700.0)
    # 699 below, 1 equal: (699 + 0.5) / 1000
    assert pos["percentile"] == 70.0
    fan = pos["fan"]
    assert fan["n"] == 1000 and fan["period"] == "2027-04" and fan["periodEnd"] == "2027-04-30"
    assert fan["p10"] < fan["p50"] < fan["p90"] and abs(fan["p50"] - 500.5) < 1e-9


def test_week_fan_on_the_same_day(tmp_path):
    cash = {"p5": 50.0, "p10": 100.0, "p25": 250.0, "p50": 500.0, "p75": 750.0, "p90": 900.0, "p95": 950.0}
    doc = {"options": {"b": {"weekFan": {"week": 26, "date": "2027-04-26", "n": 1000, "cash": cash}}}}
    (tmp_path / "month.json").write_text(json.dumps(doc))
    fan = WeekFan.at(tmp_path / "month.json")
    day = date(2027, 4, 26)
    assert fan.position("b", day, 625.0)["percentile"] == 62.5  # halfway between p50 and p75
    hi = fan.position("b", day, 990.0)
    assert hi["percentile"] == 95.0 and hi["fan"]["bound"] == "at or above p95"
    assert fan.position("b", date(2027, 4, 30), 625.0) is None  # another day: no answer
    assert fan.position("a", day, 625.0) is None
    assert WeekFan.at(tmp_path / "missing.json") is None


def test_no_position_without_the_month_or_the_fork(tmp_path):
    cube = _cube(tmp_path, np.arange(1.0, 11.0))
    assert cube.position("b", date(2027, 5, 3), 5.0) is None
    assert cube.position("c", date(2027, 4, 26), 5.0) is None
    assert Cube.at(tmp_path / "missing") is None
