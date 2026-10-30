"""The card checker: a profile may only say what the facts it cites say.

A writer answer is ``{"sentences": [{"text", "cites": [fact keys]}]}``. A card is accepted when:

- it has 3-5 sentences and 40-130 words, and it names the actor;
- every sentence cites 1-3 facts of its card, and each cited fact has evidence in the KB;
- every number, date, time, month, id, person and organisation of a sentence occurs in the
  facts it cites (``variator.narrative.check.check_text`` on a packet of exactly those facts);
  so every number equals a habit parameter or a fact value;
- no date of the text is on or after T.

When the request carried the actor's records (``records``, the extractor of T19), a sentence may also cite
records by their KB id, and the answer lists the actor's behaviour ``traits``: each cites 1-3 of those
records and says only what they say. A cited record must be one of the request and dated before T.

``check_card`` returns the problems (empty: accepted).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from variator.narrative.check import ISO_DATE, Names, _dates, allowed_of, check_text
from variator.narrative.facts import Packet

SENTENCES = (3, 5)
WORDS = (40, 130)
SENTENCE_WORDS = (4, 60)
TRAITS = (2, 5)


def first_name(name: str) -> str:
    return name.split()[0] if name else name


def citable(card: dict[str, Any], known: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The facts a writer may cite: those with at least one evidence id that exists in the KB."""
    out = []
    for f in card.get("facts") or []:
        ev = [e for e in f.get("evidence") or [] if known is None or e in known]
        if ev:
            out.append({**f, "evidence": ev})
    return out


def sentence_packet(card: dict[str, Any], facts: list[dict[str, Any]], company: str | None) -> Packet:
    """A packet of the cited facts only: what one sentence may say (no digits outside the facts)."""
    pk = Packet(id="card", kind="actor card", setting="")
    pk.facts = {"facts": [f["text"] for f in facts]}
    for p in card.get("people") or []:
        pk.people[p["id"]] = {"first": first_name(p["name"]), "name": p["name"], "role": ""}
    for o in card.get("orgs") or []:
        pk.orgs[o["id"]] = o["name"]
    if company:
        pk.orgs["company"] = company
    return pk


