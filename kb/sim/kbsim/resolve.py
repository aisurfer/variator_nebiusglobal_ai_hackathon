"""Id conventions of the exported tenant and the beat -> backend entity resolver (Docs/KB/03-SIMULATION.md §6, §8).

The exporter (kbsim.export) records, for every world event, which backend entities the delivered payload produced
(`Touch`: entity id, version seq after the delivery, whether this delivery created a new version). Entity ids come
from the real adapters when they are importable, else from the conventions below (kb.connectors.base docstring):

  jira.<KEY>                              ticket
  confluence.page-<numeric id>            page; world "page-0042" -> 600042
  confluence.page-<id>.dec-<n>            decision item n of a page (localId dec-n, positional)
  confluence.page-<id>.task-<n>           action item n
  gitlab.<project-slug>.mr-<iid>          merge request (slug = path with "/" -> "-")
  gitlab.<project-slug>.commit-<sha[:8]>  commit
  mail.<message id>                       e-mail (Graph "id" = world Email.id; chars outside [A-Za-z0-9_.@-] -> "-")
  mail.<message id>.<att id>              attachment ("att-1", ...)
  person.<employee id lower>, person.<contact id lower>, org.<org id without "org-">   directory

A beat resolves through the events that carry its `beat_id` to the entities they touched:
  entities   any-of set that counts as "this beat was retrieved" (primary artefact + derived records this beat
             changed; for decision beats the matched decision items)
  primary    the most specific entity (the decision item for a decision beat, else the artefact)
  versions   version ids `entity@seq` created by the beat
  all        every entity touched (used for forbidden beats: any of them visible = leak)
A beat without events is `unrealized`; its cases are skipped by the evaluation.
"""
from __future__ import annotations

import re
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable

from kbsim.models import Beat, Case, Cases, Scenario, WorldEvent


# ------------------------------------------------------------------------------------------------ tenant ids
@dataclass(frozen=True)
class Ids:
    tenant: str
    mail_domain: str

    @property
    def jira(self) -> str:
        return f"jira-{self.tenant}"

    @property
    def confluence(self) -> str:
        return f"confluence-{self.tenant}"

    @property
    def gitlab(self) -> str:
        return f"gitlab-{self.tenant}"

    @property
    def mail(self) -> str:
        return f"mail-{self.tenant}"

    @property
    def directory(self) -> str:
        return f"directory-{self.tenant}"

    @property
    def jira_base(self) -> str:
        return f"https://{self.tenant}.atlassian.net"

    @property
    def confluence_base(self) -> str:
        return f"{self.jira_base}/wiki"

    @property
    def gitlab_base(self) -> str:
        return f"https://git.{self.mail_domain}"

    def provider_source(self, key: str) -> str:
        """Account keys in company.yaml may be provider names ("jira") or source ids ("jira-company")."""
        k = key.strip()
        if k in ("mail", "email", "e-mail"):
            return self.mail
        if k in ("jira", "confluence", "gitlab", "directory"):
            return f"{k}-{self.tenant}"
        return k


# ------------------------------------------------------------------------------------------------ entity ids
def slug(project: str) -> str:
    return project.strip("/").replace("/", "-")


def jira_entity(key: str) -> str:
    return f"jira.{key}"


def page_entity(num: str | int) -> str:
    return f"confluence.page-{num}"


def decision_entity(num: str | int, n: int) -> str:
    return f"confluence.page-{num}.dec-{n}"


def task_entity(num: str | int, n: int) -> str:
    return f"confluence.page-{num}.task-{n}"


def mr_entity(repo: str, iid: int | str) -> str:
    return f"gitlab.{slug(repo)}.mr-{iid}"


def commit_entity(repo: str, sha: str) -> str:
    return f"gitlab.{slug(repo)}.commit-{sha[:8]}"


_UNSAFE = re.compile(r"[^0-9A-Za-z_.@-]+")


def mail_slug(value: str) -> str:
    """Same segment rule as kb.connectors.email.slug: chars outside [A-Za-z0-9_.@-] -> "-"."""
    s = _UNSAFE.sub("-", str(value).strip().strip("<>")).strip("-.")
    return s or "unknown"


def mail_entity(msg_id: str) -> str:
    return f"mail.{mail_slug(msg_id)}"


