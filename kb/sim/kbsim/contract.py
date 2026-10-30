"""Contract plancy-kb-corpus/1 read from the delivery side (Docs/KB/05-CORPUS-CONTRACT.md §2.3, §6).

Entity ids of timeline lines and the declared links their payloads express, written from the contract alone, not
from the backend adapters (kb/app/kb/connectors). The delivery checks (kbsim.intake) and the comparison of the
declared gold links with the loaded graph therefore test the adapters end to end.

    d = Delivery.load(<delivery dir>)          # config/sources.yaml + fixtures/timeline.jsonl.gz
    d.entity_of(line)                           # "jira.ENG-241", "confluence.page-600255", "mail.msg-000014", …
    d.links(line)                               # [Link(effect, predicate, cause, expressed_as, target, …)]

A link whose target is not an object of the delivery has `cause=None` (a dangling link).
"""
from __future__ import annotations

import gzip
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

CONTRACT = "plancy-kb-corpus/1"
EXPRESSED_AS = ("remote_link", "issue_link", "parent", "page_property", "decision_item", "mr_text", "reply", "text",
                "sim_cause", "sim_field")
PROVIDERS = ("jira", "confluence", "email", "gitlab", "directory", "sim")


def norm_label(text: Any) -> str:
    t = str(text or "").casefold()
    t = re.sub(r"\s+", " ", t)
    return t.strip(" \t\n:.;,*_-—–()[]")


# §6: relationship phrases of remote links and issue links (case-insensitive); inverse ones put the effect on the
# other side. An unknown phrase is a plain link without causality.
_CAUSE = {
    "implements": ("implements", "implementation of"),
    "informed_by": ("informed by",),
    "caused_by": ("is caused by", "caused by"),
    "blocked_by": ("is blocked by", "blocked by"),
    "depends_on": ("depends on", "requires"),
    "based_on": ("based on",),
    "governed_by": ("governed by",),
    "motivated_by": ("motivated by", "because of"),
    "supersedes": ("supersedes", "replaces"),
    "part_of": ("part of", "is part of"),
    "decided_in": ("decided in", "decided at"),
}
_INVERSE = {
    "blocked_by": ("blocks",),
    "caused_by": ("causes",),
    "implements": ("is implemented by",),
    "supersedes": ("is superseded by",),
    "depends_on": ("is depended on by", "is required by"),
}
RELATIONSHIPS: dict[str, tuple[str, bool]] = {norm_label(p): (pred, False) for pred, ps in _CAUSE.items() for p in ps}
RELATIONSHIPS |= {norm_label(p): (pred, True) for pred, ps in _INVERSE.items() for p in ps}

# §2.3 page-properties labels that declare a link (the value holds links to the cause)
PROPERTY_PREDICATES = {norm_label(k): v for k, v in {
    "Based on": "informed_by", "Basis": "informed_by",
    "Decision": "implements",
    "Supersedes": "supersedes", "Replaces": "supersedes",
    "Motivated by": "motivated_by",
    "Policy": "governed_by", "Governed by": "governed_by",
    "Experiment": "based_on",
}.items()}
MEETING_LABELS = {"meeting-notes", "meeting", "retro"}

# §2.4: the simulation provider `sim`. A line carries the simulator's event envelope {run, option, scenario, ts,
# process, kind, entity_id, fields{}, causes[{type, id}], seq}; external_id = entity_id; the entity is sim.<entity_id>.
# Required fields of `fields` per object type ("a|b": one of them); the last five types are the cause catalog.
SIM_REQUIRED: dict[str, tuple[str, ...]] = {
    "contract": ("number", "client", "status"),
    "invoice": ("number", "client", "amount", "currency", "status"),
    "payment": ("amount", "currency", "direction", "client|vendor|counterparty|authority"),
    "bank_entry": ("amount", "currency", "direction", "booked_at"),
    "deal": ("client", "owner", "status"),
    "employee_event": ("employee", "type", "effective_date"),
    "requisition": ("role", "department", "status"),
    "payroll": ("period", "amount", "currency"),
    "incident": ("title", "severity", "status"),
    "deploy": ("component", "version", "environment", "status"),
    "support_ticket": ("title", "client", "status"),
    "cloud_bill": ("vendor", "period", "amount", "currency"),
    "tax_payment": ("tax", "period", "amount", "currency"),
    "decision": ("statement", "status"),
    "nps_response": ("client", "contact", "score", "survey"),
    "hypothesis": ("statement",),
    "life_event": ("statement",),
    "habit": ("statement",),
    "rule": ("rules",),
    "process": ("statement",),
}
SIM_OBJECT_TYPES = tuple(SIM_REQUIRED)
SIM_CAUSE_PREDICATES = {"decision": "implements", "hypothesis": "based_on", "life_event": "caused_by",
                        "habit": "motivated_by", "rule": "governed_by", "process": "caused_by"}
