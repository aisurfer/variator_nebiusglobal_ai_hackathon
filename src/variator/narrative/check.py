"""The fact checker: a writer's text may only say what its packet says.

For each text field of an artifact:

- every required fact appears (any of its accepted forms, case-insensitive);
- every number, money amount, percentage, date, weekday, month, time and id in the text
  occurs in the packet (number words two..twenty count as numbers);
- every person (full name or first name of the directory) and organisation named in the
  text occurs in the packet;
- English only (no letters outside ASCII), no URLs, no e-mail addresses, no placeholders,
  no banned phrase (negative-case decoys, acl-restricted facts);
- the length is within the word range (with a small tolerance).

``check_artifact`` returns the problems (empty: accepted).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from variator.narrative.facts import NUMBER_WORDS, Packet

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
MON = {m.lower(): i + 1 for i, m in enumerate(MONTHS)} | {m[:3].lower(): i + 1 for i, m in enumerate(MONTHS)}
MON["sept"] = 9
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_M = (
    r"(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?"
)
DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+{_M}(?:,?\s+(\d{{4}}))?\b")
MONTH_DAY = re.compile(rf"\b{_M}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?")
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
IDS = re.compile(r"(?<![\w!])(?:[A-Z][A-Z0-9]*-\d+|SEV\d|Q[1-4]|v\d+(?:\.\d+)+)\b|![0-9]+\b")
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
MONTH_WORD = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|"
    r"December)\b"
)
WEEKDAY = re.compile(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)s?\b")
NUMWORD = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", re.I)
URL = re.compile(r"https?://|www\.", re.I)
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
PLACEHOLDER = re.compile(r"\[[^\]]*\]|\{[^}]*\}|\bTBD\b|\bTODO\b|\bXXX\b|lorem ipsum|as an ai\b", re.I)
COMMON_FIRST = {
    "May",
    "June",
    "April",
    "August",
    "Will",
    "Mark",
    "Bill",
    "Grant",
    "Hope",
    "Faith",
    "Joy",
    "Dawn",
    "Rose",
    "Summer",
    "Chase",
    "Lane",
    "Reed",
    "Page",
    "Sterling",
    "Cedar",
    "Ash",
    "Brook",
    "Clay",
}
TOLERANCE = 0.15


def norm(text: str) -> str:
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-").replace(" ", " ")
    return re.sub(r"\s+", " ", t).strip()


def _num(s: str) -> str:
    s = s.replace(",", "")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.lstrip("0") or "0"


@dataclass
class Names:
    """The directory: who and which organisations a text could name."""

    people: list[tuple[str, str]] = field(default_factory=list)  # (full name, first name)
    orgs: list[str] = field(default_factory=list)

    @classmethod
    def from_people_yaml(cls, data: dict[str, Any]) -> Names:
        from variator.narrative.facts import first_name

        ppl = [(p["name"], first_name(p["name"], p["email"].split("@")[0])) for p in data.get("people") or []]
        ppl += [(c["name"], first_name(c["name"])) for c in data.get("contacts") or []]
        orgs = [o["name"] for o in data.get("organizations") or [] if o.get("name")]
        return cls(ppl, orgs)

    def to_json(self) -> dict[str, Any]:
        return {"people": [list(x) for x in self.people], "orgs": self.orgs}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Names:
        return cls([tuple(x) for x in d["people"]], list(d["orgs"]))  # type: ignore[misc]


@dataclass
class Allowed:
    numbers: set[str]
    dates: set[tuple[int, int]]
    years: set[str]
    months: set[int]
    weekdays: set[str]
    times: set[str]
    ids: set[str]
    text: str  # the packet text, normalised and lower-cased


def _dates(text: str) -> list[tuple[int, int, int | None, tuple[int, int]]]:
    """(month, day, year or None, span) of every date in a text."""
    out = []
    for m in DAY_MONTH.finditer(text):
        out.append(
            (
                MON[m.group(2).lower().rstrip(".")],
                int(m.group(1)),
                int(m.group(3)) if m.group(3) else None,
                m.span(),
            )
        )
    for m in MONTH_DAY.finditer(text):
        if any(s[0] <= m.start() < s[1] for *_, s in out):
            continue
        out.append(
            (
                MON[m.group(1).lower().rstrip(".")],
                int(m.group(2)),
                int(m.group(3)) if m.group(3) else None,
                m.span(),
            )
        )
    for m in ISO_DATE.finditer(text):
        out.append((int(m.group(2)), int(m.group(3)), int(m.group(1)), m.span()))
    return [x for x in out if 1 <= x[1] <= 31 and 1 <= x[0] <= 12]


def allowed_of(pk: Packet) -> Allowed:
    text = norm(json.dumps(pk.for_writer(), ensure_ascii=False))
    numbers = {_num(x) for x in NUMBER.findall(text)}
    numbers |= {_num(x.group(0)) for x in re.finditer(r"\d+(?:\.\d+)?", text)}
    dates = _dates(text)
    years = {str(y) for _, _, y, _ in dates if y} | set(re.findall(r"\b(20\d\d)\b", text))
    full = []
    for mo, d, y, _ in dates:
        for yy in [y] if y else [int(x) for x in years]:
            try:
                full.append(date(yy, mo, d))
            except ValueError:
                pass
    months = {mo for mo, *_ in dates} | {MON[m.lower()] for m in MONTH_WORD.findall(text)}
    if full:
        lo, hi = min(full), max(full)
        y, mth = lo.year, lo.month
        while (y, mth) <= (hi.year, hi.month):
            months.add(mth)
            y, mth = (y + (mth == 12), mth % 12 + 1)
    weekdays = {WEEKDAYS[d.weekday()] for d in full} | set(WEEKDAY.findall(text))
    for w in NUMWORD.findall(text):
        numbers.add(str(NUMBER_WORDS[w.lower()]))
    return Allowed(
        numbers=numbers,
        dates={(mo, d) for mo, d, *_ in dates},
        years=years,
        months=months,
        weekdays=weekdays,
        times={f"{int(h)}:{m}" for h, m in TIME.findall(text)},
        ids=set(IDS.findall(text)),
        text=text.lower(),
    )


def check_text(pk: Packet, slot: str, text: str, names: Names, allowed: Allowed | None = None) -> list[str]:
    al = allowed or allowed_of(pk)
    s = pk.slots[slot]
    t = norm(text or "")
    probs: list[str] = []
    if not t:
        return [f"{slot}: empty"]
    words = len(t.split())
    lo, hi = s.words
    if words < lo * (1 - TOLERANCE) or words > hi * (1 + TOLERANCE):
        probs.append(f"{slot}: {words} words, write {lo}-{hi}")
    low = t.lower()
    for k in s.required:
        forms = pk.required[k]["forms"]
        if not any(norm(f).lower() in low for f in forms if f):
            probs.append(f"{slot}: missing the fact '{pk.required[k]['text']}' (write it exactly like that)")
    if any(ch.isalpha() and not ch.isascii() for ch in t):
        probs.append(f"{slot}: letters outside English")
    if URL.search(t):
        probs.append(f"{slot}: contains a URL")
    for e in EMAIL.findall(t):
        if e.lower() not in al.text:
            probs.append(f"{slot}: e-mail address {e} is not in the packet")
    for m in PLACEHOLDER.findall(t):
        probs.append(f"{slot}: placeholder or template text '{m}'")
    for b in pk.banned:
        if b.lower() in low:
            probs.append(f"{slot}: banned phrase '{b}'")
    for b in s.banned:
        if norm(b).lower() in low:
            probs.append(f"{slot}: '{b}' is not known yet when this text is written; leave it out")
    # ids first (they contain digits), then times, dates, the rest of the numbers
    spans: list[tuple[int, int]] = []
    for m in IDS.finditer(t):
        spans.append(m.span())
        if m.group(0) not in al.ids:
            probs.append(f"{slot}: id {m.group(0)} is not in the packet")
    for m in TIME.finditer(t):
        spans.append(m.span())
        if f"{int(m.group(1))}:{m.group(2)}" not in al.times:
            probs.append(f"{slot}: time {m.group(0)} is not in the packet")
    for mo, d, y, sp in _dates(t):
        spans.append(sp)
        if (mo, d) not in al.dates:
            probs.append(f"{slot}: date '{t[sp[0] : sp[1]]}' is not in the packet")
        if y and str(y) not in al.years:
            probs.append(f"{slot}: year {y} is not in the packet")

    def inside(i: int) -> bool:
        return any(a <= i < b for a, b in spans)

    for m in NUMBER.finditer(t):
        if inside(m.start()):
            continue
        n = _num(m.group(0))
        if n not in al.numbers:
            probs.append(f"{slot}: number {m.group(0)} is not in the packet")
    for m in NUMWORD.finditer(t):
        if str(NUMBER_WORDS[m.group(1).lower()]) not in al.numbers:
            probs.append(f"{slot}: the number '{m.group(1)}' is not in the packet")
    for m in MONTH_WORD.finditer(t):
        if inside(m.start()):
            continue
        if MON[m.group(1).lower()] not in al.months:
            probs.append(f"{slot}: month {m.group(1)} is not in the packet")
    for m in WEEKDAY.finditer(t):
        if m.group(1) not in al.weekdays:
            probs.append(f"{slot}: weekday {m.group(1)} is not in the packet (do not write weekdays)")
    # people and organisations
    for full, _first in names.people:
        if full in t and full.lower() not in al.text:
            probs.append(f"{slot}: person {full} is not in the packet")
    firsts_ok = {first for full, first in names.people if full.lower() in al.text or first.lower() in al.text}
    seen: set[str] = set()
    for _full, first in names.people:
        if first in COMMON_FIRST or first in seen or len(first) < 3 or first in firsts_ok:
            continue
        if re.search(rf"\b{re.escape(first)}\b", t):
            seen.add(first)
            probs.append(f"{slot}: person '{first}' is not in the packet")
    for org in names.orgs:
        if org.lower() in low and org.lower() not in al.text:
            probs.append(f"{slot}: organisation {org} is not in the packet")
    return sorted(set(probs), key=probs.index)


def check_artifact(pk: Packet, texts: dict[str, str] | None, names: Names) -> list[str]:
    if not isinstance(texts, dict):
        return ["no texts returned"]
    al = allowed_of(pk)
    probs: list[str] = []
    for slot in pk.slots:
        if not isinstance(texts.get(slot), str):
            probs.append(f"{slot}: missing")
            continue
        probs += check_text(pk, slot, texts[slot], names, al)
    return probs
