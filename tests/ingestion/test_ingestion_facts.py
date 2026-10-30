import json
import re

import httpx
import pytest

from variator.domain import Fact, FactOrigin
from variator.ingestion import (
    chunk_text,
    dedupe_facts,
    facts_from_profile,
    facts_from_text,
    profile_csv,
)
from variator.ingestion.facts import DOCUMENT_MAX_CONFIDENCE
from variator.ingestion.grounding import UNVERIFIED_CONFIDENCE, NumberIndex
from variator.llm import LIGHTNING, StructuredOutputError
from variator.llm.testing import chat_completion

CSV = "\n".join(
    ["month,revenue,region"]
    + [f"2024-{m:02d}-01,{1000 * m},North" for m in range(1, 13)]
    + [f"2025-{m:02d}-01,{1100 * m},South" for m in range(1, 13)]
)


@pytest.fixture
def profile():
    return profile_csv(CSV, name="sales.csv")


def draft(statement, value=None, unit=None, column=None, confidence=0.8, period=None):
    return {"statement": statement, "value": value, "unit": unit, "column": column,
            "confidence": confidence, "period": period}


def test_profile_facts_request_shape(fake_tf, profile):
    fake_tf.add_json_reply({"facts": []})
    facts_from_profile(fake_tf.client(), profile, max_facts=7)
    request = fake_tf.requests[0]
    assert request["model"] == LIGHTNING.model_id
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["name"] == "DataFactList"
    assert request["chat_template_kwargs"] == {"enable_thinking": False}
    system, user = request["messages"]
    assert system["role"] == "system" and '"column"' in system["content"]
    assert user["role"] == "user"
    # static instructions first (prefix caching), then the dynamic profile
    assert user["content"].startswith("You turn a statistical profile")
    assert "Return at most 7 facts." in user["content"]
    assert user["content"].rstrip().endswith(profile.to_prompt().splitlines()[-1])


def test_profile_facts_are_grounded_sourced_and_capped(fake_tf, profile):
    t = profile.trend("revenue")
    growth_pct = round(t.yoy_growth * 100, 1)
    fake_tf.add_json_reply(
        {
            "facts": [
                draft("Revenue in the last 12 months was 85,800.", t.trailing_year, "USD", "revenue", 1.0),
                draft(f"Revenue grew {growth_pct}% year over year.", growth_pct, "%", "revenue"),
                draft("Revenue will reach 1,000,000 next year.", 1_000_000, "USD", "revenue"),  # invented
                draft("Sales moved from North to South.", None, None, "region", 0.9),
                draft("Unknown column fact with total 163,800.", 163_800, "USD", "no_such_column"),
            ]
        }
    )
    facts = facts_from_profile(fake_tf.client(), profile)
    assert [f.value for f in facts] == [t.trailing_year, growth_pct, None, 163_800]
    assert all(f.origin == FactOrigin.DATA for f in facts)
    sources = ["sales.csv:revenue", "sales.csv:revenue", "sales.csv:region", "sales.csv"]
    assert [f.source for f in facts] == sources
    assert facts[0].confidence == 0.95  # capped for data values
    assert facts[2].confidence == 0.7  # qualitative data facts are capped lower


def test_profile_facts_empty_profile_makes_no_call(fake_tf):
    assert facts_from_profile(fake_tf.client(), profile_csv("a,b\n", name="e.csv")) == []
    assert fake_tf.requests == []


def test_profile_facts_downgrade_policy(fake_tf, profile):
    fake_tf.add_json_reply({"facts": [draft("Revenue will reach 1,000,000.", 1_000_000, "USD")]})
    facts = facts_from_profile(fake_tf.client(), profile, on_unverified="downgrade")
    assert len(facts) == 1 and facts[0].confidence == UNVERIFIED_CONFIDENCE


def _messy_row(month: int, day: int) -> str:
    umsatz = f"{day * month * 1234.5:,.2f}".replace(",", " ").replace(".", ",")  # 1 234,50
    return f"{day:02d}.{month:02d}.2025;Store {day % 3};{umsatz} \u20ac;{day % 7},5%;Note {day * month}"


