import pytest

from variator.domain import Fact, FactOrigin
from variator.ingestion.grounding import (
    UNVERIFIED_CONFIDENCE,
    NumberIndex,
    ground_facts,
    numbers_in_text,
    source_numbers,
    unit_scale,
    unverified_statement_numbers,
    value_is_grounded,
)


def fact(statement: str, value: float | None = None, unit: str | None = None, confidence: float = 0.8):
    return Fact(statement=statement, value=value, unit=unit, origin=FactOrigin.DATA, confidence=confidence)


def values(text: str) -> list[float]:
    return [n.value for n in numbers_in_text(text)]


def test_numbers_in_text_formats():
    assert values("Revenue $1.2bn, up 12.5% vs 2024") == [1.2e9, 12.5, 2024]
    assert values("Umsatz 1.234,5 Mio. EUR") == [1234.5e6]
    assert values("1 234 567 orders and 3.4k users") == [1234567, 3400]
    assert values("fell \u221212% (from 1,000)") == [-12, 1000]
    assert values("Q3 and FY2025 are not numbers") == []
    assert values("10 months, 5 mins") == [10, 5]


def test_text_number_rounding_unit():
    (n,) = numbers_in_text("1.2M")
    assert n.half_unit == pytest.approx(0.05e6)
    (n,) = numbers_in_text("12.25%")
    assert n.percent and n.half_unit == pytest.approx(0.005)


def test_source_numbers_include_all_readings_and_fractions():
    numbers = set(source_numbers("Margin 12,5 % on 1,234 units; 4.2 million EUR"))
    assert {12.5, 0.125, 1234, 1.234, 4.2e6, 4.2} <= numbers


def test_number_index_tolerance():
    index = NumberIndex([1000.0, -0.25, None, float("nan")])
    assert len(index) == 2
    assert index.contains(1009) and not index.contains(1011)
    assert index.contains(0.25) and index.contains(-0.2501)
    assert index.contains(1100, abs_tol=100)
    assert not NumberIndex().contains(1)


@pytest.mark.parametrize(
    ("unit", "scale"),
    [("million USD", 1e6), ("kEUR", 1e3), ("EUR m", 1e6), ("$bn", 1e9), ("'000 USD", 1e3), ("Mio. EUR", 1e6),
     ("USD", 1), ("months", 1), ("orders", 1), ("km", 1), (None, 1), ("%", 1)],
)
def test_unit_scale(unit, scale):
    assert unit_scale(unit) == scale


def test_value_grounding_rules():
    index = NumberIndex([1_234_567.0, 0.123, 12.3])
    assert value_is_grounded(fact("x", 1_234_567), index)
    assert value_is_grounded(fact("x", 1_240_000), index)  # 0.4% off
    assert not value_is_grounded(fact("x", 1_300_000), index)  # 5% off
    assert value_is_grounded(fact("x", 1.234567, unit="million USD"), index)
    assert value_is_grounded(fact("x", -12.3, unit="%"), index)  # sign-insensitive
    assert value_is_grounded(fact("x"), index) is None


def test_statement_numbers_are_rounding_aware_and_skip_years_and_small_ints():
    index = NumberIndex([1_234_567.0, 12.34])
    assert unverified_statement_numbers("Revenue 1.2M in 2025 over 12 months, +12.3%", index) == []
    assert unverified_statement_numbers("Revenue 1.5M", index) == [1.5e6]
    assert unverified_statement_numbers("Growth 45%", index) == [45]
    assert unverified_statement_numbers("Revenue 12.3 million", index) == []  # scale given elsewhere


def test_ground_facts_drop_policy():
    index = NumberIndex([100.0, 0.1, 10.0])
    facts = [
        fact("Revenue was 100", 100),
        fact("Revenue was 250", 250),  # invented value
        fact("Growth was 10%", 10, "%"),
        fact("Sales are seasonal"),
        fact("Revenue grew 45% to 100", 100),  # value fine, statement number invented
    ]
    report = ground_facts(facts, index)
    assert [f.statement for f in report.kept] == [
        "Revenue was 100", "Growth was 10%", "Sales are seasonal", "Revenue grew 45% to 100"
    ]
    assert [f.value for f in report.rejected] == [250]
    assert report.downgraded == 1
    assert report.kept[-1].confidence == UNVERIFIED_CONFIDENCE
    assert report.kept[0].confidence == 0.8


def test_ground_facts_downgrade_policy_keeps_with_low_confidence():
    facts = [fact("Revenue was 250", 250, confidence=0.9)]
    report = ground_facts(facts, NumberIndex([100]), policy="downgrade")
    assert report.rejected == [] and report.downgraded == 1
    assert report.kept[0].confidence == UNVERIFIED_CONFIDENCE


def test_ground_facts_rejects_unknown_policy():
    with pytest.raises(ValueError):
        ground_facts([], NumberIndex(), policy="ignore")  # type: ignore[arg-type]
