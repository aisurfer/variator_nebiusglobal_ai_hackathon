"""Cell-level parsing for messy spreadsheet exports: nulls, numbers and dates.

Everything here is deterministic. Column-level decisions (decimal separator,
date format) are made once per column from its distinct values, then applied
to every cell, so one odd cell cannot flip the interpretation of the column.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np

NULL_TOKENS = frozenset(
    {
        "", "na", "n/a", "n.a.", "nan", "null", "none", "nil", "-", "--", "---", "\u2014", "\u2013",
        "?", "#n/a", "#na", "#value!", "#div/0!", "#ref!", "#num!", "#name?", "#null!", "missing",
        "not available", "(blank)", "(empty)",
    }
)

# Longest symbols first so "US$" wins over "$".
_CURRENCY_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("US$", "USD"), ("CA$", "CAD"), ("A$", "AUD"), ("$", "USD"), ("€", "EUR"), ("£", "GBP"),
    ("¥", "JPY"), ("₽", "RUB"), ("₹", "INR"), ("₴", "UAH"), ("₸", "KZT"),
    ("zł", "PLN"),
)
_CURRENCY_CODE_RE = re.compile(
    r"(?<![A-Za-z])(USD|EUR|GBP|JPY|CNY|RMB|RUB|INR|CHF|CAD|AUD|PLN|SEK|NOK|DKK|CZK|TRY|BRL|MXN|AED|"
    r"SGD|HKD|KZT|UAH|ZAR)(?![A-Za-z])",
    re.IGNORECASE,
)
# Thousands separators that are never decimal separators.
_GROUPING_RE = re.compile(r"[\s\u00a0\u202f\u2009'\u2019]")
_BODY_RE = re.compile(r"(?:\d[\d.,]*|[.,]\d+)(?:[eE][+-]?\d+)?")
_MANTISSA_RE = re.compile(r"([^eE]+)(?:[eE]([+-]?\d+))?")
_AMBIGUOUS_GROUP = {",": re.compile(r"[1-9]\d{0,2},\d{3}"), ".": re.compile(r"[1-9]\d{0,2}\.\d{3}")}


_MAX_NULL_TOKEN_LEN = max(map(len, NULL_TOKENS))


def clean_cell(raw: str | None) -> str | None:
    """Strip a cell and map null-like tokens (``N/A``, ``-``, ``#DIV/0!``...) to None."""
    if raw is None:
        return None
    value = raw.strip().strip("\ufeff").strip()
    if len(value) <= _MAX_NULL_TOKEN_LEN and value.lower() in NULL_TOKENS:
        return None
    return value


@dataclass(frozen=True)
class NumberToken:
    """A cell stripped down to digits and separators, plus what was removed."""

    body: str  # e.g. "1.234,5" or "12e3"; no sign
    negative: bool
    unit: str | None  # "%" or an ISO currency code


def tokenize_number(raw: str) -> NumberToken | None:
    """Split a cell like ``"(€ 1.234,50)"`` into body ``1.234,50``, negative, unit EUR.

    Returns None when the cell cannot be a number at all.
    """
    s = raw.strip().replace("\u2212", "-")
    negative = False
    if len(s) > 2 and s[0] == "(" and s[-1] == ")":  # accounting negative
        s, negative = s[1:-1].strip(), True
    unit: str | None = None
    if s.endswith("%"):
        s, unit = s[:-1].strip(), "%"
    code = _CURRENCY_CODE_RE.search(s)
    if code:
        found = code.group(1).upper()
        unit = unit or ("CNY" if found == "RMB" else found)
        s = (s[: code.start()] + s[code.end() :]).strip()
    for symbol, iso in _CURRENCY_SYMBOLS:
        if symbol in s:
            unit = unit or iso
            s = s.replace(symbol, "").strip()
            break
    s = _GROUPING_RE.sub("", s)
    if s.endswith("-") and len(s) > 1 and not s.startswith("-"):  # SAP-style trailing minus
        s, negative = s[:-1], not negative
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("-"):
        s, negative = s[1:], not negative
    if not s or not _BODY_RE.fullmatch(s):
        return None
    return NumberToken(body=s, negative=negative, unit=unit)


def decide_decimal(bodies: Iterable[str], *, hint: str = ".") -> str:
    """Pick the decimal separator ("." or ",") for a column from its number bodies.

    Unambiguous evidence wins (``1.234,5``, ``12,5``, ``1,234,567``); when every value
    is ambiguous (``1,234``) the ``hint`` decides: "," for semicolon-separated files,
    which are typically produced by European locales.
    """
    votes = {".": 0, ",": 0}
    saw = {".": False, ",": False}
    for body in bodies:
        mantissa = _MANTISSA_RE.fullmatch(body)
        m = mantissa.group(1) if mantissa else body
        has_dot, has_comma = "." in m, "," in m
        if has_dot and has_comma:
            votes["." if m.rfind(".") > m.rfind(",") else ","] += 1
        elif has_comma or has_dot:
            sep = "," if has_comma else "."
            other = "." if sep == "," else ","
            saw[sep] = True
            if m.count(sep) > 1:
                votes[other] += 1  # repeated separator can only be grouping
            elif not _AMBIGUOUS_GROUP[sep].fullmatch(m):
                votes[sep] += 1  # "12,5", "0,123", "1234,56": a decimal separator
    if votes["."] or votes[","]:
        return "." if votes["."] >= votes[","] else ","
    if saw[","] and not saw["."]:
        return "," if hint == "," else "."
    if saw["."] and not saw[","]:
        return "," if hint == "," else "."
    return "."


def parse_body(body: str, decimal: str) -> float | None:
    """Convert a number body using ``decimal`` as the decimal separator; validate grouping."""
    match = _MANTISSA_RE.fullmatch(body)
    if not match:
        return None
    mantissa, exponent = match.group(1), match.group(2)
    thousands = "," if decimal == "." else "."
    if mantissa.count(decimal) > 1:
        return None
    int_part, _, frac = mantissa.partition(decimal)
    if thousands in frac:
        return None
    if thousands in int_part:
        groups = int_part.split(thousands)
        head = groups[0]
        if not (head.isdigit() and 1 <= len(head) <= 3 and head[0] != "0"):
            return None
        if not all(len(g) == 3 and g.isdigit() for g in groups[1:]):
            return None
        int_part = "".join(groups)
    if (int_part and not int_part.isdigit()) or (frac and not frac.isdigit()) or not (int_part or frac):
        return None
    text = f"{int_part or '0'}.{frac or '0'}"
    if exponent:
        text += f"e{exponent}"
    try:
        return float(text)
    except ValueError:
        return None


def decimals_in(body: str, decimal: str) -> int:
    """Number of digits after the decimal separator (for rounding-aware comparisons)."""
    match = _MANTISSA_RE.fullmatch(body)
    mantissa = match.group(1) if match else body
    _, sep, frac = mantissa.partition(decimal)
    return len(frac) if sep else 0


@dataclass
class NumericColumn:
    values: np.ndarray  # float64, NaN where null or unparseable
    unit: str | None
    decimal: str
    parsed: int
    invalid_count: int
    invalid_examples: list[str] = field(default_factory=list)


def parse_numeric(
    cells: Sequence[str | None],
    *,
    hint: str = ".",
    min_share: float = 0.9,
    counts: Counter[str] | None = None,
) -> NumericColumn | None:
    """Parse a column as numbers; None if fewer than ``min_share`` of non-null cells parse.

    ``counts`` (non-null cell -> occurrences) may be passed to avoid recounting.
    """
    counts = counts if counts is not None else Counter(c for c in cells if c is not None)
    if not counts:
        return None
    # Cheap rejection of text columns before tokenizing every distinct value.
    sample = list(itertools.islice(counts, 500))
    if sum(tokenize_number(raw) is not None for raw in sample) < 0.5 * len(sample):
        return None
    tokens = {raw: tokenize_number(raw) for raw in counts}
    decimal = decide_decimal((t.body for t in tokens.values() if t), hint=hint)
    parsed: dict[str, float] = {}
    for raw, token in tokens.items():
        if token is None:
            continue
        value = parse_body(token.body, decimal)
        if value is not None:
            parsed[raw] = -value if token.negative else value
    ok_rows = sum(n for raw, n in counts.items() if raw in parsed)
    non_null = sum(counts.values())
    if ok_rows / non_null < min_share:
        return None
    unit_votes: Counter[str] = Counter()
    for raw in parsed:
        token = tokens[raw]
        if token is not None and token.unit:
            unit_votes[token.unit] += counts[raw]
    unit = None
    if unit_votes:
        top, n = unit_votes.most_common(1)[0]
        unit = top if n >= ok_rows / 2 else None
    values = np.array([parsed.get(c, np.nan) if c is not None else np.nan for c in cells], dtype=float)
    invalid = [raw for raw in counts if raw not in parsed]
    return NumericColumn(
        values=values,
        unit=unit,
        decimal=decimal,
        parsed=ok_rows,
        invalid_count=non_null - ok_rows,
        invalid_examples=invalid[:5],
    )


# ---------------------------------------------------------------- dates

_TIME_SUFFIX_RE = re.compile(
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?\s*(?:[AaPp][Mm])?\s*(?:Z|[+-]\d{2}:?\d{2}|UTC|GMT)?)$"
)
_DATEISH_RE = re.compile(
    r"^\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,4})?$|[A-Za-z]{3}|^(?:\d{4}\s*[-/ ]?\s*)?Q[1-4]", re.IGNORECASE
)
_QUARTER_RES = (
    re.compile(r"^(\d{4})\s*[-/ ]?\s*Q([1-4])$", re.IGNORECASE),
    re.compile(r"^Q([1-4])\s*[-/ ']?\s*(\d{4})$", re.IGNORECASE),
)
_QUARTER = "quarter"

