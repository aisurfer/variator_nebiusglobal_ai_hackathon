"""Deterministic profiling of CSV exports (no LLM).

``profile_csv`` sniffs the delimiter, finds the header row, types every column
(numeric with thousands separators / decimal comma / currency, date, categorical,
free text), computes summary statistics and, when the table has a date column,
monthly (or quarterly / yearly) trend statistics. The resulting ``TableProfile``
is both the input for Lightning (``to_prompt``) and the ground truth for the
anti-hallucination guard (``grounding_numbers``).
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections import Counter
from datetime import date
from enum import StrEnum
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from variator.ingestion.grounding import source_numbers
from variator.ingestion.parsing import (
    DateColumn,
    NumericColumn,
    clean_cell,
    parse_dates,
    parse_month,
    parse_numeric,
    tokenize_number,
)
from variator.ingestion.trends import DateAxis, SeriesTrend, build_trend, detect_granularity

DELIMITERS = (",", ";", "\t", "|")
SNIFF_LINES = 60
MAX_TREND_COLUMNS = 12
TOP_VALUES = 10
CATEGORICAL_MAX_UNIQUE = 50
csv.field_size_limit(10_000_000)  # long free-text cells in CRM exports exceed the 128 KB default

_TOTAL_RE = re.compile(
    r"^((grand\s+|sub-?\s?)?totals?|итого|всего|summe|gesamt|totale|total general)\s*:?$", re.IGNORECASE
)
_IDENTIFIER_NAME_RE = re.compile(
    r"(^|[_\s.-])(id|uuid|guid|code|zip|postcode|postal|phone|sku|ean|upc|inn|account|acct|number|no|nr|#)$",
    re.IGNORECASE,
)
_YEAR_NAMES = frozenset({"year", "yr", "fy", "fiscal_year", "fiscal year", "год"})
_MONTH_NAMES = frozenset({"month", "месяц"})
_CALENDAR_NAMES = _YEAR_NAMES | _MONTH_NAMES | {"quarter", "qtr", "week", "day", "weekday", "квартал"}
_MEAN_AGGREGATION_RE = re.compile(
    r"price|rate|pct|percent|%|ratio|margin|share|avg|average|mean|median|per[_\s]|_per_|index|score|"
    r"temperature|nps|csat|conversion|yield|churn|utili[sz]ation|occupancy|aov|arpu|unit[_\s]cost",
    re.IGNORECASE,
)


class ColumnKind(StrEnum):
    NUMERIC = "numeric"
    DATE = "date"
    CATEGORICAL = "categorical"
    TEXT = "text"
    EMPTY = "empty"


class NumericStats(BaseModel):
    count: int
    sum: float
    mean: float
    median: float
    min: float
    max: float
    std: float | None = None


class CategoryCount(BaseModel):
    value: str
    count: int
    share: float = Field(description="Share of non-null cells")


class ColumnProfile(BaseModel):
    name: str
    kind: ColumnKind
    non_null: int
    missing_share: float
    unique: int
    unit: str | None = Field(default=None, description='"%" or an ISO currency code seen in cells')
    role: Literal["measure", "identifier", "calendar"] | None = Field(
        default=None, description="Numeric columns only: identifiers and calendar columns get no trends"
    )
    invalid_values: int = Field(default=0, description="Non-null cells that did not parse as the kind")
    numeric: NumericStats | None = None
    date_min: str | None = None
    date_max: str | None = None
    date_format: str | None = None
    granularity: str | None = None
    top_values: list[CategoryCount] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list, description="Sample of cells that did not parse")


class TableProfile(BaseModel):
    """Everything known about a table without asking an LLM."""

    name: str
    delimiter: str
    row_count: int
    truncated: bool = False
    header_row: int = Field(default=0, description="0-based line of the header (>0: preamble skipped)")
    columns: list[ColumnProfile]
    date_column: str | None = None
    trends: list[SeriesTrend] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def column(self, name: str) -> ColumnProfile | None:
        return next((c for c in self.columns if c.name == name), None)

    def trend(self, column: str) -> SeriesTrend | None:
        return next((t for t in self.trends if t.column == column), None)

    def grounding_numbers(self) -> list[float]:
        """Every number this profile states; ratios are also included as percentages."""
        numbers: list[float] = [self.row_count, len(self.columns), *source_numbers(self.name)]
        ratios: list[float] = []
        for col in self.columns:
            numbers += [col.non_null, col.unique, col.invalid_values, *source_numbers(col.name)]
            ratios.append(col.missing_share)
            if col.numeric:
                s = col.numeric
                numbers += [s.count, s.sum, s.mean, s.median, s.min, s.max]
                numbers += [s.std] if s.std is not None else []
                numbers += [s.max - s.min]
            for top in col.top_values:
                numbers += [top.count, *source_numbers(top.value)]
                ratios.append(top.share)
            for bound in (col.date_min, col.date_max):
                if bound:
                    numbers.append(int(bound[:4]))
        for t in self.trends:
            numbers += [t.periods, t.missing_periods]
            numbers += [v.value for v in t.values if v.value is not None]
            numbers += [x for x in (t.trailing_year, t.previous_year, t.cagr_years) if x is not None]
            if t.trailing_year is not None and t.previous_year is not None:
                numbers.append(t.trailing_year - t.previous_year)
            ratios += [x for x in (t.yoy_growth, t.cagr, t.cv) if x is not None]
            for s in t.seasonality:
                ratios += [s.index, s.index - 1]
        return [*numbers, *ratios, *(r * 100 for r in ratios)]

    def to_prompt(self, *, max_periods: int = 24) -> str:
        """Compact text rendering for an LLM. Numbers keep >= 3 significant digits
        (error < 0.5%), so values copied from it pass the 1% grounding check."""
        lines = [
            f"Table: {self.name}",
            f"Rows: {self.row_count}{' (truncated)' if self.truncated else ''}; columns: {len(self.columns)}",
            "Columns:",
        ]
        for col in self.columns:
            lines.append("- " + _render_column(col))
        if self.trends:
            trend = self.trends[0]
            lines.append(
                f"Trends by {trend.date_column} (per {trend.granularity}; sum per period unless noted):"
            )
            for t in self.trends:
                lines.extend(_render_trend(t, max_periods=max_periods))
        if self.warnings:
            lines.append("Data quality warnings:")
            lines.extend(f"- {w}" for w in self.warnings)
        return "\n".join(lines)


# ---------------------------------------------------------------- rendering


def fmt_num(x: float | None) -> str:
    """Render with >= 3 significant digits; integers >= 1000 in full with thousands commas."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    ax = abs(x)
    if ax >= 1e15:
        return f"{x:.4g}"
    if ax >= 1000 or (float(x).is_integer() and ax >= 1):
        return f"{x:,.0f}"
    if ax == 0:
        return "0"
    if ax < 1e-4:
        return f"{x:.3g}"
    decimals = max(0, 2 - math.floor(math.log10(ax)))
    return f"{x:,.{decimals}f}"


