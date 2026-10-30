"""Atlassian Document Format (ADF) helpers: render to markdown-ish text and extract structured nodes.

Used by the Confluence adapter (page bodies) and the Jira adapter (v3 descriptions and comments). Unknown node
types are rendered through their children, so new ADF features degrade to text instead of failing.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterator

_BLOCK_CONTAINERS = {
    "doc", "panel", "expand", "nestedExpand", "bodiedExtension", "layoutSection", "layoutColumn", "extensionFrame",
    "multiBodiedExtension",
}


def load(value: Any) -> dict | None:
    """ADF document from a JSON string or a dict; None for anything else."""
    if isinstance(value, dict):
        return value if value.get("type") == "doc" else None
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            doc = json.loads(value)
        except ValueError:
            return None
        return doc if isinstance(doc, dict) and doc.get("type") == "doc" else None
    return None


def children(node: Any) -> list[dict]:
    if not isinstance(node, dict):
        return []
    c = node.get("content")
    return [x for x in c if isinstance(x, dict)] if isinstance(c, list) else []


def walk(node: Any) -> Iterator[dict]:
    """Depth-first pre-order traversal."""
    if isinstance(node, list):
        for n in node:
            yield from walk(n)
        return
    if not isinstance(node, dict):
        return
    yield node
    for c in children(node):
        yield from walk(c)


def attrs(node: dict) -> dict:
    a = node.get("attrs")
    return a if isinstance(a, dict) else {}


def _date_text(ts: Any) -> str:
    try:
        v = int(str(ts))
        return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return str(ts or "")


def card_url(node: dict) -> str | None:
    a = attrs(node)
    url = a.get("url")
    if not url and isinstance(a.get("data"), dict):
        url = a["data"].get("url")
    return str(url) if url else None


def mention_name(node: dict) -> str:
    a = attrs(node)
    return str(a.get("text") or a.get("displayName") or a.get("id") or "").lstrip("@").strip()


def inline_text(node: Any, *, mentions: bool = True, links: bool = False) -> str:
    """Plain text of inline content (and of nested blocks, joined by newlines)."""
    if isinstance(node, list):
        return "".join(inline_text(n, mentions=mentions, links=links) for n in node)
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        text = str(node.get("text") or "")
        if links:
            for m in node.get("marks") or []:
                if isinstance(m, dict) and m.get("type") == "link":
                    href = attrs(m).get("href")
                    if href and href != text:
                        return f"[{text}]({href})"
        return text
    if t == "mention":
        return ("@" + mention_name(node)) if mentions else ""
    if t in ("inlineCard", "blockCard", "embedCard"):
        return card_url(node) or ""
    if t == "date":
        return _date_text(attrs(node).get("timestamp"))
    if t == "status":
        return str(attrs(node).get("text") or "")
    if t == "emoji":
        a = attrs(node)
        return str(a.get("text") or a.get("shortName") or "")
    if t == "hardBreak":
        return "\n"
    if t in ("paragraph", "heading"):
        return inline_text(children(node), mentions=mentions, links=links)
    parts = [inline_text(c, mentions=mentions, links=links) for c in children(node)]
    block = any(c.get("type") not in _INLINE for c in children(node))
    return ("\n" if block else "").join(p for p in parts if p or not block)


_INLINE = {"text", "mention", "inlineCard", "date", "status", "emoji", "hardBreak", "placeholder", "mediaInline"}


def urls(node: Any) -> list[tuple[str, str]]:
    """(url, label) of inline/block cards and link marks, in document order."""
    out: list[tuple[str, str]] = []
    for n in walk(node):
        t = n.get("type")
        if t in ("inlineCard", "blockCard", "embedCard"):
            u = card_url(n)
            if u:
                out.append((u, ""))
        elif t == "text":
            for m in n.get("marks") or []:
                if isinstance(m, dict) and m.get("type") == "link" and attrs(m).get("href"):
                    out.append((str(attrs(m)["href"]), str(n.get("text") or "")))
    return out


def mentions(node: Any) -> list[tuple[str, str]]:
    """(account id, display name) of mention nodes, in document order."""
    return [(str(attrs(n).get("id")), mention_name(n)) for n in walk(node)
            if n.get("type") == "mention" and attrs(n).get("id")]


# ------------------------------------------------------------------------------------------------ rendering


def render(doc: Any) -> str:
    """Markdown-ish rendering for search and reading."""
    lines: list[str] = []
    _render_blocks(children(doc) if isinstance(doc, dict) and doc.get("type") == "doc" else as_nodes(doc), lines, "")
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def as_nodes(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _render_blocks(nodes: list[dict], out: list[str], indent: str) -> None:
    for n in nodes:
        _render_block(n, out, indent)


def _render_block(n: dict, out: list[str], indent: str) -> None:
    t = n.get("type")
    if t == "paragraph":
        txt = inline_text(n, links=True)
        if txt.strip():
            out.append(indent + txt.replace("\n", "\n" + indent))
            if not indent:
                out.append("")
    elif t == "heading":
        level = max(1, min(6, int(attrs(n).get("level") or 2)))
        out.append("")
        out.append("#" * level + " " + inline_text(n).strip())
        out.append("")
    elif t in ("bulletList", "orderedList"):
        for i, item in enumerate(children(n), 1):
            marker = f"{i}. " if t == "orderedList" else "- "
            sub: list[str] = []
            _render_blocks(children(item), sub, "")
            sub = [s for s in sub if s.strip()]
            if sub:
                out.append(indent + marker + sub[0])
                out.extend(indent + "  " + s for s in sub[1:])
        if not indent:
            out.append("")
    elif t == "decisionList":
        for item in children(n):
            state = str(attrs(item).get("state") or "DECIDED").upper()
            out.append(indent + f"- [{state}] " + inline_text(children(item), links=True).strip())
        if not indent:
            out.append("")
    elif t == "taskList":
        for item in children(n):
            if item.get("type") == "taskList":
                _render_block(item, out, indent + "  ")
                continue
            done = str(attrs(item).get("state") or "").upper() == "DONE"
            out.append(indent + ("- [x] " if done else "- [ ] ") + inline_text(children(item), links=True).strip())
        if not indent:
            out.append("")
    elif t == "table":
        rows = children(n)
        for ri, row in enumerate(rows):
            cells = [" ".join(inline_text(c, links=True).split()) for c in children(row)]
            out.append("| " + " | ".join(cells) + " |")
            if ri == 0 and all(c.get("type") == "tableHeader" for c in children(row)) and len(rows) > 1:
                out.append("|" + "---|" * len(cells))
        out.append("")
    elif t == "codeBlock":
        out.append("```")
        out.append(inline_text(n))
        out.append("```")
        out.append("")
    elif t == "blockquote":
        sub: list[str] = []
        _render_blocks(children(n), sub, "")
        out.extend("> " + s if s else ">" for s in sub)
        out.append("")
    elif t == "rule":
        out.append("---")
        out.append("")
    elif t in ("mediaSingle", "mediaGroup", "media", "extension", "inlineExtension"):
        return
    elif t in ("blockCard", "embedCard"):
        u = card_url(n)
        if u:
            out.append(indent + u)
            out.append("")
    elif t in _BLOCK_CONTAINERS or children(n):
        title = attrs(n).get("title")
        if t in ("expand", "nestedExpand") and title:
            out.append(f"**{title}**")
        _render_blocks(children(n), out, indent)
    else:
        txt = inline_text(n)
        if txt.strip():
            out.append(indent + txt)


# ------------------------------------------------------------------------------------------------ structure


def top_blocks(doc: Any) -> list[dict]:
    """Top-level blocks; layout/panel wrappers are flattened so sections survive page layouts."""
    out: list[dict] = []

    def flat(nodes: list[dict]) -> None:
        for n in nodes:
            if n.get("type") in ("layoutSection", "layoutColumn"):
                flat(children(n))
            else:
                out.append(n)

    flat(children(doc) if isinstance(doc, dict) else [])
    return out


def sections(doc: Any) -> list[tuple[str | None, list[dict]]]:
    """Split top-level blocks by headings: [(heading text or None, blocks)]."""
    out: list[tuple[str | None, list[dict]]] = [(None, [])]
    for b in top_blocks(doc):
        if b.get("type") == "heading":
            out.append((inline_text(b).strip(), []))
        else:
            out[-1][1].append(b)
    return [(h, bl) for h, bl in out if h is not None or bl]


def section(doc: Any, names: set[str], norm: Any) -> list[dict] | None:
    for heading, blocks in sections(doc):
        if heading is not None and norm(heading) in names:
            return blocks
    return None


def tables(node: Any) -> list[dict]:
    return [n for n in walk(node) if n.get("type") == "table"]


def table_rows(table: dict) -> list[list[dict]]:
    return [children(r) for r in children(table) if r.get("type") == "tableRow"]


def properties_table(doc: Any) -> dict | None:
    """The page-properties table: inside a 'details' macro if there is one, else the first 2-column table."""
    for n in walk(doc):
        if n.get("type") == "bodiedExtension" and attrs(n).get("extensionKey") in ("details", "page-properties"):
            ts = tables(n)
            if ts:
                return ts[0]
    for t in tables(doc):
        rows = table_rows(t)
        if rows and all(len(r) == 2 for r in rows):
            return t
    return None


def nodes_of(doc: Any, node_type: str) -> list[dict]:
    """All nodes of a type in document order, e.g. every decisionItem (nested lists included)."""
    return [n for n in walk(doc) if n.get("type") == node_type]
