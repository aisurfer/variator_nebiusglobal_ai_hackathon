from datetime import date

import numpy as np
import pytest

from variator.ingestion.parsing import (
    clean_cell,
    decide_decimal,
    parse_body,
    parse_dates,
    parse_month,
    parse_numeric,
    tokenize_number,
)


@pytest.mark.parametrize("raw", ["", "  ", "N/A", "n/a", "-", "\u2014", "#DIV/0!", "null", "NaN", "\ufeff"])
def test_null_tokens(raw):
    assert clean_cell(raw) is None


def test_clean_cell_strips():
    assert clean_cell("  North ") == "North"


@pytest.mark.parametrize(
    ("raw", "body", "negative", "unit"),
    [
        ("1,234.56", "1,234.56", False, None),
        ("(1,234)", "1,234", True, None),
        ("-$1,234.50", "1,234.50", True, "USD"),
        ("€ 1.234,50", "1.234,50", False, "EUR"),
        ("1\u00a0234,5 EUR", "1234,5", False, "EUR"),
        ("12,5 %", "12,5", False, "%"),
        ("1'234.5", "1234.5", False, None),
        ("1234-", "1234", True, None),
        ("\u22127", "7", True, None),
        ("1.2E+03", "1.2E+03", False, None),
    ],
)
def test_tokenize_number(raw, body, negative, unit):
    token = tokenize_number(raw)
    assert token is not None
    assert (token.body, token.negative, token.unit) == (body, negative, unit)


@pytest.mark.parametrize("raw", ["North", "2024-01-05", "3 - 5", "12 apples", "Q1 2024", "abc123"])
def test_tokenize_rejects_non_numbers(raw):
    assert tokenize_number(raw) is None


def test_decide_decimal_unambiguous_evidence():
    assert decide_decimal(["1.234,5", "12"]) == ","
    assert decide_decimal(["1,234.5", "12"]) == "."
    assert decide_decimal(["12,5", "3"]) == ","
    assert decide_decimal(["0,125"]) == ","  # leading zero cannot be a thousands group
    assert decide_decimal(["1,234,567"]) == "."
    assert decide_decimal(["1.234.567"]) == ","


def test_decide_decimal_ambiguous_uses_hint():
    assert decide_decimal(["1,234", "5,678"], hint=".") == "."
    assert decide_decimal(["1,234", "5,678"], hint=",") == ","
    assert decide_decimal(["12", "7"]) == "."


@pytest.mark.parametrize(
    ("body", "decimal", "expected"),
    [
        ("1,234.5", ".", 1234.5),
        ("1.234,5", ",", 1234.5),
        ("1,234", ".", 1234.0),
        ("1,234", ",", 1.234),
        ("12,34,567", ".", None),  # bad grouping
        ("1.2.3", ".", None),
        (".5", ".", 0.5),
        ("1.2e3", ".", 1200.0),
    ],
)
def test_parse_body(body, decimal, expected):
    assert parse_body(body, decimal) == expected


def test_parse_numeric_decimal_comma_column():
    col = parse_numeric(["1.234,50", "12,5", None, "7"], hint=",")
    assert col is not None and col.decimal == ","
    assert np.allclose(col.values, [1234.5, 12.5, np.nan, 7.0], equal_nan=True)


def test_parse_numeric_tolerates_few_bad_cells():
    cells = [str(i) for i in range(30)] + ["oops"]
    col = parse_numeric(cells)
    assert col is not None and col.invalid_count == 1 and col.invalid_examples == ["oops"]
    assert np.isnan(col.values[-1])


def test_parse_numeric_rejects_text_column():
    assert parse_numeric(["North", "South", "12"]) is None


def test_parse_numeric_detects_currency_and_percent():
    assert parse_numeric(["$1,200", "$950", "$1,010.5"]).unit == "USD"
    assert parse_numeric(["12%", "13,5%"], hint=",").unit == "%"


def test_parse_dates_iso_and_time_suffix():
    col = parse_dates(["2024-01-05", "2024-02-10 13:45:00", "2024-03-01T00:00:00Z", None])
    assert col is not None
    assert col.values[:3] == [date(2024, 1, 5), date(2024, 2, 10), date(2024, 3, 1)]
    assert col.values[3] is None


def test_parse_dates_european_and_us():
    eu = parse_dates(["31.01.2024", "15.02.2024"])
    assert eu.values == [date(2024, 1, 31), date(2024, 2, 15)]
    us = parse_dates(["01/31/2024", "02/15/2024"])
    assert us.values == [date(2024, 1, 31), date(2024, 2, 15)] and not us.ambiguous_day_month


def test_parse_dates_ambiguous_order_flagged():
    day_first = parse_dates(["01/02/2024", "03/04/2024"], day_first=True)
    month_first = parse_dates(["01/02/2024", "03/04/2024"], day_first=False)
    assert day_first.ambiguous_day_month and day_first.values[0] == date(2024, 2, 1)
    assert month_first.values[0] == date(2024, 1, 2)


@pytest.mark.parametrize(
    ("cells", "first"),
    [
        (["Jan 2024", "Feb 2024", "Sept 2024"], date(2024, 1, 1)),
        (["2024-Q1", "2024-Q2"], date(2024, 1, 1)),
        (["Q3 2024", "Q4 2024"], date(2024, 7, 1)),
        (["2024-01", "2024-02"], date(2024, 1, 1)),
        (["05-Mar-2024", "06-Mar-2024"], date(2024, 3, 5)),
    ],
)
def test_parse_dates_formats(cells, first):
    col = parse_dates(cells)
    assert col is not None and col.values[0] == first


def test_parse_dates_rejects_numbers_and_text():
    assert parse_dates(["1.5", "2.25", "3.75"]) is None
    assert parse_dates(["North", "South"]) is None
    assert parse_dates(["2024", "2025"]) is None  # bare years stay numeric


def test_parse_month():
    assert [parse_month(x) for x in ["3", "03", "Mar", "march", "Sept.", "13", None]] == [
        3, 3, 3, 3, 9, None, None
    ]
