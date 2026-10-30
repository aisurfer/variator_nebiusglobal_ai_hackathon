"""RAG (red / amber / green) bands over the scenario tensor, and 2D tunnel maps.

A scenario gets a color per option and criterion from the band limits; a flag rule can
make the color worse, never better. The overall color of a scenario is the worst color
over the criteria with a weight above zero.

The tunnel map fixes two drivers on a grid, keeps the same samples of all other
drivers in every cell, and shows where each option goes red.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
from pydantic import BaseModel, Field

from variator.domain import Criterion, DecisionModel, RagColor, RagThresholds
from variator.mcda.core import criterion_ties, feasible_key, run_anchors, score_matrix, weight_vector
from variator.rules import RuleSet
from variator.simulation.distributions import quantile
from variator.simulation.sampling import Sampling
from variator.simulation.tensor import NO_FLAG, TensorRun, simulate_tensor

DEFAULT_GRID = 21
DEFAULT_SAMPLES_PER_CELL = 300
DEFAULT_RANGE = (0.05, 0.95)


def default_thresholds(run: TensorRun, baseline: str | None = None) -> dict[str, RagThresholds]:
    """Band limits from the baseline option (default: the first option, the status quo).

    Green: as good as the baseline median or better. Red: as bad as the baseline p10 or worse.
    A criterion with its own ``rag`` limits keeps them.
    """
    j = run.option_index(baseline) if baseline else 0
    limits: dict[str, RagThresholds] = {}
    for k, criterion in enumerate(run.criteria):
        if criterion.rag is not None:
            limits[criterion.name] = criterion.rag
            continue
        column = run.values[:, j, k]
        median = float(np.median(column))
        bad = float(np.quantile(column, 0.1 if criterion.higher_is_better else 0.9))
        limits[criterion.name] = RagThresholds(green=median, red=bad)
    return limits


def resolve_thresholds(
    run: TensorRun, overrides: Mapping[str, RagThresholds] | None = None, baseline: str | None = None
) -> dict[str, RagThresholds]:
    limits = default_thresholds(run, baseline)
    for name, value in (overrides or {}).items():
        if name not in limits:
            raise ValueError(f"RAG limits for unknown criterion {name!r}")
        criterion = run.criteria[run.criterion_index(name)]
        Criterion.model_validate({**criterion.model_dump(), "rag": value.model_dump()})  # checks the order
        limits[name] = value
    return limits


def classify(
    values: np.ndarray, criteria: Sequence[Criterion], limits: Mapping[str, RagThresholds]
) -> np.ndarray:
    """int8 colors with the shape of ``values`` (..., K)."""
    green = np.array([limits[c.name].green for c in criteria])
    red = np.array([limits[c.name].red for c in criteria])
    higher = np.array([c.higher_is_better for c in criteria], dtype=bool)
    is_green = np.where(higher, values >= green, values <= green)
    is_red = np.where(higher, values <= red, values >= red) & ~is_green
    colors = np.full(values.shape, int(RagColor.AMBER), dtype=np.int8)
    colors[is_green] = int(RagColor.GREEN)
    colors[is_red] = int(RagColor.RED)
    return colors


def scenario_colors(run: TensorRun, limits: Mapping[str, RagThresholds]) -> np.ndarray:
    """(S, O, K) colors: band color, made worse by flag rules; a failed check is red on all criteria."""
    colors = classify(run.values, run.criteria, limits)
    colors = np.where(run.flags != NO_FLAG, np.maximum(colors, run.flags), colors)
    return np.where(run.infeasible_mask()[:, :, None], int(RagColor.RED), colors).astype(np.int8)


def shares(colors: np.ndarray, axis: int = 0) -> np.ndarray:
    """Share of green, amber, red along ``axis``; a new last axis of size 3."""
    return np.stack([(colors == c).mean(axis=axis) for c in RagColor], axis=-1)


class RagShare(BaseModel):
    green: float
    amber: float
    red: float

    @classmethod
    def of(cls, triple: np.ndarray) -> RagShare:
        return cls(green=float(triple[0]), amber=float(triple[1]), red=float(triple[2]))


class OptionRag(BaseModel):
    option: str
    overall: RagShare = Field(description="Worst color over the weighted criteria, per scenario")
    criteria: dict[str, RagShare]


class RagReport(BaseModel):
    limits: dict[str, RagThresholds]
    options: list[OptionRag]


def rag_report(
    run: TensorRun,
    limits: Mapping[str, RagThresholds],
    weights: Mapping[str, float] | None = None,
) -> RagReport:
    names = [c.name for c in run.criteria]
    w = weight_vector(names, weights, [c.weight for c in run.criteria])
    colors = scenario_colors(run, limits)
    per_criterion = shares(colors)  # (O, K, 3)
    overall = shares(colors[:, :, w > 0].max(axis=2))  # (O, 3)
    return RagReport(
        limits=dict(limits),
        options=[
            OptionRag(
                option=option,
                overall=RagShare.of(overall[j]),
                criteria={name: RagShare.of(per_criterion[j, k]) for k, name in enumerate(names)},
            )
            for j, option in enumerate(run.option_names)
        ],
    )


class TunnelMap(BaseModel):
    """RAG shares on a grid of two drivers. Grids are lists of rows: [y index][x index]."""

    x: str
    y: str
    x_values: list[float]
    y_values: list[float]
    criterion: str | None = Field(description="Criterion of the colors; null means overall")
    options: list[str]
    green: dict[str, list[list[float]]]
    red: dict[str, list[list[float]]]
    best: list[list[str]] = Field(description="Option with the highest weighted score in each cell")
    samples_per_cell: int


def tunnel(
    model: DecisionModel,
    *,
    x: str,
    y: str,
    limits: Mapping[str, RagThresholds],
    rules: RuleSet | None = None,
    criterion: str | None = None,
    weights: Mapping[str, float] | None = None,
    grid: int = DEFAULT_GRID,
    samples_per_cell: int = DEFAULT_SAMPLES_PER_CELL,
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
    seed: int | None = 0,
    sampling: Sampling = "random",
) -> TunnelMap:
    """Fix ``x`` and ``y`` on a ``grid`` x ``grid`` lattice and colour every cell.

    Default ranges: p5..p95 of the variable's distribution. One vectorized simulation
    covers all cells (``simulate_tensor`` with ``tile`` and ``pins``).
    """
    if x == y:
        raise ValueError("x and y must be different variables")
    names = {v.name for v in model.variables}
    for name in (x, y):
        if name not in names:
            raise ValueError(f"unknown variable {name!r}")
    xs = np.linspace(*(x_range or _range(model, x)), grid)
    ys = np.linspace(*(y_range or _range(model, y)), grid)
    cells = grid * grid
    # Cell c = iy * grid + ix; each cell holds one block of samples_per_cell rows.
    pin_x = np.repeat(np.tile(xs, grid), samples_per_cell)
    pin_y = np.repeat(np.repeat(ys, grid), samples_per_cell)
    run = simulate_tensor(
        model,
        rules=rules,
        n_scenarios=samples_per_cell,
        seed=seed,
        tile=cells,
        pins={x: pin_x, y: pin_y},
        sampling=sampling,
    )
    colors = scenario_colors(run, limits)  # (cells * n, O, K)
    if criterion is None:
        w_all = weight_vector([c.name for c in run.criteria], weights, [c.weight for c in run.criteria])
        picked = colors[:, :, w_all > 0].max(axis=2)
    else:
        picked = colors[:, :, run.criterion_index(criterion)]
    blocks = picked.reshape(cells, samples_per_cell, -1)
    green = (blocks == int(RagColor.GREEN)).mean(axis=1).reshape(grid, grid, -1)
    red = (blocks == int(RagColor.RED)).mean(axis=1).reshape(grid, grid, -1)

    names_k = [c.name for c in run.criteria]
    w = weight_vector(names_k, weights, [c.weight for c in run.criteria])
    means = run.values.reshape(cells, samples_per_cell, *run.values.shape[1:]).mean(axis=1)  # (cells, O, K)
    feasible = 1.0 - run.infeasible_mask().reshape(cells, samples_per_cell, -1).mean(axis=1)  # (cells, O)
    # the rank rule (mcda.rank): score x share of feasible scenarios in the cell
    ties = criterion_ties(run)
    best_idx = (
        (score_matrix(means, run.higher_is_better, ties, run_anchors(run)) @ w) * feasible_key(feasible, ties)
    ).argmax(axis=1).reshape(grid, grid)
    options = run.option_names
    return TunnelMap(
        x=x,
        y=y,
        x_values=[float(v) for v in xs],
        y_values=[float(v) for v in ys],
        criterion=criterion,
        options=options,
        green={o: np.round(green[:, :, j], 4).tolist() for j, o in enumerate(options)},
        red={o: np.round(red[:, :, j], 4).tolist() for j, o in enumerate(options)},
        best=[[options[i] for i in row] for row in best_idx],
        samples_per_cell=samples_per_cell,
    )


def _range(model: DecisionModel, name: str) -> tuple[float, float]:
    dist = model.variable(name).distribution
    low, high = (quantile(dist, q) for q in DEFAULT_RANGE)
    if low == high:
        span = abs(low) * 0.2 or 1.0
        low, high = low - span, high + span
    return low, high
