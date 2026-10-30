import math
from datetime import date, timedelta

import numpy as np
import pytest

from variator.ingestion import ColumnKind, decode_text, profile_csv
from variator.ingestion.tables import fmt_num, fmt_pct, sniff_delimiter
from variator.ingestion.trends import build_trend, detect_granularity


def monthly_csv(months: int = 36, *, delimiter: str = ",", growth: float = 0.01, start=(2023, 1)) -> str:
    """Revenue with 1%/month growth and a +/-20% seasonal swing peaking in month 12."""
    rows = [delimiter.join(["month", "revenue", "orders", "region"])]
    year, month = start
    for i in range(months):
        season = 1 + 0.2 * math.cos(2 * math.pi * (month - 12) / 12)
        revenue = 100_000 * (1 + growth) ** i * season
        rows.append(delimiter.join([f"{year}-{month:02d}", f"{revenue:.2f}", str(1000 + i), "North"]))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return "\n".join(rows)


def test_basic_profile_types_and_stats():
    p = profile_csv("a,b,c\n1,x,2024-01-01\n2,y,2024-02-01\n3,x,2024-03-01\n", name="t.csv")
    assert p.delimiter == "," and p.row_count == 3
    kinds = {c.name: c.kind for c in p.columns}
    assert kinds == {"a": ColumnKind.NUMERIC, "b": ColumnKind.CATEGORICAL, "c": ColumnKind.DATE}
    a = p.column("a").numeric
    assert (a.sum, a.mean, a.median, a.min, a.max) == (6, 2, 2, 1, 3)
    b = p.column("b")
    assert b.top_values[0].value == "x" and b.top_values[0].count == 2
    assert b.top_values[0].share == pytest.approx(2 / 3)
    c = p.column("c")
    assert (c.date_min, c.date_max, c.granularity) == ("2024-01-01", "2024-03-01", "month")
    assert p.date_column == "c"


def test_semicolon_decimal_comma_currency_and_thousands():
    text = (
        "Datum;Umsatz;Marge\n"
        "01.01.2024;1.234,50 €;12,5%\n"
        "01.02.2024;2.000,00 €;13,0%\n"
        "01.03.2024;€ 999,5;11,25%\n"
    )
    p = profile_csv(text, name="de.csv")
    assert p.delimiter == ";"
    umsatz = p.column("Umsatz")
    assert umsatz.kind == ColumnKind.NUMERIC and umsatz.unit == "EUR"
    assert umsatz.numeric.sum == pytest.approx(1234.5 + 2000 + 999.5)
    marge = p.column("Marge")
    assert marge.unit == "%" and marge.numeric.mean == pytest.approx((12.5 + 13 + 11.25) / 3)
    assert p.column("Datum").date_min == "2024-01-01"


def test_tab_delimited_with_us_thousands():
    text = "item\tamount\nA\t\"1,200\"\nB\t3,400.75\nC\t(500)\n"
    p = profile_csv(text, name="tab.tsv")
    assert p.delimiter == "\t"
    assert p.column("amount").numeric.sum == pytest.approx(1200 + 3400.75 - 500)


def test_messy_export_preamble_totals_nulls_empty_and_ragged():
    text = "\n".join(
        [
            "\ufeffACME Corp sales export",
            "Generated: 2026-10-01",
            "",
            "Region,Revenue,Notes,,Revenue",
            "North,\"1,000\",ok,,5",
            "South,N/A,,,6",
            "East,#DIV/0!,late,,7",
            "West,2000,,,8,extra",
            "North,3000,,,9",
            "Total,\"6,000\",,,35",
        ]
    )
    p = profile_csv(text, name="messy.csv")
    assert p.header_row == 3
    assert [c.name for c in p.columns] == ["Region", "Revenue", "Notes", "column_4", "Revenue_2"]
    revenue = p.column("Revenue")
    assert revenue.kind == ColumnKind.NUMERIC
    assert revenue.numeric.sum == 6000  # the Total row was dropped
    assert revenue.missing_share == pytest.approx(2 / 5)
    assert p.column("column_4").kind == ColumnKind.EMPTY
    assert p.row_count == 5
    joined = " | ".join(p.warnings)
    assert "preamble" in joined and "total" in joined and "empty columns: column_4" in joined
    assert "different number of fields" in joined


def test_headerless_file_gets_generated_names():
    p = profile_csv("2024-01-01,10\n2024-02-01,20\n2024-03-01,30\n", name="raw.csv")
    assert [c.name for c in p.columns] == ["column_1", "column_2"]
    assert p.row_count == 3
    assert any("no header" in w for w in p.warnings)


def test_mostly_numeric_column_keeps_unparseable_count():
    rows = ["id_text,value"] + [f"r{i},{i}" for i in range(60)] + ["r60,unknown"]
    p = profile_csv("\n".join(rows), name="t.csv")
    value = p.column("value")
    assert value.kind == ColumnKind.NUMERIC and value.invalid_values == 1 and value.examples == ["unknown"]
    assert p.column("id_text").kind == ColumnKind.TEXT


