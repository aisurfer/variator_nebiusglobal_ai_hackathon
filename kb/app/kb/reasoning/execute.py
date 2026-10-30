"""Executor of query plans (Docs/KB/QUERY-PLANNER.md): plan -> PostgreSQL query over the journal -> results.

list / count  compiled into one SQL query over `entity` and its head version as known at K: kinds, ACL of the
              principal (the same filter as every other read path), the time filter (any kind: the event date
              property when the kind stores one — held_at, sent_at… — else the effective time of the first
              version; `changed` = a version effective in the window; `state` = a version that set the field to
              one of the values in the window) and one EXISTS per edge over the union of statements and declared
              causal links visible at K (both ends visible). The text rest ranks (and, with `text_filter`,
              filters) inside the filtered set through the hybrid search. The full set is counted; the list is
              cut at the plan's limit with an honest "shown N of M".
other         why / changes / impact / stale / timeline / lookup / search run the typed tools of the legacy
              pipeline with the plan's subject, time window and known_at.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from kb.reasoning.direction import split_by_direction
from kb.reasoning.plan import ANY_PREDICATE, Node, QueryPlan, Vocabulary
from kb.reasoning.router import ParsedQuestion
from kb.reasoning.timeparse import TimeInterpretation
from kb.reasoning.tools import ToolContext
from kb.reasoning.tools import call as _call
from kb.text import norm

log = logging.getLogger(__name__)
AMBIGUITY_RATIO = 0.85
# date properties that carry the time an entity happened, in order of preference (else the first version)
EVENT_FIELDS = ("held_at", "sent_at", "decided_at", "released_at", "signed_at", "doc_date", "date")
# a predicate that an entity may lack entirely; then weaker evidence of the same relation is used and labelled
FALLBACK_PREDICATES: dict[str, tuple[str, ...]] = {"participant": ("author", "mentions")}
PEOPLE_PREDICATES = ("participant", "author", "mentions", "addressed_to", "cc_to", "assignee", "reporter", "owner")
NAMED_KINDS = ["Person", "Organization", "Project"]


# ------------------------------------------------------------------------------------------------ grounding
def _name_forms(text: str) -> list[str]:
    """Base forms of a surname as written, most likely first: "Belov's" -> belov, belov's."""
    t = norm(text)
    forms = [t]
    for e in ("'s", "’s"):
        if t.endswith(e) and len(t) - len(e) >= 3:
            forms.insert(0, t[: -len(e)])
    return forms


def _pick(cands: list[dict], text: str | None) -> tuple[list[dict], list[dict]]:
    """Top candidate (people: the surname matched in its most likely base form) + alternatives."""
    if not cands:
        return [], []
    people = [c for c in cands if c.get("kind") == "Person"]
    if text and text.split() and people:
        last = text.split()[-1]
        forms = _name_forms(last)
        for f in forms:
            match = [c for c in people if f in norm(c.get("label") or "").split()]
            if match:
                top = match[0]
                others = [c for c in people if c.get("id") != top.get("id")
                          and set(forms) & set(norm(c.get("label") or "").split())]
                return [top], others
    top = cands[0]
    alts = [c for c in cands[1:] if c.get("id") != top.get("id")
            and float(c.get("score") or 0) >= AMBIGUITY_RATIO * float(top.get("score") or 0)]
    return [top], alts


def _stem(text: str) -> str:
    """A name as it may appear in text: quotes and a possessive "'s" dropped ("Coffee City's" -> "Coffee City")."""
    t = text.strip("«»“”\"' ")
    return re.sub(r"['’]s$", "", t) or text


def ground_node(ctx: ToolContext, node: Node) -> None:
    if node.group or not (node.text or node.key):
        return
    attempts: list[dict[str, Any]] = []
    if node.key:
        attempts.append({"text": node.key, "kind": "entity", "limit": 3})
    if node.text:
        attempts.append({"text": node.text, "kind": "entity", "entity_kinds": node.kinds or NAMED_KINDS, "limit": 6})
        attempts.append({"text": node.text, "kind": "entity", "limit": 6})
    for args in attempts:
        try:
            cands = _call(ctx, "ground", args) or []
        except Exception:
            log.exception("ground failed")
            continue
        if cands:
            node.grounded, node.alternatives = _pick(cands, node.text if not node.key else None)
            if node.key:
                node.alternatives = []
            node.ids = [c["id"] for c in node.grounded]
            return