def record_facts(records: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Record id -> a citable fact: every version of the record, each line with its day."""
    out: dict[str, dict[str, Any]] = {}
    for r in records or []:
        f = out.setdefault(r["id"], {"key": r["id"], "text": "", "dates": []})
        f["text"] = (f["text"] + " " if f["text"] else "") + f"{r['date']}: {r['text']}."
        f["dates"].append(r["date"])
    return out


def _check_line(
    label: str,
    text: str,
    cited: list[dict[str, Any]],
    cites: list[str],
    card: dict[str, Any],
    names: Names,
    t: date,
    company: str | None,
) -> list[str]:
    """One sentence or trait: its numbers, names and dates occur in what it cites; no date on or after T."""
    probs: list[str] = []
    for f in cited:
        if any(date.fromisoformat(d) >= t for d in f.get("dates") or []):
            probs.append(f"{label}: cites the record {f['key']}, dated on or after T")
    pk = sentence_packet(card, cited, company)
    allowed = allowed_of(pk)  # before the slot: the word range must not count as a fact
    pk.slot("s", "one sentence", SENTENCE_WORDS)
    for p in check_text(pk, "s", text, names, allowed):
        probs.append(f"{label}: {p.removeprefix('s: ')} (cited {', '.join(cites)})")
    for mo, d, y, _ in _dates(text):
        if y and date(y, mo, d) >= t:
            probs.append(f"{label}: the date {y}-{mo:02d}-{d:02d} is not before T")
    for m in ISO_DATE.finditer(text):
        if date.fromisoformat(m.group(0)) >= t:
            probs.append(f"{label}: the date {m.group(0)} is not before T")
    return probs


def check_traits(
    card: dict[str, Any],
    traits: Any,
    recs: dict[str, dict[str, Any]],
    names: Names,
    t: date,
    company: str | None,
) -> list[str]:
    """The behaviour traits an extractor drew from the records: each cites 1-3 records of the request."""
    if not recs:
        return []
    if not isinstance(traits, list):
        return ["no traits returned"]
    probs: list[str] = []
    low = min(TRAITS[0], len(recs))
    if not low <= len(traits) <= TRAITS[1]:
        probs.append(f"{len(traits)} traits, write {low}-{TRAITS[1]}")
    for i, tr in enumerate(traits, start=1):
        text = tr.get("text") if isinstance(tr, dict) else None
        cites = tr.get("cites") if isinstance(tr, dict) else None
        if not isinstance(text, str) or not text.strip():
            probs.append(f"trait {i}: empty")
            continue
        if not isinstance(cites, list) or not cites:
            probs.append(f"trait {i}: cites no record")
            continue
        unknown = [c for c in cites if c not in recs]
        if unknown:
            probs.append(f"trait {i}: cites records not in the request {unknown}")
        if len(cites) > 3:
            probs.append(f"trait {i}: cites {len(cites)} records, cite 1-3")
        cited = [recs[c] for c in cites if c in recs]
        if cited:
            probs += _check_line(f"trait {i}", text, cited, cites, card, names, t, company)
    return probs


def check_card(
    card: dict[str, Any],
    answer: dict[str, Any] | None,
    names: Names,
    *,
    t: date,
    known: dict[str, Any] | None = None,
    company: str | None = None,
    records: list[dict[str, Any]] | None = None,
) -> list[str]:
    if not isinstance(answer, dict) or not isinstance(answer.get("sentences"), list):
        return ["no sentences returned"]
    sents = answer["sentences"]
    probs: list[str] = []
    facts = {f["key"]: f for f in citable(card, known)}
    recs = record_facts(records)
    min_sent, min_words = limits(len(facts))
    if not min_sent <= len(sents) <= SENTENCES[1]:
        probs.append(f"{len(sents)} sentences, write {min_sent}-{SENTENCES[1]}")
    words = 0
    texts = []
    for i, s in enumerate(sents, start=1):
        text = s.get("text") if isinstance(s, dict) else None
        cites = s.get("cites") if isinstance(s, dict) else None
        if not isinstance(text, str) or not text.strip():
            probs.append(f"sentence {i}: empty")
            continue
        texts.append(text)
        words += len(text.split())
        if not isinstance(cites, list) or not cites:
            probs.append(f"sentence {i}: cites no fact")
            continue
        unknown = [c for c in cites if c not in facts and c not in recs]
        if unknown:
            probs.append(f"sentence {i}: cites unknown facts {unknown}")
        cited = [facts.get(c) or recs[c] for c in cites if c in facts or c in recs]
        if len(cited) > 3:
            probs.append(f"sentence {i}: cites {len(cited)} facts, cite 1-3")
        if not cited:
            continue
        probs += _check_line(f"sentence {i}", text, cited, cites, card, names, t, company)
    if texts and not min_words <= words <= WORDS[1]:
        probs.append(f"{words} words in total, write {min_words}-{WORDS[1]}")
    joined = " ".join(texts).lower()
    name = card.get("name") or ""
    forms = (
        [name]
        + list(card.get("aliases") or [])
        + ([first_name(name)] if card.get("kind") == "person" else [])
    )
    if texts and name and not any(f.lower() in joined for f in forms if f):
        probs.append(f"the profile does not name {name}")
    probs += check_traits(card, answer.get("traits"), recs, names, t, company)
    return sorted(set(probs), key=probs.index)


def limits(n_facts: int) -> tuple[int, int]:
    """(minimum sentences, minimum words): an actor with few facts gets a shorter profile."""
    if n_facts <= 3:
        return min(SENTENCES[0], max(n_facts, 1)), 20
    return SENTENCES[0], WORDS[0]


def fallback(card: dict[str, Any], known: dict[str, Any] | None, n: int = 4) -> dict[str, Any]:
    """A profile made of the fact texts themselves (code-written: every number is a fact)."""
    facts = citable(card, known)
    pick = [f for f in facts if f.get("topic") == "identity"][:1]
    pick += [f for f in facts if f not in pick][: n - len(pick)]
    return {"sentences": [{"text": f["text"], "cites": [f["key"]]} for f in pick]}
