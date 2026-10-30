"""Helpers shared by the provider adapters (pure functions, no I/O).

- time parsing that tolerates every provider format (Jira "+0300", GitLab "Z", epoch millis, bare dates);
- the relationship vocabulary (Jira remote links, issue links, Confluence property labels) mapped to ontology
  predicates (English labels);
- `Links`: a per-record collector of statements and causal references with deterministic de-duplication;
- small text helpers.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

from kb.contracts import AliasSpec, CausalSpec, Evidence, Ref, SourceEnvelope, StatementSpec

# ------------------------------------------------------------------------------------------------ time

_FALLBACK_OFFSETS = {"Europe/Moscow": 3, "UTC": 0, "Etc/UTC": 0, "Europe/Berlin": 1, "Europe/London": 0}
_TZ_ABBR = {"msk": 3, "utc": 0, "gmt": 0, "cet": 1, "cest": 2}


def tenant_tz(ctx: Any) -> tzinfo:
    name = getattr(getattr(ctx, "tenant_sources", None), "timezone", None) or "UTC"
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # tzdata missing in a slim image: fixed offset is good enough for the simulation
        return timezone(timedelta(hours=_FALLBACK_OFFSETS.get(name, 0)))


def parse_dt(value: Any, tz: tzinfo | None = None) -> datetime | None:
    """Parse ISO-8601 (with or without offset, 'Z', '+0300'), epoch seconds/millis or a date; None if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz or timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=tz or timezone.utc)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000.0 if abs(value) > 1e11 else float(value)
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    s = str(value).strip()
    if re.fullmatch(r"-?\d{9,}", s):
        return parse_dt(int(s), tz)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz or timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\.(\d{1,2})\.(\d{4})")
_TIME_RE = re.compile(r"(?<![\d.])(\d{1,2}):(\d{2})(?!\d)")
_OFFSET_RE = re.compile(r"(?:UTC|GMT)?\s*([+-])(\d{1,2}):?(\d{2})?\b")
_ABBR_RE = re.compile(r"\b(MSK|UTC|GMT|CET|CEST)\b", re.IGNORECASE)


def parse_when(text: str | None, tz: tzinfo) -> tuple[datetime | None, datetime | None]:
    """Parse a human meeting time such as '2026-03-02, 11:00–12:00 (MSK)' into (start, end)."""
    if not text:
        return None, None
    m = _DATE_RE.search(text)
    if not m:
        return None, None
    if m.group(1):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        d, mo, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
    rest = text[m.end():]
    zone: tzinfo = tz
    ab = _ABBR_RE.search(rest)
    if ab:
        zone = timezone(timedelta(hours=_TZ_ABBR[ab.group(1).lower()]))
    else:
        off = _OFFSET_RE.search(rest)
        if off and (":" in off.group(0) or off.group(3)):
            sign = 1 if off.group(1) == "+" else -1
            zone = timezone(sign * timedelta(hours=int(off.group(2)), minutes=int(off.group(3) or 0)))
    times = _TIME_RE.findall(rest)
    try:
        if not times:
            return datetime(y, mo, d, tzinfo=zone), None
        start = datetime(y, mo, d, int(times[0][0]), int(times[0][1]), tzinfo=zone)
        end = datetime(y, mo, d, int(times[1][0]), int(times[1][1]), tzinfo=zone) if len(times) > 1 else None
        return start, end
    except ValueError:
        return None, None


# ------------------------------------------------------------------------------------------------ text


def norm_label(text: Any) -> str:
    """Normalise a relationship / property label: casefold, collapse spaces, strip punctuation."""
    t = str(text or "").casefold()
    t = re.sub(r"\s+", " ", t)
    return t.strip(" \t\n:.;,*_-—–()[]")


def clean_ws(text: str | None) -> str:
    if not text:
        return ""
    lines = []
    for ln in str(text).replace("\r\n", "\n").split("\n"):
        lead = ln[: len(ln) - len(ln.lstrip(" \t"))].replace("\t", "    ")  # keep list indentation
        lines.append(lead + re.sub(r"[ \t]+", " ", ln.lstrip(" \t")).rstrip())
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def truncate(text: str, n: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def items(value: Any) -> list[Any]:
    """The value if it is a list, else [] (payload lists may be missing, null or of the wrong type)."""
    return value if isinstance(value, list) else []




def string(value: Any) -> str | None:
    """A non-empty string value, else None (for URLs and identifiers that must be text)."""
    return value if isinstance(value, str) and value else None


def account(ctx: Any, account_id: Any, label: Any = None) -> Ref | None:
    """ctx.account_ref() that tolerates ids and display names of the wrong type in a payload."""
    if not isinstance(account_id, (str, int)) or isinstance(account_id, bool) or account_id == "":
        return None
    return ctx.account_ref(str(account_id), label if isinstance(label, str) and label else None)




def dig(obj: Any, *path: Any, default: Any = None) -> Any:
    """Safe nested lookup: dig(p, "fields", "status", "name")."""
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and -len(cur) <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"-?\d+", str(value))
    return int(m.group(0)) if m else None