def expand_organizations(ctx: ToolContext, node: Node, vocab: Vocabulary) -> None:
    """An organisation at the people end of a relation stands for itself and its people (works_at)."""
    orgs = [g["id"] for g in node.grounded if g.get("kind") == "Organization"]
    preds = vocab.membership_predicates()
    if not orgs or not preds:
        return
    rows = ctx.conn.execute(
        f"""SELECT DISTINCT s.subject_id FROM statement s JOIN entity e ON e.tenant_id = s.tenant_id AND e.entity_id = s.subject_id
            WHERE s.tenant_id = %s AND s.predicate_id = ANY(%s) AND s.object_id = ANY(%s) AND {_open('s')}
              AND {ctx.principal.sql_filter('e.acl')[0]}""",
        (ctx.principal.tenant_id, preds, orgs, *_k(ctx), *ctx.principal.sql_filter('e.acl')[1]),
    ).fetchall()
    node.ids = list(dict.fromkeys(node.ids + [r["subject_id"] for r in rows]))


def person_groups(ctx: ToolContext) -> dict[str, list[str]]:
    """internal = people with an account of the tenant (principal.person_entity_id) or an employee id;
    external = other people that work at an organisation or are marked external in the directory."""
    p = ctx.principal
    acl_sql, acl_params = p.sql_filter("e.acl")
    rows = ctx.conn.execute(
        f"""SELECT e.entity_id,
                   (EXISTS (SELECT 1 FROM principal pr WHERE pr.tenant_id = e.tenant_id AND pr.person_entity_id = e.entity_id)
                    OR v.content ? 'employee_id') AS staff,
                   (coalesce(v.content->>'external', '') = 'true'
                    OR EXISTS (SELECT 1 FROM statement s WHERE s.tenant_id = e.tenant_id AND s.subject_id = e.entity_id
                                 AND s.predicate_id = 'works_at' AND {_open('s')})) AS outside
            FROM entity e JOIN entity_version v ON v.tenant_id = e.tenant_id AND v.version_id = e.head_version_id
            WHERE e.tenant_id = %s AND e.kind_id = 'Person' AND e.tombstone_tx IS NULL AND {acl_sql}""",
        (*_k(ctx), p.tenant_id, *acl_params),
    ).fetchall()
    internal = [r["entity_id"] for r in rows if r["staff"]]
    external = [r["entity_id"] for r in rows if r["outside"] and not r["staff"]]
    return {"internal": internal, "external": external}


# ------------------------------------------------------------------------------------------------ SQL pieces
def _open(alias: str) -> str:
    """Statements or causal links known at K (%s; their knowledge interval, Docs/KB/AS-OF.md) or open now. A derived
    fact may be written after K on the transaction axis and still be known since an earlier moment."""
    return f"(%s::timestamptz IS NULL AND upper_inf({alias}.tx) OR {alias}.known @> %s::timestamptz)"


def _k(ctx: ToolContext) -> tuple:
    return (ctx.known_at, ctx.known_at)


def _event_ts(v: str, e: str) -> str:
    props = ", ".join(f"CASE WHEN {v}.content->>'{f}' ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN ({v}.content->>'{f}')::timestamptz END"
                      for f in EVENT_FIELDS)
    first = (f"(SELECT f.valid_from FROM entity_version f WHERE f.tenant_id = {e}.tenant_id AND f.entity_id = {e}.entity_id "
             f"ORDER BY f.seq LIMIT 1)")
    return f"coalesce({props}, {first})"


class _Sql:
    """A parameterised SQL builder (positional %s, parameters collected in order)."""

    def __init__(self) -> None:
        self.params: list[Any] = []

    def p(self, *values: Any) -> str:
        self.params.extend(values)
        return ", ".join(["%s"] * len(values))


