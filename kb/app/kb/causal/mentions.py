"""Implicit references by title: a message that names a ticket by its title, without its key («here is what we
delivered: allow an S3 export for large accounts; …») refers to that ticket.

The connectors see one record at a time, so they cannot know the titles of another source. This pass runs after a
load (`python -m kb.cli link-mentions`): for each text record it finds the whole titles of tickets (at least
MIN_WORDS words) and declares `refers_to` (text -> ticket) with origin `inferred:title_mention`, review status
`pending` (a labelled candidate, never a declared fact). The effect is the first version of the text that names
the title; the cause is the ticket version valid at that time (a text cannot refer to a later version). Running
it again declares nothing new.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

import psycopg

from kb.journal.writer import journal_tx
from kb.text import norm

ORIGIN = "inferred:title_mention"
TEXT_KINDS = ("Email",)
TICKET_KINDS = ("Story", "Task", "Bug", "Epic", "Issue")
MIN_WORDS = 3
TEMPLATE_SIZE = 5  # titles with the same first 2 words from this count on may be a template, not names
SHORT_WORDS = 4  # a template title up to this length is left out
CONFIDENCE = 0.8


def _words(text: str) -> list[str]:
    return re.findall(r"[0-9a-z]+", norm(text))


def title_index(titles: Iterable[tuple[str, str]]) -> dict[tuple[str, str], list[tuple[tuple[str, ...], str]]]:
    """(entity_id, title) -> an index by the first 2 words of each title: (words, entity_id). A title shared by
    several tickets is ambiguous and left out."""
    by_words: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for eid, title in titles:
        w = tuple(_words(title or ""))
        if len(w) >= MIN_WORDS:
            by_words[w].add(eid)
    index: dict[tuple[str, str], list[tuple[tuple[str, ...], str]]] = defaultdict(list)
    for w, ids in by_words.items():
        if len(ids) == 1:
            index[(w[0], w[1])].append((w, next(iter(ids))))
    # a short title of a template («Proposal for <client>»: many tickets with the same first 2 words) names the kind
    # of work, not one ticket: a text that says «the proposal for Granite Payments» does not refer to the ticket
    return {k: [(w, e) for w, e in v if len(v) < TEMPLATE_SIZE or len(w) > SHORT_WORDS] for k, v in index.items()}


def find_mentions(text: str, index: dict[tuple[str, str], list[tuple[tuple[str, ...], str]]]) -> list[tuple[str, str]]:
    """The tickets whose whole title is in the text, as a run of whole words: [(entity_id, title words)]."""
    w = _words(text)
    out: dict[str, str] = {}
    for i in range(len(w) - 1):
        for title, eid in index.get((w[i], w[i + 1]), ()):
            if tuple(w[i:i + len(title)]) == title:
                out.setdefault(eid, " ".join(title))
    return list(out.items())


def link_mentions(conn: psycopg.Connection, tenant_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Declare the title mentions of a tenant (see the module docstring). Returns counts and examples."""
    tickets = conn.execute(
        "SELECT entity_id, title FROM entity WHERE tenant_id = %s AND kind_id = ANY(%s) AND tombstone_tx IS NULL",
        (tenant_id, list(TICKET_KINDS))).fetchall()
    index = title_index((r["entity_id"], r["title"]) for r in tickets)
    texts = conn.execute(
        """SELECT DISTINCT ON (v.entity_id) v.entity_id, v.version_id, v.valid_from, v.title, v.body
           FROM entity_version v JOIN entity e ON e.tenant_id = v.tenant_id AND e.entity_id = v.entity_id
           WHERE v.tenant_id = %s AND e.kind_id = ANY(%s) AND e.tombstone_tx IS NULL AND v.op <> 'delete'
           ORDER BY v.entity_id, v.seq""",
        (tenant_id, list(TEXT_KINDS))).fetchall()
    found: list[dict[str, Any]] = []
    for t in texts:
        for eid, quote in find_mentions(f"{t['title'] or ''}\n{t['body'] or ''}", index):
            cause = conn.execute(
                """SELECT version_id FROM entity_version WHERE tenant_id = %s AND entity_id = %s AND valid_from <= %s
                   AND op <> 'delete' ORDER BY seq DESC LIMIT 1""", (tenant_id, eid, t["valid_from"])).fetchone()
            if cause is None:  # the ticket did not exist yet: the text cannot mean it
                continue
            found.append({"effect": t["entity_id"], "effect_version": t["version_id"], "cause": eid,
                          "cause_version": cause["version_id"], "quote": quote, "valid_from": t["valid_from"]})
    created = 0
    if found and not dry_run:
        with journal_tx(conn, tenant_id, "extract", actor_id="service:link-mentions", ref={"op": "link-mentions"}) as j:
            for f in found:
                before = conn.execute(
                    "SELECT count(*) AS n FROM causal_link WHERE tenant_id = %s AND origin = %s", (tenant_id, ORIGIN)).fetchone()["n"]
                j.declare_link(effect_entity_id=f["effect"], cause_entity_id=f["cause"], predicate="refers_to",
                               origin=ORIGIN, effect_version=f["effect_version"], cause_version=f["cause_version"],
                               evidence={"quote": f["quote"], "rule": "the whole ticket title in the text"},
                               confidence=CONFIDENCE, review_status="pending", valid_from=f["valid_from"])
                created += conn.execute(
                    "SELECT count(*) AS n FROM causal_link WHERE tenant_id = %s AND origin = %s", (tenant_id, ORIGIN)).fetchone()["n"] - before
    return {"tickets": len(tickets), "titles_indexed": sum(len(v) for v in index.values()), "texts": len(texts),
            "mentions": len(found), "created": created, "dry_run": dry_run,
            "examples": [f"{f['effect']} -> {f['cause']}: {f['quote']}" for f in found[:5]]}
