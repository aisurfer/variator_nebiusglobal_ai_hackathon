"""GitLab adapter (API v4 JSON): merge requests, commits, releases and issues of a project.

External ids (container = project path):  merge_request "group/project!42", issue "group/project#77",
release "group/project@v2.4.0", commit "group/project@<full sha>".

Declared links come from text, like GitLab's own issue-closing and the GitLab-Jira integration:
  "Closes FP-105", "Fixes FP-120", "Resolves #77", "Implements FP-130"  -> closes (MR, commit)
  "Relates to FP-101", "Refs FP-101", "See #12"                           -> relates_to statement
  release description "!42" / "group/project!42"                         -> includes (cause = the MR)
  any other Jira key, !iid or #iid                                       -> mentions statement
Jira keys resolve to the Jira source named by the GitLab source's `jira_source` / `jira_keys` (sources.yaml).
"""
from __future__ import annotations

import re
from typing import Any

from kb.connectors._util import (
    Links, account, aliases, clean_ws, container_statement, dig, evidence, iso, items, parse_dt, record_acl, string,
)
from kb.connectors.base import AdapterContext, SourceConfig, register
from kb.contracts import CanonicalRecord, Ref, SourceEnvelope

_REF = r"(?:https?://[^\s,;)\]]+|(?:[\w.\-]+(?:/[\w.\-]+)+)?[#!]\d+|\b[A-Z][A-Z0-9]{1,9}-\d+\b)"
_REF_LIST = rf"{_REF}(?:\s*(?:,|&|\band\b)\s*{_REF})*"
_CLOSING = re.compile(
    rf"\b(?:[Cc]los(?:e[sd]?|ing)|[Ff]ix(?:e[sd]|ing)?|[Rr]esolv(?:e[sd]?|ing)|[Ii]mplement(?:s|ed|ing)?)\b:?"
    rf"[ \t]+(?:(?:the[ \t]+)?issues?[ \t]+)?(?P<refs>{_REF_LIST})")
_RELATING = re.compile(
    rf"(?:\b[Rr]elate[sd]?[ \t]+to|\b[Rr]elated[ \t]+to|\b[Rr]efs?\b|\b[Ss]ee(?:[ \t]+also)?)"
    rf":?[ \t]+(?P<refs>{_REF_LIST})")
_ONE_REF = re.compile(_REF)
_PATH = re.compile(r"^/(?P<project>[^?#]+?)/-/(?P<what>merge_requests|issues|releases|tags|commit|commits)/(?P<id>[^/?#]+)")
_EXT = re.compile(r"^(?P<project>.+?)(?P<sep>[!#@])(?P<id>[^!#@]+)$")


def slug(project: str) -> str:
    return project.strip("/").replace("/", "-")


def mr_ref(source_id: str, project: str, iid: Any) -> Ref:
    return Ref(source_id=source_id, object_type="merge_request", external_id=f"{project}!{iid}", label=f"!{iid}")


def issue_ref(source_id: str, project: str, iid: Any) -> Ref:
    return Ref(source_id=source_id, object_type="issue", external_id=f"{project}#{iid}", label=f"#{iid}")


def release_ref(source_id: str, project: str, tag: str) -> Ref:
    return Ref(source_id=source_id, object_type="release", external_id=f"{project}@{tag}", label=tag)


def _user(obj: Any) -> tuple[str | None, str | None]:
    if not isinstance(obj, dict):
        return None, None
    return (str(obj["username"]) if obj.get("username") else None), obj.get("name")