SIM_PERSON_FIELDS = ("owner", "assignee", "author", "reporter", "employee", "actor",
                     "contact")  # people.yaml people or contacts
SIM_ORG_FIELDS = ("client", "vendor", "counterparty", "authority")  # people.yaml organizations
SIM_REF_FIELDS = ("contract", "settles", "records", "deal", "incident", "related")  # other objects of the delivery
_SIM_KB_ID = re.compile(r"^(?:sim|jira|confluence|gitlab|mail|person|org)\.\S+$")


def sim_entity(sim_id: Any) -> str:
    """sim.<id> of a simulated object; an id that already is an entity id (jira.ENG-1, mail.msg-1, …) is kept."""
    s = str(sim_id).strip()
    return s if _SIM_KB_ID.match(s) else "sim." + (_UNSAFE.sub("-", s).strip("-") or "x")


def sim_ids(value: Any) -> list[str]:
    """A reference field of a sim payload: one id or a list of ids."""
    vals = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in vals if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip()]

_KEY = r"[A-Z][A-Z0-9]{1,9}-\d+"
_CLOSING = re.compile(rf"\b(?:[Cc]los(?:e[sd]?|ing)|[Ff]ix(?:e[sd]|ing)?|[Rr]esolv(?:e[sd]?|ing)|[Ii]mplement(?:s|ed|ing)?)"
                      rf"\b:?[ \t]+(?P<refs>{_KEY}(?:\s*(?:,|&|\band\b)\s*{_KEY})*)")
_UNSAFE = re.compile(r"[^0-9A-Za-z_.@-]+")


def mail_slug(value: str) -> str:
    s = _UNSAFE.sub("-", str(value).strip().strip("<>")).strip("-.")
    return s or "unknown"


def project_slug(project: str) -> str:
    return project.strip("/").replace("/", "-")


@dataclass
class Link:
    effect: str
    predicate: str
    cause: str | None  # entity id of the delivery, None = the target is not an object of the delivery
    expressed_as: str
    target: str  # what the payload says: URL, key, message id, page id
    since: str  # `at` of the first line that expresses it
    line: int  # index of that line in the timeline
    explicit: bool = True

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.effect, self.predicate, self.cause or f"?{self.target}")

    def gold(self) -> dict[str, Any]:
        return {"effect": self.effect, "cause": self.cause, "predicate": self.predicate,
                "expressed_as": self.expressed_as, "since": self.since, "explicit": self.explicit}


