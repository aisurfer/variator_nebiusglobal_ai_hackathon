"""Metric cube ``[scenario, option, metric, period]`` and its bridge to the decision layer.

The cube holds the value and the status of every metric in every scenario, option and
period, the check masks ``[scenario, option, period]`` and the scenario inputs.
``to_tensor_run`` makes the existing ``TensorRun`` from it, so ``mcda.rank``,
``mcda.best_path``, ``rag`` and the reports work on simulated metrics without change
(04-ANSWERS §2.6).

Storage: ``save_cube`` / ``load_cube`` behind the ``CubeFormat`` interface. The only
format is ``npz+json`` (Parquet is not installed): ``arrays.npz`` with a fixed zip
time, so the same cube gives the same bytes, and ``cube.json`` with the metadata.
"""

from __future__ import annotations

import hashlib
import io
import json
import keyword
import re
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from variator.domain import Criterion
from variator.domain.criteria import RagColor, RagThresholds
from variator.metrics.results import Bands, Direction
from variator.simulation.tensor import NO_FLAG, TensorRun

STATUS_CODES: dict[str, int] = {"good": 0, "warn": 1, "bad": 2, "neutral": 3, "unknown": 4}
STATUS_NAMES: tuple[str, ...] = tuple(STATUS_CODES)
_FLAG_OF_STATUS = np.array([NO_FLAG, int(RagColor.AMBER), int(RagColor.RED), NO_FLAG, NO_FLAG], dtype=np.int8)
CUBE_JSON = "cube.json"
CUBE_NPZ = "arrays.npz"
FORMAT_VERSION = 1


class MetricInfo(BaseModel):
    """What the cube needs to know about a metric."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str = ""
    unit: str = ""
    direction: Direction = "higher"


@dataclass
class MetricCube:
    """Values (NaN = unknown) and statuses per [scenario, option, metric, period]."""

    run: str
    options: list[str]
    metrics: list[MetricInfo]
    periods: list[str]  # period labels, for example ISO start dates of PeriodAxis
    values: np.ndarray  # float64 [S, O, K, T]
    status: np.ndarray | None = None  # int8 [S, O, K, T], codes of STATUS_CODES
    checks: dict[str, np.ndarray] = field(default_factory=dict)  # check id -> bool [S, O, T]
    inputs: dict[str, np.ndarray] = field(default_factory=dict)  # name -> [S] or [S, O]
    seed: int | None = None
    grain: str | None = None
    anchor: str | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.values = np.asarray(self.values, dtype=np.float64)
        self.metrics = [
            m if isinstance(m, MetricInfo) else MetricInfo.model_validate(m) for m in self.metrics
        ]
        want = (len(self.options), len(self.metrics), len(self.periods))
        if self.values.ndim != 4 or self.values.shape[1:] != want:
            raise ValueError(
                f"values must have shape (S, {want[0]}, {want[1]}, {want[2]}), got {self.values.shape}"
            )
        for name, items in (
            ("options", self.options),
            ("metrics", self.metric_ids),
            ("periods", self.periods),
        ):
            if len(set(items)) != len(items):
                raise ValueError(f"{name} must be unique")
        if self.status is None:
            self.status = np.where(np.isnan(self.values), STATUS_CODES["unknown"], STATUS_CODES["neutral"])
        self.status = np.asarray(self.status, dtype=np.int8)
        if self.status.shape != self.values.shape:
            raise ValueError("status must have the shape of values")
        s, o, _, t = self.values.shape
        for cid, mask in list(self.checks.items()):
            m = np.asarray(mask)
            if m.dtype != bool or m.shape != (s, o, t):
                raise ValueError(f"check {cid!r} must be a bool array of shape {(s, o, t)}")
            self.checks[cid] = m
        for name, arr in list(self.inputs.items()):
            a = np.asarray(arr, dtype=np.float64)
            if a.shape not in ((s,), (s, o)):
                raise ValueError(f"input {name!r} must have shape ({s},) or ({s}, {o})")
            self.inputs[name] = a

    @property
    def shape(self) -> tuple[int, int, int, int]:
        return tuple(self.values.shape)  # type: ignore[return-value]

    @property
    def n_scenarios(self) -> int:
        return int(self.values.shape[0])

    @property
    def metric_ids(self) -> list[str]:
        return [m.id for m in self.metrics]

    def metric_index(self, metric: str) -> int:
        return self.metric_ids.index(metric)

    def option_index(self, option: str) -> int:
        return self.options.index(option)

    def period_index(self, period: int | str | None) -> int:
        """Index of a period label or number; None is the last period. Negative numbers count from the end."""
        if period is None:
            return len(self.periods) - 1
        if isinstance(period, str):
            return self.periods.index(period)
        t = int(period) + (len(self.periods) if period < 0 else 0)
        if not 0 <= t < len(self.periods):
            raise IndexError(f"period {period} is outside 0..{len(self.periods) - 1}")
        return t

    def series(self, metric: str, option: str) -> np.ndarray:
        """Values of one metric and option: [scenario, period]."""
        return self.values[:, self.option_index(option), self.metric_index(metric), :]

    def set_status_from_bands(
        self, metric: str, bands: Bands | Mapping[str, float] | None, direction: Direction | None = None
    ) -> None:
        """Statuses of one metric from Plancy bands (the rule of ``results.status_of``)."""
        k = self.metric_index(metric)
        b = None if bands is None else (bands if isinstance(bands, Bands) else Bands.model_validate(bands))
        d = direction or self.metrics[k].direction
        x = self.values[:, :, k, :]
        if b is None or d == "context":
            code = np.full(x.shape, STATUS_CODES["neutral"])
        elif d == "higher":
            code = np.where(x >= b.good, 0, np.where(x >= b.warn, 1, 2))
        else:
            code = np.where(x <= b.good, 0, np.where(x <= b.warn, 1, 2))
        self.status[:, :, k, :] = np.where(np.isnan(x), STATUS_CODES["unknown"], code)


# ---------------------------------------------------------------------- storage
class CubeFormat(Protocol):
    name: str

    def save(self, cube: MetricCube, path: Path) -> None: ...

    def load(self, path: Path) -> MetricCube: ...


def _npz_bytes(arrays: Mapping[str, np.ndarray]) -> bytes:
    """A .npz archive with a fixed member time: the same arrays give the same bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, arr in arrays.items():
            member = io.BytesIO()
            np.lib.format.write_array(member, np.ascontiguousarray(arr), allow_pickle=False)
            info = zipfile.ZipInfo(f"{name}.npy", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, member.getvalue())
    return buf.getvalue()


