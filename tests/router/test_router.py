import pytest

from variator.llm.testing import FakeTokenFactory
from variator.router import (
    BackendUnavailable,
    Decision,
    DecisionCache,
    NemotronBackend,
    Resolver,
    TypedQuestion,
    TypeSafeBackend,
    match_choice,
)


def _question(**kw) -> TypedQuestion:
    base = {
        "kind": "choice",
        "question": "Which variable is 'Blended Rate'?",
        "choices": ["rate", "team_size"],
    }
    return TypedQuestion(**{**base, **kw})


def test_t0_match_is_used_when_confident():
    resolver = Resolver([])
    decision = resolver.decide(_question(state={"text": "rate"}), local=match_choice)
    assert decision.value == "rate" and decision.tier == "t0" and decision.confidence == 1.0


def test_escalates_to_nemotron_and_caches(tmp_path):
    fake = FakeTokenFactory()
    fake.add_json_reply({"probabilities": {"rate": 0.9, "team_size": 0.1}})
    resolver = Resolver([NemotronBackend(fake.client())], cache=DecisionCache(tmp_path))
    q = _question(state={"text": "blended cost per FTE"})
    first = resolver.decide(q, local=match_choice)
    assert (first.value, first.tier) == ("rate", "nemotron")
    assert fake.requests[0]["model"] == "nvidia/Nemotron-3_5-Lightning"
    assert fake.requests[0]["chat_template_kwargs"]["enable_thinking"] is False
    second = resolver.decide(q, local=match_choice)
    assert second.tier == "cache" and len(fake.requests) == 1


def test_low_confidence_returns_best_with_escalate_flag():
    fake = FakeTokenFactory()
    fake.add_json_reply({"probabilities": {"rate": 0.55, "team_size": 0.45}})
    decision = Resolver([NemotronBackend(fake.client())]).decide(_question())
    assert decision.escalate and decision.value == "rate"


def test_noul_and_score():
    fake = FakeTokenFactory()
    fake.add_json_reply({"truth": 0.95})
    fake.add_json_reply({"probabilities": {"1": 0, "2": 0, "3": 0.1, "4": 0.85, "5": 0.05}})
    backend = NemotronBackend(fake.client())
    noul = backend.decide(TypedQuestion(kind="noul", question="Data is monthly"))
    assert noul.value == 0.95 and noul.confidence == pytest.approx(0.9)
    score = backend.decide(TypedQuestion(kind="score", question="Data quality 1-5"))
    assert score.value == 4.0 and score.confidence == pytest.approx(0.85)


def test_typesafe_is_skipped():
    with pytest.raises(BackendUnavailable):
        TypeSafeBackend(api_key="x").decide(_question())
    with pytest.raises(BackendUnavailable):
        Resolver([TypeSafeBackend()]).decide(_question())
    fallback = Resolver([TypeSafeBackend()]).decide(
        _question(), local=lambda q: Decision(value="rate", confidence=0.3, tier="x")
    )
    assert fallback.escalate and fallback.tier == "t0"


def test_question_validation():
    with pytest.raises(ValueError):
        TypedQuestion(kind="choice", question="q", choices=["only"])