def fmt_pct(ratio: float | None, *, signed: bool = False) -> str:
    if ratio is None:
        return "n/a"
    text = fmt_num(ratio * 100)
    if signed and ratio > 0:
        text = "+" + text
    return text + "%"


def _render_column(col: ColumnProfile) -> str:
    head = f"{col.name} [{col.kind}"
    if col.unit:
        head += f", {col.unit}"
    if col.role and col.role != "measure":
        head += f", {col.role}"
    head += "]"
    parts = [f"missing {fmt_pct(col.missing_share)}"]
    if col.invalid_values:
        parts.append(f"{col.invalid_values} unparseable cells")
    if col.kind == ColumnKind.NUMERIC and col.numeric and col.role == "measure":
        s = col.numeric
        parts.insert(
            0,
            f"sum {fmt_num(s.sum)}; mean {fmt_num(s.mean)}; median {fmt_num(s.median)}; "
            f"min {fmt_num(s.min)}; max {fmt_num(s.max)}",
        )
    elif col.kind == ColumnKind.NUMERIC and col.numeric:
        parts.insert(0, f"range {fmt_num(col.numeric.min)}..{fmt_num(col.numeric.max)}")
    elif col.kind == ColumnKind.DATE:
        parts.insert(0, f"{col.date_min} .. {col.date_max} ({col.granularity})")
    elif col.kind == ColumnKind.CATEGORICAL:
        tops = ", ".join(f"{t.value} {t.count} ({fmt_pct(t.share)})" for t in col.top_values[:TOP_VALUES])
        parts.insert(0, f"{col.unique} distinct; top: {tops}")
    elif col.kind == ColumnKind.TEXT:
        examples = "; ".join(repr(t.value) for t in col.top_values)
        parts.insert(0, f"{col.unique} distinct; e.g. {examples}")
    return f"{head} " + "; ".join(parts)