# ------------------------------------------------------------------------------------------------ vocabulary

# Relationship phrase (Jira remote link "relationship", issue-link inward/outward description, generic link text)
# -> ("cause", predicate) | ("statement", predicate) | ("inverse", predicate-of-the-other-side).
# "inverse" phrases describe a link whose effect is the *other* object; the adapter keeps them as relates_to.
_RELATIONSHIPS: dict[str, tuple[str, str]] = {}


def _rel(kind: str, predicate: str, *phrases: str) -> None:
    for p in phrases:
        _RELATIONSHIPS[norm_label(p)] = (kind, predicate)


_rel("cause", "implements", "implements", "implementation of")
_rel("cause", "based_on", "based on")
_rel("cause", "governed_by", "governed by")
_rel("cause", "motivated_by", "motivated by", "because of")
_rel("cause", "informed_by", "informed by")
_rel("cause", "depends_on", "depends on", "requires")
_rel("cause", "caused_by", "is caused by", "caused by")
_rel("cause", "blocked_by", "is blocked by", "blocked by")
_rel("cause", "supersedes", "supersedes", "replaces")
_rel("cause", "part_of", "part of", "is part of")
_rel("cause", "decided_in", "decided in", "decided at")
_rel("cause", "precondition", "precondition")
_rel("statement", "mentions", "mentioned in", "mentions", "wiki page")
_rel("statement", "relates_to", "relates to", "related to", "related", "relates", "see also")
_rel("inverse", "blocked_by", "blocks")
_rel("inverse", "caused_by", "causes")
_rel("inverse", "implements", "is implemented by", "implemented by")
_rel("inverse", "depends_on", "is depended on by", "is depended by", "is required by")
_rel("inverse", "supersedes", "is superseded by", "superseded by")
_rel("inverse", "part_of", "has part", "includes work")
_rel("statement", "relates_to", "duplicates", "is duplicated by", "clones", "is cloned by")


def relationship(phrase: Any) -> tuple[str, str]:
    """Map a relationship phrase to (kind, predicate); unknown phrases are plain relates_to statements."""
    return _RELATIONSHIPS.get(norm_label(phrase), ("statement", "relates_to"))


# Confluence page-properties labels -> what the row means.
PROPERTY_ROLES: dict[str, str] = {}


def _prop(role: str, *labels: str) -> None:
    for lab in labels:
        PROPERTY_ROLES[norm_label(lab)] = role


_prop("cause:informed_by", "based on", "basis", "inputs")
_prop("cause:based_on:experiment", "experiment")
_prop("cause:implements", "decision")
_prop("cause:supersedes", "supersedes", "replaces")
_prop("cause:motivated_by", "motivated by")
_prop("cause:governed_by", "policy", "governed by")
_prop("held_at", "date", "when", "meeting date", "held at")
_prop("attendees", "attendees", "participants")
_prop("note_taker", "note taker")
_prop("owner", "owner", "driver")
_prop("status", "status")
_prop("kind", "type", "document type")
_prop("relates_to", "epic", "jira", "issue", "related")
_prop("target_metric", "target metric", "success metric")
_prop("period", "period", "reporting period")


def property_role(label: Any) -> str | None:
    return PROPERTY_ROLES.get(norm_label(label))


EXPERIMENT_DESIGN_FIELDS = ["hypothesis", "traffic_split", "start_date", "duration_days", "primary_metric"]


def based_on_sensitivity(cause: Ref, hint: str | None = None) -> list[str] | None:
    """`based_on` has no typical_sensitivity in the ontology ("depends on the target"): choose it by target type.

    Only what the dependent object actually builds on makes the link stale; e.g. a report based on an experiment
    is not invalidated when the experiment is later closed with a result that the report itself produced.
    """
    if hint == "experiment":
        return list(EXPERIMENT_DESIGN_FIELDS)
    ot = cause.object_type
    if ot == "decision":
        return ["statement"]
    if ot == "release":
        return ["tag", "merge_requests"]
    if ot == "merge_request":
        return ["state", "merge_commit"]
    if ot == "issue":
        return ["summary", "title", "description", "acceptance_criteria"]
    return None


# ------------------------------------------------------------------------------------------------ records


def record_acl(env: SourceEnvelope, ctx: Any, container: str | None) -> list[str] | None:
    """Object ACL from the connector if any, else the container ACL; None lets the journal decide."""
    if env.acl is not None:
        return list(env.acl)
    acl = ctx.container_acl(container)
    return list(acl) if acl else None


def container_statement(predicate: str, source_id: str, key: str | None, name: str | None = None) -> StatementSpec | None:
    """in_project / in_space / in_repository as a literal: no adapter or bootstrap produces container entities yet."""
    if not key:
        return None
    q: dict[str, Any] = {"source_id": source_id}
    if name and name != key:
        q["name"] = name
    return StatementSpec(predicate=predicate, literal=key, qualifiers=q)