MESSY_CSV = "\n".join(
    ["Report 2025;;;", "Datum;Filiale;Umsatz;Rabatt;Kommentar"]
    + [_messy_row(m, d) for m in range(1, 13) for d in (1, 15, 28)]
    + ["Total;;999 999,00 \u20ac;;"]
)


@pytest.mark.parametrize("csv_text", [CSV, MESSY_CSV], ids=["clean", "messy"])
def test_every_number_rendered_in_the_prompt_passes_the_guard(csv_text):
    """Values the model copies from the prompt (rounded to >= 3 digits) must stay within 1%."""
    profile = profile_csv(csv_text, name="sales_2025.csv")
    index = NumberIndex(profile.grounding_numbers())
    prompt = profile.to_prompt()
    rendered = [float(x.replace(",", "")) for x in re.findall(r"(?<![\w.-])\d[\d,]*(?:\.\d+)?", prompt)]
    assert len(rendered) > 40
    missing = [v for v in rendered if not index.contains(v)]
    assert missing == []


def test_profile_facts_propagates_structured_output_error(fake_tf, profile):
    fake_tf.add_reply(content="not json").add_reply(content="still not json")
    with pytest.raises(StructuredOutputError):
        facts_from_profile(fake_tf.client(), profile)


def test_profile_facts_repairs_invalid_json_once(fake_tf, profile):
    fake_tf.add_reply(content='{"facts": [{"value": 1}]}')  # statement missing
    fake_tf.add_json_reply({"facts": [draft("There are 24 rows.", 24)]})
    facts = facts_from_profile(fake_tf.client(), profile)
    assert [f.value for f in facts] == [24]
    assert len(fake_tf.requests) == 2


def test_confidence_out_of_range_is_clamped(fake_tf, profile):
    fake_tf.add_json_reply({"facts": [draft("There are 24 rows.", 24, confidence=7)]})
    assert facts_from_profile(fake_tf.client(), profile)[0].confidence == 0.95


# ---------------------------------------------------------------- chunking


def test_chunk_text_short_and_empty():
    assert chunk_text("  ") == []
    assert chunk_text("hello", chunk_chars=500) == ["hello"]


def test_chunk_text_respects_limit_paragraphs_and_overlap():
    paragraphs = [f"Paragraph {i}. " + "word " * 40 for i in range(30)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, chunk_chars=1000, overlap_chars=300)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    for paragraph in paragraphs:  # nothing lost, paragraphs never split
        assert any(paragraph.strip() in c for c in chunks)
    # consecutive chunks share the overlap paragraph
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a.split("\n\n")[-1] == b.split("\n\n")[0]


def test_chunk_text_splits_oversize_paragraph_by_sentences_then_hard():
    long_paragraph = " ".join(f"Sentence number {i} is here." for i in range(200))
    chunks = chunk_text(long_paragraph, chunk_chars=500, overlap_chars=0)
    assert all(len(c) <= 500 for c in chunks)
    assert chunks[0].endswith(".")
    blob = "x" * 1200
    assert [len(c) for c in chunk_text(blob, chunk_chars=500, overlap_chars=0)] == [500, 500, 200]


def test_chunk_text_validates_arguments():
    with pytest.raises(ValueError):
        chunk_text("x", chunk_chars=100)
    with pytest.raises(ValueError):
        chunk_text("x", chunk_chars=1000, overlap_chars=600)


# ---------------------------------------------------------------- dedupe


def doc_fact(statement, value=None, confidence=0.7, period=None):
    return Fact(statement=statement, value=value, period=period, origin=FactOrigin.DOCUMENT,
                confidence=confidence)


def test_dedupe_facts():
    facts = [
        doc_fact("Revenue in 2025 was 4.2 million EUR.", 4.2, 0.6),
        doc_fact("revenue in 2025 was 4.2 million eur", 4.2, 0.8),  # same words, more confident
        doc_fact("In 2025 the revenue was EUR 4.2 million.", 4.2),  # reworded, same value
        doc_fact("Revenue in 2024 was 4.2 million EUR.", 4.2, period="2024"),
        doc_fact("Headcount in 2025 was 4.2 thousand.", 4.2),  # same value, different fact
        doc_fact("Revenue in 2025 was 3.9 million EUR.", 3.9),
    ]
    out = dedupe_facts(facts)
    assert [f.statement for f in out] == [
        "revenue in 2025 was 4.2 million eur",
        "Revenue in 2024 was 4.2 million EUR.",
        "Headcount in 2025 was 4.2 thousand.",
        "Revenue in 2025 was 3.9 million EUR.",
    ]


