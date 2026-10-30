"""Compile rule packs and run them on all scenarios at the same time.

A rule never runs Python code: every condition and value goes through the safe
formula compiler of ``variator.expr``. Unknown names are an error at
compile time, so a bad pack fails before the simulation starts.

Order: effect rules run first, in pack order and rule order; each one sees the values
that the earlier rules set. Check and flag rules run after all effects.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import numpy as np

from variator.domain.criteria import RagColor
from variator.expr import CompiledFormula, FormulaError, compile_with_derived
from variator.rules.model import Action, Rule, RuleKind, RulePack


class RuleError(ValueError):
    """A rule pack does not match the decision model (unknown names, bad syntax)."""


@dataclass(frozen=True)
class _CompiledAction:
    sets: tuple[tuple[str, CompiledFormula], ...]
    fail: str | None
    flag: RagColor | None
    criterion: str | None


@dataclass(frozen=True)
class CompiledRule:
    rule: Rule
    conditions: tuple[CompiledFormula, ...]
    actions: tuple[_CompiledAction, ...]  # one per condition
    otherwise: _CompiledAction | None

    @property
    def id(self) -> str:
        return self.rule.id

    def applies_to(self, option: str) -> bool:
        return self.rule.options is None or option in self.rule.options

    def branch_masks(self, env: dict[str, Any], xp: ModuleType, n: int) -> list[Any]:
        """One boolean mask per branch (and one for `else`); the masks do not overlap."""
        remaining = xp.ones(n, dtype=bool)
        masks = []
        for condition in self.conditions:
            hit = xp.logical_and(condition.evaluate(env, xp, size=n) != 0, remaining)
            masks.append(hit)
            remaining = xp.logical_and(remaining, xp.logical_not(hit))
        masks.append(remaining)
        return masks

    def branch_actions(self) -> list[_CompiledAction | None]:
        return [*self.actions, self.otherwise]


@dataclass
class RuleOutcome:
    """Result of the rules for one option, per scenario."""

    env: dict[str, Any]
    fired: dict[str, Any] = field(default_factory=dict)  # rule id -> mask: a non-else branch was true
    failed: dict[str, Any] = field(default_factory=dict)  # check rule id -> mask of failures
    flags: dict[str | None, Any] = field(default_factory=dict)  # criterion (None: all) -> int8 colors


@dataclass(frozen=True)
class RuleSet:
    """All compiled rules of a case. Empty is valid and does nothing."""

    rules: tuple[CompiledRule, ...] = ()

    @property
    def names(self) -> frozenset[str]:
        """Variables that the rules read or write."""
        found: set[str] = set()
        for rule in self.rules:
            for condition in rule.conditions:
                found |= condition.names
            for action in rule.branch_actions():
                if action:
                    for target, formula in action.sets:
                        found |= formula.names | {target}
        return frozenset(found)

    def by_kind(self, kind: RuleKind) -> list[CompiledRule]:
        return [r for r in self.rules if r.rule.kind == kind]

    def apply(self, env: dict[str, Any], option: str, xp: ModuleType, n: int) -> RuleOutcome:
        """Run all rules for ``option``; return a new env (the input env is not changed)."""
        env = dict(env)
        outcome = RuleOutcome(env=env)
        for rule in self.by_kind(RuleKind.EFFECT):
            if not rule.applies_to(option):
                continue
            masks = rule.branch_masks(env, xp, n)
            outcome.fired[rule.id] = xp.logical_not(masks[-1])
            # Values of all branches use the env before this rule, so branch order does not matter.
            before = dict(env)
            for mask, action in zip(masks, rule.branch_actions(), strict=True):
                if action is None:
                    continue
                for target, formula in action.sets:
                    value = formula.evaluate(before, xp, size=n)
                    env[target] = xp.where(mask, value, env[target])
        for rule in self.rules:
            if rule.rule.kind == RuleKind.EFFECT or not rule.applies_to(option):
                continue
            masks = rule.branch_masks(env, xp, n)
            outcome.fired[rule.id] = xp.logical_not(masks[-1])
            for mask, action in zip(masks, rule.branch_actions(), strict=True):
                if action is None:
                    continue
                if action.fail is not None:
                    prior = outcome.failed.get(rule.id)
                    outcome.failed[rule.id] = mask if prior is None else xp.logical_or(prior, mask)
                if action.flag is not None:
                    colors = outcome.flags.get(action.criterion)
                    if colors is None:
                        colors = xp.full(n, -1, dtype=xp.int8)
                    painted = xp.where(mask, xp.int8(int(action.flag)), xp.int8(-1))
                    outcome.flags[action.criterion] = xp.maximum(colors, painted)
        return outcome


def compile_rules(
    packs: Iterable[RulePack],
    *,
    variables: Iterable[str],
    options: Sequence[str] = (),
    criteria: Sequence[str] = (),
    derived: Mapping[str, str] | None = None,
) -> RuleSet:
    """Validate ``packs`` against the model names and compile them.

    Conditions and values may use derived names (``derived``: name -> formula); they are
    calculated from the current values at each evaluation (``expr.compile_with_derived``).
    Effect rules can only set existing variables. Raises ``RuleError``.
    """
    known = set(variables)
    derived = dict(derived or {})
    option_set, criterion_set = set(options), set(criteria)
    compiled: list[CompiledRule] = []
    seen: set[str] = set()
    for pack in packs:
        for rule in pack.rules:
            key = f"{pack.pack}:{rule.id}"
            if rule.id in seen:
                raise RuleError(f"{key}: rule id is already used by another pack")
            seen.add(rule.id)
            if rule.options is not None and option_set and set(rule.options) - option_set:
                raise RuleError(f"{key}: unknown options {sorted(set(rule.options) - option_set)}")
            conditions = tuple(
                _compile(text, known, f"{key} condition", derived) for text, _ in rule.branches()
            )
            actions = tuple(
                _compile_action(a, known, criterion_set, key, derived) for _, a in rule.branches()
            )
            otherwise = (
                _compile_action(rule.else_, known, criterion_set, key, derived) if rule.else_ else None
            )
            compiled.append(CompiledRule(rule, conditions, actions, otherwise))
    return RuleSet(tuple(compiled))


def _compile(
    text: str, known: set[str], where: str, derived: Mapping[str, str] | None = None
) -> CompiledFormula:
    try:
        formula = compile_with_derived(text, derived)
    except (FormulaError, ValueError) as exc:
        raise RuleError(f"{where}: {exc}") from exc
    unknown = formula.names - known
    if unknown:
        raise RuleError(f"{where}: unknown variables {sorted(unknown)} in {text!r}")
    return formula


def _compile_action(
    action: Action, known: set[str], criteria: set[str], key: str, derived: Mapping[str, str] | None = None
) -> _CompiledAction:
    sets = []
    for target, text in action.formulas().items():
        if derived and target in derived:
            raise RuleError(f"{key}: `set` cannot change derived value {target!r}; set its variables")
        if target not in known:
            raise RuleError(f"{key}: `set` targets unknown variable {target!r}")
        sets.append((target, _compile(text, known, f"{key} set {target}", derived)))
    if action.criterion is not None and criteria and action.criterion not in criteria:
        raise RuleError(f"{key}: flag uses unknown criterion {action.criterion!r}")
    flag = RagColor.parse(action.flag) if action.flag else None
    return _CompiledAction(tuple(sets), action.fail, flag, action.criterion)


def summarize_masks(masks: dict[str, Any]) -> dict[str, float]:
    """Rule id -> share of scenarios where the mask is true (host floats)."""
    return {key: float(np.asarray(_host(mask)).mean()) for key, mask in masks.items()}


def _host(array: Any) -> Any:
    get = getattr(array, "get", None)
    return get() if callable(get) else array