def _edge_exists(b: _Sql, ctx: ToolContext, alias: str, predicate: str, reverse: bool, target_ids: list[str],
                 fallback: bool = True) -> str:
    """EXISTS over statements and declared causal links visible at K between `alias` and the target set."""
    t = ctx.principal.tenant_id
    me_s, other_s = ("s.object_id", "s.subject_id") if reverse else ("s.subject_id", "s.object_id")
    me_l, other_l = ("l.cause_entity_id", "l.effect_entity_id") if reverse else ("l.effect_entity_id", "l.cause_entity_id")
    k = ctx.known_at

    def one(preds: list[str] | None) -> str:
        # parameters are appended in the order they appear in the text
        part_s = f"SELECT 1 FROM statement s WHERE s.tenant_id = {b.p(t)} AND {me_s} = {alias}.entity_id AND "
        part_s += "TRUE" if preds is None else f"s.predicate_id = ANY({b.p(preds)})"
        part_s += (f" AND {other_s} = ANY({b.p(target_ids)}) AND "
                   f"({b.p(k)}::timestamptz IS NULL AND upper_inf(s.tx) OR s.known @> {b.p(k)}::timestamptz)")
        part_l = f"SELECT 1 FROM causal_link l WHERE l.tenant_id = {b.p(t)} AND {me_l} = {alias}.entity_id AND "
        part_l += "TRUE" if preds is None else f"l.predicate_id = ANY({b.p(preds)})"
        part_l += (f" AND {other_l} = ANY({b.p(target_ids)}) AND l.review_status = 'accepted' AND "
                   f"({b.p(k)}::timestamptz IS NULL AND upper_inf(l.tx) OR l.known @> {b.p(k)}::timestamptz)")
        return f"EXISTS ({part_s} UNION ALL {part_l})"

    if predicate == ANY_PREDICATE:
        return one(None)
    weak = FALLBACK_PREDICATES.get(predicate) if fallback and not reverse else None
    main = one([predicate])
    if not weak:
        return main
    has_any = (f"EXISTS (SELECT 1 FROM statement s WHERE s.tenant_id = {b.p(t)} AND s.subject_id = {alias}.entity_id "
               f"AND s.predicate_id = {b.p(predicate)} AND "
               f"({b.p(k)}::timestamptz IS NULL AND upper_inf(s.tx) OR s.known @> {b.p(k)}::timestamptz))")
    return f"({main} OR (NOT {has_any} AND {one(list(weak))}))"


# ------------------------------------------------------------------------------------------------ selection
def _node_ids(ctx: ToolContext, plan: QueryPlan, vocab: Vocabulary, node: Node, groups: dict[str, list[str]],
              depth: int = 0) -> list[str]:
    if node.group:
        return groups.get(node.group, [])
    if not node.variable:
        return node.ids
    if depth > 2:
        return []
    rows, _ = select_ids(ctx, plan, vocab, node.id, node.kinds, groups, depth + 1, with_time=plan.time.applies_to == node.id)
    return rows


