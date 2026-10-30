"""Tiered resolver for small typed decisions (choice, score, noul).

Order: T0 local function -> disk cache -> backends (T1 Nemotron Lightning by default).
The first answer with ``confidence >= threshold`` wins. If no tier is confident, the
most confident answer comes back with ``escalate=True``, so the caller can ask a
bigger model or the user. Only confident LLM answers go into the cache.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

DEFAULT_THRESHOLD = 0.8


class TypedQuestion(BaseModel):
    kind: Literal["choice", "score", "noul"]
    question: str
    choices: list[str] = Field(default_factory=list, description="choice: the options")
    scale: tuple[int, int] = Field(default=(1, 5), description="score: integer range")
    state: dict[str, Any] | str = Field(default_factory=dict, description="Data the answer depends on")
    task: str = Field(default="general", description="Cache namespace, for example 'column_map'")

    @model_validator(mode="after")
    def _check(self) -> TypedQuestion:
        if self.kind == "choice" and len(self.choices) < 2:
            raise ValueError("a choice question needs at least 2 choices")
        if self.kind == "score" and self.scale[0] >= self.scale[1]:
            raise ValueError("score scale must be (low, high) with low < high")
        return self

    def key(self, backend: str) -> str:
        body = self.model_dump(mode="json", exclude={"task"})
        raw = json.dumps([backend, self.task, body], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Decision(BaseModel):
    value: str | float
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    tier: str = Field(description="t0, cache or a backend name")
    escalate: bool = False


class DecisionBackend(Protocol):
    name: str

    def decide(self, question: TypedQuestion) -> Decision: ...


class BackendUnavailable(RuntimeError):
    """The backend is not configured (no key) or not verified; the resolver skips it."""


class DecisionCache:
    """One JSON file per answer under ``root``."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def get(self, key: str) -> Decision | None:
        path = self.root / f"{key}.json"
        if not path.is_file():
            return None
        try:
            return Decision.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def put(self, key: str, decision: Decision) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{key}.json").write_text(decision.model_dump_json(), encoding="utf-8")


LocalRule = Callable[[TypedQuestion], Decision | None]


class Resolver:
    def __init__(
        self,
        backends: Sequence[DecisionBackend] = (),
        *,
        threshold: float = DEFAULT_THRESHOLD,
        cache: DecisionCache | None = None,
    ):
        self.backends = list(backends)
        self.threshold = threshold
        self.cache = cache
        self.log: list[tuple[str, str, float]] = []  # (task, tier, confidence)

    def decide(self, question: TypedQuestion, local: LocalRule | None = None) -> Decision:
        candidates: list[Decision] = []
        if local is not None:
            answer = local(question)
            if answer is not None:
                answer.tier = "t0"
                if answer.confidence >= self.threshold:
                    return self._done(question, answer)
                candidates.append(answer)
        for backend in self.backends:
            key = question.key(backend.name)
            if self.cache is not None and (hit := self.cache.get(key)) is not None:
                hit.tier = "cache"
                return self._done(question, hit)
            try:
                answer = backend.decide(question)
            except BackendUnavailable:
                continue
            answer.tier = backend.name
            if answer.confidence >= self.threshold:
                if self.cache is not None:
                    self.cache.put(key, answer)
                return self._done(question, answer)
            candidates.append(answer)
        if not candidates:
            raise BackendUnavailable(f"no tier could answer {question.task!r}")
        best = max(candidates, key=lambda d: d.confidence)
        best.escalate = True
        return self._done(question, best)

    def _done(self, question: TypedQuestion, decision: Decision) -> Decision:
        self.log.append((question.task, decision.tier, decision.confidence))
        return decision


def match_choice(question: TypedQuestion) -> Decision | None:
    """T0 rule: pick the choice most similar to the question text or ``state['text']``.

    Confidence is the ``difflib`` similarity ratio; exact matches (case and '_'/' '
    insensitive) give 1.0. Useful to map a column name to a variable name.
    """
    if question.kind != "choice":
        return None
    text = (
        question.state.get("text", question.question) if isinstance(question.state, dict) else question.state
    )
    norm = _norm(str(text))
    scores = {
        c: (1.0 if _norm(c) == norm else difflib.SequenceMatcher(None, norm, _norm(c)).ratio())
        for c in question.choices
    }
    best = max(scores, key=scores.__getitem__)
    total = sum(scores.values()) or 1.0
    return Decision(
        value=best,
        probabilities={c: s / total for c, s in scores.items()},
        confidence=scores[best],
        tier="t0",
    )


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("_", " ").replace("-", " ").split())