class NpzJsonFormat:
    """``<dir>/arrays.npz`` + ``<dir>/cube.json``."""

    name = "npz+json"

    def save(self, cube: MetricCube, path: Path) -> None:
        checks, inputs = list(cube.checks), list(cube.inputs)
        arrays = {"values": cube.values, "status": cube.status}
        arrays |= {f"check_{i}": cube.checks[c] for i, c in enumerate(checks)}
        arrays |= {f"input_{i}": cube.inputs[n] for i, n in enumerate(inputs)}
        blob = _npz_bytes(arrays)
        meta = {
            "format": self.name,
            "format_version": FORMAT_VERSION,
            "run": cube.run,
            "options": cube.options,
            "metrics": [m.model_dump() for m in cube.metrics],
            "periods": cube.periods,
            "checks": checks,
            "inputs": inputs,
            "seed": cube.seed,
            "grain": cube.grain,
            "anchor": cube.anchor,
            "notes": cube.notes,
            "shape": list(cube.shape),
            "arrays_sha256": hashlib.sha256(blob).hexdigest(),
        }
        path.mkdir(parents=True, exist_ok=True)
        (path / CUBE_NPZ).write_bytes(blob)
        (path / CUBE_JSON).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def load(self, path: Path) -> MetricCube:
        meta = json.loads((path / CUBE_JSON).read_text(encoding="utf-8"))
        blob = (path / CUBE_NPZ).read_bytes()
        if hashlib.sha256(blob).hexdigest() != meta["arrays_sha256"]:
            raise ValueError(f"{path / CUBE_NPZ}: sha256 differs from {CUBE_JSON}")
        with np.load(io.BytesIO(blob), allow_pickle=False) as npz:
            arrays = {k: npz[k] for k in npz.files}
        return MetricCube(
            run=meta["run"],
            options=meta["options"],
            metrics=meta["metrics"],
            periods=meta["periods"],
            values=arrays["values"],
            status=arrays["status"],
            checks={c: arrays[f"check_{i}"] for i, c in enumerate(meta["checks"])},
            inputs={n: arrays[f"input_{i}"] for i, n in enumerate(meta["inputs"])},
            seed=meta["seed"],
            grain=meta["grain"],
            anchor=meta["anchor"],
            notes=meta["notes"],
        )


