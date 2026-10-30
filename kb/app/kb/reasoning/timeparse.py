"""resolve_time (research §5.1): time expressions -> explicit intervals, always with a printed interpretation.

Time arithmetic is done here, never by the LLM. Recognised (English): month names ("in March 2026"), relative
periods ("last week", "last month", "yesterday"), rolling periods ("in the last 10 days"), explicit dates ("March 2",
"2026-03-02"), ranges ("from March 1 to March 15"), "as of <date>" -> known_at (transaction axis), and event anchors
("before release v2.4.0", "after meeting Growth sync") that are grounded to an entity whose effective time becomes the
boundary.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates

MONTHS_EN = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | {
    m.lower(): i for i, m in enumerate(calendar.month_abbr) if m
}
_MONTH_EN_RE = re.compile(
    r"\b(?:in|during|for|by|before|after|since)?\s*(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_AS_OF_RE = re.compile(
    r"(as of|as known (?:at|on)|known at)\s+(.+?)(?:[?,.;]|$)",
    re.IGNORECASE,
)
_ANCHOR_RE = re.compile(
    r"\b(before|after|since)\s+((?:release|meeting|decision|experiment|launch)\s+[^?,;]+?)(?=[?,;]|\.(?:\s|$)|$)",
    re.IGNORECASE,
)
_RELATIVE = [
    (re.compile(r"\b(today)\b", re.I), "day", 0),
    (re.compile(r"\b(the day before yesterday)\b", re.I), "day", -2),
    (re.compile(r"\b(yesterday)\b", re.I), "day", -1),
    (re.compile(r"\b(this week)\b", re.I), "week", 0),
    (re.compile(r"\b(last week)\b", re.I), "week", -1),
    (re.compile(r"\b(this month)\b", re.I), "month", 0),
    (re.compile(r"\b(last month)\b", re.I), "month", -1),
    (re.compile(r"\b(this quarter)\b", re.I), "quarter", 0),
    (re.compile(r"\b(last quarter)\b", re.I), "quarter", -1),
    (re.compile(r"\b(this year)\b", re.I), "year", 0),
    (re.compile(r"\b(last year)\b", re.I), "year", -1),
]
_RANGE_RE = re.compile(r"\b(?:from)\s+(.+?)\s+(?:to|until)\s+(.+?)(?:[?,.;]|$)", re.IGNORECASE)

# Rolling periods ending with the day of `now` inclusive: "in the last week" = the 7 days up to today.
_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "fourteen": 14, "fifteen": 15, "twenty": 20, "thirty": 30, "a": 1,
}
_ROLL_UNIT_EN = r"(days?|weeks?|months?|years?|hours?|fortnight)"
_ROLLING = [
    # "in the last 10 days", "over the past week", "during the last two weeks", "of the last 7 days"
    re.compile(r"\b(?:in|over|during|for|of|from|within)\s+the\s+(?:last|past|previous)\s+(?:(\d+|[a-z]+)\s+)?"
               + _ROLL_UNIT_EN + r"\b", re.I),
    re.compile(r"\b(?:past|last)\s+(\d+)\s+" + _ROLL_UNIT_EN + r"\b", re.I),
]


def _rolling(text: str, now: datetime) -> tuple[str, datetime, datetime] | None:
    for rx in _ROLLING:
        m = rx.search(text)
        if not m:
            continue
        num, unit = m.group(1), m.group(2).lower()
        n = 1
        if num:
            n = int(num) if num.isdigit() else _NUM_WORDS.get(num.lower(), 0)
            if not n:
                continue
        tz = now.tzinfo
        end = _day_start(now.date() + timedelta(days=1), tz)
        if unit.startswith("hour"):
            days = max(1, -(-n // 24))
            start = _day_start(now.date() - timedelta(days=days - 1), tz)
        elif unit.startswith("day"):
            start = _day_start(now.date() - timedelta(days=n - 1), tz)
        elif unit.startswith(("week", "fortnight")):
            days = 14 * n if unit.startswith("fortnight") else 7 * n
            start = _day_start(now.date() - timedelta(days=days - 1), tz)
        elif unit.startswith("month"):
            y, mo = now.year, now.month - n
            while mo < 1:
                y, mo = y - 1, mo + 12
            day = min(now.day, calendar.monthrange(y, mo)[1])
            start = _day_start(date(y, mo, day) + timedelta(days=1), tz)
        else:  # years
            start = _day_start(date(now.year - n, now.month, min(now.day, 28)) + timedelta(days=1), tz)
        return m.group(0).strip(), start, end
    return None


@dataclass
class TimeInterpretation:
    expression: str | None = None
    start: datetime | None = None  # inclusive, business (valid) axis
    end: datetime | None = None  # exclusive
    known_at: datetime | None = None  # transaction axis ("what did we know as of ...")
    granularity: str | None = None
    anchor: dict[str, Any] | None = None  # grounded event used as a boundary
    interpretation: str | None = None  # human-readable, always shown in the answer
    confidence: float = 1.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("start", "end", "known_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d

    @property
    def empty(self) -> bool:
        return self.start is None and self.end is None and self.known_at is None


def _day_start(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time.min, tzinfo=tz)


def _period(kind: str, offset: int, now: datetime) -> tuple[datetime, datetime]:
    tz = now.tzinfo
    today = now.date()
    if kind == "day":
        d = today + timedelta(days=offset)
        return _day_start(d, tz), _day_start(d + timedelta(days=1), tz)
    if kind == "week":
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        return _day_start(monday, tz), _day_start(monday + timedelta(days=7), tz)
    if kind == "month":
        y, m = today.year, today.month + offset
        while m < 1:
            y, m = y - 1, m + 12
        return _month(y, m, tz)
    if kind == "quarter":
        q = (today.month - 1) // 3 + offset
        y = today.year + q // 4
        q %= 4
        s = _day_start(date(y, q * 3 + 1, 1), tz)
        e = _month(y, q * 3 + 3, tz)[1]
        return s, e
    y = today.year + offset
    return _day_start(date(y, 1, 1), tz), _day_start(date(y + 1, 1, 1), tz)


def _month(y: int, m: int, tz) -> tuple[datetime, datetime]:
    s = _day_start(date(y, m, 1), tz)
    e = _day_start(date(y + (m == 12), 1 if m == 12 else m + 1, 1), tz)
    return s, e


def _month_num(word: str) -> int | None:
    return MONTHS_EN.get(word.lower())


def _parse_point(text: str, now: datetime) -> datetime | None:
    dt = dateparser.parse(
        text,
        languages=["en"],
        settings={"RELATIVE_BASE": now.replace(tzinfo=None), "PREFER_DATES_FROM": "past",
                  "TIMEZONE": str(now.tzinfo), "RETURN_AS_TIMEZONE_AWARE": True, "PREFER_DAY_OF_MONTH": "first"},
    )
    return dt


def _first_date(text: str, now: datetime) -> datetime | None:
    d = _parse_point(text, now)
    if d:
        return d
    found = search_dates(text, languages=["en"],
                         settings={"RELATIVE_BASE": now.replace(tzinfo=None), "PREFER_DATES_FROM": "past",
                                   "TIMEZONE": str(now.tzinfo), "RETURN_AS_TIMEZONE_AWARE": True}) or []
    found = [(s, d) for s, d in found if re.search(r"\d", s)]
    return found[0][1] if found else None


def _q(text: str) -> str:
    return f"“{text}”"


def _fmt(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M") if (d.hour or d.minute) else d.strftime("%Y-%m-%d")


Grounder = Callable[[str], dict[str, Any] | None]  # text -> {"entity_id", "title", "version_id", "effective_from"}


def resolve_time(
    text: str, now: datetime, *, tz: str = "UTC", ground_event: Grounder | None = None
) -> TimeInterpretation:
    """Find the time constraint in a question. Unknown -> empty interpretation (no constraint), never a guess."""
    zone = ZoneInfo(tz)
    now = now.astimezone(zone)
    ti = TimeInterpretation()

    m = _AS_OF_RE.search(text)
    if m:
        k = _first_date(m.group(2), now)
        if k:
            ti.known_at = k if (k.hour or k.minute) else _day_start(k.date() + timedelta(days=1), zone)
            expr = m.group(0).strip()
            ti.expression = expr
            ti.interpretation = (f"“{expr}” read as knowledge at {_fmt(ti.known_at)} (transaction axis)")
            text = text.replace(expr, " ")

    m = _ANCHOR_RE.search(text)
    if m and ground_event:
        hit = ground_event(m.group(2).strip())
        if hit and hit.get("effective_from"):
            t = hit["effective_from"]
            t = datetime.fromisoformat(t) if isinstance(t, str) else t
            before = m.group(1).lower() == "before"
            ti.start, ti.end = (None, t) if before else (t, None)
            ti.anchor = hit
            ti.granularity = "event"
            ti.expression = m.group(0).strip()
            rel = "before" if before else "after"
            interp = (f"“{m.group(0).strip()}” read as "
                      + f"{rel} {_fmt(t.astimezone(zone))} ({hit.get('title')}, {hit.get('version_id')})")
            ti.interpretation = f"{ti.interpretation}; {interp}" if ti.interpretation else interp
            return ti

    roll = _rolling(text, now)
    if roll:
        expr, ti.start, ti.end = roll
        ti.granularity = "rolling"
        ti.expression = expr
        last = (ti.end - timedelta(days=1)).date().isoformat()
        interp = (f"“{expr}” read as [{_fmt(ti.start)}, {_fmt(ti.end)}) — through {last} inclusive, from "
                    f"{now.date().isoformat()}")
        ti.interpretation = f"{ti.interpretation}; {interp}" if ti.interpretation else interp
        return ti

    for rx, kind, off in _RELATIVE:
        m = rx.search(text)
        if m:
            ti.start, ti.end = _period(kind, off, now)
            ti.granularity = kind
            ti.expression = m.group(0)
            interp = (f"“{m.group(0)}” read as [{_fmt(ti.start)}, {_fmt(ti.end)}) from {now.date().isoformat()}")
            ti.interpretation = f"{ti.interpretation}; {interp}" if ti.interpretation else interp
            return ti

    m = _RANGE_RE.search(text)
    if m:
        left, right = m.group(1).strip(), m.group(2).strip()
        b = _first_date(right, now)
        if re.fullmatch(r"\d{1,2}", left) and b:  # "from 1 to 15 March": the month comes from the right side
            a = b.replace(day=int(left))
        else:
            a = _first_date(left, now)
        if a and b:
            ti.start, ti.end = _day_start(a.date(), zone), _day_start(b.date() + timedelta(days=1), zone)
            ti.granularity = "range"
            ti.expression = m.group(0).strip()
            interp = _q(ti.expression) + " read as " + f"[{_fmt(ti.start)}, {_fmt(ti.end)})"
            ti.interpretation = f"{ti.interpretation}; {interp}" if ti.interpretation else interp
            return ti

    for rx in (_MONTH_EN_RE,):
        for m in rx.finditer(text):
            # "2 March" is a date, not a month: handled below by search_dates
            if re.search(r"\d{1,2}\s*$", text[: m.start(1)]):
                continue
            if re.match(r"\s+\d{1,2}(?:st|nd|rd|th)?\b(?!\d)", text[m.end(1):]):  # "July 1, 2026" too
                continue
            n = _month_num(m.group(1))
            if not n:
                continue
            year = int(m.group(2)) if m.group(2) else (now.year if n <= now.month else now.year - 1)
            ti.start, ti.end = _month(year, n, zone)
            ti.granularity = "month"
            ti.expression = m.group(0).strip()
            interp = (_q(ti.expression) + " read as "
                      + f"{calendar.month_name[n]} {year} [{_fmt(ti.start)}, {_fmt(ti.end)})")
            ti.interpretation = f"{ti.interpretation}; {interp}" if ti.interpretation else interp
            return ti

    found = search_dates(
        text, languages=["en"],
        settings={"RELATIVE_BASE": now.replace(tzinfo=None), "PREFER_DATES_FROM": "past", "TIMEZONE": tz,
                  "RETURN_AS_TIMEZONE_AWARE": True, "STRICT_PARSING": False},
    ) or []
    # a date needs a month word or a separator: "30%", "2,6" or "15 minutes" are not dates
    found = [(s, d) for s, d in found if re.search(r"\d", s) and len(s.strip()) >= 3
             and re.search(r"[a-z]{3,}|\d[./-]\d", s, re.I) and not re.search(re.escape(s) + r"\s*%", text)
             and not re.search(r"\b(minutes?|hours?|seconds?|days?|percent)", s, re.I)]
    if found:
        s, d = found[0]
        d = d.astimezone(zone)
        if d.hour or d.minute:
            ti.start, ti.end, ti.granularity = d, d + timedelta(hours=1), "hour"
        else:
            ti.start, ti.end, ti.granularity = _day_start(d.date(), zone), _day_start(d.date() + timedelta(days=1), zone), "day"
        ti.expression = s
        interp = _q(s) + " read as " + f"[{_fmt(ti.start)}, {_fmt(ti.end)})"
        ti.interpretation = f"{ti.interpretation}; {interp}" if ti.interpretation else interp
        ti.confidence = 0.8
    return ti