# ---------------------------------------------------------------- text facts


REPORT = (
    "ACME GmbH annual report 2025.\n\n"
    "Revenue reached EUR 4.2 million in 2025, up 12.5% on 2024.\n\n"
    + "Filler text about the company history and values. " * 40
    + "\n\nThe board approved a capex budget of EUR 1.5 million for 2026.\n\n"
    + "More filler about sustainability. " * 40
    + "\n\nGross margin was 38,4 % in 2025."
)


def test_text_facts_chunked_grounded_deduped(fake_tf):
    chunks = chunk_text(REPORT, chunk_chars=1500)
    assert len(chunks) >= 2
    replies = []
    for chunk in chunks:
        facts = []
        revenue = "ACME revenue was EUR 4.2 million in 2025."
        if "4.2 million" in chunk:
            facts.append(draft(revenue, 4.2, "million EUR", confidence=0.9))
            facts.append(draft("ACME revenue grew 12.5% in 2025.", 12.5, "%"))
        if "1.5 million" in chunk:
            facts.append(draft("ACME approved a 2026 capex budget of EUR 1.5 million.", 1_500_000, "EUR"))
            facts.append(draft(revenue, 4.2, "million EUR"))  # duplicate from another chunk
            facts.append(draft("ACME plans to hire 300 people.", 300))  # not in the text
        if "38,4" in chunk:
            facts.append(draft("ACME gross margin was 38.4% in 2025.", 38.4, "%"))
        replies.append({"facts": [{k: v for k, v in f.items() if k != "column"} for f in facts]})
    for reply in replies:
        fake_tf.add_json_reply(reply)

    facts = facts_from_text(fake_tf.client(), REPORT, source="report_2025.pdf", chunk_chars=1500)

    assert len(fake_tf.requests) == len(chunks)
    assert [f.value for f in facts] == [4.2, 12.5, 1_500_000, 38.4]
    assert all(f.origin == FactOrigin.DOCUMENT and f.source == "report_2025.pdf" for f in facts)
    assert facts[0].confidence == DOCUMENT_MAX_CONFIDENCE  # 0.9 capped
    first = fake_tf.requests[0]
    assert first["model"] == LIGHTNING.model_id
    assert first["response_format"]["json_schema"]["name"] == "FactList"
    assert first["chat_template_kwargs"] == {"enable_thinking": False}
    user = first["messages"][1]["content"]
    assert user.startswith("You extract facts")
    assert f"Source: report_2025.pdf (part 1 of {len(chunks)})" in user
    assert "<excerpt>" in user


def test_text_facts_empty_text_makes_no_call(fake_tf):
    assert facts_from_text(fake_tf.client(), "   ", source="x") == []
    assert fake_tf.requests == []


def test_text_facts_error_propagates(fake_tf):
    fake_tf.add_reply(content="oops").add_reply(content="nope")
    with pytest.raises(StructuredOutputError):
        facts_from_text(fake_tf.client(), "Revenue was 5.", source="x")


def test_text_facts_concurrent_keeps_document_order(fake_tf):
    text = "\n\n".join(f"Section {i}: revenue of unit {i} was {i * 1000 + 7} EUR. " + "pad " * 80
                       for i in range(1, 5))
    chunks = chunk_text(text, chunk_chars=500, overlap_chars=0)
    assert len(chunks) == 4

    def responder(body):
        content = body["messages"][1]["content"]
        part = int(re.search(r"part (\d+) of", content).group(1))
        value = part * 1000 + 7
        payload = {"facts": [{"statement": f"Unit {part} revenue was {value} EUR.", "value": value}]}
        return httpx.Response(200, json=chat_completion(content=json.dumps(payload)))

    for _ in chunks:
        fake_tf.add_responder(responder)
    facts = facts_from_text(fake_tf.client(), text, source="s", chunk_chars=500, overlap_chars=0,
                            max_workers=4)
    assert [f.value for f in facts] == [1007, 2007, 3007, 4007]
