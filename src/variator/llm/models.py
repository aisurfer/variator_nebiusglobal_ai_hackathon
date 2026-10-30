"""Registry of NVIDIA Nemotron models served by Nebius Token Factory.

Ids, context sizes and prices were checked in the Token Factory catalog on
2026-10-24 (see docs/research). Ids are case-sensitive; verify them with
``GET /v1/models`` and override via ``VARIATOR_MODEL_<KEY>`` if they change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ModelSpec:
    key: str
    model_id: str
    context_tokens: int
    # USD per 1M tokens; cached is None when the catalog lists no cached price
    price_input: float
    price_cached: float | None
    price_output: float
    vision: bool = False
    purpose: str = ""

    def cost(self, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float:
        uncached = max(prompt_tokens - cached_tokens, 0)
        cached_price = self.price_cached if self.price_cached is not None else self.price_input
        return (
            uncached * self.price_input
            + cached_tokens * cached_price
            + completion_tokens * self.price_output
        ) / 1_000_000


LIGHTNING = ModelSpec(
    key="lightning",
    model_id="nvidia/Nemotron-3_5-Lightning",
    context_tokens=1_024_000,
    price_input=0.06,
    price_cached=None,
    price_output=0.24,
    purpose="fast everyday calls: extraction, classification, tool-loop steps, chat",
)
SUPER = ModelSpec(
    key="super",
    model_id="nvidia/nemotron-3-super-120b-a12b",
    context_tokens=256_000,
    price_input=0.30,
    price_cached=0.06,
    price_output=0.90,
    purpose="mid-level reasoning: build the decision model, list risks, critique",
)
ULTRA = ModelSpec(
    key="ultra",
    model_id="nvidia/Nemotron-3-Ultra-550b-a55b",
    context_tokens=1_024_000,
    price_input=1.00,
    price_cached=0.12,
    price_output=3.00,
    purpose="deep reasoning: interpret simulations, recommend, red-team assumptions",
)
SUPER_VL = ModelSpec(
    key="super_vl",
    model_id="nvidia/Nemotron-3.5-Super-VL",
    context_tokens=1_024_000,
    price_input=0.30,
    price_cached=0.06,
    price_output=0.90,
    vision=True,
    purpose="read charts, scanned reports and dashboard screenshots",
)
NANO = ModelSpec(
    key="nano",
    model_id="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    context_tokens=262_000,
    price_input=0.06,
    price_cached=None,
    price_output=0.24,
    purpose="fallback for lightning (same price, slower)",
)

DEFAULT_MODELS: tuple[ModelSpec, ...] = (LIGHTNING, SUPER, ULTRA, SUPER_VL, NANO)

# Which model does each LLM role (slide 9 of the submission): Ultra reasons where depth decides everything
# downstream, Super builds and critiques, Lightning writes and extracts at volume. Each call site takes its
# default from here.
ROLE_MODEL: Mapping[str, str] = {
    "actor_cards": ULTRA.key,  # extract the actor cards from the records (the world model)
    "case_design": ULTRA.key,  # design the case from a question: forks, metric, horizon, constraints
    "analysis": ULTRA.key,  # interpret the simulations, recommend, red-team, explain a decision
    "decision_model": SUPER.key,  # build the decision model of a designed case
    "critique": SUPER.key,  # list risks and critique a draft
    "agent_loop": SUPER.key,  # the agent's tool loop over the MCP tools
    "narrative": LIGHTNING.key,  # the actor swarm: a year of mail, tickets and notes
    "extraction": LIGHTNING.key,  # facts from documents, charts and web research
}


class ModelRegistry:
    """Resolves a model key ("ultra") or raw id to a ``ModelSpec``."""

    def __init__(self, overrides: Mapping[str, str] | None = None) -> None:
        overrides = overrides or {}
        self._by_key = {
            spec.key: replace(spec, model_id=overrides.get(spec.key, spec.model_id))
            for spec in DEFAULT_MODELS
        }
        self._by_id = {spec.model_id: spec for spec in self._by_key.values()}

    def get(self, key_or_id: str) -> ModelSpec | None:
        return self._by_key.get(key_or_id) or self._by_id.get(key_or_id)

    def model_id(self, key_or_id: str) -> str:
        spec = self.get(key_or_id)
        return spec.model_id if spec else key_or_id

    def __iter__(self):
        return iter(self._by_key.values())