def attachment_entity(msg_id: str, att_id: str) -> str:
    return f"mail.{mail_slug(msg_id)}.{mail_slug(att_id)}"


_VERSION = re.compile(r"^(?P<ent>.+)@(?P<seq>\d+)$")


def entity_of(ref: str) -> str:
    """`jira.X-1@3` -> `jira.X-1`; an entity id is returned unchanged (mail ids may contain '@')."""
    m = _VERSION.match(ref)
    return m.group("ent") if m else ref


def version_id(entity_id: str, seq: int) -> str:
    return f"{entity_id}@{seq}"


class PageNumbers:
    """World page ids -> numeric Confluence ids, deterministic: "page-0042" -> "600042", others by crc32."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._used: set[str] = set()

    def __call__(self, world_id: str) -> str:
        if world_id in self._map:
            return self._map[world_id]
        m = re.fullmatch(r"(?:page-?)?0*(\d{1,5})", world_id)
        n = 600000 + int(m.group(1)) if m else 700000 + zlib.crc32(world_id.encode()) % 100000
        while str(n) in self._used:
            n += 1
        self._map[world_id] = str(n)
        self._used.add(str(n))
        return str(n)

    def items(self) -> dict[str, str]:
        return dict(self._map)


# ------------------------------------------------------------------------------------------------ touches
@dataclass
class Touch:
    """One backend entity produced by the delivery of one world event."""

    entity_id: str
    seq: int | None  # head version seq of the entity after this delivery (None = unknown)
    changed: bool  # this delivery created a new version (or tombstoned the entity)
    role: str  # primary | decision | task | attachment | derived
    text: str = ""  # decision statement, task text, attachment title (for matching)
    url: str | None = None
    deleted: bool = False

    @property
    def version_id(self) -> str | None:
        return version_id(self.entity_id, self.seq) if self.seq else None


# ------------------------------------------------------------------------------------------------ text matching
_NBSP = re.compile(r"[     ]")


def norm(text: Any) -> str:
    """Casefold, unify spaces/dashes/quotes, drop thousands separators, decimal comma -> dot."""
    t = _NBSP.sub(" ", str(text or "")).casefold()
    t = re.sub(r"[‐‑‒–—―−]", "-", t)
    t = re.sub(r"[«»“”„\"]", '"', t)
    t = re.sub(r"[’‘`]", "'", t)
    t = re.sub(r"(?<=\d)[ .,'](?=\d{3}(?!\d))", "", t)
    t = re.sub(r"(?<=\d) (?=\d{3}(?!\d))", "", t)  # second pass for "1 500 000"
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    t = re.sub(r"(?<=\d) +%", "%", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def fact_in(forms: Iterable[str], text_norm: str) -> bool:
    return any(norm(f) and norm(f) in text_norm for f in forms)


# ------------------------------------------------------------------------------------------------ resolution
@dataclass
class ResolvedBeat:
    beat: str
    realized: bool
    reason: str | None = None  # why unrealized: "no events" | "unknown beat" | "export failed"
    channel: str | None = None
    confidential: bool = False
    events: list[int] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    primary: str | None = None
    versions: list[str] = field(default_factory=list)
    all: list[str] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _uniq(xs: Iterable[str | None]) -> list[str]:
    return list(dict.fromkeys(x for x in xs if x))


def _pick_decisions(beat: Beat, candidates: list[Touch]) -> list[Touch]:
    """Decision items of a page event that a decision beat made: best fact match, else the only one, else all."""
    if len(candidates) <= 1:
        return candidates
    scores = []
    for t in candidates:
        tn = norm(t.text)
        scores.append(sum(1 for f in beat.facts if fact_in(f.any, tn)))
    best = max(scores)
    if best > 0:
        return [t for t, s in zip(candidates, scores, strict=True) if s == best]
    return candidates


def resolve_beat(beat_id: str, beats: dict[str, Beat], events_by_beat: dict[str, list[WorldEvent]],
                 touches: dict[int, list[Touch]], failed: set[int] | None = None) -> ResolvedBeat:
    beat = beats.get(beat_id)
    evs = events_by_beat.get(beat_id, [])
    rb = ResolvedBeat(beat=beat_id, realized=False, channel=beat.channel if beat else None,
                      confidential=bool(beat and beat.confidential),
                      facts=[f.model_dump() for f in beat.facts] if beat else [])
    if beat is None and not evs:
        rb.reason = "unknown beat"
        return rb
    if not evs:
        rb.reason = "no events"
        return rb
    rb.events = [e.seq for e in evs]
    entities: list[str] = []
    primaries: list[str] = []
    versions: list[str] = []
    every: list[str] = []
    for ev in evs:
        ts = touches.get(ev.seq) or []
        if not ts:
            continue
        every += [t.entity_id for t in ts]
        prim = next((t for t in ts if t.role == "primary"), ts[0])
        derived = [t for t in ts if t is not prim and t.changed and not t.deleted]
        decisions = [t for t in derived if t.role == "decision"]
        if beat is not None and beat.decision and decisions:
            chosen = _pick_decisions(beat, decisions)
            others = [t for t in derived if t.role != "decision"]
            picked = chosen + others
            primaries.append(chosen[0].entity_id)
        else:
            picked = derived
            primaries.append(prim.entity_id)
        entities += [prim.entity_id] + [t.entity_id for t in picked]
        for t in [prim, *picked]:
            if t.changed and t.version_id:
                versions.append(t.version_id)
    if not entities:
        rb.reason = "export failed" if failed and any(e.seq in failed for e in evs) else "no artefact"
        return rb
    rb.realized = True
    rb.entities = _uniq(entities)
    rb.primary = primaries[0] if primaries else rb.entities[0]
    rb.versions = _uniq(versions)
    rb.all = _uniq(every)
    return rb


def _iso(dt: datetime | None, tz: Any) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None and tz is not None:
        dt = dt.replace(tzinfo=tz)
    return dt.isoformat()


def resolve_cases(cases: Cases, scenario: Scenario | None, events: list[WorldEvent], touches: dict[int, list[Touch]],
                  *, tenant: str, tz: Any = None, failed: set[int] | None = None) -> dict[str, Any]:
    """cases.resolved.json: every case with its beats resolved to entity ids, versions and unrealized beats."""
    beats = {b.id: b for b in (scenario.beats if scenario else [])}
    by_beat: dict[str, list[WorldEvent]] = {}
    for ev in events:
        if ev.beat_id:
            by_beat.setdefault(ev.beat_id, []).append(ev)
    cache: dict[str, ResolvedBeat] = {}

    def rb(bid: str) -> ResolvedBeat:
        if bid not in cache:
            cache[bid] = resolve_beat(bid, beats, by_beat, touches, failed)
        return cache[bid]

    out_cases = []
    for c in cases.cases:
        ev = [rb(b) for b in c.expect.evidence_beats]
        ro = [rb(b) for b in c.expect.root_beats]
        fo = [rb(b) for b in c.expect.forbidden_beats]
        unrealized = _uniq(x.beat for x in ev + ro + fo if not x.realized)
        out_cases.append({
            "id": c.id,
            "difficulty": c.difficulty,
            "category": c.category,
            "split": c.split,
            "question": c.question,
            "principal": c.principal,
            "known_at": _iso(c.known_at, tz),
            "answer_facts": [f.model_dump() for f in c.expect.answer_facts],
            "expect_not_found": c.expect.expect_not_found,
            "rationale": c.rationale,
            "evidence": [x.as_dict() for x in ev],
            "roots": [x.as_dict() for x in ro],
            "forbidden": [x.as_dict() for x in fo],
            "unrealized": unrealized,
            "skip": bool(unrealized),
            "skip_reason": ("unrealized beats: " + ", ".join(unrealized)) if unrealized else None,
        })
    for b in beats:  # every scenario beat, for debugging and coverage
        rb(b)
    realized = sum(1 for x in cache.values() if x.realized)
    return {
        "tenant": tenant,
        "world": {"events": len(events), "first_ts": events[0].ts.isoformat() if events else None,
                  "last_ts": events[-1].ts.isoformat() if events else None},
        "stats": {"cases": len(out_cases), "skipped": sum(1 for c in out_cases if c["skip"]),
                  "beats": len(cache), "beats_realized": realized, "beats_unrealized": len(cache) - realized},
        "cases": out_cases,
        "beats": {k: v.as_dict() for k, v in sorted(cache.items())},
    }


def case_ids(c: Case) -> list[str]:
    return list(dict.fromkeys(c.expect.evidence_beats + c.expect.root_beats + c.expect.forbidden_beats))