def _jkey(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def ref_key(ref: Ref) -> str:
    return ref.external_key() or f"entity:{ref.entity_id}"


class Links:
    """Collects a record's statements and causal references; de-duplicates deterministically (first wins).

    - a causal reference to the record itself is dropped;
    - `mentions` of an object that the record already links to (causally or by another statement) are dropped.
    """

    def __init__(self, self_key: Ref | None = None):
        self._self = ref_key(self_key) if self_key else None
        self._statements: list[StatementSpec] = []
        self._causes: list[CausalSpec] = []
        self._skeys: set[tuple[str, str]] = set()
        self._ckeys: set[tuple[str, str]] = set()

    def statement(self, predicate: str, obj: Ref | None = None, literal: Any = None, *,
                  qualifiers: dict[str, Any] | None = None, evidence: Evidence | None = None,
                  valid_from: datetime | None = None, confidence: float = 1.0) -> None:
        if obj is None and (literal is None or literal == "" or literal == []):
            return
        if obj is not None and ref_key(obj) == self._self:
            return
        k = (predicate, ref_key(obj) if obj is not None else "lit:" + _jkey(literal))
        if k in self._skeys:
            return
        self._skeys.add(k)
        self._statements.append(StatementSpec(predicate=predicate, object=obj, literal=None if obj else literal,
                                              qualifiers=qualifiers or {}, evidence=evidence, valid_from=valid_from,
                                              confidence=confidence))

    def add(self, spec: StatementSpec | None) -> None:
        if spec is not None:
            self.statement(spec.predicate, spec.object, spec.literal, qualifiers=spec.qualifiers,
                           evidence=spec.evidence, valid_from=spec.valid_from)

    def cause(self, predicate: str, ref: Ref | None, *, sensitivity: list[str] | None = None,
              evidence: Evidence | None = None, valid_from: datetime | None = None, confidence: float = 1.0) -> None:
        if ref is None or ref_key(ref) == self._self:
            return
        k = (predicate, ref_key(ref))
        if k in self._ckeys:
            return
        self._ckeys.add(k)
        self._causes.append(CausalSpec(predicate=predicate, cause=ref, sensitivity=sensitivity, evidence=evidence,
                                       valid_from=valid_from, confidence=confidence))

    def link(self, phrase: Any, ref: Ref | None, *, evidence: Evidence | None = None,
             qualifiers: dict[str, Any] | None = None, sensitivity_hint: str | None = None) -> None:
        """Route a link by its relationship phrase (remote link, issue link, property row)."""
        if ref is None:
            return
        kind, predicate = relationship(phrase)
        if kind == "cause":
            sens = based_on_sensitivity(ref, sensitivity_hint) if predicate == "based_on" else None
            self.cause(predicate, ref, sensitivity=sens, evidence=evidence)
        elif kind == "statement":
            q = dict(qualifiers or {})
            if norm_label(phrase) not in ("relates to", "related to", "related", "relates", "mentions"):
                q.setdefault("relationship", str(phrase))  # e.g. "mentioned in": the other side mentions this one
            self.statement(predicate, ref, qualifiers=q, evidence=evidence)
        else:  # inverse: the other side owns the causal link; keep the association visible from this side
            q = dict(qualifiers or {})
            q.setdefault("relationship", str(phrase))
            self.statement("relates_to", ref, qualifiers=q, evidence=evidence)

    @property
    def statements(self) -> list[StatementSpec]:
        linked = {ref_key(c.cause) for c in self._causes}
        linked |= {ref_key(s.object) for s in self._statements if s.object is not None and s.predicate != "mentions"}
        return [s for s in self._statements
                if not (s.predicate == "mentions" and s.object is not None and ref_key(s.object) in linked)]

    @property
    def causes(self) -> list[CausalSpec]:
        return list(self._causes)


def aliases(*items: tuple[str | None, str]) -> list[AliasSpec]:
    """aliases(("FP-101", "key"), (title, "title")) with blanks and duplicates removed."""
    out: list[AliasSpec] = []
    seen: set[tuple[str, str]] = set()
    for alias, kind in items:
        if not alias or not str(alias).strip():
            continue
        a = str(alias).strip()
        if (a, kind) in seen:
            continue
        seen.add((a, kind))
        out.append(AliasSpec(alias=a, kind=kind))  # type: ignore[arg-type]
    return out


def evidence(quote: str | None, **locator: Any) -> Evidence:
    return Evidence(quote=truncate(quote, 400) if quote else None, locator={k: v for k, v in locator.items() if v is not None})


def with_label(ref: Ref | None, label: Any) -> Ref | None:
    if ref is None or not label or ref.label:
        return ref
    return ref.model_copy(update={"label": truncate(str(label), 200)})
