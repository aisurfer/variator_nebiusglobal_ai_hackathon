"""Bank holidays of England and Wales, and the company calendar.

Rules: New Year's Day, Good Friday, Easter Monday, the first and the last Monday of May,
the last Monday of August, Christmas Day and Boxing Day. A holiday on a weekend moves to
the next free working day. One-off holidays (coronations, jubilees) are not included.
"""

from __future__ import annotations

from datetime import date, timedelta

from variator.orgsim.calendar import BusinessCalendar


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _first_monday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(7 - d.weekday()) % 7)


def _last_monday(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=d.weekday())


def england_bank_holidays(year: int) -> list[date]:
    """Bank holidays of one year, with the substitute days for weekends."""
    easter = easter_sunday(year)
    out = {
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        _first_monday(year, 5),
        _last_monday(year, 5),
        _last_monday(year, 8),
    }
    new_year = date(year, 1, 1)
    while new_year.weekday() >= 5:
        new_year += timedelta(days=1)
    out.add(new_year)
    # Christmas and Boxing Day: each moves to the next working day that is not taken.
    for d in (date(year, 12, 25), date(year, 12, 26)):
        while d.weekday() >= 5 or d in out:
            d += timedelta(days=1)
        out.add(d)
    return sorted(out)


def company_calendar(timezone: str, first_year: int, last_year: int) -> BusinessCalendar:
    """Mon-Fri, 09:00-18:00 local, without the bank holidays of the years given."""
    from datetime import time

    days = [d for y in range(first_year, last_year + 1) for d in england_bank_holidays(y)]
    return BusinessCalendar(
        timezone=timezone, holidays=tuple(days), day_start=time(9, 0), day_end=time(18, 0)
    )
