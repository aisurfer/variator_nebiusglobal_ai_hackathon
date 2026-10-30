"""Rolling periods of the rule parser ("over the past 10 days"): the window ends with the day of `now` inclusive.
Calendar periods ("last week") keep their meaning. No infra."""
from __future__ import annotations

from datetime import datetime

import pytest

from kb.reasoning.timeparse import resolve_time

NOW = datetime.fromisoformat("2026-09-22T15:00:00+07:00")  # Tuesday
TZ = "Asia/Novosibirsk"


@pytest.mark.parametrize("q,start,end", [
    ("Which meetings did the team hold over the past week?", "2026-09-16", "2026-09-23"),
    ("Tickets closed in the last 10 days", "2026-09-13", "2026-09-23"),
    ("emails from the last two weeks", "2026-09-09", "2026-09-23"),
    ("What changed in the past month?", "2026-08-23", "2026-09-23"),
    ("decisions of the last 7 days", "2026-09-16", "2026-09-23"),
    ("What happened during the last 24 hours?", "2026-09-22", "2026-09-23"),
    ("What changed last week?", "2026-09-14", "2026-09-21"),  # calendar week, unchanged
    ("What changed last month?", "2026-08-01", "2026-09-01"),  # calendar month
    ("What happened the day before yesterday?", "2026-09-20", "2026-09-21"),
])
def test_english_rolling_periods(q, start, end):
    ti = resolve_time(q, NOW, tz=TZ)
    assert (ti.start.date().isoformat(), ti.end.date().isoformat()) == (start, end), ti.interpretation
    assert "read as" in ti.interpretation


def test_what_did_we_know_as_of():
    ti = resolve_time("What did we know about the deadline of the second stage as of August 1?", NOW, tz=TZ)
    assert ti.known_at.isoformat().startswith("2026-08-02T00:00") and ti.expression.startswith("as of August 1")
