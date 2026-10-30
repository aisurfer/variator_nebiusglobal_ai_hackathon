"""Jira Cloud adapter: REST issue JSON (fields, names, changelog) + remote links -> one CanonicalRecord per issue.

Payload shape (simplified GET /rest/api/2/issue/{key}?expand=names,changelog plus GET .../remotelink):
  key, fields.{summary, description (wiki text or ADF), issuetype, status, assignee, reporter, creator, project,
  created, updated, parent, labels, issuelinks, comment.comments, customfield_*}, names {customfield_id: name},
  changelog.histories, remotelinks [{id, relationship, object: {url, title}}].

Declared links:
  parent / Epic Link                        -> part_of (cause = parent issue)
  issue link "is blocked by" (inward Blocks) -> blocked_by;  "is caused by" -> caused_by;  "depends on" -> depends_on
  issue link Relates / Duplicate / inverse   -> relates_to statement
  remote link relationship                   -> see kb.connectors._util.relationship
"""
from __future__ import annotations

import re
from typing import Any

from kb.connectors import _adf
from kb.connectors._util import (
    Links, account, aliases, clean_ws, container_statement, dig, evidence, iso, items, norm_label, parse_dt, record_acl,
    string, to_int, with_label,
)
from kb.connectors.base import AdapterContext, SourceConfig, register
from kb.contracts import CanonicalRecord, Ref, SourceEnvelope

KIND_BY_TYPE = {
    "epic": "Epic", "story": "Story", "user story": "Story", "task": "Task", "sub-task": "Task", "subtask": "Task",
    "bug": "Bug", "defect": "Bug", "incident": "Bug", "experiment": "Experiment",
    "spike": "Task", "improvement": "Story",
}

# Custom field display names (payload "names", expand=names) -> canonical field names.
_CUSTOM: dict[str, str] = {}
for _canon, _names in {
    "acceptance_criteria": ["Acceptance criteria", "Acceptance Criteria", "AC"],
    "hypothesis": ["Hypothesis"],
    "traffic_split": ["Traffic split", "Traffic allocation"],
    "start_date": ["Start date", "Experiment start"],
    "duration_days": ["Duration (days)", "Duration"],
    "primary_metric": ["Primary metric"],
    "result": ["Result", "Experiment result"],
    "epic_link": ["Epic Link"],
}.items():
    for _n in _names:
        _CUSTOM[norm_label(_n)] = _canon

# Used when a payload carries no "names" map (ids of the Vector Forge Jira site).
DEFAULT_CUSTOM_IDS = {
    "customfield_10014": "epic_link",
    "customfield_10050": "acceptance_criteria",
    "customfield_10201": "hypothesis",
    "customfield_10202": "traffic_split",
    "customfield_10203": "start_date",
    "customfield_10204": "duration_days",
    "customfield_10205": "primary_metric",
    "customfield_10206": "result",
}

EXPERIMENT_FIELDS = ("hypothesis", "traffic_split", "start_date", "duration_days", "primary_metric", "result")
_SECTION_FIELDS = {"acceptance_criteria", "hypothesis", "result"}
_FIELD_TITLES = {
    "hypothesis": "Hypothesis", "traffic_split": "Traffic split", "start_date": "Start date",
    "duration_days": "Duration (days)", "primary_metric": "Primary metric", "result": "Result",
}

_BROWSE = re.compile(r"^/browse/(?P<key>[A-Z][A-Z0-9_]*-\d+)(?:[/?#].*)?$")
_SELECTED = re.compile(r"[?&]selectedIssue=(?P<key>[A-Z][A-Z0-9_]*-\d+)")
_URL = re.compile(r"https?://[^\s\]\)\|>\"'<]+")


def issue_ref(source_id: str, key: str, label: Any = None) -> Ref:
    return Ref(source_id=source_id, object_type="issue", external_id=key, label=str(label) if label else key)


def _value_text(value: Any) -> str | None:
    """Custom field values come as strings, numbers, ADF, option objects or lists of those."""
    if value is None:
        return None
    if isinstance(value, dict):
        if value.get("type") == "doc":
            return clean_ws(_adf.render(value)) or None
        for k in ("value", "name", "displayName", "key"):
            if value.get(k) is not None:
                return str(value[k])
        return None
    if isinstance(value, list):
        parts = [t for t in (_value_text(v) for v in value) if t]
        return ", ".join(parts) if parts else None
    s = str(value).strip()
    return s or None