def test_identifier_and_calendar_columns_get_no_trends():
    rows = ["order_id,date,amount,year"] + [
        f"{i},2024-{(i % 12) + 1:02d}-01,{(i * 37) % 101 + 50},2024" for i in range(1, 41)
    ]
    p = profile_csv("\n".join(rows), name="o.csv")
    assert p.column("order_id").role == "identifier"
    assert p.column("year").role == "calendar"
    assert [t.column for t in p.trends] == ["amount"]


def test_empty_and_blank_input():
    assert profile_csv("", name="e.csv").warnings == ["file is empty"]
    p = profile_csv("a,b\n", name="h.csv")
    assert p.row_count == 0 and all(c.kind == ColumnKind.EMPTY for c in p.columns)


def test_max_rows_truncates():
    rows = ["x"] + [str(i) for i in range(100)]
    p = profile_csv("\n".join(rows), name="big.csv", max_rows=10)
    assert p.row_count == 10 and p.truncated
    assert p.column("x").numeric.max == 9


def test_sniff_delimiter_prefers_consistent_split():
    assert sniff_delimiter("a;b;c\n1,5;2,5;3,5\n4,0;5,0;6,0")[0] == ";"
    assert sniff_delimiter("a,b\n1,2\n3,4")[0] == ","
    assert sniff_delimiter("a|b\n1|2")[0] == "|"


def test_decode_text_variants():
    assert decode_text("\ufeffa,b".encode()) == "a,b"
    assert decode_text("a,b".encode("utf-16")) == "a,b"
    assert decode_text("caf\xe9".encode("cp1252")) == "caf\xe9"


# ---------------------------------------------------------------- trends


def test_monthly_trend_growth_cagr_cv_and_seasonality():
    p = profile_csv(monthly_csv(36), name="m.csv")
    t = p.trend("revenue")
    assert t is not None and t.granularity == "month" and t.aggregation == "sum"
    assert (t.first_period, t.last_period, t.periods, t.missing_periods) == ("2023-01", "2025-12", 36, 0)
    values = np.array([v.value for v in t.values])
    assert t.trailing_year == pytest.approx(values[-12:].sum())
    assert t.previous_year == pytest.approx(values[-24:-12].sum())
    # with 1%/month growth and identical seasonality, YoY growth is exactly 1.01**12 - 1
    assert t.yoy_growth == pytest.approx(1.01**12 - 1, rel=1e-6)
    assert t.cagr == pytest.approx(1.01**12 - 1, rel=1e-6)
    assert t.cagr_years == pytest.approx(2.0)
    assert t.cv == pytest.approx(values.std(ddof=1) / values.mean())
    assert t.seasonality_method == "ratio_to_moving_average"
    assert t.peak.label == "Dec" and t.trough.label == "Jun"
    assert np.mean([s.index for s in t.seasonality]) == pytest.approx(1.0)
    assert t.peak.index == pytest.approx(1.2, abs=0.03)


def test_short_series_uses_simple_seasonality_and_no_growth():
    t = profile_csv(monthly_csv(14, growth=0.0), name="m.csv").trend("revenue")
    assert t.yoy_growth is None and t.cagr is None
    assert t.trailing_year is not None
    assert t.seasonality_method == "ratio_to_mean"


def test_daily_data_drops_partial_months():
    start = date(2024, 1, 10)
    rows = ["date,sales"]
    d = start
    while d <= date(2025, 3, 15):
        rows.append(f"{d.isoformat()},10")
        d += timedelta(days=1)
    t = profile_csv("\n".join(rows), name="d.csv").trend("sales")
    assert t.excluded_partial_periods == ["2025-03", "2024-01"]
    assert (t.first_period, t.last_period) == ("2024-02", "2025-02")
    assert t.values[0].value == 290  # Feb 2024: 29 days x 10


def test_missing_months_are_gaps_not_zeros_and_get_annualized():
    rows = ["month,revenue"]
    for year in (2023, 2024):
        for month in range(1, 13):
            if (year, month) == (2024, 5):
                continue
            rows.append(f"{year}-{month:02d}-01,100")
    t = profile_csv("\n".join(rows), name="g.csv").trend("revenue")
    assert t.missing_periods == 1
    assert t.trailing_year == pytest.approx(1200)  # 11 months x 100, annualized
    assert any("annualized" in n for n in t.notes)
    assert t.yoy_growth == pytest.approx(0.0)


def test_rate_columns_are_averaged_not_summed():
    rows = ["month,avg_price,units"] + [f"2024-{m:02d}-01,{10 + m},{m}" for m in range(1, 13)]
    rows += [f"2024-{m:02d}-15,{12 + m},{m}" for m in range(1, 13)]
    p = profile_csv("\n".join(rows), name="p.csv")
    price = p.trend("avg_price")
    assert price.aggregation == "mean"
    assert price.values[0].value == pytest.approx(12.0)  # mean of 11 and 13
    assert p.trend("units").aggregation == "sum"


