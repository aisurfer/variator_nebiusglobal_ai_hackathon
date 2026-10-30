"""The records behind an actor card: the actor's own dated records before T, read by the card extractor.

A record is ``{"id": "sim.SUP-0001", "date": "2025-10-27", "kind": "ticket opened", "text": "..."}``: its KB
id, its day in the company time zone, its kind in words and one line of its content. Records come from the
versioned ``sim_*`` records of ``load_history``, which drops every row at or after T. Newest first, within a
character budget per card. When the KB ids are known, only records with a KB page are kept, so every
citation opens.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from variator.actors.history import History

# the record fields that name the actor of a card, by the card's link field
LINK_FIELDS = {
    "employee": ("employee", "actor", "assignee", "reporter"),
    "client": ("client", "affected_clients"),
    "vendor": ("vendor",),
}
DETAILS = ("status", "priority", "severity", "category", "type", "event_type", "score", "amount", "currency")
BUDGET_CHARS = 60_000  # about 15k tokens per card: a batch of cards stays far below Ultra's context


def _value(v: Any) -> str:
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def record_line(r: dict[str, Any]) -> str:
    """One line of a record: its title (or statement) and its main fields."""
    head = str(r.get("title") or r.get("statement") or str(r.get("kind") or "").replace("_", " ")).strip()
    extra = [f"{k.replace('_', ' ')} {_value(r[k])}" for k in DETAILS if r.get(k) not in (None, "", [])]
    return "; ".join([head, *extra]) if extra else head


def _actors_of(r: dict[str, Any], fields: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    for f in fields:
        v = r.get(f)
        if isinstance(v, list):
            out.update(x for x in v if isinstance(x, str))
        elif isinstance(v, str) and v:
            out.add(v)
    return out


def records_for(
    h: History,
    cards: list[dict[str, Any]],
    known: dict[str, Any] | None = None,
    budget: int = BUDGET_CHARS,
) -> dict[str, list[dict[str, Any]]]:
    """Card id -> the records of its actor (newest first, within ``budget`` characters of text)."""
    wanted: dict[str, list[tuple[str, str]]] = defaultdict(list)  # actor id -> (link field, card id)
    for c in cards:
        link = c.get("link") or {}
        if link.get("field") in LINK_FIELDS and link.get("id"):
            wanted[link["id"]].append((link["field"], c["id"]))
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for otype, rows in h.records.items():
        for r in rows:
            rid = r.get("id")
            if not rid or r["ts"] >= h.t_end:  # load_history cuts at T already; a card never sees past T
                continue
            kb = h.kb(otype, rid)
            if known is not None and kb not in known:
                continue
            for field, fields in LINK_FIELDS.items():
                for actor in _actors_of(r, fields) & wanted.keys():
                    for f, cid in wanted[actor]:
                        if f == field:
                            found[cid].append(
                                {
                                    "id": kb,
                                    "date": r["ts"].astimezone(h.tz).date().isoformat(),
                                    "kind": str(r.get("kind") or otype).replace("_", " "),
                                    "text": record_line(r),
                                    "_ts": r["ts"],
                                }
                            )
    out: dict[str, list[dict[str, Any]]] = {}
    for c in cards:
        rows = sorted(found.get(c["id"], []), key=lambda x: (x["_ts"], x["id"]), reverse=True)
        keep, used, seen = [], 0, set()
        for x in rows:
            key = (x["id"], x["kind"], x["text"])
            if key in seen:
                continue
            seen.add(key)
            used += len(x["text"]) + 40
            if used > budget:
                break
            keep.append({k: v for k, v in x.items() if k != "_ts"})
        out[c["id"]] = keep
    return out