def select_ids(ctx: ToolContext, plan: QueryPlan, vocab: Vocabulary, focus: str, kinds: list[str],
               groups: dict[str, list[str]], depth: int = 0, *, with_time: bool = True,
               ignore_time: bool = False) -> tuple[list[str], list[dict]]:
    """Entities of `kinds` matching the plan's edges around node `focus` (x or a variable node)."""
    p = ctx.principal
    acl_sql, acl_params = p.sql_filter("e.acl")
    k = ctx.known_at
    t = plan.time
    timed = with_time and not ignore_time and t.window
    # three builders, joined in text order: SELECT list, head join, WHERE
    bs, bh, b = _Sql(), _Sql(), _Sql()

    def change_cond(bb: _Sql) -> str:
        cond = ["v.tenant_id = e.tenant_id", "v.entity_id = e.entity_id"]
        if t.start:
            cond.append(f"v.valid_from >= {bb.p(t.start)}")
        if t.end:
            cond.append(f"v.valid_from < {bb.p(t.end)}")
        if k is not None:
            cond.append(f"lower(v.tx) <= {bb.p(k)}")
        if t.event == "state" and t.state_field:
            cond.append(f"({bb.p(t.state_field)} = ANY(v.changed_fields) OR v.seq = 1)")
            cond.append(f"lower(v.content->>{bb.p(t.state_field)}) = ANY({bb.p([x.lower() for x in t.state_values])})")
        return " AND ".join(cond)

    ev = _event_ts("h", "e")
    if timed and t.event != "occurred":  # the time of the matching change is the item's date
        ev = f"(SELECT max(v.valid_from) FROM entity_version v WHERE {change_cond(bs)})"
    head = ("JOIN entity_version h ON h.tenant_id = e.tenant_id AND h.version_id = e.head_version_id"
            if k is None else
            f"JOIN LATERAL (SELECT * FROM entity_version hv WHERE hv.tenant_id = e.tenant_id AND hv.entity_id = e.entity_id "
            f"AND hv.tx @> {bh.p(k)}::timestamptz ORDER BY hv.seq DESC LIMIT 1) h ON TRUE")
    where = [f"e.tenant_id = {b.p(p.tenant_id)}", acl_sql.replace("%s", b.p(*acl_params)) if acl_params else acl_sql,
             "h.op <> 'delete'"]
    if k is None:
        where.append("e.tombstone_tx IS NULL")
    if kinds:
        where.append(f"e.kind_id = ANY({b.p(kinds)})")
    if timed:
        if t.event == "occurred":
            if t.start:
                where.append(f"{ev} >= {b.p(t.start)}")
            if t.end:
                where.append(f"{ev} < {b.p(t.end)}")
        else:
            where.append(f"EXISTS (SELECT 1 FROM entity_version v WHERE {change_cond(b)})")
    # several relations between the focus and the same node are alternatives («assignee or reporter of X»)
    groups_of: dict[tuple[str, bool], list[Any]] = {}
    for edge in plan.edges:
        if focus not in (edge.src, edge.dst):
            continue
        other_id = edge.dst if edge.src == focus else edge.src
        if other_id == "x" or plan.node(other_id) is None:
            continue  # an edge back to x is applied from x's side
        groups_of.setdefault((other_id, edge.negate), []).append(edge)
    for (other_id, edge_negate), edges in groups_of.items():
        other = plan.node(other_id)
        negate = edge_negate
        if other.group == "internal":
            # «only staff»: no object of the relation among the external people (an edge to the external group
            # means «at least one outsider»); the internal group is never matched positively
            ids, negate = groups.get("external", []), not negate
        else:
            ids = _node_ids(ctx, plan, vocab, other, groups, depth)
        alts = []
        for edge in edges:
            reverse = edge.dst == focus
            cond = _edge_exists(b, ctx, "e", edge.predicate, reverse, ids)
            if not other.variable and not other.group:
                # a named entity: the direction a model wrote may be inverted («X -assignee-> task»)
                cond = f"({cond} OR {_edge_exists(b, ctx, 'e', edge.predicate, not reverse, ids, fallback=False)})"
            loose = edge.predicate in (ANY_PREDICATE, "mentions") or (edge.predicate not in PEOPLE_PREDICATES and (d := vocab.predicates.get(edge.predicate))
                                                        and not d["domain"] and not d["range"] and not d["causal"])
            if loose and (other.text or other.key):
                # "decisions about PayPro": a relation to the entity, or its name in the text itself
                pat = f"%{_stem(other.key or other.text or '')}%"
                cond = f"({cond} OR h.title ILIKE {b.p(pat)} OR h.body ILIKE {b.p(pat)})"
            alts.append(cond)
        cond = alts[0] if len(alts) == 1 else "(" + " OR ".join(alts) + ")"
        where.append(f"NOT {cond}" if negate else cond)
    sql = (f"SELECT e.entity_id, e.kind_id, h.version_id, h.title, h.valid_from, {ev} AS event_at FROM entity e {head} "
           f"WHERE {' AND '.join(where)} ORDER BY event_at DESC NULLS LAST, h.valid_from DESC, e.entity_id")
    rows = ctx.conn.execute(sql, bs.params + bh.params + b.params).fetchall()
    return [r["entity_id"] for r in rows], rows


def _participants(ctx: ToolContext, ids: list[str]) -> dict[str, list[str]]:
    """People of each shown item (participants, else author and mentions): what the relation filter saw."""
    if not ids:
        return {}
    k = ctx.known_at
    rows = ctx.conn.execute(
        f"""SELECT s.subject_id, s.predicate_id, pe.title FROM statement s
            JOIN entity pe ON pe.tenant_id = s.tenant_id AND pe.entity_id = s.object_id AND pe.kind_id = 'Person'
            WHERE s.tenant_id = %s AND s.subject_id = ANY(%s) AND s.predicate_id = ANY(%s) AND {_open('s')}
              AND {ctx.principal.sql_filter('pe.acl')[0]}
            ORDER BY s.subject_id, s.predicate_id, pe.title""",
        (ctx.principal.tenant_id, ids, list(PEOPLE_PREDICATES), k, k, *ctx.principal.sql_filter('pe.acl')[1]),
    ).fetchall()
    by: dict[str, dict[str, list[str]]] = {}
    for r in rows:
        by.setdefault(r["subject_id"], {}).setdefault(r["predicate_id"], []).append(r["title"])
    out = {}
    for eid, d in by.items():
        people = d.get("participant") or list(dict.fromkeys((d.get("author") or []) + (d.get("mentions") or [])))
        out[eid] = people[:12]
    return out


