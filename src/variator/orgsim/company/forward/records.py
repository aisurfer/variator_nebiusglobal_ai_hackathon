"""Business records of a forward run: versions with the full state and every cause that still holds.

The same semantics as ``orgsim.company.model.Record`` (a later version without a cause closes that
causal link in the KB), without the history's world object. Ids are fixed when a record is made;
``Ids`` continues the numbering of the history (``INC-0043`` after ``INC-0042``), so a branch never
reuses an id of the history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# containers of the sim source and their acl (as orgsim.company.render.SIM_CONTAINERS)
CONTAINERS = {
    "company": ["grp:all-staff"],
    "finance": ["grp:management", "grp:finance"],
    "sales": ["grp:management", "grp:sales", "grp:finance"],
    "people": ["grp:management", "grp:finance"],
    "engineering": ["grp:engineering", "grp:delivery", "grp:management"],
    "support": ["grp:all-staff"],
    "feedback": ["grp:management", "grp:sales", "grp:support", "grp:delivery"],
}
Cause = tuple[str, str]  # (cause type, record id)


@dataclass
class Version:
    ts: datetime
    process: str
    kind: str
    fields: dict[str, Any]
    causes: tuple[Cause, ...]
    order: int


@dataclass
class Rec:
    id: str
    otype: str
    container: str
    state: dict[str, Any] = field(default_factory=dict)
    causes: list[Cause] = field(default_factory=list)
    versions: list[Version] = field(default_factory=list)
    text_slots: dict[str, str] = field(default_factory=dict)  # field -> artifact id of a writer text

    @property
    def first_ts(self) -> datetime:
        return self.versions[0].ts


class Book:
    """All records of one run, in creation order, with a global version order."""

    def __init__(self) -> None:
        self.records: dict[str, Rec] = {}
        self._order = 0

    def new(
        self,
        rid: str,
        otype: str,
        container: str,
        ts: datetime,
        process: str,
        kind: str,
        causes: tuple[Cause, ...] = (),
        **state: Any,
    ) -> Rec:
        if rid in self.records:
            raise ValueError(f"duplicate record id {rid}")
        r = Rec(rid, otype, container)
        self.records[rid] = r
        return self.update(r, ts, process, kind, causes, **state)

    def update(
        self,
        r: Rec,
        ts: datetime,
        process: str,
        kind: str,
        causes: tuple[Cause, ...] = (),
        drop: tuple[Cause, ...] = (),
        **changes: Any,
    ) -> Rec:
        if r.versions and ts <= r.versions[-1].ts:
            ts = r.versions[-1].ts + timedelta(seconds=30)
        for c in causes:
            if c not in r.causes:
                r.causes.append(c)
        r.causes = [c for c in r.causes if c not in drop]
        for k, v in changes.items():
            if v is None:
                r.state.pop(k, None)
            else:
                r.state[k] = v
        self._order += 1
        r.versions.append(Version(ts, process, kind, dict(r.state), tuple(r.causes), self._order))
        return r

    def of(self, otype: str) -> list[Rec]:
        return [r for r in self.records.values() if r.otype == otype]


class Ids:
    """Next ids per prefix, after the history's highest number (``StateAtT.id_top``)."""

    def __init__(self, top: dict[str, tuple[int, int]]):
        self.top = {k: list(v) for k, v in top.items()}

    def next(self, prefix: str, width: int = 4) -> str:
        n, w = self.top.get(prefix, [0, width])
        n += 1
        self.top[prefix] = [n, w]
        return f"{prefix}-{n:0{w}d}"
