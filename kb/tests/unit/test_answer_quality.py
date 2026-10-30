"""Answer quality rules without infra: quoted names, plausible subjects, the asked field, «nothing names X»."""
from __future__ import annotations

from datetime import datetime

from kb.reasoning.answer import compose
from kb.reasoning.execute import plausible, question_names
from kb.reasoning.plan import Vocabulary, plan_rules
from kb.reasoning.router import parse_rules, quotes, unquoted
from kb.reasoning.timeparse import TimeInterpretation, resolve_time
from kb.reasoning.tools import ToolContext

NOW = datetime.fromisoformat("2026-10-26T12:00:00+00:00")
TZ = "Europe/London"
V = Vocabulary(
    "t", kinds={k: {"label": l, "count": 10} for k, l in [("Deal", "Deal"), ("Payment", "Payment"), ("Email", "Email"),
                                                        ("Story", "Story"), ("Report", "Report")]},
    predicates={"caused_by": {"label": "caused by", "domain": ["Deal"], "range": ["Payment"], "causal": "caused_by", "count": 9}},
    states={}, state_fields={})


def test_quoted_names_are_found_and_masked():
    q = "On 2025-11-06, which stage was the deal 'New business: Granite Payments' in?"
    assert quotes(q) == ["New business: Granite Payments"]
    assert "Payments" not in unquoted(q)
    assert quotes("Did Calder Insurance's contract end?") == []  # an apostrophe in a word is not a quote


def test_a_kind_word_inside_a_quoted_name_is_not_a_kind():
    q = "On 2025-11-06, which stage was the deal 'New business: Granite Payments' in?"
    p = plan_rules(q, now=NOW, tz=TZ, vocab=V, ti=resolve_time(q, NOW, tz=TZ))
    assert p.operation != "list" and "Payment" not in p.kinds
    assert parse_rules(q).kind_hints == ["Deal"]


def test_a_plural_kind_with_a_quote_is_still_a_list():
    q = "Which emails mention 'Granite Payments'?"
    p = plan_rules(q, now=NOW, tz=TZ, vocab=V, ti=resolve_time(q, NOW, tz=TZ))
    assert p.operation == "list" and p.kinds == ["Email"]


def test_question_names_skip_first_word_months_and_kinds():
    assert question_names("Did we sign a contract with Quillfeather Aerospace?") == ["quillfeather", "aerospace"]
    assert question_names("When did we open the office in Reykjavik?") == ["reykjavik"]
    assert question_names("Why did incidents rise in December?") == []
    assert question_names("Who is the account manager for Northgate Academy?") == ["northgate", "academy"]


def _cand(label, matched=("semantic",), description=""):
    return {"id": "x", "label": label, "description": description, "matched": list(matched), "score": 0.5}


def test_a_semantic_near_miss_is_not_the_subject():
    q = parse_rules("Did we sign a contract with Quillfeather Aerospace?")
    names = question_names(q.question)
    assert not plausible(_cand("Fairhaven Academy: platform services agreement", ("kind Contract", "semantic")), q, names)
    q = parse_rules("When did we open the office in Reykjavik?")
    assert not plausible(_cand("Opening balances", ("title", "semantic")), q, question_names(q.question))
    q = parse_rules("Who approved the purchase of the corporate jet?")
    assert not plausible(_cand("Approved: backfill Frontend Engineer", ("title",)), q, question_names(q.question))


def test_true_subjects_stay_plausible():
    q = parse_rules("Why did Calder Insurance give notice on its contract?")
    assert plausible(_cand("Calder Insurance: platform services agreement", ("title", "semantic"), "Contract CTR-0008"),
                     q, question_names(q.question))
    q = parse_rules("Why did incidents rise?")
    assert plausible(_cand("Service incident on 09 January: resolved"), q, question_names(q.question))
    q = parse_rules("Did invoice INV-00460 become overdue?")
    assert plausible(_cand("Invoice INV-00460", ("key INV-00460",)), q, [])


class _Ctx(ToolContext):
    def __init__(self):  # noqa: D401 - a context without a database for the composer
        self.tz, self.known_at, self.now = TZ, None, NOW


def test_the_asked_field_comes_first_and_names_its_record():
    parsed = parse_rules("Who is the account manager for Northgate Academy?")
    asked = [{"field": "owner_name", "value": "Marcus Delaney", "entity_id": "sim.CTR-0009",
              "title": "Northgate Academy: platform services agreement", "version_id": "sim.CTR-0009@1",
              "effective_from": "2025-10-27T06:00:00+00:00"}]
    out = compose(parsed, {"id": "org.CL-009", "label": "Northgate Academy", "kind": "Organization", "matched": ["title"]},
                  [], {"asked": asked}, TimeInterpretation(), _Ctx())
    first = out["text"].splitlines()[0]
    assert first.startswith("Answer: owner name of") and "Marcus Delaney" in first and "sim.CTR-0009@1" in first
    assert "sim.CTR-0009@1" in out["citations"]


def test_nothing_names_a_missing_object():
    parsed = parse_rules("Did we sign a contract with Quillfeather Aerospace?")
    parsed.missing = ["quillfeather", "aerospace"]
    out = compose(parsed, None, [], {"search": []}, TimeInterpretation(), _Ctx())
    assert out["text"].startswith("Nothing in the sources names")