def _render_trend(t: SeriesTrend, *, max_periods: int) -> list[str]:
    agg = "" if t.aggregation == "sum" else " (mean per period)"
    stats = [f"{t.periods} periods {t.first_period}..{t.last_period}"]
    if t.missing_periods:
        stats.append(f"{t.missing_periods} empty")
    if t.excluded_partial_periods:
        stats.append("partial periods excluded: " + ", ".join(t.excluded_partial_periods))
    latest, prior = ("latest year", "prior year") if t.granularity == "year" else ("last 12 months",
                                                                                   "prior 12 months")
    if t.trailing_year is not None:
        stats.append(f"{latest} {fmt_num(t.trailing_year)}")
    if t.previous_year is not None:
        stats.append(f"{prior} {fmt_num(t.previous_year)}")
    if t.yoy_growth is not None:
        stats.append(f"YoY growth {fmt_pct(t.yoy_growth, signed=True)}")
    if t.cagr is not None:
        stats.append(f"CAGR {fmt_pct(t.cagr, signed=True)} over {fmt_num(t.cagr_years)} years")
    if t.cv is not None:
        stats.append(f"coefficient of variation {fmt_num(t.cv)}")
    if t.peak and t.trough:
        stats.append(
            f"seasonality peak {t.peak.label} {fmt_num(t.peak.index)}, trough {t.trough.label} "
            f"{fmt_num(t.trough.index)}"
        )
    lines = [f"- {t.column}{agg}: " + "; ".join(stats)]
    if t.notes:
        lines.append("  notes: " + "; ".join(t.notes))
    shown = t.values[-max_periods:]
    lines.append(
        f"  values (last {len(shown)}): "
        + ", ".join(f"{v.period} {fmt_num(v.value)}" for v in shown)
    )
    return lines


# ---------------------------------------------------------------- reading


