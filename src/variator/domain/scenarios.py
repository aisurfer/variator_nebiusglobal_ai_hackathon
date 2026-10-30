"""Scenario presets: named, combinable changes to a template (typical situations of a use case).

A scenario is a list of relative ops, so any set of scenarios combines without conflicts:

* ``scale``: multiply the location of a distribution (and its std); a risk probability or
  magnitude; or a criterion weight.
* ``shift``: add to the location of a distribution (not its std).
* ``probability``: combine a risk probability as ``1 - (1 - p) * (1 - q)``.

Scenarios apply in the order of their definition, not in the order of activation, so the
same set always gives the same model. The playground page (``model-simulator.html``) uses
the same ops on the exported JSON; ``check_model_page.py --scenarios`` checks the parity.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from variator.domain.decision import DecisionModel

_LOCATION = ("value", "mean", "low", "mode", "high")
_SCALE_ONLY = ("std",)


class ScenarioOp(BaseModel):
    kind: Literal["variable", "risk", "weight"] = "variable"
    target: str = Field(description="Variable, risk or criterion name")
    scale: float | None = Field(default=None, gt=0, description="Multiply (variable, risk magnitude, weight)")
    shift: float | None = Field(default=None, description="Add to the location (variable only)")
    probability: float | None = Field(default=None, ge=0, le=1, description="Risk: 1 - (1 - p) * (1 - q)")
    options: list[str] | None = Field(default=None, description="Variable: only these options (overrides)")

    @model_validator(mode="after")
    def _check(self) -> ScenarioOp:
        given = [x for x in (self.scale, self.shift, self.probability) if x is not None]
        if len(given) != 1:
            raise ValueError("give exactly one of scale, shift, probability")
        if self.kind == "weight" and self.scale is None:
            raise ValueError("a weight op needs scale")
        if self.kind != "risk" and self.probability is not None:
            raise ValueError("probability is for risk ops only")
        if self.kind != "variable" and (self.shift is not None or self.options):
            raise ValueError("shift and options are for variable ops only")
        return self


class Scenario(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    emoji: str = ""
    description: str
    ops: list[ScenarioOp]


def _dist_op(dist: dict[str, Any], op: ScenarioOp) -> None:
    if dist.get("kind") == "bernoulli":
        p = dist["p"] * op.scale if op.scale is not None else dist["p"] + (op.shift or 0.0)
        dist["p"] = min(max(p, 0.0), 1.0)
        return
    for key in _LOCATION:
        if dist.get(key) is not None:
            dist[key] = dist[key] * op.scale if op.scale is not None else dist[key] + op.shift
    if op.scale is not None:
        for key in _SCALE_ONLY:
            if dist.get(key) is not None:
                dist[key] = dist[key] * op.scale


def _apply_op(data: dict[str, Any], op: ScenarioOp) -> None:
    if op.kind == "weight":
        crit = next((c for c in data["criteria"] if c["name"] == op.target), None)
        if crit is None:
            raise ValueError(f"no criterion {op.target!r}")
        crit["weight"] = crit["weight"] * op.scale
        return
    if op.kind == "risk":
        risk = next((r for r in data["risks"] if r["name"] == op.target), None)
        if risk is None:
            raise ValueError(f"no risk {op.target!r}")
        if op.probability is not None:
            risk["probability"] = 1 - (1 - risk["probability"]) * (1 - op.probability)
        else:
            _dist_op(risk["magnitude"], op)
        return
    var = next((v for v in data["variables"] if v["name"] == op.target), None)
    if var is None:
        raise ValueError(f"no variable {op.target!r}")
    if op.options is None:
        _dist_op(var["distribution"], op)
    for option in data["options"]:
        overrides = option.setdefault("overrides", {})
        if op.options is None:
            if op.target in overrides:
                _dist_op(overrides[op.target], op)
        elif option["name"] in op.options:
            overrides.setdefault(op.target, dict(var["distribution"]))
            _dist_op(overrides[op.target], op)


def apply_scenarios(
    model: DecisionModel, scenarios: Sequence[Scenario], active: Iterable[str]
) -> DecisionModel:
    """The model with the active scenarios applied in definition order; unknown names raise."""
    wanted = set(active)
    unknown = wanted - {s.name for s in scenarios}
    if unknown:
        raise ValueError(f"unknown scenarios {sorted(unknown)}; known: {[s.name for s in scenarios]}")
    data = model.model_dump(mode="json")
    for scenario in scenarios:
        if scenario.name in wanted:
            for op in scenario.ops:
                _apply_op(data, op)
    return DecisionModel.model_validate(data)
