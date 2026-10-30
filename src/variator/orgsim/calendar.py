"""Business calendar, period axis and dates relative to the anchor date T.

- ``BusinessCalendar``: working days, holidays and work hours in one time zone (the
  same rules as ``kbsim.densify.Calendar``: Mon-Fri 09:30-18:30 by default).
- ``PeriodAxis``: periods of a day, a business day, a week or a month, counted from T.
- ``RelativeDates``: dates as offsets from T (default: today), so synthetic data does
  not look overdue on the day somebody opens it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import cached_property
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np

Grain = Literal["day", "bday", "week", "month"]
GRAINS: tuple[Grain, ...] = ("day", "bday", "week", "month")


def _day(d: date | datetime | np.datetime64 | str) -> np.datetime64:
    if isinstance(d, datetime):
        d = d.date()
    return np.datetime64(d, "D")


def _to_date(x: np.datetime64) -> date:
    return x.astype("datetime64[D]").astype(object)


def add_months(d: date, n: int) -> date:
    """``d`` plus ``n`` months; the day is clamped to the last day of the month (Jan 31 + 1 = Feb 28)."""
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    first_next = date(y + (m == 12), m % 12 + 1, 1)
    return date(y, m, min(d.day, (first_next - timedelta(days=1)).day))


@dataclass(frozen=True)
class BusinessCalendar:
    """Working days and hours of a company in its time zone. Monday is weekday 0."""

    timezone: str = "UTC"
    holidays: tuple[date, ...] = ()
    weekend: tuple[int, ...] = (5, 6)
    day_start: time = time(9, 30)
    day_end: time = time(18, 30)

    def __post_init__(self) -> None:
        object.__setattr__(self, "holidays", tuple(sorted(set(self.holidays))))
        object.__setattr__(self, "weekend", tuple(sorted(set(self.weekend))))
        if not set(self.weekend) <= set(range(7)) or len(self.weekend) == 7:
            raise ValueError("weekend must be weekday numbers 0..6 and leave at least one working day")
        if self.day_end <= self.day_start:
            raise ValueError("day_end must be after day_start")
        ZoneInfo(self.timezone)  # raises for an unknown zone

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @cached_property
    def _busdays(self) -> np.busdaycalendar:
        mask = [0 if d in self.weekend else 1 for d in range(7)]
        return np.busdaycalendar(weekmask=mask, holidays=[np.datetime64(h, "D") for h in self.holidays])

    @property
    def work_hours(self) -> float:
        """Working hours in one working day."""
        span = datetime.combine(date.min, self.day_end) - datetime.combine(date.min, self.day_start)
        return span.total_seconds() / 3600

    # ------------------------------------------------------------------ days
    def is_workday(self, d: date | datetime) -> bool:
        return bool(np.is_busday(_day(d), busdaycal=self._busdays))

    def workdays(self, start: date, end: date) -> list[date]:
        """Working days in [start, end)."""
        days = np.arange(_day(start), _day(end), dtype="datetime64[D]")
        return [_to_date(d) for d in days[np.is_busday(days, busdaycal=self._busdays)]]

    def roll_forward(self, d: date) -> date:
        """``d`` when it is a working day, else the next working day."""
        return _to_date(np.busday_offset(_day(d), 0, roll="forward", busdaycal=self._busdays))

    def roll_backward(self, d: date) -> date:
        """``d`` when it is a working day, else the previous working day."""
        return _to_date(np.busday_offset(_day(d), 0, roll="backward", busdaycal=self._busdays))

    def add_business_days(self, d: date | datetime, n: int) -> date:
        """The working day ``n`` working days after ``d`` (before it when n < 0).

        A non-working ``d`` counts from its boundary: Saturday + 1 = Monday,
        Saturday - 1 = Friday, Saturday + 0 = Monday.
        """
        roll = "backward" if n > 0 else "forward"
        return _to_date(np.busday_offset(_day(d), int(n), roll=roll, busdaycal=self._busdays))

    def business_days_between(self, a: date | datetime, b: date | datetime) -> int:
        """Working days in [a, b); negative when b < a (the count of [b, a))."""
        return int(np.busday_count(_day(a), _day(b), busdaycal=self._busdays))

    # ------------------------------------------------------------------ hours
    def at(self, d: date, hhmm: str | time) -> datetime:
        """A local time on day ``d`` (aware datetime)."""
        t = time.fromisoformat(hhmm) if isinstance(hhmm, str) else hhmm
        return datetime.combine(d, t, tzinfo=self.tz)

    def local(self, t: datetime) -> datetime:
        if t.tzinfo is None:
            raise ValueError("use an aware datetime")
        return t.astimezone(self.tz)

    def is_work_time(self, t: datetime) -> bool:
        lt = self.local(t)
        return self.is_workday(lt.date()) and self.day_start <= lt.time() < self.day_end

    def next_work_time(self, t: datetime) -> datetime:
        """``t`` when it is work time, else the start of the next working period."""
        lt = self.local(t)
        if self.is_workday(lt.date()) and lt.time() < self.day_end:
            return max(lt, self.at(lt.date(), self.day_start))
        return self.at(self.add_business_days(lt.date(), 1), self.day_start)

    def add_work_hours(self, t: datetime, hours: float) -> datetime:
        """The moment ``hours`` of work time after ``t``; non-working time is skipped."""
        if hours < 0:
            raise ValueError("hours must not be negative")
        left = timedelta(hours=hours)
        cur = self.next_work_time(t)
        while True:
            day_end = self.at(cur.date(), self.day_end)
            if left <= day_end - cur:
                return cur + left
            left -= day_end - cur
            cur = self.next_work_time(day_end)


@dataclass(frozen=True)
class PeriodAxis:
    """``count`` periods of one grain from the anchor T. Period 0 starts at T.

    - ``day``: calendar days; ``week``: 7 days from T; ``month``: from T to T + 1 month
      (the day is clamped, see ``add_months``).
    - ``bday``: working days. Period k starts on the k-th working day at or after T and
      ends before the next one, so a weekend belongs to the Friday before it.

    Dates before T get negative indices; dates after the axis get indices >= count.
    """

    anchor: date
    grain: Grain
    count: int
    calendar: BusinessCalendar = field(default_factory=BusinessCalendar)

    def __post_init__(self) -> None:
        if self.grain not in GRAINS:
            raise ValueError(f"grain must be one of {GRAINS}")
        if self.count < 1:
            raise ValueError("count must be >= 1")
        if isinstance(self.anchor, datetime):
            object.__setattr__(self, "anchor", self.anchor.date())

    def start(self, k: int) -> date:
        if self.grain == "day":
            return self.anchor + timedelta(days=k)
        if self.grain == "week":
            return self.anchor + timedelta(days=7 * k)
        if self.grain == "month":
            return add_months(self.anchor, k)
        first = self.calendar.roll_forward(self.anchor)
        return self.calendar.add_business_days(first, k) if k else first

    def end(self, k: int) -> date:
        """The first day after period ``k`` (exclusive end)."""
        return self.start(k + 1)

    def starts(self) -> list[date]:
        return [self.start(k) for k in range(self.count)]

    def labels(self) -> list[str]:
        """ISO dates of the period starts."""
        return [d.isoformat() for d in self.starts()]

    def _days(self, dates: Iterable[date | datetime | str]) -> np.ndarray:
        out = []
        for d in dates:
            if isinstance(d, str):
                d = datetime.fromisoformat(d) if "T" in d else date.fromisoformat(d)
            if isinstance(d, datetime):
                d = self.calendar.local(d).date() if d.tzinfo else d.date()
            out.append(np.datetime64(d, "D"))
        return np.array(out, dtype="datetime64[D]")

    def bucket(self, dates: Iterable[date | datetime | str] | np.ndarray) -> np.ndarray:
        """Period index of each date (int64). Aware datetimes use the calendar time zone."""
        if isinstance(dates, np.ndarray) and np.issubdtype(dates.dtype, np.datetime64):
            d = dates.astype("datetime64[D]")
        else:
            d = self._days(dates)
        t0 = np.datetime64(self.anchor, "D")
        if self.grain == "day":
            return (d - t0).astype(np.int64)
        if self.grain == "week":
            return np.floor_divide((d - t0).astype(np.int64), 7)
        if self.grain == "bday":
            first = np.datetime64(self.calendar.roll_forward(self.anchor), "D")
            cal = self.calendar._busdays
            after = np.busday_count(
                first, np.maximum(d + 1, first), busdaycal=cal
            )  # working days in [first, d]
            before = np.busday_count(
                np.minimum(d + 1, first), first, busdaycal=cal
            )  # working days in (d, first)
            return np.where(d >= first, after - 1, -before - 1).astype(np.int64)
        month = d.astype("datetime64[M]")
        months = (month - t0.astype("datetime64[M]")).astype(np.int64)
        month_start = month.astype("datetime64[D]")
        length = ((month + 1).astype("datetime64[D]") - month_start).astype(np.int64)
        boundary = month_start + np.minimum(self.anchor.day, length) - 1
        return months - (d < boundary)

    def index(self, d: date | datetime | str) -> int:
        return int(self.bucket([d])[0])

    def counts(
        self, dates: Iterable[date | datetime | str], weights: Iterable[float] | None = None
    ) -> np.ndarray:
        """Count (or sum ``weights``) per period; dates outside the axis are left out."""
        idx = self.bucket(dates)
        w = np.ones(len(idx)) if weights is None else np.asarray(list(weights), dtype=np.float64)
        inside = (idx >= 0) & (idx < self.count)
        return np.bincount(idx[inside], weights=w[inside], minlength=self.count)


def anchor_date(anchor: date | None = None, calendar: BusinessCalendar | None = None) -> date:
    """``anchor``, or today in the calendar time zone."""
    if anchor is not None:
        return anchor
    tz = (calendar or BusinessCalendar()).tz
    return datetime.now(tz).date()


@dataclass(frozen=True)
class RelativeDates:
    """Dates as offsets from the anchor T, so generated data stays current.

    Generate every date as ``rel.bdays(n)`` or ``rel.days(n)``. A due date of an open
    task is then always after T, and a closed task is always before it. ``rebase``
    moves dates of a fixture made with another anchor and keeps their offsets.
    """

    anchor: date
    calendar: BusinessCalendar = field(default_factory=BusinessCalendar)

    @classmethod
    def from_today(cls, calendar: BusinessCalendar | None = None) -> RelativeDates:
        cal = calendar or BusinessCalendar()
        return cls(anchor_date(None, cal), cal)

    def days(self, n: int) -> date:
        return self.anchor + timedelta(days=n)

    def bdays(self, n: int) -> date:
        """The working day n working days from T (n = 0: T, or the next working day)."""
        return self.calendar.add_business_days(self.calendar.roll_forward(self.anchor), n)

    def months(self, n: int) -> date:
        return add_months(self.anchor, n)

    def at(self, n_bdays: int, hhmm: str | time) -> datetime:
        """Local time on the working day ``bdays(n_bdays)``."""
        return self.calendar.at(self.bdays(n_bdays), hhmm)

    def rebase(self, d: date | datetime, source_anchor: date, *, business: bool = True) -> date | datetime:
        """Move ``d`` from ``source_anchor`` to T and keep its offset.

        ``business=True`` keeps the offset in working days (a working day stays one;
        a day off keeps its distance from the working day before it); else the offset
        is in calendar days. A datetime keeps its time of day.
        """
        day = d.date() if isinstance(d, datetime) else d
        if business:
            cal = self.calendar
            prev = cal.roll_backward(day)
            n = cal.business_days_between(source_anchor, prev)
            new = cal.add_business_days(cal.roll_forward(self.anchor), n) + timedelta(days=(day - prev).days)
        else:
            new = self.anchor + (day - source_anchor)
        return datetime.combine(new, d.timetz()) if isinstance(d, datetime) else new
