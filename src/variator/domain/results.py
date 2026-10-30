"""Results of simulation and optimization, consumed by Nemotron Ultra and the UI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from variator.domain.decision import MetricSpec


class HistogramBin(BaseModel):
    left: float
    right: float
    count: int


class OptionStats(BaseModel):
    """Distribution of the metric for one option across all scenarios.

    For a lower-is-better metric (``MetricSpec.higher_is_better=False``) the risk
    fields are mirrored: ``prob_loss`` is P(metric > 0), ``var_95`` is the 95th
    percentile and ``cvar_95`` the mean of the highest 5%.
    """

    option: str
    mean: float
    std: float
    p5: float
    p25: float
    p50: float
    p75: float
    p95: float
    prob_loss: float = Field(description="P(metric < 0); mirrored for lower-is-better")
    var_95: float = Field(description="5th percentile (bad-tail value at risk level)")
    cvar_95: float = Field(description="Mean of the worst 5% of scenarios, in metric units")
    prob_best: float = Field(description="Share of scenarios where this option is the best")
    histogram: list[HistogramBin] = Field(default_factory=list)


class Sensitivity(BaseModel):
    """Tornado-chart entry: metric when one variable sits at its p10 vs p90."""

    option: str
    variable: str
    metric_at_low: float
    metric_at_high: float
    swing: float = Field(description="abs(metric_at_high - metric_at_low)")


class SimulationResult(BaseModel):
    metric: MetricSpec
    n_scenarios: int
    seed: int | None
    backend: Literal["numpy", "cupy"]
    elapsed_s: float
    options: list[OptionStats]
    sensitivities: list[Sensitivity] = Field(default_factory=list)
    risk_hit_rates: dict[str, float] = Field(
        default_factory=dict, description="Risk name -> share of scenarios where it fired"
    )
    notes: list[str] = Field(
        default_factory=list, description="Engine caveats, e.g. a repaired correlation matrix"
    )

    def best_by_mean(self) -> OptionStats:
        """Option with the best mean, respecting ``metric.higher_is_better``."""
        if self.metric.higher_is_better:
            return max(self.options, key=lambda o: o.mean)
        return min(self.options, key=lambda o: o.mean)


class OptimizationResult(BaseModel):
    """Portfolio of options chosen under budget / risk constraints across scenarios."""

    selected: list[str]
    expected_value: float
    cvar: float | None = Field(
        default=None,
        description="Mean portfolio value over the worst (1 - cvar_alpha) of scenarios, in metric "
        "units (same convention as OptionStats.cvar_95, not CVaR of loss)",
    )
    cvar_alpha: float | None = None
    budget_used: float | None = None
    solver: Literal["cuopt", "highs"]
    status: str
    proven_optimal: bool
    elapsed_s: float
    notes: list[str] = Field(default_factory=list)