def coverage(ctx: ToolContext, kinds: list[str]) -> dict[str, Any] | None:
    """The period the knowledge base holds data for (of these kinds, visible to the principal)."""
    p = ctx.principal
    acl_sql, acl_params = p.sql_filter("e.acl")
    ev = _event_ts("h", "e")
    row = ctx.conn.execute(
        f"""SELECT min({ev}) AS a, max({ev}) AS b, count(*) AS n FROM entity e
            JOIN entity_version h ON h.tenant_id = e.tenant_id AND h.version_id = e.head_version_id
            WHERE e.tenant_id = %s AND {acl_sql} AND e.tombstone_tx IS NULL AND (%s::text[] IS NULL OR e.kind_id = ANY(%s))""",
        (p.tenant_id, *acl_params, kinds or None, kinds or None),
    ).fetchone()
    if not row or not row["n"]:
        return None
    return {"start": row["a"], "end": row["b"], "count": row["n"]}


def select(ctx: ToolContext, plan: QueryPlan, vocab: Vocabulary, warnings: list[str]) -> dict[str, Any]:
    groups = person_groups(ctx) if any(n.group for n in plan.nodes) else {}
    ids, rows = select_ids(ctx, plan, vocab, "x", plan.kinds, groups)
    by_id = {r["entity_id"]: r for r in rows}
    order = list(ids)
    text_hits: dict[str, dict] = {}
    named = {norm(n.text or "") for n in plan.nodes if n.text and n.grounded}
    if plan.text_filter and norm(plan.text) in named:
        plan.text_filter = False  # the topic is the named entity, already an edge of the plan
    if plan.text and ids and (plan.text_filter or plan.sort == "relevance"):
        try:
            hits = _call(ctx, "search", {"query": plan.text, "entity_ids": ids, "limit": 100,
                                         "mode": "lexical" if plan.text_filter else "hybrid"}) or []
            text_hits = {h["entity_id"]: h for h in hits}
        except Exception as exc:
            warnings.append("text ranking is unavailable: " + type(exc).__name__)
        if plan.text_filter:
            words = [w for w in re.findall(r"\w{4,}", norm(plan.text))]
            title_hit = {i for i in ids if words and any(w[:6] in norm(by_id[i]["title"] or "") for w in words)}
            order = [i for i in ids if i in text_hits or i in title_hit]
        if plan.sort == "relevance":
            rank = {eid: n for n, eid in enumerate(text_hits)}
            order = sorted(order, key=lambda i: rank.get(i, len(rank) + order.index(i)))
    if plan.sort == "time_asc":
        order = list(reversed(order))
    total = len(order)
    shown = order[: plan.limit]
    people = _participants(ctx, shown) if plan.kinds and set(plan.kinds) & {"Meeting", "Email", "Decision"} else {}
    items = []
    for i in shown:
        r = by_id[i]
        item = {"entity_id": i, "kind": r["kind_id"], "title": r["title"], "version_id": r["version_id"],
                "event_at": r["event_at"], "effective_from": r["valid_from"], "people": people.get(i, [])}
        if i in text_hits:
            item["snippet"] = text_hits[i].get("snippet")
        items.append(item)
    out: dict[str, Any] = {"items": items, "total": total, "shown": len(items), "limit": plan.limit,
                           "window": {"start": plan.time.start, "end": plan.time.end, "event": plan.time.event},
                           "groups": {g: len(v) for g, v in groups.items()}}
    if not total:
        out["coverage"] = coverage(ctx, plan.kinds)
        if plan.time.window:
            # what the same plan finds without the time filter: is it the window or the other filters that empty it
            any_ids, _ = select_ids(ctx, plan, vocab, "x", plan.kinds, groups, ignore_time=True)
            out["without_window"] = len(any_ids)
    return out


# ------------------------------------------------------------------------------------------------ other operations
# capitalised words that are not names of a thing: months, days, kind words, question words
_NOT_NAMES = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
    "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "monday",
    "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "i", "ok", "q1", "q2", "q3", "q4",
}
_STOP = {"with", "from", "into", "about", "after", "before", "their", "there", "where", "which", "what", "when",
         "does", "that", "this", "than", "them", "they", "your", "ours", "sign", "signed", "give", "gave", "make",
         "made", "much", "many", "most", "last", "first", "latest", "company", "work", "used", "uses", "ever"}