def decode_text(data: bytes) -> str:
    """Decode an uploaded file: UTF-8 (with or without BOM), UTF-16 with BOM, else cp1252."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def sniff_delimiter(text: str) -> tuple[str, int]:
    """Return (delimiter, typical field count) from the first lines of the file."""
    sample_lines = [line for line in text.splitlines()[: SNIFF_LINES * 2] if line.strip()][:SNIFF_LINES]
    sample = "\n".join(sample_lines)
    best: tuple[float, int, str] = (0.0, 1, ",")
    for delimiter in DELIMITERS:
        rows = [r for r in csv.reader(io.StringIO(sample), delimiter=delimiter) if any(c.strip() for c in r)]
        if not rows:
            continue
        counts = Counter(len(r) for r in rows)
        width, freq = counts.most_common(1)[0]
        if width < 2:
            continue
        consistency = round(freq / len(rows), 1)
        if (consistency, width) > best[:2]:
            best = (consistency, width, delimiter)
    return best[2], best[1]


def _header_names(raw: list[str]) -> list[str]:
    names: list[str] = []
    seen: Counter[str] = Counter()
    for i, cell in enumerate(raw):
        name = " ".join(cell.replace("\ufeff", "").split()) or f"column_{i + 1}"
        seen[name] += 1
        names.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return names


def _is_total_row(row: list[str]) -> bool:
    for cell in row[:2]:
        text = cell.strip()
        if text and len(text) <= 16 and text[0].isalpha() and _TOTAL_RE.match(text):
            return True
    return False


def _looks_like_data(row: list[str]) -> bool:
    """A header has at least one cell that is neither a number nor a date."""
    cells = [c for c in (clean_cell(x) for x in row) if c is not None]
    return bool(cells) and all(
        tokenize_number(c) is not None or parse_dates([c], min_share=1.0) is not None for c in cells
    )


# ---------------------------------------------------------------- columns


def _numeric_role(name: str, values: np.ndarray) -> Literal["measure", "identifier", "calendar"]:
    key = name.strip().lower()
    if key in _CALENDAR_NAMES:
        return "calendar"
    if _IDENTIFIER_NAME_RE.search(key):
        return "identifier"
    present = values[~np.isnan(values)]
    if len(present) >= 20 and np.all(present == np.round(present)) and np.all(np.diff(present) == 1):
        return "identifier"  # a running row number
    return "measure"


def _numeric_profile(name: str, cells: list[str | None], parsed: NumericColumn, rows: int) -> ColumnProfile:
    present = parsed.values[~np.isnan(parsed.values)]
    stats = None
    if len(present):
        stats = NumericStats(
            count=len(present),
            sum=float(present.sum()),
            mean=float(present.mean()),
            median=float(np.median(present)),
            min=float(present.min()),
            max=float(present.max()),
            std=float(present.std(ddof=1)) if len(present) > 1 else None,
        )
    non_null = sum(c is not None for c in cells)
    return ColumnProfile(
        name=name,
        kind=ColumnKind.NUMERIC,
        non_null=non_null,
        missing_share=1 - non_null / rows if rows else 0.0,
        unique=len(np.unique(present)),
        unit=parsed.unit,
        role=_numeric_role(name, parsed.values),
        invalid_values=parsed.invalid_count,
        numeric=stats,
        examples=parsed.invalid_examples,
    )


def _date_profile(name: str, cells: list[str | None], parsed: DateColumn, rows: int) -> ColumnProfile:
    present = [d for d in parsed.values if d is not None]
    non_null = sum(c is not None for c in cells)
    return ColumnProfile(
        name=name,
        kind=ColumnKind.DATE,
        non_null=non_null,
        missing_share=1 - non_null / rows if rows else 0.0,
        unique=len(set(present)),
        invalid_values=parsed.invalid_count,
        date_min=min(present).isoformat(),
        date_max=max(present).isoformat(),
        date_format=parsed.fmt,
        granularity=detect_granularity(present),
    )


def _category_profile(name: str, counts: Counter[str], rows: int) -> ColumnProfile:
    non_null = sum(counts.values())
    unique = len(counts)
    repeats = unique < non_null
    kind = (
        ColumnKind.CATEGORICAL
        if (unique <= CATEGORICAL_MAX_UNIQUE and repeats) or unique <= 0.5 * non_null
        else ColumnKind.TEXT
    )
    top_n = TOP_VALUES if kind == ColumnKind.CATEGORICAL else 3
    return ColumnProfile(
        name=name,
        kind=kind,
        non_null=non_null,
        missing_share=1 - non_null / rows if rows else 0.0,
        unique=len(counts),
        top_values=[
            CategoryCount(value=value[:80], count=n, share=n / non_null)
            for value, n in counts.most_common(top_n)
        ],
    )


def _aggregation_for(col: ColumnProfile) -> Literal["sum", "mean"]:
    if col.unit == "%" or _MEAN_AGGREGATION_RE.search(col.name):
        return "mean"
    return "sum"


def _calendar_dates(
    profiles: list[ColumnProfile], numeric: dict[str, NumericColumn], columns: dict[str, list[str | None]]
) -> tuple[str, list[date | None]] | None:
    """Build dates from a year column (plus an optional month column) when no date column exists."""
    year_col = next(
        (p.name for p in profiles if p.name.strip().lower() in _YEAR_NAMES and p.name in numeric), None
    )
    if year_col is None:
        return None
    years = numeric[year_col].values
    if not np.all((years[~np.isnan(years)] >= 1900) & (years[~np.isnan(years)] <= 2100)):
        return None
    month_col = next((p.name for p in profiles if p.name.strip().lower() in _MONTH_NAMES), None)
    months: list[int | None] = (
        [parse_month(c) for c in columns[month_col]] if month_col else [1] * len(years)
    )
    dates = [
        date(int(y), m, 1) if not math.isnan(y) and m is not None else None
        for y, m in zip(years, months, strict=True)
    ]
    label = f"{year_col}+{month_col}" if month_col else year_col
    return label, dates


def profile_csv(text: str, *, name: str, max_rows: int = 200_000) -> TableProfile:
    """Profile a CSV export deterministically.

    Handles ``,`` ``;`` tab and ``|`` delimiters, preamble lines above the header,
    missing or duplicate header names, ragged rows, total/subtotal rows (dropped so
    sums are not doubled), null tokens (``N/A``, ``-``, ``#DIV/0!``), thousands
    separators, decimal commas, currency symbols, percents and accounting negatives.
    Only the first ``max_rows`` data rows are profiled.
    """
    text = text.lstrip("\ufeff")
    warnings: list[str] = []
    if not text.strip():
        return TableProfile(name=name, delimiter=",", row_count=0, columns=[], warnings=["file is empty"])

    delimiter, width = sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    header: list[str] | None = None
    header_row = 0
    rows: list[list[str]] = []
    ragged = totals = 0
    truncated = False
    for line_no, row in enumerate(reader):
        if not any(cell.strip() for cell in row):
            continue
        if header is None:
            filled = sum(bool(cell.strip()) for cell in row)
            if width > 1 and (len(row) < width - 1 or filled < 2):
                continue  # preamble such as "Report generated 2026-10-01"
            header_row = line_no
            if _looks_like_data(row):
                header = [""] * max(len(row), width)
                warnings.append("no header row detected; columns named column_1..")
            else:
                header = row + [""] * (width - len(row))
                continue
        if len(rows) >= max_rows:
            truncated = True
            break
        if _is_total_row(row):
            totals += 1
            continue
        if len(row) != len(header):
            ragged += 1
            row = (row + [""] * len(header))[: len(header)]
        rows.append(row)
    if header is None:
        return TableProfile(name=name, delimiter=delimiter, row_count=0, columns=[],
                            warnings=["no rows with a consistent number of fields"])
    if header_row:
        warnings.append(f"skipped {header_row} preamble line(s) above the header")
    if ragged:
        warnings.append(f"{ragged} row(s) had a different number of fields and were padded or cut")
    if totals:
        warnings.append(f"dropped {totals} total/subtotal row(s) so sums are not double counted")
    if truncated:
        warnings.append(f"only the first {max_rows} rows were profiled")

    names = _header_names(header)
    n_rows = len(rows)
    columns: dict[str, list[str | None]] = {}
    for i, col in enumerate(names):
        raw = [row[i] for row in rows]
        cleaned = {value: clean_cell(value) for value in set(raw)}  # exports repeat values a lot
        columns[col] = [cleaned[value] for value in raw]
    decimal_hint = "," if delimiter == ";" else "."
    day_first = delimiter != ","

    profiles: list[ColumnProfile] = []
    numeric: dict[str, NumericColumn] = {}
    dated: dict[str, DateColumn] = {}
    for col, cells in columns.items():
        counts = Counter(c for c in cells if c is not None)
        if not counts:
            profiles.append(ColumnProfile(name=col, kind=ColumnKind.EMPTY, non_null=0,
                                          missing_share=1.0 if n_rows else 0.0, unique=0))
            continue
        as_dates = parse_dates(cells, day_first=day_first, counts=counts)
        if as_dates is not None:
            dated[col] = as_dates
            profiles.append(_date_profile(col, cells, as_dates, n_rows))
            if as_dates.ambiguous_day_month:
                order = "day-first" if day_first else "month-first"
                warnings.append(f"{col}: day/month order is ambiguous; assumed {order}")
            continue
        as_numbers = parse_numeric(cells, hint=decimal_hint, counts=counts)
        if as_numbers is not None:
            numeric[col] = as_numbers
            profiles.append(_numeric_profile(col, cells, as_numbers, n_rows))
            continue
        profiles.append(_category_profile(col, counts, n_rows))

    empty = [p.name for p in profiles if p.kind == ColumnKind.EMPTY]
    if empty:
        warnings.append("empty columns: " + ", ".join(empty))
    header_dates = sum(parse_dates([n], min_share=1.0) is not None for n in names)
    if header_dates >= 3:
        warnings.append("dates appear in the header (wide format); unpivot the table to get trends")

    date_column: str | None = None
    dates: list[date | None] | None = None
    if dated:
        date_column = max(dated, key=lambda c: dated[c].parsed)
        dates = dated[date_column].values
    else:
        calendar_dates = _calendar_dates(profiles, numeric, columns)
        if calendar_dates:
            date_column, dates = calendar_dates

    trends: list[SeriesTrend] = []
    if dates is not None and date_column is not None:
        axis = DateAxis.from_dates(dates)
        measures = [p for p in profiles if p.kind == ColumnKind.NUMERIC and p.role == "measure"]
        if len(measures) > MAX_TREND_COLUMNS:
            warnings.append(f"trends computed for the first {MAX_TREND_COLUMNS} numeric columns only")
        for col in measures[:MAX_TREND_COLUMNS]:
            trend = build_trend(col.name, date_column, axis, numeric[col.name].values,
                                aggregation=_aggregation_for(col))
            if trend is not None:
                trends.append(trend)

    return TableProfile(
        name=name,
        delimiter=delimiter,
        row_count=n_rows,
        truncated=truncated,
        header_row=header_row,
        columns=profiles,
        date_column=date_column,
        trends=trends,
        warnings=warnings,
    )
