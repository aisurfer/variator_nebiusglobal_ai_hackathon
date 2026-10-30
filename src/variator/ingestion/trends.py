"""Period aggregation and trend statistics for a numeric column over a date column.

Deterministic: these numbers are what Lightning later phrases as facts, and
what the anti-hallucination guard checks fact values against.
"""

from __future__ import annotations

import calendar
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

Granularity = Literal["month", "quarter", "year"]
Aggregation = Literal["sum", "mean"]

PERIODS_PER_YEAR: dict[str, int] = {"month": 12, "quarter": 4, "year": 1}
MAX_PERIODS = 1200  # wider spans usually mean garbage dates (e.g. 1900-01-01 placeholders)
WINDOW_MIN_COVERAGE = 0.75  # share of periods a yearly window needs before it is annualized
DENSE_DAYS_PER_MONTH = 8
MIN_SEASONAL_AMPLITUDE = 0.01  # max - min index below this is reported as no seasonality
_MONTH_LABELS = [calendar.month_abbr[i] for i in range(1, 13)]


class PeriodValue(BaseModel):
    period: str = Field(description="2025-03, 2025-Q1 or 2025")
    value: float | None = Field(description="None when no rows fall into the period")


class SeasonalIndex(BaseModel):
    period_of_year: int = Field(description="1-12 for months, 1-4 for quarters")
    label: str
    index: float = Field(description="Typical level relative to the trend; 1.2 = 20% above")


class SeriesTrend(BaseModel):
    """Trend statistics for one numeric column aggregated per period."""

    column: str
    date_column: str
    aggregation: Aggregation
    granularity: Granularity
    first_period: str
    last_period: str
    periods: int = Field(description="Periods in the range, including empty ones")
    missing_periods: int
    excluded_partial_periods: list[str] = Field(default_factory=list)
    trailing_year: float | None = Field(
        default=None, description="Sum (or mean) over the last 12 months / 4 quarters / 1 year"
    )
    previous_year: float | None = Field(default=None, description="Same, for the year before")
    yoy_growth: float | None = Field(default=None, description="trailing_year / previous_year - 1")
    cagr: float | None = Field(default=None, description="Compound annual growth, first vs last year")
    cagr_years: float | None = None
    cv: float | None = Field(default=None, description="Coefficient of variation of period values")
    seasonality: list[SeasonalIndex] = Field(default_factory=list)
    seasonality_method: Literal["ratio_to_moving_average", "ratio_to_mean"] | None = None
    values: list[PeriodValue] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def peak(self) -> SeasonalIndex | None:
        return max(self.seasonality, key=lambda s: s.index) if self.seasonality else None

    @property
    def trough(self) -> SeasonalIndex | None:
        return min(self.seasonality, key=lambda s: s.index) if self.seasonality else None


def _month_index(d: date) -> int:
    return d.year * 12 + d.month - 1