def question_names(question: str) -> list[str]:
    """Proper names of a question («Quillfeather Aerospace», «Reykjavik»): capitalised words that are not its first
    word, not a key (PLAT-34), not a month, day or kind word. Quoted names are matched as a whole elsewhere."""
    from kb.reasoning.router import KIND_HINTS, unquoted

    words = re.findall(r"(?<![\w-])([A-Z][a-z][\w’'-]*)", unquoted(question))
    first = re.match(r"\s*([A-Za-z][\w’'-]*)", question)
    out = []
    for w in words:
        w = re.sub(r"['’]s$", "", w)
        low = w.lower()
        if (first and w == first.group(1)) or low in _NOT_NAMES or any(rx.fullmatch(w) for rx, _ in KIND_HINTS):
            continue
        if low not in out:
            out.append(low)
    return out


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9]{3,}", norm(text)) if w not in _STOP]


def plausible(cand: dict, parsed: ParsedQuestion, names: list[str]) -> bool:
    """A candidate may be the subject: an exact key or name match, or every proper name of the question in its
    label or description and more than a third of the question's content words (5-letter stems). A semantic near
    miss («Fairhaven Academy» for «a contract with Quillfeather Aerospace») is not the subject."""
    matched = " ".join(cand.get("matched") or []) if isinstance(cand.get("matched"), list) else str(cand.get("matched") or "")
    if re.search(r"\b(key|exact)\b", matched):
        return True
    text = norm(f"{cand.get('label') or ''} {cand.get('description') or ''}")
    if any(n not in text for n in names):
        return False
    words = _content_words(parsed.subject_text or parsed.question)
    if not words:
        return True
    stems = {w[:5] for w in re.findall(r"[a-z0-9]+", text)}
    return sum(w[:5] in stems for w in words) > len(words) / 3


def _quoted_title(ctx: ToolContext, parsed: ParsedQuestion) -> dict | None:
    """A quoted name that is exactly the title of a record wins («… the report 'Company metrics: week of 08 Dec
    2025'» is that report, not the semantically nearest week). With several quoted titles, one of a kind the
    question names first («which ticket …») wins."""
    exact: list[dict] = []
    for qt in parsed.quotes:
        try:
            cands = _call(ctx, "ground", {"text": qt, "kind": "entity", "limit": 10}) or []
        except Exception:
            log.exception("ground failed")
            continue
        exact += [c for c in cands if norm(c.get("label") or "") == norm(qt)]
    if not exact:
        return None
    for k in parsed.kind_hints:
        for c in exact:
            if c.get("kind") == k:
                return c
    return exact[0]


def ground_subject(ctx: ToolContext, parsed: ParsedQuestion) -> tuple[dict | None, list[dict]]:
    """Exact keys first, then a quoted exact title, then the subject phrase with kind hints, then without them.

    A candidate whose direction is the opposite of the question's («fell» for «why did incidents rise?») is never
    the subject: it ranks below the others, and when only such candidates are found there is no subject (they stay
    alternatives the answer names). A candidate that does not carry the question's names is not the subject either
    (`plausible`); the names no candidate carries go to `parsed.missing`, and the answer says that nothing names them.
    """
    if not parsed.keys:
        top = _quoted_title(ctx, parsed)
        if top:
            return top, []
    names = question_names(parsed.question)
    implausible: list[dict] = []
    attempts: list[dict[str, Any]] = []
    for key in parsed.keys:
        attempts.append({"text": key, "kind": "entity", "limit": 3})
    if parsed.subject_text:
        if parsed.kind_hints:
            attempts.append({"text": parsed.subject_text, "kind": "entity", "entity_kinds": parsed.kind_hints, "limit": 5})
        attempts.append({"text": parsed.subject_text, "kind": "entity", "limit": 5})
    against: list[dict] = []
    for args in attempts:
        try:
            cands = _call(ctx, "ground", args)
        except Exception:
            log.exception("ground failed")
            continue
        if not cands:
            continue
        if args.get("text") not in parsed.keys:  # an exact key is what was asked for, whatever its wording
            cands, opposite = split_by_direction(parsed.question, cands)
            against += [c for c in opposite if c.get("id") not in {a.get("id") for a in against}]
            weak = [c for c in cands if not plausible(c, parsed, names)]
            implausible += [c for c in weak if c.get("id") not in {a.get("id") for a in implausible}]
            cands = [c for c in cands if c not in weak]
            if not cands:
                continue
        top = cands[0]
        alts = [c for c in cands[1:] if c.get("id") != top.get("id")
                and float(c.get("score") or 0) >= AMBIGUITY_RATIO * float(top.get("score") or 0)]
        if args.get("text") in parsed.keys:  # an exact key is not ambiguous
            alts = []
        return top, alts
    if implausible:
        found = " ".join(norm(f"{c.get('label') or ''} {c.get('description') or ''}") for c in implausible)
        parsed.missing = [n for n in names if n not in found] or names or [parsed.subject_text]
    return None, (against + implausible)[:3]


