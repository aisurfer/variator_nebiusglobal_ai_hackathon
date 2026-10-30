"""Read-only audit of a generated world: what a careful reader would notice before trusting the corpus.

    PYTHONPATH=sim:app python -m kbsim.audit company [--json]

Checks
- order: a reply sent before its parent; a comment/update/merge before the artefact was created;
- absent: a person acting on a day of their absence (availability.yaml) or an internal person on a day off;
- banned: topics of the negative cases (banned_terms.yaml) anywhere in the text;
- future: a fact of the scenario seen before the first beat that introduces it (a leak of the plot into an
  earlier artefact, usually from a persona card or a document written ahead of time);
- confidential: a fact of a confidential beat in an artefact visible to people outside the beat.
Every finding is a warning with context: some of them are legitimate (a number that also means something else).
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

from kbsim.models import WorldEvent
from kbsim.tenant import TenantState

SKIP_KEYS = {"id", "message_id", "thread_id", "in_reply_to", "sender", "to", "cc", "mailbox_acl", "author", "assignee",
             "reporter", "handle", "doc_id", "content_type", "filename", "sha", "key", "project", "space", "repo",
             "sent", "created", "updated", "merged_at", "doc_date", "ts"}


def norm(s: str) -> str:
    """Casefold, unify non-breaking spaces, drop quotes, collapse whitespace (for substring checks)."""
    s = s.casefold().replace("\u00a0", " ").replace("\u202f", " ")
    s = re.sub(r"[\"\u201c\u201d\u201e\u00ab\u00bb]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def texts(x: Any, key: str = "") -> Iterator[str]:
    if isinstance(x, str):
        if key not in SKIP_KEYS:
            yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield from texts(v, k)
    elif isinstance(x, list):
        for v in x:
            yield from texts(v, key)


def event_text(ev: WorldEvent) -> str:
    return "\n".join(texts(ev.data))


def _ctx(text: str, form: str, width: int = 70) -> str:
    t = text.replace("\n", " ")
    i = norm(t).find(norm(form))
    if i < 0:
        return t[:2 * width]
    return t[max(0, i - width):i + len(form) + width]


class Audit:
    def __init__(self, s: TenantState):
        self.s, self.w, self.d = s, s.w, s.d
        self.found: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def add(self, check: str, ev: WorldEvent, msg: str, **extra: Any) -> None:
        self.found[check].append({"seq": ev.seq, "ts": ev.ts.isoformat(), "actor": ev.actor, "kind": ev.kind,
                                  "artifact": ev.artifact_id, "beat": ev.beat_id, "msg": msg, **extra})

    def order(self) -> None:
        created: dict[tuple[str, str], Any] = {}
        sent = {}
        for ev in self.w.events:
            if ev.kind == "email.sent":
                sent[ev.data.get("message_id")] = ev.ts
                parent = ev.data.get("in_reply_to")
                if parent and parent in sent and sent[parent] > ev.ts:
                    self.add("order", ev, f"reply sent before its parent ({sent[parent].isoformat()})")
            if ev.kind.endswith(".created"):
                created.setdefault((ev.artifact_type, ev.artifact_id), ev.ts)
            elif ev.kind.split(".")[1] in ("commented", "updated", "merged"):
                c = created.get((ev.artifact_type, ev.artifact_id))
                if c and c > ev.ts:
                    self.add("order", ev, f"{ev.kind} before creation ({c.isoformat()})")

    def absent(self) -> None:
        for ev in self.w.events:
            if ev.actor not in self.w.people:
                continue
            if self.d.absent(ev.actor, ev.day):
                self.add("absent", ev, "action during an absence")
            elif not self.d.is_workday(ev.day):
                self.add("absent", ev, "action on a day off")

    def banned(self) -> None:
        pats = [re.compile(p, re.I) for p in self.s.banned]
        for ev in self.w.events:
            t = event_text(ev)
            for p in pats:
                m = p.search(t)
                if m:
                    self.add("banned", ev, f"banned topic \"{m.group(0)}\"", context=_ctx(t, m.group(0)))

    def _known(self) -> list[tuple[int, str]]:
        """What a person may legitimately know, by the day it becomes known: the company file from day 1,
        then every beat's intent, facts, document terms and document texts from the beat's day."""
        known = [(1, norm((self.s.tenant_dir / "company.yaml").read_text(encoding="utf-8")))]
        for b in self.s.scenario.beats:
            parts = [b.intent, *(f for fact in b.facts for f in fact.any)]
            for a in b.attachments:
                parts += [a.title, *a.key_terms, self.d.docs.get(a.handle, "")]
            known.append((b.day, norm("\n".join(parts))))
        return known

    def future(self) -> None:
        first: dict[str, tuple[int, str]] = {}
        for b in sorted(self.s.scenario.beats, key=lambda b: (b.day, b.tick)):
            for f in (f for fact in b.facts for f in fact.any):
                f = norm(f)
                if any(ch.isdigit() for ch in f) and len(f) >= 4 or len(f) >= 12 and " " in f:
                    first.setdefault(f, (b.day, b.id))
        known = self._known()
        cache: dict[tuple[str, int], bool] = {}

        def is_known(f: str, day: int) -> bool:
            if (f, day) not in cache:
                cache[(f, day)] = any(d <= day and f in t for d, t in known)
            return cache[(f, day)]

        seen: set[tuple[str, str]] = set()
        for ev in self.w.events:
            t = norm(event_text(ev))
            for f, (day, bid) in first.items():
                if ev.day < day and f in t and (f, ev.artifact_id) not in seen and not is_known(f, ev.day):
                    seen.add((f, ev.artifact_id))
                    self.add("future", ev, f"\"{f}\" before beat {bid} (day {day})", context=_ctx(event_text(ev), f))

    def confidential(self) -> None:
        people = list(self.w.people)
        for b in self.s.scenario.beats:
            if not b.confidential:
                continue
            allowed = {b.actor, *b.audience}
            forms = [norm(f) for fact in b.facts for f in fact.any if len(norm(f)) >= 5]
            for ev in self.w.events:
                if ev.beat_id == b.id:  # the beat's own artefact: its ACL is set by the engine
                    continue
                t = norm(event_text(ev))
                hit = next((f for f in forms if f in t), None)
                if not hit:
                    continue
                viewers = {p for p in people if self.w.can_see(p, ev)}
                extra = sorted(viewers - allowed)
                if extra:
                    self.add("confidential", ev, f"fact \"{hit}\" of beat {b.id} is visible to: {', '.join(extra)}",
                             context=_ctx(event_text(ev), hit))

    def run(self) -> dict[str, list[dict[str, Any]]]:
        self.order()
        self.absent()
        self.banned()
        self.future()
        self.confidential()
        return dict(self.found)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tenant")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", type=Path, default=None)
    a = ap.parse_args()
    found = Audit(TenantState(a.tenant, a.root)).run()
    if a.json:
        print(json.dumps(found, ensure_ascii=False, indent=1))
        return
    for check, items in found.items():
        print(f"== {check}: {len(items)}")
        for x in items[:40]:
            print(f"  [{x['seq']}] {x['ts'][:16]} {x['actor']} {x['kind']} {x['artifact']} {x['beat'] or ''}: {x['msg']}")
            if x.get("context"):
                print(f"      …{x['context']}…")
    if not found:
        print("OK: no findings")


if __name__ == "__main__":
    main()
