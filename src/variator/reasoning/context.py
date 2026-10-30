"""Serialize facts, models and results into compact prompt context.

Everything here is deterministic. Compact forms drop what the LLM does not need
(histograms, timings, duplicated statistics) and round numbers, which saves
tokens on every Ultra call.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from variator.domain import DecisionModel, Fact, OptimizationResult, SimulationResult

METRIC_DIGITS = 4  # significant digits for metric values
PROB_DECIMALS = 3  # absolute decimals for probabilities
PROB_FIELDS = frozenset({"prob_loss", "prob_best"})
# Dropped from the per-option stats: histogram is bulky, var_95 equals p5 by definition.
DROPPED_OPTION_FIELDS = frozenset({"histogram", "var_95"})


def fact_id(index: int) -> str:
    """Zero-based position in the facts list -> citation id ("F1", "F2", ...)."""
    return f"F{index + 1}"


def format_facts(facts: Sequence[Fact]) -> str:
    """Number facts as ``[F1] ...`` lines so the model can cite them."""
    if not facts:
        return "(no evidence provided)"
    return "\n".join(_format_fact(i, fact) for i, fact in enumerate(facts))


def _format_fact(index: int, fact: Fact) -> str:
    parts = [f"[{fact_id(index)}] {fact.statement.strip()}"]
    if fact.value is not None:
        parts.append(f"value={_plain(fact.value)}" + (f" {fact.unit}" if fact.unit else ""))
    if fact.period:
        parts.append(f"period={fact.period}")
    parts.append(f"origin={fact.origin.value}")
    if fact.source:
        parts.append(f"source={fact.source}")
    parts.append(f"confidence={fact.confidence:.2f}")
    return " | ".join(parts)


def round_sig(x: float, digits: int = METRIC_DIGITS) -> float | int:
    """Round to significant digits; integral results become ints (shorter JSON)."""
    if not math.isfinite(x) or x == 0:
        return x
    rounded = round(x, digits - 1 - math.floor(math.log10(abs(x))))
    return int(rounded) if float(rounded).is_integer() and abs(rounded) < 1e15 else rounded


def _plain(x: float) -> str:
    value = round_sig(x, 6)
    return str(value)


def _round_prob(x: float) -> float:
    return round(x, PROB_DECIMALS)


def compact_model(model: DecisionModel) -> dict[str, Any]:
    """Decision model without null fields, ready for a prompt.

    Defaults are kept on purpose: e.g. a missing ``effect`` would hide that a risk multiplies.
    """
    return model.model_dump(mode="json", exclude_none=True)


def compact_simulation(sim: SimulationResult, *, top_drivers: int = 5) -> dict[str, Any]:
    """Simulation summary for prompts: no histograms, rounded, top drivers per option."""
    options = []
    for stats in sim.options:
        row: dict[str, Any] = {}
        for key, value in stats.model_dump(exclude=set(DROPPED_OPTION_FIELDS)).items():
            if key == "option":
                row[key] = value
            elif key in PROB_FIELDS:
                row[key] = _round_prob(value)
            else:
                row[key] = round_sig(value)
        options.append(row)

    drivers: dict[str, list[dict[str, Any]]] = {}
    for option in (o.option for o in sim.options):
        entries = sorted(
            (s for s in sim.sensitivities if s.option == option), key=lambda s: s.swing, reverse=True
        )[:top_drivers]
        if entries:
            drivers[option] = [
                {
                    "variable": s.variable,
                    "at_p10": round_sig(s.metric_at_low),
                    "at_p90": round_sig(s.metric_at_high),
                    "swing": round_sig(s.swing),
                }
                for s in entries
            ]

    out: dict[str, Any] = {
        "metric": sim.metric.model_dump(mode="json"),
        "n_scenarios": sim.n_scenarios,
        "options": options,
    }
    if drivers:
        out["top_drivers"] = drivers
    if sim.risk_hit_rates:
        out["risk_hit_rates"] = {k: _round_prob(v) for k, v in sim.risk_hit_rates.items()}
    return out


def compact_optimization(opt: OptimizationResult | None) -> dict[str, Any] | None:
    """Optimizer outcome for prompts (timings dropped, numbers rounded)."""
    if opt is None:
        return None
    out: dict[str, Any] = {
        "selected": opt.selected,
        "expected_value": round_sig(opt.expected_value),
        "solver": opt.solver,
        "status": opt.status,
        "proven_optimal": opt.proven_optimal,
    }
    if opt.cvar is not None:
        out["cvar"] = round_sig(opt.cvar)
    if opt.budget_used is not None:
        out["budget_used"] = round_sig(opt.budget_used)
    if opt.notes:
        out["notes"] = opt.notes
    return out


def to_json(obj: Any) -> str:
    """Compact JSON (no spaces) for prompts."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