# business words of a question -> the record fields that hold the answer (first present wins)
FIELD_WORDS: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"account manager|\bowner\b|responsible", re.I), ("owner_name", "owner")),
    (re.compile(r"\bstage\b|\bstatus\b|\bstate\b", re.I), ("status", "stage", "state")),
    (re.compile(r"due date|\bdue\b|deadline", re.I), ("due_date", "due_at")),
    (re.compile(r"\bamount\b|\bvalue\b|how much", re.I), ("amount", "value", "total")),
    (re.compile(r"expected close|close date", re.I), ("expected_close", "closed_at")),
    (re.compile(r"\bassignee\b|assigned to|who works on", re.I), ("assignee_name", "assignee")),
    (re.compile(r"\bpriority\b", re.I), ("priority",)),
    (re.compile(r"\bprobability\b", re.I), ("probability",)),
    (re.compile(r"notice period|\bnotice\b", re.I), ("notice_days",)),
    (re.compile(r"lost reason|why .* lost", re.I), ("lost_reason",)),
    (re.compile(r"\bversion\b|\brelease\b", re.I), ("version", "release")),
]


def asked_fields(ctx: ToolContext, parsed: ParsedQuestion, subject: dict | None, alternatives: list[dict],
                 ti: TimeInterpretation) -> list[dict]:
    """The field the question asks for («who is the account manager», «which stage was the deal in on …»), read from
    the subject or, when the subject lacks it, from the other candidates (the contract of a client). With a time
    window, the value valid at its end."""
    wanted = [fs for rx, fs in FIELD_WORDS if rx.search(parsed.question)]
    if not wanted or not subject:
        return []
    out: list[dict] = []
    for cand in [subject] + list(alternatives)[:3]:
        eid = cand.get("id")
        if not eid:
            continue
        version = None
        if ti.end is not None:
            try:
                rows = _call(ctx, "as_of", {"entity_ids": [eid], "valid_at": (ti.end - timedelta(seconds=1)).isoformat()}) or []
                version = rows[0] if rows else None
            except Exception:
                log.warning("as_of failed for %s", eid)
        if version is None:
            try:
                card = _call(ctx, "entity", {"entity_id": eid}) or {}
            except Exception:
                continue
            version = card.get("head") or {}
        fields = version.get("fields") or {}
        for names in wanted:
            key = next((f for f in names if fields.get(f) not in (None, "", [])), None)
            if key:
                out.append({"field": key, "value": fields[key], "entity_id": eid, "title": cand.get("label") or version.get("title"),
                            "version_id": version.get("version_id"), "effective_from": version.get("effective_from")})
        if out:
            return out
    return out


def _in_window(h: dict, ti: TimeInterpretation) -> bool:
    t = h.get("effective_from")
    if isinstance(t, str):
        try:
            t = datetime.fromisoformat(t)
        except ValueError:
            return False
    if not isinstance(t, datetime):
        return False
    return (ti.start is None or t >= ti.start) and (ti.end is None or t < ti.end)


