"""Facts packets: what a writer may say, in the exact form it must say it.

A ``Packet`` collects the facts of one artifact as display strings ("12 November 2025",
"26 minutes", "$12.22M"), the people and organisations it may name, the text fields to
write with their word limits and required facts, and the banned phrases. The writer sees
``Packet.for_writer()``; the checker uses the same packet to derive what is allowed.

Formatting rules (one display form per value, so the checker can be exact):

- dates ``12 November 2025`` (forms: ``12 November``, ``November 12``, ``12 Nov``);
- times ``10:24`` (company time zone);
- money ``$12.22M`` for millions, ``$837,200`` below a million;
- durations in minutes ``26 minutes``; percentages ``99.5%``; sprint load ``0.97``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

NUMBER_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
WORD_OF = {v: k for k, v in NUMBER_WORDS.items()}


# ---------------------------------------------------------------------------------------------- display
def fmt_date(d: date | datetime) -> str:
    return f"{d.day} {d:%B %Y}"


def fmt_day(d: date | datetime) -> str:
    """Without the year: 12 November."""
    return f"{d.day} {d:%B}"


def fmt_time(t: datetime) -> str:
    return f"{t:%H:%M}"


def fmt_money(x: float) -> str:
    if abs(x) >= 1_000_000:
        return f"${x / 1_000_000:.2f}M"
    return f"${x:,.0f}"


def fmt_pct(x: float, digits: int = 1) -> str:
    s = f"{100 * x:.{digits}f}".rstrip("0").rstrip(".")
    return f"{s}%"


def fmt_minutes(m: float) -> str:
    n = int(round(m))
    return f"{n} minute" + ("" if n == 1 else "s")


def fmt_count(n: int, noun: str, plural: str | None = None) -> str:
    return f"{n} {noun if n == 1 else (plural or noun + 's')}"


def date_forms(d: date | datetime) -> list[str]:
    return [fmt_date(d), fmt_day(d), f"{d:%B} {d.day}", f"{d.day} {d:%b}", f"{d:%b} {d.day}"]


def money_forms(text: str) -> list[str]:
    out = [text]
    m = re.fullmatch(r"\$(\d+(?:\.\d+)?)M", text)
    if m:
        out += [f"${m.group(1)} million", f"{m.group(1)}M", f"${m.group(1)}m"]
    return out


def count_forms(text: str) -> list[str]:
    m = re.fullmatch(r"(\d+) (.+)", text)
    if m and int(m.group(1)) in WORD_OF:
        return [text, f"{WORD_OF[int(m.group(1))]} {m.group(2)}"]
    return [text]


def minute_forms(text: str) -> list[str]:
    m = re.fullmatch(r"(\d+) minutes?", text)
    return [text, f"{m.group(1)} min", f"{m.group(1)}-minute"] if m else [text]


# ---------------------------------------------------------------------------------------------- packet
@dataclass
class Slot:
    """A text field the writer fills."""

    name: str
    about: str
    words: tuple[int, int]
    required: list[str] = field(default_factory=list)  # fact keys
    banned: list[str] = field(default_factory=list)  # e.g. a date not yet known when this text is written


@dataclass
class Packet:
    """The facts of one artifact for its writer and its checker."""

    id: str
    kind: str  # human label: "post-incident review notes"
    setting: str  # one sentence: where the text appears, who writes it, when
    facts: dict[str, Any] = field(default_factory=dict)  # nested display facts (context)
    required: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> {text, forms}
    people: dict[str, dict[str, str]] = field(default_factory=dict)  # id -> {first, name, role}
    orgs: dict[str, str] = field(default_factory=dict)  # id -> name
    slots: dict[str, Slot] = field(default_factory=dict)
    banned: list[str] = field(default_factory=list)
    voice: str = ""  # who speaks (for mails)

    # ------------------------------------------------------------------ registration
    def person(self, base: Any, pid: str) -> str:
        """Register an employee; return the first name to use."""
        p = base.people[pid]
        first = first_name(p.name, p.login)
        self.people[pid] = {"first": first, "name": p.name, "role": p.title}
        return first

    def contact(self, base: Any, cid: str) -> str:
        c = base.contacts[cid]
        self.people[cid] = {"first": c.first, "name": c.name, "role": c.role}
        if c.org in base.orgs:
            self.org(base, c.org)
        return c.name

    def org(self, base: Any, oid: str) -> str:
        name = base.org_name(oid)
        self.orgs[oid] = name
        return name

    def need(self, key: str, text: str, forms: list[str] | None = None) -> str:
        """A fact that a slot may require; ``forms`` are the accepted spellings."""
        allf = [text] + [f for f in (forms or []) if f != text]
        self.required[key] = {"text": text, "forms": allf}
        return text

    def slot(
        self,
        name: str,
        about: str,
        words: tuple[int, int],
        required: list[str] | None = None,
        banned: list[str] | None = None,
    ) -> None:
        for k in required or []:
            if k not in self.required:
                raise KeyError(f"{self.id}: slot {name} requires the unknown fact {k}")
        self.slots[name] = Slot(name, about, words, list(required or []), list(banned or []))

    # ------------------------------------------------------------------ views
    def for_writer(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "document": self.kind,
            "setting": self.setting,
            **({"voice": self.voice} if self.voice else {}),
            "people": [
                {"first_name": v["first"], "full_name": v["name"], "role": v["role"]}
                for v in self.people.values()
            ],
            "organisations": sorted(set(self.orgs.values())),
            "facts": self.facts,
            "fields": {
                s.name: {
                    "about": s.about,
                    "words": f"{s.words[0]}-{s.words[1]}",
                    "must_include": [self.required[k]["text"] for k in s.required],
                    **({"must_not_mention": s.banned} if s.banned else {}),
                }
                for s in self.slots.values()
            },
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "setting": self.setting,
            "voice": self.voice,
            "facts": self.facts,
            "required": self.required,
            "people": self.people,
            "orgs": self.orgs,
            "slots": {
                k: {"about": s.about, "words": list(s.words), "required": s.required, "banned": s.banned}
                for k, s in self.slots.items()
            },
            "banned": self.banned,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Packet:
        p = cls(d["id"], d["kind"], d["setting"])
        p.voice = d.get("voice") or ""
        p.facts = d["facts"]
        p.required = d["required"]
        p.people = d["people"]
        p.orgs = d["orgs"]
        p.slots = {
            k: Slot(k, v["about"], tuple(v["words"]), list(v["required"]), list(v.get("banned") or []))
            for k, v in d["slots"].items()
        }
        p.banned = list(d.get("banned") or [])
        return p


def first_name(name: str, login: str | None = None) -> str:
    """The first name; 'Mei Lin Chow' with login 'meilin.chow' -> 'Mei Lin'."""
    parts = name.replace("Dr. ", "").split()
    if login and len(parts) >= 3:
        lead = login.split(".")[0]
        if lead == (parts[0] + parts[1]).lower():
            return f"{parts[0]} {parts[1]}"
    return parts[0]


def placeholder_text(packet: Packet, slot: str) -> str:
    """A text that passes the checker without an LLM: the required facts, joined (for tests)."""
    s = packet.slots[slot]
    facts = [packet.required[k]["text"] for k in s.required]
    words = "This note records the facts below for the team" if not facts else "Facts for this section"
    body = f"{words}: " + "; ".join(facts) + "." if facts else f"{words}."
    n = len(body.split())
    filler = " The team reviewed the points and agreed to keep the notes short and factual."
    while n < s.words[0]:
        body += filler
        n = len(body.split())
    return body