FORMATS: dict[str, CubeFormat] = {NpzJsonFormat.name: NpzJsonFormat()}


def save_cube(cube: MetricCube, path: str | Path, fmt: str = NpzJsonFormat.name) -> Path:
    """Save a cube into the directory ``path``."""
    p = Path(path)
    FORMATS[fmt].save(cube, p)
    return p


def load_cube(path: str | Path) -> MetricCube:
    p = Path(path)
    fmt = json.loads((p / CUBE_JSON).read_text(encoding="utf-8")).get("format", NpzJsonFormat.name)
    return FORMATS[fmt].load(p)


# ---------------------------------------------------------------------- bridge to TensorRun
Reduce = Literal["at", "mean", "min", "max", "sum"]


class CriterionSpec(BaseModel):
    """One ranking criterion from a cube metric.

    ``reduce``: ``at`` takes the value at the period; the others fold periods 0..period
    (the explicit fold of a criterion, §2.6).
    """

    model_config = ConfigDict(extra="forbid")

    metric: str
    name: str | None = None
    higher_is_better: bool | None = None
    weight: float = Field(default=1.0, ge=0)
    reduce: Reduce = "at"
    unit: str | None = None
    rag: RagThresholds | None = None


def _spec(c: CriterionSpec | Mapping[str, Any] | Sequence[Any]) -> CriterionSpec:
    if isinstance(c, CriterionSpec):
        return c
    if isinstance(c, Mapping):
        return CriterionSpec.model_validate(c)
    parts = list(c)
    direction = parts[1] if len(parts) > 1 else None
    weight = parts[2] if len(parts) > 2 else 1.0
    if isinstance(direction, str):
        if direction not in ("higher", "lower"):
            raise ValueError(f"direction must be 'higher' or 'lower', got {direction!r}")
        direction = direction == "higher"
    return CriterionSpec(metric=parts[0], higher_is_better=direction, weight=weight)


def criterion_name(metric_id: str) -> str:
    """A snake_case identifier for a metric id (``ceo.cash-runway`` -> ``ceo_cash_runway``)."""
    name = re.sub(r"[^0-9a-zA-Z_]+", "_", metric_id).strip("_").lower() or "metric"
    if name[0].isdigit():
        name = "m_" + name
    return name + "_" if keyword.iskeyword(name) else name


def _fold(x: np.ndarray, how: Reduce) -> np.ndarray:
    """Fold the last axis (periods 0..t) of x."""
    if how == "at":
        return x[..., -1]
    return {"mean": np.mean, "min": np.min, "max": np.max, "sum": np.sum}[how](x, axis=-1)