def run_operation(ctx: ToolContext, parsed: ParsedQuestion, subject: dict | None, ti: TimeInterpretation,
                  warnings: list[str]) -> dict[str, Any]:
    """The typed tools of an operation other than list/count. A failing tool is recorded (receipt + warning) and
    the answer degrades."""
    def call(c: ToolContext, name: str, args: dict[str, Any]) -> Any:
        try:
            return _call(c, name, args)
        except Exception as exc:
            log.warning("tool %s failed: %s", name, exc)
            warnings.append(f"tool {name} is unavailable: " + type(exc).__name__)
            return None

    def search(limit: int = 8) -> list[dict]:
        """Fragments answering the question; with a time window, those effective in it come first."""
        if ti.start is None and ti.end is None:
            return call(ctx, "search", {"query": parsed.question, "limit": limit}) or []
        pool = call(ctx, "search", {"query": parsed.question, "limit": 30}) or []
        inside = [h for h in pool if _in_window(h, ti)]
        rest = [h for h in pool if not _in_window(h, ti)]
        return (inside + rest)[:limit] if inside else pool[:limit]

    sid = subject["id"] if subject else None
    out: dict[str, Any] = {}
    if parsed.intent == "why" and sid:
        # inferred candidates (when a tenant has any) join the chains as labelled hypotheses
        out["why"] = call(ctx, "why", {"target": sid, "depth": 8, "include_inferred": True})
        if not (out["why"] or {}).get("links"):
            # regimes 2 and 3 (research §5.6): nothing declared -> likely related + textual evidence, as hypotheses
            card = call(ctx, "entity", {"entity_id": sid}) or {}
            t = ((card.get("head") or {}).get("effective_from"))
            t = t if isinstance(t, datetime) else (datetime.fromisoformat(t) if t else ctx.now)
            related = call(ctx, "ppr", {"seeds": [sid], "window_start": (t - timedelta(days=45)).isoformat(),
                                        "window_end": t.isoformat(), "top_k": 8}) or []
            out["related"] = [r for r in related if r.get("kind") not in ("Person", "Account")][:5]
            hits = search()
            out["text_evidence"] = sorted(hits, key=lambda h: h.get("entity_id") != sid)[:5]
    elif parsed.intent == "impact" and sid:
        # a cancellation changes the object as a whole: the impact of a change of any field (Plancy ran its what-if
        # simulator here; this copy does not include it)
        fields = ["*"] if parsed.hypothetical == "cancel" else (parsed.fields or ["*"])
        out["impact"] = call(ctx, "impact", {"entity_id": sid, "fields": fields, "depth": 4})
    elif parsed.intent == "stale":
        out["stale"] = call(ctx, "stale_dependencies", {"scope": sid, "limit": 30})
    elif parsed.intent == "changes" and sid:
        out["history"] = call(ctx, "history", {"entity_id": sid, "start": ti.start.isoformat() if ti.start else None,
                                               "end": ti.end.isoformat() if ti.end else None})
        hist = out["history"] or []
        if len(hist) >= 2:
            out["diff"] = call(ctx, "diff", {"entity_id": sid, "from_seq": hist[0]["seq"] - (1 if hist[0]["seq"] > 1 else 0),
                                             "to_seq": hist[-1]["seq"]})
    elif parsed.intent == "timeline":
        ids = [sid] if sid else None
        out["timeline"] = call(ctx, "timeline", {"entity_ids": ids, "start": ti.start.isoformat() if ti.start else None,
                                                 "end": ti.end.isoformat() if ti.end else None, "limit": 50})
        if sid:
            out["why"] = call(ctx, "why", {"target": sid, "depth": 3, "include_inferred": True})
    elif parsed.intent == "lookup" and sid:
        out["entity"] = call(ctx, "entity", {"entity_id": sid})
    if not out:
        out["search"] = search()
        if sid and not out.get("entity"):
            out["entity"] = call(ctx, "entity", {"entity_id": sid})
    elif parsed.intent != "stale" and "text_evidence" not in out:
        # the grounded subject may be the wrong artefact: fragments answering the question itself go along as
        # evidence, so the answer (template and LLM) can rest on what retrieval found
        hits = search()
        out["search"] = sorted(hits, key=lambda h: h.get("entity_id") != sid)[:6]
    return out


def execute(ctx: ToolContext, plan: QueryPlan, vocab: Vocabulary, parsed: ParsedQuestion, ti: TimeInterpretation,
            warnings: list[str]) -> tuple[dict[str, Any], dict | None, list[dict]]:
    """Ground the plan's nodes and run its operation. Returns (results, subject, alternatives)."""
    for n in plan.nodes:
        if plan.operation in ("list", "count") or n.id != plan.subject:
            ground_node(ctx, n)
            if any(e.predicate in PEOPLE_PREDICATES or e.predicate == ANY_PREDICATE
                   for e in plan.edges if n.id in (e.src, e.dst)):
                expand_organizations(ctx, n, vocab)
    if plan.operation in ("list", "count"):
        res = select(ctx, plan, vocab, warnings)
        return {"list": res}, None, []
    subject, alternatives = None, []
    if parsed.intent != "stale" or parsed.keys or parsed.kind_hints:
        subject, alternatives = ground_subject(ctx, parsed)
        if parsed.intent == "stale" and subject and not parsed.keys:
            subject, alternatives = None, []  # "what is stale" is tenant-wide unless a key names the scope
    if plan.subject and subject:
        n = plan.node(plan.subject)
        if n:
            n.grounded, n.alternatives, n.ids = [subject], alternatives, [subject["id"]]
    results = run_operation(ctx, parsed, subject, ti, warnings)
    asked = asked_fields(ctx, parsed, subject, alternatives, ti)
    if asked:
        results["asked"] = asked
    return results, subject, alternatives
