"""Business calendar, period axis and dates relative to T."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from variator.orgsim.calendar import BusinessCalendar, PeriodAxis, RelativeDates, add_months, anchor_date

CAL = BusinessCalendar(timezone="Europe/Berlin", holidays=(date(2026, 12, 25), date(2026, 12, 28)))
FRI, SAT, MON = date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 2)


def test_workdays_skip_weekends_and_holidays():
    assert CAL.is_workday(FRI) and not CAL.is_workday(SAT) and CAL.is_workday(MON)
    assert not CAL.is_workday(date(2026, 12, 25))
    assert CAL.workdays(date(2026, 12, 21), date(2026, 12, 29)) == [
        date(2026, 12, 21),
        date(2026, 12, 22),
        date(2026, 12, 23),
        date(2026, 12, 24),
    ]
    assert CAL.work_hours == 9


def test_add_business_days():
    assert CAL.add_business_days(FRI, 1) == MON
    assert CAL.add_business_days(SAT, 1) == MON
    assert CAL.add_business_days(SAT, 0) == MON
    assert CAL.add_business_days(SAT, -1) == FRI
    assert CAL.add_business_days(MON, -1) == FRI
    assert CAL.add_business_days(date(2026, 12, 24), 1) == date(
        2026, 12, 29
    )  # Christmas and the 28th are off


def test_business_days_between():
    assert CAL.business_days_between(MON, MON + timedelta(days=7)) == 5
    assert CAL.business_days_between(MON + timedelta(days=7), MON) == -5
    assert CAL.business_days_between(FRI, MON) == 1


@pytest.mark.parametrize("seed", range(5))
def test_add_and_between_agree(seed):
    rng = np.random.default_rng(seed)
    for _ in range(50):
        d = CAL.roll_forward(date(2026, 1, 1) + timedelta(days=int(rng.integers(0, 365))))
        n = int(rng.integers(-40, 40))
        assert CAL.business_days_between(d, CAL.add_business_days(d, n)) == n


def test_work_hours():
    tz = ZoneInfo("Europe/Berlin")
    fri_5pm = datetime(2026, 10, 30, 17, 0, tzinfo=tz)
    assert CAL.is_work_time(fri_5pm) and not CAL.is_work_time(fri_5pm.replace(hour=20))
    assert CAL.add_work_hours(fri_5pm, 2) == datetime(2026, 11, 2, 10, 0, tzinfo=tz)
    assert CAL.next_work_time(datetime(2026, 10, 31, 12, 0, tzinfo=tz)) == datetime(
        2026, 11, 2, 9, 30, tzinfo=tz
    )
    assert CAL.add_work_hours(fri_5pm, 0) == fri_5pm
    assert CAL.at(MON, "09:30") == datetime(2026, 11, 2, 9, 30, tzinfo=tz)
    with pytest.raises(ValueError):
        CAL.is_work_time(datetime(2026, 10, 30, 12, 0))  # naive


def test_add_months_clamps_the_day():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 1, 31), 2) == date(2026, 3, 31)
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)


@pytest.mark.parametrize("grain", ["day", "bday", "week", "month"])
@pytest.mark.parametrize("anchor", [date(2026, 1, 31), date(2026, 10, 31), date(2026, 10, 27)])
def test_each_period_contains_its_own_days(grain, anchor):
    axis = PeriodAxis(anchor, grain, 14, CAL)
    for k in range(-3, 14):
        start, end = axis.start(k), axis.end(k)
        assert start < end
        days = [start + timedelta(days=i) for i in range((end - start).days)]
        assert set(axis.bucket(days).tolist()) == {k}


def test_bucketing_by_hand():
    t = date(2026, 10, 27)  # a Tuesday
    assert PeriodAxis(t, "day", 30).bucket([t, date(2026, 10, 26), date(2026, 11, 5)]).tolist() == [0, -1, 9]
    assert PeriodAxis(t, "week", 10).bucket([date(2026, 11, 2), date(2026, 11, 3)]).tolist() == [0, 1]
    months = PeriodAxis(t, "month", 12)
    assert months.bucket([date(2026, 11, 26), date(2026, 11, 27), date(2027, 10, 26)]).tolist() == [0, 1, 11]
    assert months.labels()[:2] == ["2026-10-27", "2026-11-27"]
    bdays = PeriodAxis(date(2026, 10, 31), "bday", 5, CAL)  # a Saturday: period 0 is Monday
    assert bdays.start(0) == MON and bdays.bucket([MON, date(2026, 11, 7)]).tolist() == [0, 4]


def test_aware_datetimes_use_the_calendar_zone():
    axis = PeriodAxis(date(2026, 10, 27), "day", 5, CAL)
    late_utc = datetime(2026, 10, 27, 23, 30, tzinfo=ZoneInfo("UTC"))  # already the 28th in Berlin
    assert axis.index(late_utc) == 1 and axis.index("2026-10-29") == 2
    assert axis.bucket(np.array(["2026-10-27", "2026-10-30"], dtype="datetime64[D]")).tolist() == [0, 3]


def test_counts_leave_out_dates_outside_the_axis():
    axis = PeriodAxis(date(2026, 10, 27), "week", 3)
    dates = [
        date(2026, 10, 20),
        date(2026, 10, 27),
        date(2026, 10, 28),
        date(2026, 11, 10),
        date(2026, 12, 1),
    ]
    assert axis.counts(dates).tolist() == [2, 0, 1]
    assert axis.counts(dates, weights=[1, 2, 3, 4, 5]).tolist() == [5, 0, 4]


def test_relative_dates_follow_the_anchor():
    rel = RelativeDates(date(2026, 10, 27), CAL)
    assert rel.days(-1) == date(2026, 10, 26) and rel.bdays(4) == MON and rel.months(1) == date(2026, 11, 27)
    assert rel.at(0, "10:00") == datetime(2026, 10, 27, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    weekend = RelativeDates(SAT, CAL)
    assert [weekend.bdays(n) for n in (-1, 0, 1)] == [FRI, MON, MON + timedelta(days=1)]


def test_rebase_keeps_offsets_so_open_work_stays_in_the_future():
    old_t = date(2026, 6, 1)  # a Monday
    due = [date(2026, 6, 3), date(2026, 6, 8), date(2026, 5, 29)]  # +2, +5 working days, -1
    for new_t in (date(2026, 10, 27), date(2027, 3, 1)):
        rel = RelativeDates(new_t, CAL)
        moved = [rel.rebase(d, old_t) for d in due]
        assert [CAL.business_days_between(new_t, d) for d in moved] == [2, 5, -1]
        assert moved[0] > new_t and moved[2] < new_t
    t = datetime(2026, 6, 3, 14, 15, tzinfo=ZoneInfo("Europe/Berlin"))
    assert RelativeDates(date(2026, 10, 27), CAL).rebase(t, old_t).timetz() == time(14, 15, tzinfo=t.tzinfo)
    assert RelativeDates(date(2026, 10, 27), CAL).rebase(date(2026, 6, 6), old_t, business=False) == date(
        2026, 11, 1
    )


def test_default_anchor_is_today():
    assert anchor_date(date(2026, 1, 1)) == date(2026, 1, 1)
    assert anchor_date(None, CAL) == datetime.now(ZoneInfo("Europe/Berlin")).date()
    assert RelativeDates.from_today(CAL).anchor == anchor_date(None, CAL)


def test_bad_calendar_is_refused():
    with pytest.raises(ValueError):
        BusinessCalendar(weekend=tuple(range(7)))
    with pytest.raises(ValueError):
        BusinessCalendar(day_start=time(18), day_end=time(9))
    with pytest.raises(ValueError):
        PeriodAxis(date(2026, 1, 1), "year", 3)  # type: ignore[arg-type]