class _Text:
    """Resolve textual references of one GitLab object."""

    def __init__(self, ctx: AdapterContext, project: str):
        self.ctx = ctx
        self.project = project
        raw = ctx.source.raw or {}
        self.jira_source = raw.get("jira_source")
        self.jira_keys = {str(k) for k in items(raw.get("jira_keys"))}

    def resolve(self, token: str) -> Ref | None:
        token = token.strip().rstrip(".,;:")
        if token.startswith(("http://", "https://")):
            return self.ctx.ref_from_url(token)
        m = re.fullmatch(r"(?P<path>[\w.\-]+(?:/[\w.\-]+)+)?(?P<sep>[#!])(?P<iid>\d+)", token)
        if m:
            project = m.group("path") or self.project
            maker = issue_ref if m.group("sep") == "#" else mr_ref
            return maker(self.ctx.source.id, project, m.group("iid"))
        m = re.fullmatch(r"(?P<key>[A-Z][A-Z0-9]{1,9})-(?P<n>\d+)", token)
        if m and self.jira_source and (not self.jira_keys or m.group("key") in self.jira_keys):
            return Ref(source_id=self.jira_source, object_type="issue", external_id=token, label=token)
        return None

    def scan(self, text: str | None, links: Links, field: str, *, closing: bool = True) -> None:
        """closes / relates_to / mentions from free text, each reference used once (closing wins)."""
        if not text:
            return
        text = str(text)
        consumed: list[tuple[int, int]] = []
        if closing:
            for m in _CLOSING.finditer(text):
                for r in _ONE_REF.finditer(m.group("refs")):
                    ref = self.resolve(r.group(0))
                    links.cause("closes", ref, evidence=evidence(m.group(0), field=field))
                consumed.append(m.span())
        for m in _RELATING.finditer(text):
            if any(a <= m.start() < b for a, b in consumed):
                continue
            for r in _ONE_REF.finditer(m.group("refs")):
                links.statement("relates_to", self.resolve(r.group(0)), evidence=evidence(m.group(0), field=field))
            consumed.append(m.span())
        for r in _ONE_REF.finditer(text):
            if any(a <= r.start() < b for a, b in consumed):
                continue
            links.statement("mentions", self.resolve(r.group(0)))


