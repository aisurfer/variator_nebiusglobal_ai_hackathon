"""T1 backends for typed decisions.

``NemotronBackend`` (default): Lightning, reasoning off, a short JSON answer. The model
states its probabilities in the JSON. # ASSUMPTION: Token Factory ``logprobs`` support is
not verified, so the backend does not use them yet.

``TypeSafeBackend``: TypeSafe Jev. Not used during the hackathon (Nemotron is required).
The API is not verified, so the backend raises ``BackendUnavailable``. See TODO-2 in
docs/plan/implementation-plan.md.
"""

from __future__ import annotations

import json
import os

from pydantic import BaseModel, Field

from variator.llm import TokenFactoryClient
from variator.router.core import BackendUnavailable, Decision, TypedQuestion

_SYSTEM = (
    "You make one small decision for a decision-analysis tool. Use only the given state. "
    "Give honest, calibrated probabilities that sum to 1."
)


class _ChoiceAnswer(BaseModel):
    probabilities: dict[str, float] = Field(description="Every choice -> probability")


class _ScoreAnswer(BaseModel):
    probabilities: dict[str, float] = Field(description="Every integer score (as a string) -> probability")


class _NoulAnswer(BaseModel):
    truth: float = Field(ge=0, le=1, description="Probability that the statement is true")


class NemotronBackend:
    name = "nemotron"

    def __init__(
        self, client: TokenFactoryClient | None = None, *, model: str = "lightning", max_tokens: int = 300
    ):
        self._client = client
        self.model = model
        self.max_tokens = max_tokens

    @property
    def client(self) -> TokenFactoryClient:
        if self._client is None:
            try:
                self._client = TokenFactoryClient()
            except Exception as exc:  # missing key -> skip the tier
                raise BackendUnavailable(f"Token Factory is not configured: {exc}") from exc
        return self._client

    def decide(self, question: TypedQuestion) -> Decision:
        state = (
            question.state
            if isinstance(question.state, str)
            else json.dumps(question.state, ensure_ascii=False)
        )
        prompt = f"Question: {question.question}\nState: {state[:6000]}"
        if question.kind == "choice":
            prompt += f"\nChoices: {json.dumps(question.choices, ensure_ascii=False)}"
            answer = self._ask(prompt, _ChoiceAnswer)
            probs = _normalized({c: answer.probabilities.get(c, 0.0) for c in question.choices})
            best = max(probs, key=probs.__getitem__)
            return Decision(value=best, probabilities=probs, confidence=probs[best], tier=self.name)
        if question.kind == "score":
            low, high = question.scale
            labels = [str(i) for i in range(low, high + 1)]
            prompt += f"\nScores: integers {low}..{high}"
            answer = self._ask(prompt, _ScoreAnswer)
            probs = _normalized({s: answer.probabilities.get(s, 0.0) for s in labels})
            best = max(probs, key=probs.__getitem__)
            return Decision(value=float(best), probabilities=probs, confidence=probs[best], tier=self.name)
        answer = self._ask(
            prompt + "\nGive the probability that the question statement is true.", _NoulAnswer
        )
        return Decision(
            value=answer.truth,
            probabilities={"true": answer.truth, "false": 1 - answer.truth},
            confidence=abs(2 * answer.truth - 1),
            tier=self.name,
        )

    def _ask(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        return self.client.structured(
            self.model,
            [{"role": "user", "content": prompt}],
            schema,
            reasoning=False,
            temperature=0.0,
            max_tokens=self.max_tokens,
            system=_SYSTEM,
        )


class TypeSafeBackend:
    """Placeholder for TypeSafe Jev (hosted, early access). Always unavailable for now."""

    name = "typesafe"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")

    def decide(self, question: TypedQuestion) -> Decision:
        # ASSUMPTION: no verified API contract yet (docs.typesafe.ai gives no endpoint schema).
        raise BackendUnavailable("TypeSafe Jev backend is not implemented (post-hackathon, TODO-2)")


def _normalized(probs: dict[str, float]) -> dict[str, float]:
    clipped = {k: max(float(v), 0.0) for k, v in probs.items()}
    total = sum(clipped.values())
    if total <= 0:
        return {k: 1 / len(clipped) for k in clipped}
    return {k: v / total for k, v in clipped.items()}
