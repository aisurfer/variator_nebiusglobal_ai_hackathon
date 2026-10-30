"""Token and cost accounting across all Token Factory calls."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from variator.llm.models import ModelRegistry

# (model_id, usage, cost_usd or None, duration_s or None); see variator.telemetry
UsageListener = Callable[[str, "Usage", float | None, float | None], None]


@dataclass
class Usage:
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ModelUsage:
    calls: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = 0.0


class UsageLedger:
    """Thread-safe running totals per model id. Cost is None for unknown models."""

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        self._registry = registry or ModelRegistry()
        self._lock = threading.Lock()
        self._by_model: dict[str, ModelUsage] = {}
        self.listeners: list[UsageListener] = []

    def record(self, model_id: str, usage: Usage, *, duration_s: float | None = None) -> None:
        spec = self._registry.get(model_id)
        cost = spec.cost(usage.prompt_tokens, usage.cached_tokens, usage.completion_tokens) if spec else None
        for listener in list(self.listeners):
            listener(model_id, usage, cost, duration_s)
        with self._lock:
            entry = self._by_model.setdefault(model_id, ModelUsage())
            entry.calls += 1
            entry.usage.prompt_tokens += usage.prompt_tokens
            entry.usage.cached_tokens += usage.cached_tokens
            entry.usage.completion_tokens += usage.completion_tokens
            if spec is None:
                entry.cost_usd = None
            elif entry.cost_usd is not None:
                entry.cost_usd += cost

    def by_model(self) -> dict[str, ModelUsage]:
        with self._lock:
            return dict(self._by_model)

    def total_cost(self) -> float:
        return sum(m.cost_usd or 0.0 for m in self.by_model().values())