def read_timeline(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for raw in fh:
            if raw.strip():
                yield json.loads(raw)


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _text(node: Any) -> str:
    return "".join(n.get("text", "") for n in _walk(node) if n.get("type") == "text")


def _urls(node: Any) -> list[str]:
    out = []
    for n in _walk(node):
        attrs = n.get("attrs") or {}
        if n.get("type") in ("inlineCard", "blockCard", "embedCard") and attrs.get("url"):
            out.append(str(attrs["url"]))
        for m in n.get("marks") or []:
            if m.get("type") == "link" and (m.get("attrs") or {}).get("href"):
                out.append(str(m["attrs"]["href"]))
    return out


def adf_of(page: dict) -> dict:
    value = ((page.get("body") or {}).get("atlas_doc_format") or {}).get("value")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def page_properties(doc: dict) -> list[tuple[str, dict]]:
    """Rows (label, value cell) of the page-properties macro (bodiedExtension `details`)."""
    rows = []
    for n in _walk(doc):
        if n.get("type") == "bodiedExtension" and (n.get("attrs") or {}).get("extensionKey") == "details":
            for row in (x for x in _walk(n) if x.get("type") == "tableRow"):
                cells = row.get("content") or []
                if len(cells) >= 2:
                    rows.append((_text(cells[0]).strip(), cells[1]))
    return rows


def items(doc: dict, kind: str) -> list[tuple[int, dict]]:
    """decisionItem / taskItem nodes with their number (localId dec-N / task-N, else the position)."""
    out = []
    pos = 0
    for n in _walk(doc):
        if n.get("type") == kind:
            pos += 1
            m = re.fullmatch(r"(?:dec|task)-(\d+)", str((n.get("attrs") or {}).get("localId") or ""))
            out.append((int(m.group(1)) if m else pos, n))
    return out


@dataclass
class Sources:
    tenant: str
    by_id: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> "Sources":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(str((data.get("tenant") or {}).get("id") or ""), {s["id"]: s for s in data.get("sources") or []})

    def provider(self, source_id: str) -> str | None:
        return (self.by_id.get(source_id) or {}).get("provider")

    def of(self, provider: str) -> list[dict[str, Any]]:
        return [s for s in self.by_id.values() if s.get("provider") == provider]


@dataclass
class Delivery:
    sources: Sources
    lines: list[dict[str, Any]]
    entities: set[str] = field(default_factory=set)
    _mail: dict[str, str] = field(default_factory=dict)  # internetMessageId -> entity id
    _commits: dict[str, str] = field(default_factory=dict)  # sha (full and 8) -> entity id

    @classmethod
    def load(cls, root: Path) -> "Delivery":
        return cls.from_files(root / "config" / "sources.yaml", root / "fixtures" / "timeline.jsonl.gz")

    @classmethod
    def from_files(cls, sources: Path, timeline: Path) -> "Delivery":
        d = cls(Sources.load(sources), list(read_timeline(timeline)))
        d.index()
        return d

    # ---------------------------------------------------------------------------------------------- ids (§2.3)
    def entity_of(self, ln: dict[str, Any]) -> str | None:
        prov = self.sources.provider(ln.get("source", ""))
        ext = str(ln.get("external_id") or "")
        p = ln.get("payload") or {}
        if prov == "jira":
            return f"jira.{ext}"
        if prov == "confluence":
            return f"confluence.page-{ext}"
        if prov == "email":
            return f"mail.{mail_slug(p.get('id') or ext)}" if p or ext else None
        if prov == "gitlab":
            m = re.match(r"^(?P<project>.+?)(?P<sep>[!#@])(?P<id>[^!#@]+)$", ext)
            if not m:
                return None
            s = project_slug(m.group("project"))
            if m.group("sep") == "!":
                return f"gitlab.{s}.mr-{m.group('id')}"
            if m.group("sep") == "#":
                return f"gitlab.{s}.issue-{m.group('id')}"
            if ln.get("object_type") == "release":
                return f"gitlab.{s}.release-{m.group('id')}"
            return f"gitlab.{s}.commit-{m.group('id')[:8]}"
        if prov == "sim":
            return sim_entity(p.get("entity_id") or ext) if p.get("entity_id") or ext else None
        return None

    def derived(self, ln: dict[str, Any]) -> list[str]:
        """Entities a line yields besides its primary one: decision/task items, mail attachments."""
        prov = self.sources.provider(ln.get("source", ""))
        p = ln.get("payload") or {}
        base = self.entity_of(ln)
        if not base or not p:
            return []
        if prov == "confluence":
            doc = adf_of(p)
            return [f"{base}.dec-{n}" for n, _ in items(doc, "decisionItem")] + \
                [f"{base}.task-{n}" for n, _ in items(doc, "taskItem")]
        if prov == "email":
            return [f"{base}.{mail_slug(a.get('id'))}" for a in p.get("attachments") or [] if a.get("id")]
        return []

    def index(self) -> None:
        for ln in self.lines:
            eid = self.entity_of(ln)
            if not eid:
                continue
            if ln.get("op") == "delete":
                continue
            self.entities.add(eid)
            self.entities.update(self.derived(ln))
            p = ln.get("payload") or {}
            if self.sources.provider(ln["source"]) == "email" and p.get("internetMessageId"):
                self._mail[str(p["internetMessageId"]).strip()] = eid
            if ln.get("object_type") == "commit":
                sha = str(p.get("id") or str(ln["external_id"]).rpartition("@")[2])
                self._commits[sha] = self._commits[sha[:8]] = eid

    # ---------------------------------------------------------------------------------------------- URLs
    def url_entity(self, url: str) -> str | None:
        """An URL of a delivery object (by the base_url of its source) -> its entity id, else None."""
        u = urlsplit(url.strip())
        full = f"{u.scheme}://{u.netloc}{u.path}"
        # the most specific base first: Confluence Cloud lives under the Jira site (…/wiki)
        for s in sorted(self.sources.by_id.values(), key=lambda s: -len(str(s.get("base_url") or ""))):
            base = str(s.get("base_url") or "").rstrip("/")
            if not base or not (full == base or full.startswith(base + "/")):
                continue
            path = unquote(full[len(base):])
            prov = s.get("provider")
            if prov == "jira":
                m = re.match(rf"^/browse/({_KEY})/?$", path)
                return f"jira.{m.group(1)}" if m else None
            if prov == "confluence":
                m = re.match(r"^/spaces/[^/]+/pages/(\d+)(?:/[^?#]*)?$", path)
                pid = m.group(1) if m else (parse_qs(u.query).get("pageId") or [None])[0] if path.endswith(
                    "/pages/viewpage.action") else None
                if not pid:
                    return None
                frag = re.fullmatch(r"(dec|task)-(\d+)", u.fragment or "")
                return f"confluence.page-{pid}" + (f".{frag.group(1)}-{frag.group(2)}" if frag else "")
            if prov == "gitlab":
                m = re.match(r"^/(?P<project>.+?)/-/(?P<what>merge_requests|issues|commit|commits|releases|tags)/(?P<id>[^/]+)", path)
                if not m:
                    return None
                s_ = project_slug(m.group("project"))
                what, ident = m.group("what"), m.group("id")
                if what == "merge_requests":
                    return f"gitlab.{s_}.mr-{ident}"
                if what == "issues":
                    return f"gitlab.{s_}.issue-{ident}"
                if what in ("commit", "commits"):
                    return f"gitlab.{s_}.commit-{ident[:8]}"
                return f"gitlab.{s_}.release-{ident}"
        return None

    def known(self, entity_id: str | None) -> str | None:
        return entity_id if entity_id and entity_id in self.entities else None

    # ---------------------------------------------------------------------------------------------- links (§6)
    def links(self, ln: dict[str, Any], index: int = 0) -> list[Link]:
        prov = self.sources.provider(ln.get("source", ""))
        p = ln.get("payload") or {}
        eid = self.entity_of(ln)
        if not p or not eid:
            return []
        at = str(ln.get("at") or "")
        out: list[Link] = []

        def add(effect: str, pred: str, target: str, cause: str | None, how: str) -> None:
            out.append(Link(effect, pred, self.known(cause), how, target, at, index))

        if prov == "jira":
            f = p.get("fields") or {}
            parent = (f.get("parent") or {}).get("key")
            if parent:
                add(eid, "part_of", parent, f"jira.{parent}", "parent")
            for lk in f.get("issuelinks") or []:
                t = lk.get("type") or {}
                if lk.get("inwardIssue"):  # «this issue <inward> the other one»
                    phrase, other = t.get("inward"), lk["inwardIssue"].get("key")
                else:
                    phrase, other = t.get("outward"), (lk.get("outwardIssue") or {}).get("key")
                pred = RELATIONSHIPS.get(norm_label(phrase))
                if other and pred:
                    p_, inverse = pred
                    if inverse:
                        add(f"jira.{other}", p_, eid, eid, "issue_link")
                    else:
                        add(eid, p_, other, f"jira.{other}", "issue_link")
            for rl in p.get("remotelinks") or []:
                pred = RELATIONSHIPS.get(norm_label(rl.get("relationship")))
                url = str((rl.get("object") or {}).get("url") or "")
                if pred and url:
                    p_, inverse = pred
                    target = self.url_entity(url)
                    if inverse:
                        if target:
                            add(target, p_, url, eid, "remote_link")
                    else:
                        add(eid, p_, url, target, "remote_link")
        elif prov == "confluence":
            doc = adf_of(p)
            labels = {str(x.get("name") or "").casefold() for x in ((p.get("labels") or {}).get("results") or [])}
            for label, cell in page_properties(doc):
                pred = PROPERTY_PREDICATES.get(norm_label(label))
                if pred:
                    for url in _urls(cell):
                        add(eid, pred, url, self.url_entity(url), "page_property")
            for n, _ in items(doc, "decisionItem"):
                add(f"{eid}.dec-{n}", "decided_in", str(ln["external_id"]), eid, "decision_item")
            if labels & MEETING_LABELS:
                for n, _ in items(doc, "taskItem"):
                    add(f"{eid}.task-{n}", "decided_in", str(ln["external_id"]), eid, "decision_item")
        elif prov == "email":
            parent = p.get("inReplyTo") or ((p.get("references") or [None])[-1] if p.get("references") else None)
            if parent:
                add(eid, "replies_to", str(parent), self._mail.get(str(parent).strip()), "reply")
        elif prov == "gitlab":
            text = "\n".join(str(p.get(k) or "") for k in ("title", "description", "message"))
            keys = self._jira_keys(ln["source"])
            for m in _CLOSING.finditer(text):
                for key in re.findall(_KEY, m.group("refs")):
                    if not keys or key.split("-")[0] in keys:
                        add(eid, "closes", key, f"jira.{key}", "mr_text")
        elif prov == "sim":
            for c in p.get("causes") or []:
                if isinstance(c, dict) and c.get("type") in SIM_CAUSE_PREDICATES and sim_ids(c.get("id")):
                    cid = sim_ids(c["id"])[0]
                    add(eid, SIM_CAUSE_PREDICATES[c["type"]], cid, sim_entity(cid), "sim_cause")
            fields = p.get("fields") if isinstance(p.get("fields"), dict) else {}
            for cid in sim_ids(fields.get("supersedes")):
                add(eid, "supersedes", cid, sim_entity(cid), "sim_field")
        return out

    def sim_refs(self, ln: dict[str, Any]) -> list[tuple[str, str, str | None]]:
        """(field, id, entity id or None) of the references of a sim line to other objects of the delivery."""
        p = ln.get("payload") or {}
        if self.sources.provider(ln.get("source", "")) != "sim" or not isinstance(p.get("fields"), dict):
            return []
        return [(k, t, self.known(sim_entity(t))) for k in SIM_REF_FIELDS for t in sim_ids(p["fields"].get(k))]

    def link_urls(self, ln: dict[str, Any]) -> list[str]:
        """Every remote link and page-property link of a line, whatever its relationship (§13: they must resolve)."""
        p = ln.get("payload") or {}
        prov = self.sources.provider(ln.get("source", ""))
        if prov == "jira":
            return [str((rl.get("object") or {}).get("url") or "") for rl in p.get("remotelinks") or []]
        if prov == "confluence" and p:
            return [u for _, cell in page_properties(adf_of(p)) for u in _urls(cell)]
        return []

    def inside(self, url: str) -> bool:
        """Does an URL point into a source of the delivery (by its base_url)?"""
        return any(url == b or url.startswith(b + "/") for b in
                   (str(s.get("base_url") or "").rstrip("/") for s in self.sources.by_id.values()) if b)

    def references(self, ln: dict[str, Any], index: int = 0) -> set[str]:
        """Entities a line points at: causes of its declared links and targets of all its links into the delivery."""
        out = {lk.cause for lk in self.links(ln, index) if lk.cause}
        out |= {e for e in (self.url_entity(u) for u in self.link_urls(ln) if self.inside(u)) if e}
        out |= {e for _, _, e in self.sim_refs(ln) if e}
        return out

    def _jira_keys(self, source_id: str) -> set[str]:
        return {str(k) for k in (self.sources.by_id.get(source_id) or {}).get("jira_keys") or []}

    def declared(self) -> list[Link]:
        """Every declared link of the delivery, first expression only, in timeline order."""
        seen: dict[tuple, Link] = {}
        for i, ln in enumerate(self.lines):
            for lk in self.links(ln, i):
                seen.setdefault(lk.key, lk)
        return list(seen.values())


def gold_line(link: Link | dict[str, Any]) -> str:
    d = link.gold() if isinstance(link, Link) else link
    return json.dumps(d, ensure_ascii=False, sort_keys=False, separators=(",", ":"))


def as_dict(obj: Any) -> dict[str, Any]:
    return asdict(obj)


def lines_of(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            if raw.strip():
                yield json.loads(raw)
