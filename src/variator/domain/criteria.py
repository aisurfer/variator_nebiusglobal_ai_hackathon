"""Criteria: the axes of a multi-criteria decision.

Each criterion is one output dimension of the scenario tensor ``X[s, o, k]``. The core
supplies generic axes (money, time, resources); a plugin adds domain axes. The user
sets the weights, and the code recalculates the ranking without an LLM call.
"""

from __future__ import annotations

import keyword
import re
from enum import IntEnum

from pydantic import BaseModel, Field, field_validator, model_validator

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class RagColor(IntEnum):
    """Integer codes keep the per-scenario color arrays small (int8); worse is higher."""

    GREEN = 0
    AMBER = 1
    RED = 2

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: str | int) -> RagColor:
        if isinstance(value, int):
            return cls(value)
        return cls[value.strip().upper()]


class RagThresholds(BaseModel):
    """Band limits in criterion units.

    For a higher-is-better criterion: value >= ``green`` is green, value <= ``red`` is red.
    For a lower-is-better criterion: value <= ``green`` is green, value >= ``red`` is red.
    Values between the limits are amber.
    """

    green: float
    red: float


class RagVariables(BaseModel):
    """Variables that set the band limits: each limit is the median of its variable (a fixed value as is).

    The user changes a limit as any other input; the simulation resolves it to ``RagThresholds``.
    """

    green: str
    red: str


class Criterion(BaseModel):
    """One axis of the analysis (for example net value, lead time, team months)."""

    name: str = Field(description="snake_case identifier, the key in DecisionOption.metrics")
    description: str = ""
    unit: str = ""
    higher_is_better: bool = True
    weight: float = Field(default=1.0, ge=0, description="Default weight; the user can change it")
    rag: RagThresholds | None = Field(
        default=None, description="Band limits; null means defaults from the baseline option"
    )
    rag_from: RagVariables | None = Field(
        default=None, description="Variables whose medians give the band limits; they win over `rag`"
    )
    tie_within: float = Field(
        default=0.0,
        ge=0,
        description="Meaningful difference: options closer than this on the criterion count as equal "
        "(score, P(best)); 0 = every difference counts",
    )
    tie_from: str | None = Field(
        default=None, description="Variable whose median gives `tie_within` (the user can edit it)"
    )
    source: str = Field(default="core", description="Plugin that supplies the criterion")

    @field_validator("name")
    @classmethod
    def _identifier(cls, v: str) -> str:
        if not _IDENTIFIER_RE.match(v) or keyword.iskeyword(v):
            raise ValueError(f"{v!r} must be a snake_case identifier")
        return v

    @model_validator(mode="after")
    def _check_rag(self) -> Criterion:
        if self.rag is None:
            return self
        ordered = self.rag.green >= self.rag.red if self.higher_is_better else self.rag.green <= self.rag.red
        if not ordered:
            side = "green >= red" if self.higher_is_better else "green <= red"
            raise ValueError(f"criterion {self.name!r}: RAG limits must satisfy {side}")
        return self


# Generic axes that every domain can use. Plugins copy and adapt them.
MONEY = Criterion(
    name="money", description="Net financial result", unit="USD", higher_is_better=True, weight=1.0
)
TIME = Criterion(
    name="time", description="Time to the result", unit="months", higher_is_better=False, weight=1.0
)
RESOURCES = Criterion(
    name="resources",
    description="Effort or capacity used",
    unit="FTE-months",
    higher_is_better=False,
    weight=1.0,
)
CORE_CRITERIA = (MONEY, TIME, RESOURCES)
