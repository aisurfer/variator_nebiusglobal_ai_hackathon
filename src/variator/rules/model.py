"""If-then / if-elif-else rule packs, loaded from YAML.

Example::

    pack: logistics_basic
    rules:
      - id: stockout
        kind: effect
        if: lead_time_days > 30 and cover_days < lead_time_days
        then: {set: {lost_sales: "demand * 0.05"}}
        elif:
          - if: lead_time_days > 20
            then: {set: {lost_sales: "demand * 0.02"}}
        else: {set: {lost_sales: 0}}

Conditions and values are formulas in the same safe syntax as option formulas.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from variator.domain.criteria import RagColor

_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")


class RuleKind(StrEnum):
    EFFECT = "effect"  # changes variable values per scenario
    CHECK = "check"  # validates inputs: reports the share of scenarios that fail
    FLAG = "flag"  # sets a RAG color on a criterion


class Action(BaseModel):
    """What a branch does where its condition is true."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    set: dict[str, str | float] = Field(default_factory=dict, description="variable -> formula")
    fail: str | None = Field(default=None, description="Check message; the branch is a failure")
    flag: str | None = Field(default=None, description="green, amber or red")
    criterion: str | None = Field(default=None, description="Criterion of the flag; null means all")

    @field_validator("flag")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        if v is not None:
            RagColor.parse(v)
        return v

    def formulas(self) -> dict[str, str]:
        return {name: str(value) for name, value in self.set.items()}


class Branch(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    if_: str = Field(alias="if")
    then: Action


class Rule(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    kind: RuleKind = RuleKind.EFFECT
    description: str = ""
    if_: str = Field(alias="if")
    then: Action
    elif_: list[Branch] = Field(default_factory=list, alias="elif")
    else_: Action | None = Field(default=None, alias="else")
    options: list[str] | None = Field(default=None, description="Options in scope; null means all")

    @field_validator("id")
    @classmethod
    def _identifier(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"rule id {v!r} must be lowercase (letters, digits, '_', '.', '-')")
        return v

    @model_validator(mode="after")
    def _check_actions(self) -> Rule:
        for action in self.actions():
            if self.kind == RuleKind.EFFECT and (action.fail or action.flag):
                raise ValueError(f"rule {self.id!r}: an effect rule can only `set` values")
            if self.kind == RuleKind.CHECK and (action.set or action.flag):
                raise ValueError(f"rule {self.id!r}: a check rule can only `fail`")
            if self.kind == RuleKind.FLAG and (action.set or action.fail):
                raise ValueError(f"rule {self.id!r}: a flag rule can only set `flag`")
        return self

    def branches(self) -> list[tuple[str, Action]]:
        """Conditions and actions in evaluation order (the first true condition wins)."""
        return [(self.if_, self.then), *((b.if_, b.then) for b in self.elif_)]

    def actions(self) -> list[Action]:
        return [a for _, a in self.branches()] + ([self.else_] if self.else_ else [])


class RulePack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack: str
    description: str = ""
    source: str = Field(default="user", description="Plugin or user that supplies the pack")
    templates: list[str] | None = Field(
        default=None, description="Plugin templates that use this pack automatically; null means all"
    )
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> RulePack:
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError(f"pack {self.pack!r}: rule ids must be unique")
        return self


def load_pack(source: str | Path | dict[str, Any]) -> RulePack:
    """Load a pack from a YAML file path, a YAML string or a parsed mapping."""
    if isinstance(source, dict):
        data = source
    else:
        text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("a rule pack must be a YAML mapping with `pack` and `rules`")
    return RulePack.model_validate(data)
