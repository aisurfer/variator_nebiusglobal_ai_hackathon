"""Confluence Cloud adapter: v2 page JSON with an ADF body -> the page plus derived Decision / ActionItem records.

Payload shape (GET /wiki/api/v2/pages/{id}?body-format=atlas_doc_format&include-labels=true, simplified):
  id, title, status, spaceId, space {key, name}, authorId, createdAt, version {number, createdAt, authorId, message},
  labels {results: [{name}]}, body.atlas_doc_format.value (ADF as a JSON string).

Page kind comes from labels (meeting-notes, report, prd/spec), else a "Type" property, else the title.
The page-properties table (Page Properties macro, first column = label) carries the declared links:
  Based on   -> informed_by        Experiment -> based_on (experiment design fields)
  Decision   -> implements         Supersedes -> supersedes
Every decisionItem becomes a Decision `confluence.page-<id>.dec-<n>` (decided_in the page), every taskItem an
ActionItem `confluence.page-<id>.task-<n>`. n comes from the ADF localId when it is "dec-<n>"/"task-<n>", so
numbers stay stable across page edits; otherwise items are numbered by order of appearance.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from kb.connectors import _adf
from kb.connectors._util import (
    Links, account, aliases, based_on_sensitivity, clean_ws, container_statement, dig, evidence, iso, items, norm_label,
    parse_dt, parse_when, property_role, record_acl, tenant_tz, truncate, with_label,
)
from kb.connectors.base import AdapterContext, SourceConfig, register
from kb.contracts import CanonicalRecord, Ref, SourceEnvelope

_SECTIONS = {
    "agenda": {"agenda"},
    "discussion": {"discussion", "notes"},
    "findings": {"findings", "key findings", "results"},
    "summary": {"summary", "tl;dr"},
    "metrics": {"metrics", "key metrics"},
    "goal": {"goal", "goals"},
    "problem": {"problem", "context"},
    "requirements": {"requirements"},
}
_KIND_LABELS = {
    "Meeting": {"meeting-notes", "meeting"},
    "Report": {"report", "analytics-report"},
    "Spec": {"prd", "spec", "requirements"},
}
_KIND_WORDS = {
    "Meeting": {"meeting", "meeting notes"},
    "Report": {"report"},
    "Spec": {"prd", "spec", "specification"},
    "Document": {"document", "draft"},
}
_PAGE = re.compile(r"^/spaces/(?P<space>[^/?#]+)/pages/(?P<id>\d+)(?:/[^?#]*)?(?:\?[^#]*)?(?:#(?P<frag>.*))?$")
_VIEW = re.compile(r"^/pages/viewpage\.action\?(?:[^#]*&)?pageId=(?P<id>\d+)[^#]*(?:#(?P<frag>.*))?$")
_FRAG = re.compile(r"^(?P<kind>dec|task)-(?P<n>\d+)$")


def page_ref(source_id: str, page_id: str, label: str | None = None) -> Ref:
    return Ref(source_id=source_id, object_type="page", external_id=str(page_id), label=label)


def page_url(source: SourceConfig, space: str | None, page_id: str) -> str | None:
    if not source.base_url:
        return None
    base = source.base_url.rstrip("/")
    return f"{base}/spaces/{space}/pages/{page_id}" if space else f"{base}/pages/viewpage.action?pageId={page_id}"


def _number(nodes: list[dict], prefix: str) -> list[tuple[dict, int]]:
    """Stable item numbers: localId "<prefix>-<n>" wins, the rest are numbered by order, skipping used numbers."""
    used: set[int] = set()
    nums: list[int | None] = []
    for n in nodes:
        m = re.fullmatch(rf"{prefix}-(\d+)", str(_adf.attrs(n).get("localId") or ""))
        v = int(m.group(1)) if m else None
        if v is not None and v not in used and v > 0:
            used.add(v)
            nums.append(v)
        else:
            nums.append(None)
    counter = 0
    out: list[tuple[dict, int]] = []
    for n, v in zip(nodes, nums, strict=True):
        if v is None:
            counter += 1
            while counter in used:
                counter += 1
            used.add(counter)
            v = counter
        out.append((n, v))
    return out


_LEAD = re.compile(r"^[\s—–\-:,;]+")
_TRAIL = re.compile(r"[\s—–\-:,;(]*(?:owner|assignee)?[\s:—–\-,;()]*$", re.IGNORECASE)


def _item_text(node: dict) -> str:
    t = " ".join(_adf.inline_text(_adf.children(node), mentions=False).split())
    t = _LEAD.sub("", t)
    t = _TRAIL.sub("", t)
    return t.rstrip(" .;")


def _task_label(node: dict) -> str:
    who = _adf.mentions(node)
    text = _item_text(node)
    return f"{who[0][1]}: {text}" if who else text


def _metrics(blocks: list[dict]) -> list[dict] | str | None:
    ts = _adf.tables({"type": "doc", "content": blocks})
    if not ts:
        text = clean_ws(_adf.render(blocks))
        return text or None
    rows = _adf.table_rows(ts[0])
    if not rows:
        return None
    header = [" ".join(_adf.inline_text(c).split()) for c in rows[0]]
    has_header = all(c.get("type") == "tableHeader" for c in rows[0])
    if not has_header:
        return [{str(i): " ".join(_adf.inline_text(c).split()) for i, c in enumerate(r)} for r in rows]
    out = []
    for r in rows[1:]:
        cells = [" ".join(_adf.inline_text(c).split()) for c in r]
        out.append({(header[i] if i < len(header) and header[i] else str(i)): v for i, v in enumerate(cells)})
    return out


class _Prop:
    __slots__ = ("label", "role", "text", "urls", "mentions")

    def __init__(self, label: str, cell: dict):
        self.label = label
        self.role = property_role(label)
        self.text = " ".join(_adf.inline_text(cell).split())
        self.urls = _adf.urls(cell)
        self.mentions = _adf.mentions(cell)


def _properties(doc: dict) -> tuple[list[_Prop], dict | None]:
    table = _adf.properties_table(doc)
    if table is None:
        return [], None
    props = []
    for row in _adf.table_rows(table):
        if len(row) < 2:
            continue
        label = " ".join(_adf.inline_text(row[0]).split()).strip(" :")
        if label:
            props.append(_Prop(label, row[1]))
    return props, table


def _kind(labels: list[str], props: list[_Prop], title: str) -> str:
    ls = {norm_label(x) for x in labels}
    for kind, names in _KIND_LABELS.items():
        if ls & names:
            return kind
    for p in props:
        if p.role == "kind":
            v = norm_label(p.text)
            for kind, words in _KIND_WORDS.items():
                if v in words:
                    return kind
    t = norm_label(title)
    if t.startswith("report"):
        return "Report"
    if t.startswith(("prd", "spec")):
        return "Spec"
    return "Document"


class ConfluenceAdapter:
    provider = "confluence"

    def normalize(self, env: SourceEnvelope, ctx: AdapterContext) -> list[CanonicalRecord]:
        page_id = str(env.external_id)
        key = page_ref(env.source_id, page_id)
        entity_id = f"confluence.page-{page_id}"
        p = env.payload if isinstance(env.payload, dict) else {}
        title = str(p.get("title") or page_id)
        if env.operation == "delete" or env.payload is None:
            labels = [str(x.get("name")) for x in items(dig(p, "labels", "results")) if isinstance(x, dict)]
            return [CanonicalRecord(key=key, entity_id=entity_id, kind=_kind(labels, [], title), title=title, deleted=True)]

        space = env.container or dig(p, "space", "key")
        space_name = dig(p, "space", "name") or (ctx.source.containers[space].name if space in ctx.source.containers else None)
        acl = record_acl(env, ctx, space)
        url = page_url(ctx.source, space, page_id)
        tz = tenant_tz(ctx)
        doc = _adf.load(dig(p, "body", "atlas_doc_format", "value")) or _adf.load(dig(p, "body", "atlas_doc_format")) \
            or {"type": "doc", "content": []}
        labels = sorted({str(x.get("name")) for x in items(dig(p, "labels", "results"))
                         if isinstance(x, dict) and x.get("name")})
        props, props_table = _properties(doc)
        kind = _kind(labels, props, title)
        author_id = p.get("authorId")
        version_no = to_version(dig(p, "version", "number"))
        created_at = parse_dt(p.get("createdAt")) or parse_dt(dig(p, "version", "createdAt"))
        editor_id = dig(p, "version", "authorId") or author_id

        links = Links(key)
        links.add(container_statement("in_space", env.source_id, space, space_name))
        links.statement("author", account(ctx, author_id))
        for lab in labels:
            links.statement("labels", literal=lab)

        fields: dict[str, Any] = {"title": title, "space": space, "version": version_no, "labels": labels,
                                  "properties": {pr.label: pr.text for pr in props}}
        held_at: datetime | None = None
        ends_at: datetime | None = None
        attendees: list[tuple[str, str]] = []
        status = None
        prop_urls: set[str] = set()
        for pr in props:
            role = pr.role or ""
            prop_urls.update(u for u, _ in pr.urls)
            if role.startswith("cause:"):
                _, predicate, *hint = role.split(":")
                for u, lbl in pr.urls:
                    ref = with_label(ctx.ref_from_url(u), lbl or None)
                    if ref is None:
                        continue
                    sens = based_on_sensitivity(ref, hint[0] if hint else None) if predicate == "based_on" else None
                    links.cause(predicate, ref, sensitivity=sens,
                                evidence=evidence(f"{pr.label}: {pr.text}", field="properties", property=pr.label, url=u))
            elif role == "held_at":
                held_at, ends_at = parse_when(pr.text, tz)
            elif role == "attendees":
                attendees = pr.mentions
            elif role == "owner":
                fields["owner"] = ", ".join(n for _, n in pr.mentions) or pr.text or None
                for acc, name in pr.mentions:
                    links.statement("owner", account(ctx, acc, name))
            elif role == "note_taker":
                fields["note_taker"] = ", ".join(n for _, n in pr.mentions) or pr.text or None
            elif role == "status":
                status = pr.text or None
            elif role in ("target_metric", "period"):
                fields[role] = pr.text or None
            elif role == "relates_to":
                for u, lbl in pr.urls:
                    links.statement("relates_to", with_label(ctx.ref_from_url(u), lbl or None),
                                    evidence=evidence(f"{pr.label}: {pr.text}", field="properties", property=pr.label))
            else:
                for u, lbl in pr.urls:
                    links.statement("mentions", with_label(ctx.ref_from_url(u), lbl or None))

        # links and people elsewhere in the page are mentions (attendees are participants, see below)
        body_nodes = [n for n in _adf.children(doc) if n is not props_table]
        for u, lbl in _adf.urls(doc):
            if u in prop_urls:
                continue
            links.statement("mentions", with_label(ctx.ref_from_url(u), lbl or None))
        for acc, name in _adf.mentions(doc):
            if acc not in {a for a, _ in attendees}:
                links.statement("mentions", account(ctx, acc, name))

        def sec(name: str) -> list[dict] | None:
            return _adf.section(doc, _SECTIONS[name], norm_label)

        def sec_text(name: str) -> str | None:
            blocks = sec(name)
            return (clean_ws(_adf.render(blocks)) or None) if blocks else None

        decisions = _number(_adf.nodes_of(doc, "decisionItem"), "dec")
        tasks = _number(_adf.nodes_of(doc, "taskItem"), "task")

        if kind == "Meeting":
            for acc, name in attendees:
                links.statement("participant", account(ctx, acc, name))
            fields.update({
                "held_at": iso(held_at),
                "ends_at": iso(ends_at),
                "attendees": [name for _, name in attendees],
                "decisions": [_item_text(n) for n, _ in decisions],
                "agenda": sec_text("agenda"),
                "discussion": sec_text("discussion"),
                "action_items": [_task_label(n) for n, _ in tasks],
            })
        elif kind == "Report":
            blocks = sec("metrics")
            if blocks is None:
                others = [t for t in _adf.tables(doc) if t is not props_table]
                blocks = [others[0]] if others else None
            fields.update({"findings": sec_text("findings") or sec_text("summary"),
                           "metrics": _metrics(blocks) if blocks else None})
        elif kind == "Spec":
            fields.update({"goal": sec_text("goal"), "problem": sec_text("problem"),
                           "requirements": sec_text("requirements")})
        else:
            first = next((clean_ws(_adf.render([n])) for n in body_nodes if n.get("type") == "paragraph"), None)
            fields["summary"] = sec_text("summary") or first or None
        fields["status"] = status or (p.get("status") if p.get("status") not in (None, "current") else None) or "current"
        links.statement("status", literal=fields["status"])

        body = f"# {title}\n\n" + _adf.render(doc)
        primary = CanonicalRecord(
            key=key,
            entity_id=entity_id,
            kind=kind,
            title=title,
            url=url,
            fields=fields,
            body=body.strip(),
            actor=account(ctx, editor_id),
            aliases=aliases((title, "title")),
            statements=links.statements,
            causes=links.causes,
            acl=acl,
        )
        records = [primary]
        meeting_ref = page_ref(env.source_id, page_id)  # no label: a renamed page must not re-version its children
        # derived records must not change when only the page's edit time changes: stable times only
        when = held_at or created_at
        where = (f"Meeting: {title}" + (f" · {held_at:%Y-%m-%d %H:%M}" if held_at else "")) if kind == "Meeting" \
            else f"Page: {title}"
        creator = account(ctx, author_id)

        for node, n in decisions:
            statement = _item_text(node)
            owner = (_adf.mentions(node) or [(None, None)])[0]
            state = str(_adf.attrs(node).get("state") or "DECIDED").lower()
            dl = Links(Ref(source_id=env.source_id, object_type="decision", external_id=f"{page_id}#dec-{n}"))
            dl.statement("owner", account(ctx, owner[0], owner[1]))
            dl.statement("status", literal=state)
            for u, lbl in _adf.urls(node):
                dl.statement("mentions", with_label(ctx.ref_from_url(u), lbl or None))
            dl.cause("decided_in", meeting_ref, evidence=evidence(statement, field="decisions",
                                                                  local_id=_adf.attrs(node).get("localId")))
            records.append(CanonicalRecord(
                key=Ref(source_id=env.source_id, object_type="decision", external_id=f"{page_id}#dec-{n}"),
                entity_id=f"{entity_id}.dec-{n}",
                kind="Decision",
                title=truncate(statement or f"{title} · dec-{n}"),
                url=f"{url}#dec-{n}" if url else None,
                fields={"statement": statement, "state": state, "owner": owner[1], "decided_at": iso(when)},
                body=clean_ws(f"# {statement}\n\nDecision · {state.upper()}"
                              + (f" · owner: {owner[1]}" if owner[1] else "")
                              + "\n" + where),
                valid_from=held_at,
                actor=creator,
                aliases=aliases((truncate(statement, 200), "title")),
                statements=dl.statements,
                causes=dl.causes,
                acl=acl,
            ))

        for node, n in tasks:
            text = _item_text(node)
            who = (_adf.mentions(node) or [(None, None)])[0]
            state = str(_adf.attrs(node).get("state") or "TODO").lower()
            due = next((_adf.inline_text(x) for x in _adf.walk(node) if x.get("type") == "date"), None)
            tl = Links(Ref(source_id=env.source_id, object_type="task", external_id=f"{page_id}#task-{n}"))
            tl.statement("assignee", account(ctx, who[0], who[1]))
            tl.statement("status", literal=state)
            for u, lbl in _adf.urls(node):
                tl.statement("mentions", with_label(ctx.ref_from_url(u), lbl or None))
            if kind == "Meeting":
                # assigned at the meeting: only a moved meeting invalidates it, not edits of the decision list
                tl.cause("decided_in", meeting_ref, sensitivity=["held_at"],
                         evidence=evidence(text, field="action_items", local_id=_adf.attrs(node).get("localId")))
            label = f"{who[1]}: {text}" if who[1] else text
            records.append(CanonicalRecord(
                key=Ref(source_id=env.source_id, object_type="task", external_id=f"{page_id}#task-{n}"),
                entity_id=f"{entity_id}.task-{n}",
                kind="ActionItem",
                title=truncate(label or f"{title} · task-{n}"),
                url=f"{url}#task-{n}" if url else None,
                fields={"text": text, "state": state, "assignee": who[1], "due": due},
                body=clean_ws(f"# {label}\n\nAction item · {state.upper()}"
                              + (f" · due: {due}" if due else "")
                              + "\n" + where),
                actor=creator,
                aliases=aliases((truncate(label, 200), "title")),
                statements=tl.statements,
                causes=tl.causes,
                acl=acl,
            ))
        return records

    def ref_from_url(self, url: str, source: SourceConfig) -> Ref | None:
        base = (source.base_url or "").rstrip("/")
        if not base or not url.startswith(base):
            return None
        path = url[len(base):]
        if path and path[0] not in "/?#":
            return None
        m = _PAGE.match(path) or _VIEW.match(path)
        if not m:
            return None
        page_id = m.group("id")
        frag = _FRAG.match(m.group("frag") or "")
        if frag:
            object_type = "decision" if frag.group("kind") == "dec" else "task"
            return Ref(source_id=source.id, object_type=object_type, external_id=f"{page_id}#{frag.group('kind')}-{frag.group('n')}")
        return page_ref(source.id, page_id)


def to_version(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


register(ConfluenceAdapter())