def wiki_to_markdown(text: str) -> str:
    """Jira wiki markup (API v2 text fields) -> markdown-ish text."""
    out: list[str] = []
    in_code = False
    for line in str(text).replace("\r\n", "\n").split("\n"):
        s = line.rstrip()
        if re.match(r"^\{(code|noformat)(:[^}]*)?\}\s*$", s.strip()):
            in_code = not in_code
            out.append("```")
            continue
        if in_code:
            out.append(s)
            continue
        m = re.match(r"^h([1-6])\.\s+(.*)$", s)
        if m:
            out.append("#" * int(m.group(1)) + " " + m.group(2).strip())
            continue
        m = re.match(r"^([*#-]+)\s+(.*)$", s)
        if m and (set(m.group(1)) <= {"*"} or set(m.group(1)) <= {"#"} or m.group(1) == "-"):
            depth = len(m.group(1)) - 1
            marker = "1. " if m.group(1)[0] == "#" else "- "
            s = "  " * depth + marker + m.group(2)
        s = re.sub(r"\[([^|\]]+)\|([^\]]+)\]", r"[\1](\2)", s)
        out.append(s)
    return clean_ws("\n".join(out))


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return clean_ws(_adf.render(value))
    if value is None:
        return ""
    return wiki_to_markdown(str(value))


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$|^(?P<plain>[^\n#*\-][^\n]{0,60}?):\s*$")


def split_sections(md: str) -> tuple[str, dict[str, str]]:
    """Split a markdown description into (text outside known sections, {canonical field: section text})."""
    lines = md.split("\n")
    keep: list[str] = []
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        m = _HEADING.match(line.strip())
        if m:
            title = m.group(2) if m.group(2) else m.group("plain")
            canon = _CUSTOM.get(norm_label(title))
            if canon in _SECTION_FIELDS:
                current = canon
                found.setdefault(canon, [])
                continue
            if m.group(1):  # another heading ends a captured section
                current = None
        if current:
            found[current].append(line)
        else:
            keep.append(line)
    return clean_ws("\n".join(keep)), {k: clean_ws("\n".join(v)) for k, v in found.items() if clean_ws("\n".join(v))}


def _person(obj: Any) -> tuple[str | None, str | None]:
    if not isinstance(obj, dict):
        return None, None
    return (str(obj["accountId"]) if obj.get("accountId") else None), obj.get("displayName")


def _fmt_time(value: Any) -> str:
    dt = parse_dt(value)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else str(value or "")


class JiraAdapter:
    provider = "jira"

    def normalize(self, env: SourceEnvelope, ctx: AdapterContext) -> list[CanonicalRecord]:
        key_s = str(env.external_id)
        key = Ref(source_id=env.source_id, object_type="issue", external_id=key_s)
        entity_id = f"jira.{key_s}"
        p = env.payload if isinstance(env.payload, dict) else {}
        f = p.get("fields") if isinstance(p.get("fields"), dict) else {}
        if env.operation == "delete" or env.payload is None:
            kind = KIND_BY_TYPE.get(norm_label(dig(f, "issuetype", "name")), "Task") if f else "Task"
            return [CanonicalRecord(key=key, entity_id=entity_id, kind=kind, title=string(f.get("summary")) or key_s,
                                    deleted=True)]

        project = env.container or dig(f, "project", "key") or key_s.split("-")[0]
        project_name = dig(f, "project", "name") or (ctx.source.containers[project].name if project in ctx.source.containers else None)
        issue_type = dig(f, "issuetype", "name") or "Task"
        kind = KIND_BY_TYPE.get(norm_label(issue_type), "Task")
        summary = " ".join(str(f.get("summary") or key_s).split())
        url = f"{(ctx.source.base_url or '').rstrip('/')}/browse/{key_s}" if ctx.source.base_url else None

        # ---- custom fields
        names = p.get("names") if isinstance(p.get("names"), dict) else {}
        custom: dict[str, Any] = {}
        for fid, val in f.items():
            if not str(fid).startswith("customfield_"):
                continue
            canon = _CUSTOM.get(norm_label(names.get(fid))) if names.get(fid) else DEFAULT_CUSTOM_IDS.get(fid)
            if canon and val is not None and canon not in custom:
                custom[canon] = val

        description_md = _text(f.get("description"))
        description, desc_sections = split_sections(description_md) if description_md else ("", {})
        acceptance = _value_text(custom.get("acceptance_criteria"))
        acceptance = wiki_to_markdown(acceptance) if acceptance else desc_sections.get("acceptance_criteria")

        assignee_id, assignee_name = _person(f.get("assignee"))
        reporter_id, reporter_name = _person(f.get("reporter"))
        creator_id, creator_name = _person(f.get("creator"))
        labels = sorted({str(x) for x in (f.get("labels") if isinstance(f.get("labels"), list) else []) if x})
        status = dig(f, "status", "name")
        parent_key = dig(f, "parent", "key") if isinstance(f.get("parent"), dict) else None
        parent_key = parent_key or _value_text(custom.get("epic_link"))
        parent_summary = dig(f, "parent", "fields", "summary")

        comments = []
        for c in items(dig(f, "comment", "comments")):
            if not isinstance(c, dict):
                continue
            cid, cname = _person(c.get("author"))
            comments.append({"id": str(c.get("id") or ""), "author": cname, "author_id": cid,
                             "created": iso(parse_dt(c.get("created"))), "body": _text(c.get("body"))})

        links = Links(key)
        links.add(container_statement("in_project", env.source_id, project, project_name))
        links.statement("reporter", account(ctx, reporter_id, reporter_name))
        links.statement("assignee", account(ctx, assignee_id, assignee_name))
        links.statement("author", account(ctx, creator_id, creator_name))
        links.statement("status", literal=status)
        for lab in labels:
            links.statement("labels", literal=lab)

        if parent_key:
            links.cause("part_of", issue_ref(env.source_id, str(parent_key), parent_summary),
                        evidence=evidence(f"parent: {parent_key}", field="parent"))

        issue_links = []
        link_lines = []
        for il in items(f.get("issuelinks")):
            if not isinstance(il, dict):
                continue
            t = il.get("type") if isinstance(il.get("type"), dict) else {}
            if isinstance(il.get("inwardIssue"), dict):
                other, phrase, direction = il["inwardIssue"], t.get("inward") or t.get("name"), "inward"
            elif isinstance(il.get("outwardIssue"), dict):
                other, phrase, direction = il["outwardIssue"], t.get("outward") or t.get("name"), "outward"
            else:
                continue
            okey = other.get("key")
            if not okey or not isinstance(okey, str):
                continue
            osum = dig(other, "fields", "summary")
            issue_links.append({"type": t.get("name"), "direction": direction, "phrase": phrase, "key": okey})
            link_lines.append(f"- {phrase}: {okey}" + (f" {osum}" if osum else ""))
            links.link(phrase, issue_ref(env.source_id, okey, osum),
                       evidence=evidence(f"{phrase} {okey}", field="issuelinks", link_type=t.get("name")),
                       qualifiers={"link_type": t.get("name")} if t.get("name") else None)

        remote = []
        for rl in items(p.get("remotelinks")):
            if not isinstance(rl, dict):
                continue
            obj = rl.get("object") if isinstance(rl.get("object"), dict) else {}
            rurl = str(obj["url"]) if obj.get("url") else None
            rtitle = str(obj["title"]) if obj.get("title") else None
            rel = rl.get("relationship") or "relates to"
            remote.append({"id": str(rl.get("id") or ""), "relationship": rel, "url": rurl, "title": rtitle})
            link_lines.append(f"- {rel}: {rtitle or rurl}" + (f" ({rurl})" if rurl and rtitle else ""))
            ref = with_label(ctx.ref_from_url(rurl), rtitle)
            links.link(rel, ref, evidence=evidence(f"{rel}: {rtitle or rurl}", field="remotelinks",
                                                   id=str(rl.get("id")) if rl.get("id") is not None else None, url=rurl))

        # people mentioned in ADF text and tenant URLs in free text are plain mentions
        for acc, name in _adf.mentions(f.get("description")) if isinstance(f.get("description"), dict) else []:
            links.statement("mentions", account(ctx, acc, name))
        for text in [description_md] + [c["body"] for c in comments]:
            for u in _URL.findall(text or ""):
                links.statement("mentions", ctx.ref_from_url(u.rstrip(".,;")))

        fields: dict[str, Any] = {
            "key": key_s,
            "summary": summary,
            "description": description or None,
            "acceptance_criteria": acceptance or None,
            "status": status,
            "issue_type": issue_type,
            "priority": dig(f, "priority", "name"),
            "assignee": assignee_name,
            "reporter": reporter_name,
            "labels": labels,
            "parent": parent_key,
            "resolution": dig(f, "resolution", "name"),
            "due_date": f.get("duedate"),
            "created": iso(parse_dt(f.get("created"))),
            "comments": comments,
            "issue_links": issue_links,
            "remote_links": remote,
        }
        if kind == "Experiment":
            for name in EXPERIMENT_FIELDS:
                raw = custom.get(name)
                if raw is None and name in desc_sections:
                    raw = desc_sections[name]
                if name == "duration_days":
                    fields[name] = to_int(_value_text(raw)) if raw is not None else None
                elif name == "start_date":
                    d = parse_dt(_value_text(raw)) if raw is not None else None
                    fields[name] = d.date().isoformat() if d else _value_text(raw)
                else:
                    fields[name] = _value_text(raw)

        # ---- readable body
        head = [f"# {key_s} {summary}", "",
                " · ".join(x for x in [issue_type, f"Status: {status}" if status else None,
                                       f"Project: {project}" + (f" ({project_name})" if project_name else "")] if x)]
        people = " · ".join(x for x in [f"Reporter: {reporter_name}" if reporter_name else None,
                                         f"Assignee: {assignee_name}" if assignee_name else None] if x)
        if people:
            head.append(people)
        if parent_key:
            head.append(f"Parent: {parent_key}" + (f" {parent_summary}" if parent_summary else ""))
        if labels:
            head.append("Labels: " + ", ".join(labels))
        parts = ["\n".join(head)]
        if description:
            parts.append("## Description\n\n" + description)
        if acceptance:
            parts.append("## Acceptance criteria\n\n" + acceptance)
        if kind == "Experiment":
            design = [f"- {_FIELD_TITLES[n]}: {fields[n]}" for n in EXPERIMENT_FIELDS
                      if n not in ("hypothesis", "result") and fields.get(n) is not None]
            if fields.get("hypothesis"):
                parts.append("## Hypothesis\n\n" + str(fields["hypothesis"]))
            if design:
                parts.append("## Experiment design\n\n" + "\n".join(design))
            if fields.get("result"):
                parts.append("## Result\n\n" + str(fields["result"]))
        if link_lines:
            parts.append("## Links\n\n" + "\n".join(link_lines))
        if comments:
            parts.append("## Comments\n\n" + "\n\n".join(
                f"**{c['author'] or 'unknown'}**, {_fmt_time(c['created'])}: {c['body']}" for c in comments))
        body = "\n\n".join(parts)

        return [CanonicalRecord(
            key=key,
            entity_id=entity_id,
            kind=kind,
            title=summary,
            url=url,
            fields=fields,
            body=body,
            actor=self._actor(p, f, comments, ctx),
            aliases=aliases((key_s, "key"), (summary, "title")),
            statements=links.statements,
            causes=links.causes,
            acl=record_acl(env, ctx, project),
        )]

    @staticmethod
    def _actor(p: dict, f: dict, comments: list[dict], ctx: AdapterContext) -> Ref | None:
        """Author of this revision: the latest changelog entry or comment, else the creator/reporter."""
        updated = parse_dt(f.get("updated"))
        events: list[tuple[Any, str, str | None]] = []
        for h in items(dig(p, "changelog", "histories")):
            if isinstance(h, dict):
                aid, name = _person(h.get("author"))
                if aid:
                    events.append((parse_dt(h.get("created")), aid, name))
        for c in comments:
            if c.get("author_id"):
                events.append((parse_dt(c.get("created")), c["author_id"], c.get("author")))
        events = [e for e in events if e[0] is not None and (updated is None or e[0] <= updated)]
        if events:
            _, aid, name = max(events, key=lambda e: e[0])
            return account(ctx, aid, name)
        for who in ("creator", "reporter"):
            aid, name = _person(f.get(who))
            if aid:
                return account(ctx, aid, name)
        return None

    def ref_from_url(self, url: str, source: SourceConfig) -> Ref | None:
        base = (source.base_url or "").rstrip("/")
        if not base or not url.startswith(base):
            return None
        path = url[len(base):]
        if path and path[0] not in "/?#":
            return None
        m = _BROWSE.match(path) or _SELECTED.search(path)
        if not m:
            return None
        return issue_ref(source.id, m.group("key"))


register(JiraAdapter())