def to_tensor_run(
    cube: MetricCube,
    criteria: Sequence[CriterionSpec | Mapping[str, Any] | Sequence[Any]],
    period: int | str | None = None,
    *,
    checks: Literal["through", "at", "none"] = "through",
    unknown: Literal["raise", "infeasible"] = "raise",
    flags_from_status: bool = True,
) -> TensorRun:
    """A ``TensorRun`` with ``X[s, o, k]`` of the criteria at ``period`` (default: the last).

    - ``criteria``: ``CriterionSpec``, a dict, or a tuple ``(metric_id, direction, weight)``
      with direction ``"higher"``, ``"lower"`` or a bool. The default direction is the
      metric's; a ``context`` metric needs an explicit one.
    - ``checks``: ``through`` marks (s, o) infeasible when a check fails in any period
      0..period (path-dependent checks); ``at`` uses the period only; ``none`` ignores them.
    - ``unknown``: a NaN value raises, or (``infeasible``) marks its (s, o) infeasible
      and takes the worst known value of its column, so statistics stay finite.
    - ``flags_from_status``: status warn / bad of a criterion metric gives an amber / red flag.
    - probes: ``in:<name>`` for each input and ``first_fail:<check>`` (-1 = never), [S, O].
    """
    start = time.perf_counter()
    t = cube.period_index(period)
    specs = [_spec(c) for c in criteria]
    if not specs:
        raise ValueError("at least one criterion is needed")
    s_n, o_n = cube.n_scenarios, len(cube.options)
    values = np.empty((s_n, o_n, len(specs)), dtype=np.float64)
    flags = np.full(values.shape, NO_FLAG, dtype=np.int8)
    out: list[Criterion] = []
    for k, spec in enumerate(specs):
        m = cube.metric_index(spec.metric)
        info = cube.metrics[m]
        higher = spec.higher_is_better
        if higher is None:
            if info.direction == "context":
                raise ValueError(f"metric {spec.metric!r} has direction 'context': give higher_is_better")
            higher = info.direction == "higher"
        window = slice(t, t + 1) if spec.reduce == "at" else slice(0, t + 1)
        values[:, :, k] = _fold(cube.values[:, :, m, window], spec.reduce)
        if flags_from_status:
            worst = cube.status[:, :, m, window]
            code = np.where(np.isin(worst, (0, 1, 2)), worst, -1).max(axis=-1)
            flags[:, :, k] = np.where(code >= 0, _FLAG_OF_STATUS[np.clip(code, 0, 4)], NO_FLAG)
        name = spec.name or criterion_name(spec.metric)
        if any(c.name == name for c in out):
            name = f"{name}_{k}"
        out.append(
            Criterion(
                name=name,
                description=info.title or info.id,
                unit=spec.unit or info.unit,
                higher_is_better=higher,
                weight=spec.weight,
                rag=spec.rag,
                source="metrics.cube",
            )
        )

    infeasible = np.zeros((s_n, o_n), dtype=bool)
    check_failures: dict[str, list[float]] = {}
    for cid, mask in cube.checks.items():
        if checks == "none":
            break
        failed = mask[:, :, : t + 1].any(axis=-1) if checks == "through" else mask[:, :, t]
        infeasible |= failed
        check_failures[cid] = failed.mean(axis=0).tolist()

    notes = [f"bridged from cube {cube.run!r} at period {cube.periods[t]} (index {t})"]
    missing = np.isnan(values)
    if missing.any():
        where = {out[k].name: int(missing[:, :, k].sum()) for k in range(len(out)) if missing[:, :, k].any()}
        if unknown == "raise":
            raise ValueError(
                f"unknown (NaN) values in criteria {where}; use unknown='infeasible' to keep them out"
            )
        for k, c in enumerate(out):
            col = values[:, :, k]
            for j in range(o_n):
                known = col[~np.isnan(col[:, j]), j]
                if known.size == 0:
                    raise ValueError(
                        f"criterion {c.name!r} has no known value for option {cube.options[j]!r}"
                    )
                col[np.isnan(col[:, j]), j] = known.min() if c.higher_is_better else known.max()
        infeasible |= missing.any(axis=-1)
        notes.append(f"unknown values made infeasible: {where}")

    probes = {
        f"in:{n}": np.broadcast_to(a[:, None] if a.ndim == 1 else a, (s_n, o_n)).astype(np.float64)
        for n, a in cube.inputs.items()
    }
    for cid, mask in cube.checks.items():
        first = np.where(mask.any(axis=-1), mask.argmax(axis=-1), -1)
        probes[f"first_fail:{cid}"] = first.astype(np.float64)
    return TensorRun(
        criteria=out,
        option_names=list(cube.options),
        values=values,
        flags=flags,
        rule_hits={cid: list(v) for cid, v in check_failures.items()},
        check_failures=check_failures,
        seed=cube.seed,
        backend="cube",
        elapsed_s=time.perf_counter() - start,
        notes=notes,
        infeasible=infeasible,
        probes=probes,
    )
