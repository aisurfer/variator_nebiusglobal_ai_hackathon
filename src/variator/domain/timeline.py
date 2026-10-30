"""Timeline contract: start positions, events, milestones and goals on a month axis, plus levers.

A month is a model variable (the page moves its marker; Python fixes its value), so each
item changes the same formulas as any other input. Milestones and goals carry a condition
(a formula that is true when met) and a target probability. Levers are the inputs under
the owner's control (resources and their months); ``variator.timeline.solve_plan`` finds a
plan: how much of each resource, and when.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from variator.domain.decision import DecisionModel

ItemKind = Literal["start", "plan", "event", "milestone", "goal"]


class TimelineItem(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    kind: ItemKind
    emoji: str = ""
    description: str = ""
    month: str | float = Field(description="Variable that holds the month (movable) or a fixed month")
    condition: str | None = Field(default=None, description="Milestone or goal: formula, true = met")
    target: float = Field(default=0.8, gt=0, le=1, description="Wanted P(met)")
    place: dict[str, float] = Field(default_factory=dict, description="Event: variable values when placed")
    show: str | None = Field(default=None, description="Formula whose spread (p10-p90) the page draws")
    options: list[str] | None = Field(default=None, description="Only these options (None: all)")
    depends: list[str] = Field(
        default_factory=list, description="Items that must end first (arrows on the page)"
    )

    def applies(self, option: str) -> bool:
        return self.options is None or option in self.options

    @model_validator(mode="after")
    def _check(self) -> TimelineItem:
        if self.kind in ("milestone", "goal") and not self.condition:
            raise ValueError(f"{self.name}: a {self.kind} needs a condition")
        return self


class Lever(BaseModel):
    variable: str
    label: str
    low: float
    high: float
    step: float = Field(gt=0)
    unit: str = ""
    mode: str | None = Field(default=None, description="0/1 variable: search this lever with each mode")
    modes: dict[str, float] = Field(default_factory=dict, description="Mode label -> value of `mode`")
    prefer: Literal["low", "high"] = Field(
        default="low", description="Among equal results: low (fewer resources) or high (later month)"
    )
    needs: str | None = Field(default=None, description="Skip the lever while this variable is 0")
    options: list[str] | None = Field(default=None, description="Only for these options (None: all)")

    def grid(self) -> list[float]:
        count = int(round((self.high - self.low) / self.step))
        return [round(self.low + i * self.step, 10) for i in range(count + 1)]


class Timeline(BaseModel):
    horizon: float = Field(gt=0, description="Months on the axis")
    items: list[TimelineItem]
    levers: list[Lever] = Field(default_factory=list)

    def goals(self) -> list[TimelineItem]:
        """Milestones and goals: the items with a condition."""
        return [i for i in self.items if i.condition]

    def check(self, model: DecisionModel) -> None:
        """Raise when an item or lever names a variable that the model does not have."""
        known = {v.name for v in model.variables}
        names = [i.month for i in self.items if isinstance(i.month, str)]
        names += [k for i in self.items for k in i.place]
        names += [x for lever in self.levers for x in (lever.variable, lever.mode, lever.needs) if x]
        missing = sorted(set(names) - known)
        if missing:
            raise ValueError(f"timeline names unknown variables {missing}")


def with_values(model: DecisionModel, values: Mapping[str, float]) -> DecisionModel:
    """The model with these variables fixed (all options): a moved marker, a placed event, a lever."""
    if not values:
        return model
    data = model.model_dump(mode="json")
    for var in data["variables"]:
        if var["name"] in values:
            var["distribution"] = {"kind": "fixed", "value": float(values[var["name"]])}
    for option in data["options"]:
        for name in values:
            (option.get("overrides") or {}).pop(name, None)
    return DecisionModel.model_validate(data)