def _is_month_end(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def is_day_level(dates: Sequence[date]) -> bool:
    """True when dates fall on arbitrary days (not all on the 1st or all on month ends)."""
    return any(d.day != 1 for d in dates) and not all(_is_month_end(d) for d in dates)


def detect_granularity(dates: Sequence[date]) -> Granularity:
    """Month, quarter or year, from the spacing of distinct months in the data."""
    months = sorted({_month_index(d) for d in dates})
    if len(months) < 2 or is_day_level(dates):
        return "month"
    gaps = np.diff(months)
    if np.all(gaps % 12 == 0) and len({m % 12 for m in months}) == 1:
        return "year"
    if np.all(gaps % 3 == 0) and len({m % 3 for m in months}) == 1:
        return "quarter"
    return "month"


def period_label(index: int, granularity: Granularity) -> str:
    if granularity == "month":
        return f"{index // 12:04d}-{index % 12 + 1:02d}"
    if granularity == "quarter":
        return f"{index // 4:04d}-Q{index % 4 + 1}"
    return f"{index:04d}"


def _window(series: np.ndarray, start: int, stop: int, aggregation: Aggregation) -> tuple[float | None, bool]:
    """Aggregate ``series[start:stop]``; annualize when a few periods are missing."""
    window = series[start:stop]
    present = window[~np.isnan(window)]
    size = len(window)
    if len(present) == 0 or len(present) < math.ceil(WINDOW_MIN_COVERAGE * size):
        return None, False
    if aggregation == "mean":
        return float(present.mean()), False
    if len(present) == size:
        return float(present.sum()), False
    return float(present.mean() * size), True


def _seasonality(
    series: np.ndarray, start_index: int, granularity: Granularity
) -> tuple[list[SeasonalIndex], str | None]:
    p = PERIODS_PER_YEAR[granularity]
    n = len(series)
    if p == 1 or n < p:
        return [], None
    if n >= 2 * p:
        # Classical decomposition: ratio to a centered 2xp moving average removes trend.
        weights = np.r_[0.5, np.ones(p - 1), 0.5] / p
        moving = np.convolve(series, weights, mode="valid")  # centers at p/2 .. n-p/2-1
        ratios = np.full(n, np.nan)
        half = p // 2
        centered = series[half : half + len(moving)]
        with np.errstate(divide="ignore", invalid="ignore"):
            ratios[half : half + len(moving)] = np.where(moving > 0, centered / moving, np.nan)
        method = "ratio_to_moving_average"
    else:
        mean = np.nanmean(series)
        if not mean > 0:
            return [], None
        ratios = series / mean
        method = "ratio_to_mean"
    positions = (start_index + np.arange(n)) % p
    raw: dict[int, float] = {}
    for pos in range(p):
        vals = ratios[(positions == pos) & ~np.isnan(ratios)]
        if len(vals):
            raw[pos] = float(vals.mean())
    if len(raw) < max(2, p // 2):
        return [], None
    scale = float(np.mean(list(raw.values())))
    if not scale > 0 or (max(raw.values()) - min(raw.values())) / scale < MIN_SEASONAL_AMPLITUDE:
        return [], None
    labels = _MONTH_LABELS if p == 12 else [f"Q{i}" for i in range(1, 5)]
    return [
        SeasonalIndex(period_of_year=pos + 1, label=labels[pos], index=value / scale)
        for pos, value in sorted(raw.items())
    ], method


@dataclass(frozen=True)
class DateAxis:
    """A date column prepared once and shared by every trend computed over it."""

    valid: np.ndarray  # bool per row
    month: np.ndarray  # int64 year*12 + month-1 per row; -1 where the date is missing
    granularity: Granularity
    day_level: bool
    dense_days: bool  # >= DENSE_DAYS_PER_MONTH distinct dates per month: daily data, not a sample
    first: date | None
    last: date | None

    @classmethod
    def from_dates(cls, dates: Sequence[date | None]) -> DateAxis:
        present = [d for d in dates if d is not None]
        month = np.fromiter(
            (_month_index(d) if d is not None else -1 for d in dates), dtype=np.int64, count=len(dates)
        )
        return cls(
            valid=month >= 0,
            month=month,
            granularity=detect_granularity(present) if present else "month",
            day_level=is_day_level(present) if present else False,
            dense_days=bool(present)
            and len(set(present)) >= DENSE_DAYS_PER_MONTH * len({_month_index(d) for d in present}),
            first=min(present) if present else None,
            last=max(present) if present else None,
        )


def build_trend(
    column: str,
    date_column: str,
    dates: Sequence[date | None] | DateAxis,
    values: np.ndarray,
    *,
    aggregation: Aggregation = "sum",
    granularity: Granularity | None = None,
) -> SeriesTrend | None:
    """Aggregate ``values`` per period of ``dates`` and compute trend statistics.

    Periods with no rows are missing (NaN), not zero. For day-level data summed per
    month, a partial first or last month (export cut mid-month) is excluded so it
    does not fake a drop. Pass a ``DateAxis`` to reuse date parsing across columns.
    """
    axis = dates if isinstance(dates, DateAxis) else DateAxis.from_dates(dates)
    values = np.asarray(values, dtype=float)
    mask = axis.valid & ~np.isnan(values)
    if mask.sum() < 2:
        return None
    granularity = granularity or axis.granularity
    months = axis.month[mask]
    idx = months if granularity == "month" else months // (3 if granularity == "quarter" else 12)
    vals = values[mask]
    start, end = int(idx.min()), int(idx.max())
    n = end - start + 1
    if n > MAX_PERIODS:
        return None
    sums = np.bincount(idx - start, weights=vals, minlength=n)
    counts = np.bincount(idx - start, minlength=n)
    with np.errstate(invalid="ignore", divide="ignore"):
        series = np.where(counts > 0, sums if aggregation == "sum" else sums / np.maximum(counts, 1), np.nan)

    excluded: list[str] = []
    partial_ok = (
        aggregation == "sum" and granularity == "month" and axis.day_level and axis.dense_days and n > 2
    )
    if partial_ok and axis.first is not None and axis.last is not None:
        first, last = axis.first, axis.last
        if end == _month_index(last) and last.day < calendar.monthrange(last.year, last.month)[1] - 2:
            excluded.append(period_label(end, granularity))
            series, end = series[:-1], end - 1
        if start == _month_index(first) and first.day > 3:
            excluded.append(period_label(start, granularity))
            series, start = series[1:], start + 1
    n = len(series)
    p = PERIODS_PER_YEAR[granularity]
    notes: list[str] = []

    trailing = previous = growth = cagr = cagr_years = None
    if n >= p:
        trailing, annualized = _window(series, n - p, n, aggregation)
        if annualized:
            notes.append("trailing_year annualized from incomplete periods")
    if n >= 2 * p:
        previous, annualized = _window(series, n - 2 * p, n - p, aggregation)
        if annualized:
            notes.append("previous_year annualized from incomplete periods")
        if trailing is not None and previous is not None and previous > 0:
            growth = trailing / previous - 1
        first_year, _ = _window(series, 0, p, aggregation)
        if first_year is not None and trailing is not None and first_year > 0 and trailing > 0:
            cagr_years = (n - p) / p
            cagr = (trailing / first_year) ** (1 / cagr_years) - 1

    present = series[~np.isnan(series)]
    cv = None
    if len(present) >= 3 and present.mean() != 0:
        cv = float(present.std(ddof=1) / abs(present.mean()))
        cv = 0.0 if cv < 1e-9 else cv  # float noise on a constant series

    seasonality, method = _seasonality(series, start, granularity)
    missing = int(np.isnan(series).sum())
    if missing:
        notes.append(f"{missing} of {n} periods have no data")
    return SeriesTrend(
        column=column,
        date_column=date_column,
        aggregation=aggregation,
        granularity=granularity,
        first_period=period_label(start, granularity),
        last_period=period_label(end, granularity),
        periods=n,
        missing_periods=missing,
        excluded_partial_periods=excluded,
        trailing_year=trailing,
        previous_year=previous,
        yoy_growth=growth,
        cagr=cagr,
        cagr_years=cagr_years,
        cv=cv,
        seasonality=seasonality,
        seasonality_method=method,
        values=[
            PeriodValue(period=period_label(start + i, granularity),
                        value=None if math.isnan(v) else float(v))
            for i, v in enumerate(series)
        ],
        notes=notes,
    )
