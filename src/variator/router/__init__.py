"""Tiered resolver: T0 local -> cache -> T1 typed decision (Nemotron Lightning)."""

from variator.router.backends import NemotronBackend, TypeSafeBackend
from variator.router.core import (
    DEFAULT_THRESHOLD,
    BackendUnavailable,
    Decision,
    DecisionBackend,
    DecisionCache,
    Resolver,
    TypedQuestion,
    match_choice,
)

__all__ = [
    "DEFAULT_THRESHOLD",
    "BackendUnavailable",
    "Decision",
    "DecisionBackend",
    "DecisionCache",
    "NemotronBackend",
    "Resolver",
    "TypeSafeBackend",
    "TypedQuestion",
    "match_choice",
]