class GitLabAdapter:
    provider = "gitlab"

    def normalize(self, env: SourceEnvelope, ctx: AdapterContext) -> list[CanonicalRecord]:
        ext = str(env.external_id)
        m = _EXT.match(ext)
        project = env.container or (m.group("project") if m else "")
        local = m.group("id") if m else ext
        p = env.payload if isinstance(env.payload, dict) else {}
        handler = {
            "merge_request": self._merge_request, "commit": self._commit, "release": self._release,
            "issue": self._issue,
        }.get(env.object_type)
        key = Ref(source_id=env.source_id, object_type=env.object_type, external_id=ext)
        if env.operation == "delete" or env.payload is None or handler is None:
            kind, eid, title = self._identity(env.object_type, project, local, p)
            return [CanonicalRecord(key=key, entity_id=eid, kind=kind, title=title, deleted=True)]
        return [handler(env, ctx, key, project, local, p)]

    @staticmethod
    def _identity(object_type: str, project: str, local: str, p: dict) -> tuple[str, str, str]:
        s = slug(project)
        if object_type == "merge_request":
            return "MergeRequest", f"gitlab.{s}.mr-{local}", string(p.get("title")) or f"!{local}"
        if object_type == "issue":
            return "Issue", f"gitlab.{s}.issue-{local}", string(p.get("title")) or f"#{local}"
        if object_type == "release":
            return "Release", f"gitlab.{s}.release-{local}", string(p.get("name")) or local
        if object_type == "commit":
            return "Commit", f"gitlab.{s}.commit-{local[:8]}", string(p.get("title")) or local[:8]
        return "Document", f"gitlab.{s}.{object_type}-{local}", local

    @staticmethod
    def _base(env: SourceEnvelope, ctx: AdapterContext, key: Ref, project: str) -> Links:
        links = Links(key)
        name = ctx.source.containers[project].name if project in ctx.source.containers else None
        links.add(container_statement("in_repository", env.source_id, project, name))
        return links

    def _merge_request(self, env, ctx, key, project, iid, p) -> CanonicalRecord:
        kind, eid, _ = self._identity("merge_request", project, iid, p)
        title = str(p.get("title") or f"!{iid}")
        description = clean_ws(p.get("description"))
        state = p.get("state")
        author, author_name = _user(p.get("author"))
        links = self._base(env, ctx, key, project)
        links.statement("author", account(ctx, author, author_name))
        for a in items(p.get("assignees")) or ([p["assignee"]] if isinstance(p.get("assignee"), dict) else []):
            u, n = _user(a)
            links.statement("assignee", account(ctx, u, n))
        for r in items(p.get("reviewers")):
            u, n = _user(r)
            links.statement("participant", account(ctx, u, n), qualifiers={"role": "reviewer"})
        links.statement("status", literal=state)
        labels = sorted({str(x) for x in items(p.get("labels")) if x})
        for lab in labels:
            links.statement("labels", literal=lab)
        text = _Text(ctx, project)
        text.scan(description, links, "description")
        text.scan(title, links, "title")
        text.scan(p.get("source_branch"), links, "source_branch", closing=False)
        merged_by, merged_by_name = _user(p.get("merged_by"))
        merged_at = parse_dt(p.get("merged_at"))
        fields = {
            "iid": int(iid) if str(iid).isdigit() else iid,
            "reference": f"!{iid}",
            "title": title,
            "description": description or None,
            "state": state,
            "merged_at": iso(merged_at),
            "merge_commit": p.get("merge_commit_sha") or p.get("squash_commit_sha"),
            "source_branch": p.get("source_branch"),
            "target_branch": p.get("target_branch"),
            "author": author,
            "merged_by": merged_by,
            "labels": labels,
            "created": iso(parse_dt(p.get("created_at"))),
        }
        head = [f"# !{iid} {title}", "",
                f"Merge request in {project} · state: {state}" + (f" · merged {merged_at:%Y-%m-%d %H:%M}" if merged_at else ""),
                f"Author: {author_name or author}" + (f" · merged by {merged_by_name or merged_by}" if merged_by else ""),
                f"Branch: {p.get('source_branch')} → {p.get('target_branch')}"]
        if labels:
            head.append("Labels: " + ", ".join(labels))
        body = "\n".join(head) + ("\n\n## Description\n\n" + description if description else "")
        # review discussion (GitLab notes): searchable text, commenters as participants, links in the comments
        notes = [n for n in items(p.get("notes")) if isinstance(n, dict) and not n.get("system") and n.get("body")]
        if notes:
            lines = []
            comments = []
            for n in notes:
                u, nm = _user(n.get("author"))
                when = parse_dt(n.get("created_at"))
                note_body = clean_ws(n.get("body"))
                comments.append({"author": u, "body": note_body, "created": iso(when)})
                lines.append(f"**{nm or u}**" + (f" ({when:%Y-%m-%d %H:%M})" if when else "") + f": {note_body}")
                links.statement("participant", account(ctx, u, nm), qualifiers={"role": "commenter"})
                text.scan(note_body, links, "comments", closing=False)
            fields["comments"] = comments
            body += "\n\n## Discussion\n\n" + "\n\n".join(lines)
        actor = merged_by if state == "merged" and merged_by else author
        return CanonicalRecord(
            key=key, entity_id=eid, kind=kind, title=title, url=string(p.get("web_url")) or self._url(ctx.source, project, "merge_requests", iid),
            fields=fields, body=body, actor=account(ctx, actor),
            aliases=aliases((f"!{iid}", "key"), (f"{project}!{iid}", "key"), (title, "title")),
            statements=links.statements, causes=links.causes, acl=record_acl(env, ctx, project),
        )

    def _commit(self, env, ctx, key, project, sha, p) -> CanonicalRecord:
        full = string(p.get("id")) or sha
        kind, eid, _ = self._identity("commit", project, sha, p)
        short = sha[:8]
        message = clean_ws(p.get("message") or p.get("title"))
        title = str(p.get("title") or (message.split("\n", 1)[0] if message else short))
        username = p.get("author_username") or dig(p, "author", "username")  # connector enrichment (push event)
        links = self._base(env, ctx, key, project)
        links.statement("author", account(ctx, username, p.get("author_name")))
        _Text(ctx, project).scan(message, links, "message")
        authored = parse_dt(p.get("authored_date") or p.get("created_at"))
        fields = {
            "sha": full, "short_sha": short, "title": title, "message": message or None,
            "author": username or p.get("author_name"), "author_email": p.get("author_email"),
            "authored_at": iso(authored), "parents": [str(x) for x in items(p.get("parent_ids"))],
        }
        body = (f"# {short} {title}\n\nCommit in {project} · {p.get('author_name') or username or ''}"
                + (f" · {authored:%Y-%m-%d %H:%M}" if authored else "") + ("\n\n" + message if message else ""))
        return CanonicalRecord(
            key=key, entity_id=eid, kind=kind, title=title,
            url=string(p.get("web_url")) or self._url(ctx.source, project, "commit", full),
            fields=fields, body=body, actor=account(ctx, username, p.get("author_name")),
            aliases=aliases((short, "key"), (full, "key"), (title, "title")),
            statements=links.statements, causes=links.causes, acl=record_acl(env, ctx, project),
        )

    def _release(self, env, ctx, key, project, tag, p) -> CanonicalRecord:
        kind, eid, _ = self._identity("release", project, tag, p)
        tag = string(p.get("tag_name")) or tag
        name = str(p.get("name") or tag)
        description = clean_ws(p.get("description"))
        author, author_name = _user(p.get("author"))
        links = self._base(env, ctx, key, project)
        links.statement("author", account(ctx, author, author_name))
        text = _Text(ctx, project)
        mrs: list[str] = []
        for m in re.finditer(r"(?:(?P<path>[\w.\-]+(?:/[\w.\-]+)+))?!(?P<iid>\d+)\b", description or ""):
            ref = mr_ref(env.source_id, m.group("path") or project, m.group("iid"))
            line = next((ln for ln in description.split("\n") if m.group(0) in ln), m.group(0))
            links.cause("includes", ref, evidence=evidence(line.strip(" -*"), field="description"))
            label = f"!{m.group('iid')}" if not m.group("path") or m.group("path") == project else m.group(0)
            if label not in mrs:
                mrs.append(label)
        text.scan(re.sub(r"(?:[\w.\-]+(?:/[\w.\-]+)+)?!\d+\b", " ", description or ""), links, "description", closing=False)
        released = parse_dt(p.get("released_at") or p.get("created_at"))
        fields = {
            "tag": tag, "name": name, "description": description or None, "released_at": iso(released),
            "merge_requests": mrs, "commit": dig(p, "commit", "id"), "author": author,
            "upcoming": bool(p.get("upcoming_release")) if p.get("upcoming_release") is not None else None,
        }
        title = name if tag in name else f"{tag} {name}"
        body = (f"# Release {tag} — {name}\n\nRelease of {project}" + (f" · {released:%Y-%m-%d %H:%M}" if released else "")
                + f" · by {author_name or author or 'unknown'}" + ("\n\n" + description if description else ""))
        return CanonicalRecord(
            key=key, entity_id=eid, kind=kind, title=title,
            url=string(dig(p, "_links", "self")) or self._url(ctx.source, project, "releases", tag),
            fields=fields, body=body, actor=account(ctx, author, author_name),
            aliases=aliases((tag, "key"), (f"{project}@{tag}", "key"), (name, "title")),
            statements=links.statements, causes=links.causes, acl=record_acl(env, ctx, project),
        )

    def _issue(self, env, ctx, key, project, iid, p) -> CanonicalRecord:
        kind, eid, _ = self._identity("issue", project, iid, p)
        title = str(p.get("title") or f"#{iid}")
        description = clean_ws(p.get("description"))
        state = p.get("state")
        author, author_name = _user(p.get("author"))
        closed_by, _ = _user(p.get("closed_by"))
        links = self._base(env, ctx, key, project)
        links.statement("author", account(ctx, author, author_name))
        for a in items(p.get("assignees")):
            u, n = _user(a)
            links.statement("assignee", account(ctx, u, n))
        links.statement("status", literal=state)
        labels = sorted({str(x) for x in items(p.get("labels")) if x})
        for lab in labels:
            links.statement("labels", literal=lab)
        _Text(ctx, project).scan(description, links, "description", closing=False)
        fields = {
            "iid": int(iid) if str(iid).isdigit() else iid, "reference": f"#{iid}", "title": title,
            "description": description or None, "state": state, "labels": labels, "author": author,
            "closed_at": iso(parse_dt(p.get("closed_at"))), "closed_by": closed_by,
            "created": iso(parse_dt(p.get("created_at"))),
        }
        body = (f"# #{iid} {title}\n\nIssue in {project} · state: {state} · author: {author_name or author}"
                + ("\nLabels: " + ", ".join(labels) if labels else "")
                + ("\n\n## Description\n\n" + description if description else ""))
        return CanonicalRecord(
            key=key, entity_id=eid, kind=kind, title=title,
            url=string(p.get("web_url")) or self._url(ctx.source, project, "issues", iid),
            fields=fields, body=body, actor=account(ctx, closed_by if state == "closed" and closed_by else author),
            aliases=aliases((f"#{iid}", "key"), (f"{project}#{iid}", "key"), (title, "title")),
            statements=links.statements, causes=links.causes, acl=record_acl(env, ctx, project),
        )

    @staticmethod
    def _url(source: SourceConfig, project: str, what: str, ident: Any) -> str | None:
        if not source.base_url or not project:
            return None
        return f"{source.base_url.rstrip('/')}/{project}/-/{what}/{ident}"

    def ref_from_url(self, url: str, source: SourceConfig) -> Ref | None:
        base = (source.base_url or "").rstrip("/")
        if not base or not url.startswith(base):
            return None
        path = url[len(base):]
        m = _PATH.match(path)
        if not m:
            return None
        project, what, ident = m.group("project").strip("/"), m.group("what"), m.group("id")
        if what == "merge_requests" and ident.isdigit():
            return mr_ref(source.id, project, ident)
        if what == "issues" and ident.isdigit():
            return issue_ref(source.id, project, ident)
        if what in ("releases", "tags"):
            return release_ref(source.id, project, ident)
        if what in ("commit", "commits") and re.fullmatch(r"[0-9a-f]{7,40}", ident):
            return Ref(source_id=source.id, object_type="commit", external_id=f"{project}@{ident}", label=ident[:8])
        return None


register(GitLabAdapter())