_DAY_FIRST = ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%y", "%d/%m/%y")
_MONTH_FIRST = ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y")
_SWAPPED = {
    **dict(zip(("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"), _MONTH_FIRST, strict=True)),
    **dict(zip(_MONTH_FIRST, ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"), strict=True)),
}
_UNAMBIGUOUS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m", "%Y/%m", "%m/%Y", "%m.%Y",
    "%b %Y", "%B %Y", "%b-%Y", "%b-%y", "%b %y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%d %b %Y",
    "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y",
)


def _normalize_date_text(raw: str) -> str:
    text = _TIME_SUFFIX_RE.sub("", raw.strip())
    return re.sub(r"\bSept\b", "Sep", text, flags=re.IGNORECASE)


def _quarter_date(text: str) -> date | None:
    for i, pattern in enumerate(_QUARTER_RES):
        m = pattern.match(text)
        if m:
            year, q = (m.group(1), m.group(2)) if i == 0 else (m.group(2), m.group(1))
            return date(int(year), 3 * int(q) - 2, 1)
    return None


def _parse_with(text: str, fmt: str) -> date | None:
    if fmt == _QUARTER:
        return _quarter_date(text)
    try:
        return datetime.strptime(text, fmt).date()
    except ValueError:
        return None


@dataclass
class DateColumn:
    values: list[date | None]
    fmt: str
    parsed: int
    invalid_count: int
    ambiguous_day_month: bool


def parse_dates(
    cells: Sequence[str | None],
    *,
    day_first: bool = True,
    min_share: float = 0.9,
    sample_size: int = 400,
    counts: Counter[str] | None = None,
) -> DateColumn | None:
    """Detect one date format for the column and parse every cell with it.

    ``day_first`` breaks the tie when all values fit both dd/mm and mm/dd.
    """
    counts = counts if counts is not None else Counter(c for c in cells if c is not None)
    if not counts:
        return None
    sample = [_normalize_date_text(raw) for raw in list(counts)[:sample_size]]
    if sum(bool(_DATEISH_RE.search(s)) for s in sample) < len(sample) * min_share:
        return None
    ordered = (
        (_QUARTER, *_UNAMBIGUOUS, *_DAY_FIRST, *_MONTH_FIRST)
        if day_first
        else (_QUARTER, *_UNAMBIGUOUS, *_MONTH_FIRST, *_DAY_FIRST)
    )
    scores = {fmt: sum(_parse_with(s, fmt) is not None for s in sample) for fmt in ordered}
    best = max(scores, key=lambda f: scores[f])  # first maximum wins: ``ordered`` encodes preference
    if scores[best] < len(sample) * min_share:
        return None
    swapped = _SWAPPED.get(best)
    ambiguous = swapped is not None and scores[swapped] == scores[best]

    parsed_unique = {raw: _parse_with(_normalize_date_text(raw), best) for raw in counts}
    values = [parsed_unique[c] if c is not None else None for c in cells]
    ok_rows = sum(n for raw, n in counts.items() if parsed_unique[raw] is not None)
    non_null = sum(counts.values())
    if ok_rows / non_null < min_share:
        return None
    return DateColumn(
        values=values,
        fmt=best,
        parsed=ok_rows,
        invalid_count=non_null - ok_rows,
        ambiguous_day_month=ambiguous,
    )


MONTH_NAMES = {
    name: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
            ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
            ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}


def parse_month(cell: str | None) -> int | None:
    """Month number from ``"3"``, ``"03"``, ``"Mar"`` or ``"March"``."""
    if cell is None:
        return None
    text = cell.strip().lower().rstrip(".")
    if text.isdigit():
        month = int(text)
        return month if 1 <= month <= 12 else None
    return MONTH_NAMES.get(text)