def test_quarterly_and_yearly_granularity():
    quarterly = ["quarter,revenue"] + [f"{y}-Q{q},{100 * (y - 2020) + q}" for y in (2022, 2023, 2024)
                                        for q in (1, 2, 3, 4)]
    t = profile_csv("\n".join(quarterly), name="q.csv").trend("revenue")
    assert t.granularity == "quarter" and t.periods == 12 and t.first_period == "2022-Q1"
    assert t.trailing_year == pytest.approx(sum(400 + q for q in range(1, 5)))
    assert len(t.seasonality) == 4

    yearly = "year,revenue\n2021,100\n2022,110\n2023,121\n2024,133.1\n"
    p = profile_csv(yearly, name="y.csv")
    assert p.date_column == "year"
    t = p.trend("revenue")
    assert t.granularity == "year" and t.yoy_growth == pytest.approx(0.1)
    assert t.cagr == pytest.approx(0.1) and t.cagr_years == pytest.approx(3)
    assert t.seasonality == []


def test_year_and_month_columns_combine_into_dates():
    rows = ["Year;Month;Revenue"] + [f"2024;{m};{m * 10}" for m in range(1, 13)]
    p = profile_csv("\n".join(rows), name="ym.csv")
    assert p.date_column == "Year+Month"
    assert p.trend("Revenue").values[2].value == 30


def test_wide_format_warns():
    p = profile_csv("product,Jan 2024,Feb 2024,Mar 2024\nA,1,2,3\nB,4,5,6\n", name="w.csv")
    assert any("wide format" in w for w in p.warnings)


def test_detect_granularity():
    assert detect_granularity([date(2024, 1, 1), date(2024, 4, 1), date(2024, 10, 1)]) == "quarter"
    assert detect_granularity([date(2022, 12, 31), date(2023, 12, 31)]) == "year"
    assert detect_granularity([date(2024, 1, 5), date(2024, 4, 5)]) == "month"  # day-level dates


def test_build_trend_needs_two_points():
    assert build_trend("x", "d", [date(2024, 1, 1)], np.array([1.0])) is None
    assert build_trend("x", "d", [None, None], np.array([1.0, 2.0])) is None


# ---------------------------------------------------------------- rendering and grounding numbers


@pytest.mark.parametrize(
    ("x", "text"),
    [(1234567.891, "1,234,568"), (12.3456, "12.3"), (0.012345, "0.0123"), (5.0, "5"), (0.0, "0"),
     (None, "n/a"), (-250.4, "-250")],
)
def test_fmt_num(x, text):
    assert fmt_num(x) == text


def test_fmt_num_keeps_relative_error_below_half_percent():
    for x in np.geomspace(0.0011, 99_999, 400):
        assert abs(float(fmt_num(x).replace(",", "")) - x) / x < 0.005


def test_fmt_pct():
    assert fmt_pct(0.1234, signed=True) == "+12.3%"
    assert fmt_pct(-0.01234) == "-1.23%"


def test_to_prompt_mentions_key_numbers():
    p = profile_csv(monthly_csv(36), name="m.csv")
    text = p.to_prompt(max_periods=6)
    t = p.trend("revenue")
    assert "Table: m.csv" in text and "revenue [numeric]" in text
    assert fmt_num(t.trailing_year) in text
    assert fmt_pct(t.yoy_growth, signed=True) in text
    assert "seasonality peak Dec" in text
    assert "values (last 6)" in text and "2025-12" in text and "2025-06" not in text


def test_grounding_numbers_cover_rendered_stats_and_percent_forms():
    p = profile_csv(monthly_csv(36), name="m.csv")
    numbers = p.grounding_numbers()
    t = p.trend("revenue")
    for expected in (t.trailing_year, t.yoy_growth, t.yoy_growth * 100, p.column("revenue").numeric.sum,
                     36, t.peak.index):
        assert any(abs(n - expected) <= 1e-9 * max(1, abs(expected)) for n in numbers)


def test_sparse_day_level_data_keeps_last_month():
    rows = ["date,sales"] + [f"2025-{m:02d}-{d:02d},10" for m in range(1, 13) for d in (1, 15, 28)]
    t = profile_csv("\n".join(rows), name="s.csv").trend("sales")
    assert t.excluded_partial_periods == [] and t.last_period == "2025-12"


def test_constant_series_has_no_seasonality_and_zero_cv():
    rows = ["month,price_avg"] + [f"{2023 + i // 12}-{i % 12 + 1:02d}-01,1.17" for i in range(36)]
    t = profile_csv("\n".join(rows), name="c.csv").trend("price_avg")
    assert t.cv == 0.0 and t.seasonality == [] and t.seasonality_method is None
    assert t.yoy_growth == pytest.approx(0.0)
