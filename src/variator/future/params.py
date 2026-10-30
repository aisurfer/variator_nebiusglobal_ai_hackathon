"""Habit parameters of the future engine: values, ranges and source tags; one draw per scenario.

A parameter has a point (posterior median), a PERT range (low = p1, mode, high = p99) and a source tag:
``data:`` (the history of the actor), ``group:`` (a cohort), ``web:``, ``llm:`` or ``assumption:``.
``draw_theta`` draws each parameter once per scenario (the epistemic uncertainty, 04-ANSWERS §1.4.1)
from its own keyed stream, so a new parameter does not move the draws of the others.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy.stats import beta as beta_dist

from variator.orgsim.streams import stream_label
from variator.synthetic.company import derived_rng

ACTORS_DIR = Path("/tmp/variator-1000/mvp-artifacts/actors")
HISTORY_DIR = Path("/tmp/variator-1000/mvp-history-mgmt")
DOCS = ("habits.json", "cohorts.json", "risk_register.json", "actors.json", "manifest.json")


@dataclass(frozen=True)
class Param:
    """One parameter: a PERT range around a point, with its source tag."""

    name: str
    point: float
    low: float
    mode: float
    high: float
    source: str
    unit: str = ""
    note: str = ""

    @classmethod
    def fixed(cls, name: str, value: float, source: str, unit: str = "", note: str = "") -> Param:
        return cls(name, float(value), float(value), float(value), float(value), source, unit, note)

    @classmethod
    def pert(
        cls, name: str, low: float, mode: float, high: float, source: str, unit: str = "", note: str = ""
    ) -> Param:
        return cls(name, float(mode), float(low), float(mode), float(high), source, unit, note)

    @property
    def is_fixed(self) -> bool:
        return not self.high > self.low

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        for k in ("point", "low", "mode", "high"):
            row[k] = float(f"{row[k]:.6g}")
        return row


def pert_ppf(u: np.ndarray, low: float, mode: float, high: float) -> np.ndarray:
    """Inverse CDF of PERT(low, mode, high) at the uniforms ``u``."""
    if not high > low:
        return np.full(np.shape(u), float(mode))
    m = min(max(mode, low), high)
    a = 1.0 + 4.0 * (m - low) / (high - low)
    b = 1.0 + 4.0 * (high - m) / (high - low)
    return low + (high - low) * beta_dist.ppf(np.clip(u, 1e-9, 1 - 1e-9), a, b)


def stream(seed: int, *parts: object) -> np.random.Generator:
    """The keyed stream ``future:<parts>`` (shared by all forks)."""
    return derived_rng(seed, stream_label("future", *parts))


def uniforms(seed: int, n: int, *parts: object, shape: tuple[int, ...] = ()) -> np.ndarray:
    """Uniforms of shape (n, *shape); scenario s takes row s (prefix-stable in n)."""
    return stream(seed, *parts).random((n, *shape))


def exponentials(seed: int, n: int, *parts: object, shape: tuple[int, ...] = ()) -> np.ndarray:
    return stream(seed, *parts).standard_exponential((n, *shape))


def normals(seed: int, n: int, *parts: object, shape: tuple[int, ...] = ()) -> np.ndarray:
    return stream(seed, *parts).standard_normal((n, *shape))


def draw_theta(
    params: Mapping[str, Param], n: int, seed: int, *, point: bool = False
) -> dict[str, np.ndarray]:
    """One value per scenario for each parameter: (n,) arrays. ``point=True`` gives the points only."""
    out = {}
    for name, p in params.items():
        if point or p.is_fixed:
            out[name] = np.full(n, p.point)
        else:
            out[name] = pert_ppf(uniforms(seed, n, "theta", name), p.low, p.mode, p.high)
    return out


# ---------------------------------------------------------------------------------------------- habits
@dataclass
class Habits:
    """The learned documents of ``variator.actors`` (read only)."""

    habits: dict[str, Any]
    cohorts: dict[str, Any]
    risks: dict[str, Any]
    actors: dict[str, Any]
    manifest: dict[str, Any] = field(default_factory=dict)
    path: str = ""
    sha256: str = ""

    @classmethod
    def load(cls, path: str | Path = ACTORS_DIR) -> Habits:
        p = Path(path)
        docs = {}
        for name in DOCS:
            f = p / name
            docs[name] = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
        digest = hashlib.sha256((p / "habits.json").read_bytes()).hexdigest()
        return cls.from_docs(docs, path=str(p), sha256=digest)

    @classmethod
    def from_docs(cls, docs: Mapping[str, Any], *, path: str = "", sha256: str = "") -> Habits:
        """From ``Learned.documents()`` (the backtest learns in memory) or from the loaded files."""
        if not sha256:
            sha256 = hashlib.sha256(
                json.dumps(docs["habits.json"], sort_keys=True, default=str).encode()
            ).hexdigest()
        return cls(
            habits=docs["habits.json"],
            cohorts=docs["cohorts.json"],
            risks=docs["risk_register.json"],
            actors=docs["actors.json"],
            manifest=docs.get("manifest.json", {}),
            path=path,
            sha256=sha256,
        )

    @property
    def as_of(self) -> str:
        return str(self.habits.get("as_of", ""))

    def raw(self, *keys: str) -> dict[str, Any] | None:
        d: Any = self.habits
        for k in keys:
            if not isinstance(d, Mapping) or k not in d:
                return None
            d = d[k]
        return d

    def cohort(self, cid: str) -> dict[str, Any]:
        for c in self.cohorts.get("cohorts", []):
            if c.get("id") == cid:
                return c
        return {}

    def risk(self, code: str) -> dict[str, Any]:
        for r in self.risks.get("risks", []):
            if r.get("code") == code:
                return r
        return {}

    def actor_list(self, kind: str) -> list[dict[str, Any]]:
        return [a for a in self.actors.get("actors", []) if a.get("kind") == kind]


def learned(
    name: str,
    raw: Mapping[str, Any] | None,
    *,
    default: Param | None = None,
    lo: float | None = None,
    hi: float | None = None,
    note: str = "",
) -> Param:
    """A ``Param`` from a learned parameter dict (point, low, mode, high, source); ``default`` if missing.
    ``lo`` and ``hi`` clip a range that the data cannot support (the note says so)."""
    if not raw or "point" not in raw:
        if default is None:
            raise KeyError(f"no learned value for {name}")
        return default
    point, low = float(raw["point"]), float(raw.get("low", raw["point"]))
    mode, high = float(raw.get("mode", point)), float(raw.get("high", point))
    clipped = []
    if lo is not None and low < lo:
        low, clipped = lo, clipped + [f"low {raw.get('low'):.4g} -> {lo:.4g}"]
    if hi is not None and high > hi:
        high, clipped = hi, clipped + [f"high {raw.get('high'):.4g} -> {hi:.4g}"]
    lo_b, hi_b = min(low, point), max(high, point)
    mode = min(max(mode, lo_b), hi_b)
    text = "; ".join(x for x in (note, "clipped: " + ", ".join(clipped) if clipped else "") if x)
    return Param(
        name, point, lo_b, mode, hi_b, str(raw.get("source", "data:")), str(raw.get("unit", "")), text
    )


def aic_weight(aic: Mapping[str, float], selected_alternative: str) -> float:
    """Akaike weight of one model against the model 'none' (two-model average)."""
    if "none" not in aic or selected_alternative not in aic:
        return 0.0
    d = aic[selected_alternative] - aic["none"]
    w = np.exp(-0.5 * d)
    return float(w / (1.0 + w))
