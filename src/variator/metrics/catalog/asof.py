"""Time rules of the catalogue: the as-of cut, weekly dates, calendar months and business days.

The as-of rule: a metric "as of date D" uses only what was known at the end of D in the company time zone.
A record version counts when its known time (the event ``ts``) is at or before that instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import numpy as np

DAYS_PER_MONTH = Decimal("30.4375")  # 365.25 / 12


def parse_ts(value: str) -> datetime:
    """An ISO timestamp as an aware datetime (no offset: UTC)."""
    d = datetime.fromisoformat(str(value).strip())
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)


def parse_date(value: object) -> date | None:
    """A date from "YYYY-MM-DD" (or the date part of a timestamp); empty -> None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return date.fromisoformat(s[:10])


@dataclass(frozen=True)
class Clock:
    """The company time zone and the instants of its days."""

    tz: ZoneInfo

    def start_of_day(self, d: date) -> datetime:
        return datetime.combine(d, time(0), tzinfo=self.tz)

    def end_of_day(self, d: date) -> datetime:
        """The last instant of the local day ``d``: the as-of cut of that date."""
        return self.start_of_day(d + timedelta(days=1)) - timedelta(microseconds=1)

    def local_date(self, ts: datetime) -> date:
        return ts.astimezone(self.tz).date()


def week_dates(as_of: date, n: int) -> list[date]:
    """``n`` weekly as-of dates ending at ``as_of``, oldest first."""
    return [as_of - timedelta(days=7 * k) for k in reversed(range(n))]


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_first(key: str) -> date:
    y, m = key.split("-")[:2]
    return date(int(y), int(m[:2]), 1)


def add_months(key: str, n: int) -> str:
    d = month_first(key)
    k = d.year * 12 + d.month - 1 + n
    return f"{k // 12:04d}-{k % 12 + 1:02d}"


def month_last(key: str) -> date:
    return month_first(add_months(key, 1)) - timedelta(days=1)


def period_months(period: str) -> list[str]:
    """The calendar months of an invoice period: "2026-07" or "2026-07/3m" (three months from July)."""
    head, _, tail = str(period).partition("/")
    n = int(tail.rstrip("m")) if tail else 1
    return [add_months(head[:7], k) for k in range(n)]


def bdays(a: date, b: date) -> int:
    """Business days (Monday to Friday) from ``a`` (inclusive) to ``b`` (exclusive); negative when b < a."""
    return int(np.busday_count(a, b))
