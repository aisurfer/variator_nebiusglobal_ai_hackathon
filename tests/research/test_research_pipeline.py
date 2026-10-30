from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from variator.config import MissingCredentialsError, Settings
from variator.domain import Fact, FactOrigin
from variator.llm import LIGHTNING
from variator.llm.testing import chat_completion
from variator.research import (
    ResearchQuery,
    SearchResponse,
    TavilyAuthError,
    TavilyCache,
    TavilyClient,
    gather_external_facts,
    merge_results,
    normalize_url,
)
from variator.research.pipeline import is_grounded, is_near_duplicate, numbers_in, truncate
from variator.research.prompts import FACT_EXTRACTOR_INSTRUCTIONS, QUERY_PLANNER_INSTRUCTIONS
from variator.research.testing import FakeTavily, search_payload, search_result

QUESTION = "Should we open a second fulfilment warehouse in Poland in 2027?"
TODAY = date(2026, 10, 25)

RENT_Q = "warehouse rent growth Poland 2026"
FEES_Q = "3PL fulfilment fees Europe 2026"
WAGES_Q = "Poland logistics wages 2026"

CBRE = "https://www.cbre.example/poland-rents"
NEWS = "https://news.example/logistics?utm_source=feed&id=7"
FEES = "https://3pl.example/fees-2026"

SEARCH_RESULTS = {
    RENT_Q: [
        search_result(
            CBRE,
            title="Poland industrial market 2026",
            content="Prime warehouse rents in Poland rose 6.5% year over year in 2026, "
            "reaching 4.6 EUR/m2/month.",
            score=0.91,
            published_date="2026-06-30",
        ),
        search_result(
            NEWS, title="Logistics vacancy", content="Vacancy in Polish logistics fell to 7.2% in Q2 2026."
        ),
    ],
    FEES_Q: [
        search_result(
            FEES,
            title="3PL fee survey",
            content="Average 3PL fulfilment fees in Europe increased by 12% in 2026 according to the survey.",
            score=0.88,
        ),
        # Same page as CBRE (no www, trailing slash) with a different query-specific snippet.
        search_result(
            "https://cbre.example/poland-rents/",
            content="Developers expect rent growth to slow to 3% in 2027.",
            score=0.95,
        ),
    ],
}

PLAN = {
    "queries": [
        {"query": RENT_Q, "purpose": "warehouse rent", "topic": "general"},
        {"query": "  warehouse  rent growth POLAND 2026 ", "purpose": "duplicate", "topic": "general"},
        {"query": FEES_Q, "purpose": "3PL fees", "topic": "news"},
        {"query": WAGES_Q, "purpose": "wages", "topic": "general"},
        {"query": "Poland e-commerce growth 2027", "purpose": "demand", "topic": "general"},
    ]
}


def _fact(statement, source_url, evidence, value=None, unit=None, period=None, confidence=0.7):
    return {
        "statement": statement,
        "source_url": source_url,
        "evidence": evidence,
        "value": value,
        "unit": unit,
        "period": period,
        "confidence": confidence,
    }


EXTRACTION = {
    "facts": [
        _fact(
            "Prime warehouse rents in Poland rose 6.5% year over year in 2026.",
            CBRE,
            "Prime warehouse rents in Poland rose 6.5% year over year in 2026",
            6.5,
            "%",
            "2026",
        ),
        _fact(  # cites the block label instead of the URL
            "Average 3PL fulfilment fees in Europe increased by 12% in 2026.",
            "S3",
            "Average 3PL fulfilment fees in Europe increased by 12% in 2026",
            12,
            "%",
            "2026",
        ),
        _fact(  # fabricated source
            "Warehouse rents in Germany rose 9% in 2026.",
            "https://made-up.example/report",
            "rents in Germany rose 9%",
            9,
            "%",
            "2026",
        ),
        _fact(  # near-duplicate of the first, higher confidence, URL without scheme
            "Prime warehouse rents in Poland rose by 6.5% year-over-year in 2026",
            "cbre.example/poland-rents",
            "Prime warehouse rents in Poland rose 6.5% year over year",
            6.5,
            "%",
            "2026",
            0.9,
        ),
        _fact(  # number not in the source
            "Polish logistics vacancy fell to 5% in 2026.",
            NEWS,
            "Vacancy in Polish logistics fell to 7.2% in Q2 2026",
            5,
            "%",
            "2026",
        ),
        _fact(  # evidence not in the source
            "Demand for Polish warehouses will collapse in 2027.",
            CBRE,
            "Analysts forecast a sharp collapse of demand",
        ),
        _fact(  # from the merged second snippet of the same page
            "Developers expect Polish warehouse rent growth to slow to 3% in 2027.",
            CBRE,
            "Developers expect rent growth to slow to 3% in 2027.",
            3,
            "%",
            "2027",
        ),
    ]
}


def _respx_tavily(router):
    def search(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        return httpx.Response(200, json=search_payload(query, SEARCH_RESULTS.get(query, [])))

    return router.post("/search").mock(side_effect=search)


def _seed(fake: FakeTavily) -> FakeTavily:
    for query, results in SEARCH_RESULTS.items():
        fake.add_search(query, results)
    return fake


def test_end_to_end_queries_search_extract_validate(tavily_api, fake_tf):
    route = _respx_tavily(tavily_api)
    fake_tf.add_json_reply(PLAN).add_json_reply(EXTRACTION)
    tavily = TavilyClient(Settings(tavily_api_key="tvly-test-key", max_retries=0))
    context = [
        Fact(statement="Our Lodz warehouse costs 4.1 EUR/m2/month.", value=4.1, origin=FactOrigin.DATA)
    ]

    bundle = gather_external_facts(
        QUESTION,
        tf_client=fake_tf.client(),
        tavily=tavily,
        context_facts=context,
        max_queries=3,
        results_per_query=5,
        today=TODAY,
    )

    # Planner: cleaned, deduplicated, capped at max_queries.
    assert [q.query for q in bundle.queries] == [RENT_Q, FEES_Q, WAGES_Q]
    searches = [json.loads(call.request.content) for call in route.calls]
    assert [s["query"] for s in searches] == [RENT_Q, FEES_Q, WAGES_Q]
    assert [s["topic"] for s in searches] == ["general", "news", "general"]
    assert {(s["max_results"], s["search_depth"]) for s in searches} == {(5, "basic")}

    # Results deduplicated by URL across queries, snippets merged, ids stable.
    assert [(d.source_id, d.url) for d in bundle.results] == [("S1", CBRE), ("S2", NEWS), ("S3", FEES)]
    cbre = bundle.results[0]
    assert cbre.queries == [RENT_Q, FEES_Q]
    assert cbre.score == 0.95
    assert "rose 6.5%" in cbre.content and "[...] Developers expect" in cbre.content

    # Facts: only cited, grounded, deduplicated ones survive.
    assert [f.statement for f in bundle.facts] == [
        "Prime warehouse rents in Poland rose by 6.5% year-over-year in 2026",
        "Average 3PL fulfilment fees in Europe increased by 12% in 2026.",
        "Developers expect Polish warehouse rent growth to slow to 3% in 2027.",
    ]
    assert all(f.origin is FactOrigin.WEB for f in bundle.facts)
    assert [f.source for f in bundle.facts] == [CBRE, FEES, CBRE]
    first = bundle.cited_facts[0]
    assert (first.fact.value, first.fact.unit, first.fact.period, first.fact.confidence) == (
        6.5,
        "%",
        "2026",
        0.9,
    )
    assert first.evidence == "Prime warehouse rents in Poland rose 6.5% year over year"
    assert (first.source_title, first.published_date, first.query) == (
        "Poland industrial market 2026",
        "2026-06-30",
        RENT_Q,
    )
    reasons = {d.statement: d.reason for d in bundle.dropped}
    assert "not among the provided search results" in reasons["Warehouse rents in Germany rose 9% in 2026."]
    assert "numbers not in the source text: 5" in reasons["Polish logistics vacancy fell to 5% in 2026."]
    assert "evidence" in reasons["Demand for Polish warehouses will collapse in 2027."]

    assert bundle.tavily_credits == 3
    assert bundle.tavily_cache_hits == 0
    assert bundle.warnings == []
    assert bundle.model_dump()["facts"][0]["source"] == CBRE

    # Token Factory calls: Lightning, reasoning off, deterministic, schema-constrained.
    planner, extractor = fake_tf.requests
    for request in (planner, extractor):
        assert request["model"] == LIGHTNING.model_id
        assert request["chat_template_kwargs"] == {"enable_thinking": False}
        assert request["temperature"] == 0.0
    assert planner["response_format"]["json_schema"]["name"] == "PlannedQueries"
    assert extractor["response_format"]["json_schema"]["name"] == "ExtractedFacts"
    planner_prompt = planner["messages"][-1]["content"]
    assert planner_prompt.startswith(QUERY_PLANNER_INSTRUCTIONS)  # static prefix first
    assert "Today: 2026-10-25" in planner_prompt and "Maximum number of queries: 3" in planner_prompt
    assert QUESTION in planner_prompt and "Our Lodz warehouse costs 4.1 EUR/m2/month. (4.1)" in planner_prompt
    extraction_prompt = extractor["messages"][-1]["content"]
    assert extraction_prompt.startswith(FACT_EXTRACTOR_INSTRUCTIONS)
    assert f"[S1] Poland industrial market 2026\nURL: {CBRE}\nPublished: 2026-06-30" in extraction_prompt
    assert f"URL: {FEES}" in extraction_prompt


def test_pinned_queries_skip_the_planner(fake_tf, fake_tavily):
    _seed(fake_tavily)
    fake_tf.add_json_reply({"facts": []})
    bundle = gather_external_facts(
        QUESTION,
        tf_client=fake_tf.client(),
        tavily=fake_tavily.client(),
        queries=[RENT_Q, ResearchQuery(query=FEES_Q, topic="finance")],
        time_range="year",
    )
    assert len(fake_tf.requests) == 1  # extraction only
    searches = fake_tavily.endpoint_requests("search")
    assert [(s["query"], s["topic"], s["time_range"]) for s in searches] == [
        (RENT_Q, "general", "year"),
        (FEES_Q, "finance", "year"),
    ]
    assert bundle.facts == []


def test_planner_failure_falls_back_to_the_question(fake_tf, fake_tavily):
    fake_tf.add_reply(content="no json here").add_reply(content="still none")
    bundle = gather_external_facts(QUESTION, tf_client=fake_tf.client(), tavily=fake_tavily.client())
    assert [q.query for q in bundle.queries] == [QUESTION]
    assert fake_tavily.endpoint_requests("search")[0]["query"] == QUESTION
    assert any("query planning failed" in w for w in bundle.warnings)


def test_search_failures_become_warnings(fake_tf, fake_tavily):
    _seed(fake_tavily).fail(RENT_Q, 500).fail(WAGES_Q, 432)
    fake_tf.add_json_reply({"facts": [EXTRACTION["facts"][1] | {"source_url": FEES}]})
    bundle = gather_external_facts(
        QUESTION,
        tf_client=fake_tf.client(),
        tavily=fake_tavily.client(),
        queries=[RENT_Q, FEES_Q, WAGES_Q],
    )
    assert [d.url for d in bundle.results] == [FEES, "https://cbre.example/poland-rents/"]
    assert [f.source for f in bundle.facts] == [FEES]
    assert any("search failed" in w and RENT_Q in w for w in bundle.warnings)
    assert any("credits exhausted" in w and WAGES_Q in w for w in bundle.warnings)
    assert bundle.tavily_credits == 1


def test_auth_errors_propagate(fake_tf, fake_tavily):
    fake_tavily.fail(RENT_Q, 401)
    with pytest.raises(TavilyAuthError):
        gather_external_facts(
            QUESTION, tf_client=fake_tf.client(), tavily=fake_tavily.client(), queries=[RENT_Q]
        )


def test_missing_tavily_key_propagates(fake_tf, fake_tavily):
    tavily = fake_tavily.client(Settings())
    with pytest.raises(MissingCredentialsError, match="TAVILY_API_KEY"):
        gather_external_facts(QUESTION, tf_client=fake_tf.client(), tavily=tavily, queries=[RENT_Q])
    assert fake_tavily.requests == []


def test_no_results_skips_extraction(fake_tf, fake_tavily):
    bundle = gather_external_facts(
        QUESTION, tf_client=fake_tf.client(), tavily=fake_tavily.client(), queries=["nothing here"]
    )
    assert fake_tf.requests == []
    assert bundle.results == [] and bundle.facts == []
    assert "no search results" in bundle.warnings[-1]


def test_long_contents_are_truncated_and_batched(fake_tf, fake_tavily):
    long_text = "Freight rates rose sharply this quarter. " * 200
    fake_tavily.add_search(
        "q", [search_result(f"https://site{i}.example/a", content=long_text) for i in range(5)]
    )
    for _ in range(3):
        fake_tf.add_json_reply({"facts": []})
    bundle = gather_external_facts(
        QUESTION,
        tf_client=fake_tf.client(),
        tavily=fake_tavily.client(),
        queries=["q"],
        batch_size=2,
        max_chars_per_source=300,
        max_workers=1,
    )
    assert len(fake_tf.requests) == 3
    assert all(len(d.content) <= 304 and d.content.endswith(" ...") for d in bundle.results)
    prompts = [r["messages"][-1]["content"] for r in fake_tf.requests]
    assert [p.count("URL: https://site") for p in prompts] == [2, 2, 1]
    assert max(len(p) for p in prompts) < len(FACT_EXTRACTOR_INSTRUCTIONS) + 1200


def test_batch_extraction_failure_is_a_warning(fake_tf, fake_tavily):
    _seed(fake_tavily)
    fake_tf.add_reply(content="garbage").add_reply(content="garbage again")
    bundle = gather_external_facts(
        QUESTION, tf_client=fake_tf.client(), tavily=fake_tavily.client(), queries=[RENT_Q]
    )
    assert bundle.facts == []
    assert any("fact extraction failed for sources S1, S2" in w for w in bundle.warnings)


def test_extract_top_deepens_the_best_result(fake_tf, fake_tavily):
    fake_tavily.add_search(
        "q",
        [
            search_result("https://low.example", content="Short snippet.", score=0.2),
            search_result("https://best.example/report", content="Rents rose.", score=0.9),
        ],
    )
    fake_tavily.add_extract(
        "https://best.example/report",
        "Full report. Prime rents in Warsaw rose 8.1% in 2026 on strong demand.",
    )
    fake_tf.add_json_reply(
        {
            "facts": [
                _fact(
                    "Prime rents in Warsaw rose 8.1% in 2026.",
                    "https://best.example/report",
                    "Prime rents in Warsaw rose 8.1% in 2026",
                    8.1,
                    "%",
                    "2026",
                )
            ]
        }
    )
    bundle = gather_external_facts(
        QUESTION, tf_client=fake_tf.client(), tavily=fake_tavily.client(), queries=["q"], extract_top=1
    )
    (extract_request,) = fake_tavily.endpoint_requests("extract")
    assert extract_request["urls"] == ["https://best.example/report"]
    assert extract_request["query"] == QUESTION
    assert extract_request["chunks_per_source"] == 3
    assert bundle.results[1].content.startswith("Full report.")
    assert [f.value for f in bundle.facts] == [8.1]
    assert bundle.tavily_credits == pytest.approx(1.2)  # search 1 + extract 1/5 (estimated)


def test_offline_replay_uses_cache_only(fake_tf, fake_tavily, tmp_path):
    _seed(fake_tavily)
    fake_tf.add_json_reply(EXTRACTION).add_json_reply(EXTRACTION)
    online = fake_tavily.client(cache=TavilyCache(tmp_path))
    first = gather_external_facts(
        QUESTION, tf_client=fake_tf.client(), tavily=online, queries=[RENT_Q, FEES_Q]
    )

    empty = FakeTavily()  # would return nothing if it were ever called
    offline = empty.client(Settings(), cache=TavilyCache(tmp_path, offline=True))
    replay = gather_external_facts(
        QUESTION, tf_client=fake_tf.client(), tavily=offline, queries=[RENT_Q, FEES_Q]
    )

    assert empty.requests == []
    assert replay.results == first.results
    assert replay.facts == first.facts
    assert (first.tavily_credits, replay.tavily_credits, replay.tavily_cache_hits) == (2, 0, 2)
    assert offline.ledger.summary().credits_saved == 2


def test_offline_miss_is_a_warning_not_a_crash(fake_tf, tmp_path):
    offline = FakeTavily().client(Settings(), cache=TavilyCache(tmp_path, offline=True))
    bundle = gather_external_facts(QUESTION, tf_client=fake_tf.client(), tavily=offline, queries=[RENT_Q])
    assert bundle.facts == []
    assert any("OfflineCacheMissError" in w for w in bundle.warnings)


def test_max_facts_keeps_the_most_confident_in_order(fake_tf, fake_tavily):
    content = "Rents rose 1% in Gdansk, 2% in Lodz, 3% in Poznan and 4% in Wroclaw."
    fake_tavily.add_search("q", [search_result("https://a.example", content=content)])
    cities = [("Gdansk", 1, 0.5), ("Lodz", 2, 0.9), ("Poznan", 3, 0.6), ("Wroclaw", 4, 0.8)]
    fake_tf.add_json_reply(
        {
            "facts": [
                _fact(f"Rents rose {n}% in {city}.", "https://a.example", f"{n}% in {city}", confidence=c)
                for city, n, c in cities
            ]
        }
    )
    bundle = gather_external_facts(
        QUESTION, tf_client=fake_tf.client(), tavily=fake_tavily.client(), queries=["q"], max_facts=2
    )
    assert [f.statement for f in bundle.facts] == ["Rents rose 2% in Lodz.", "Rents rose 4% in Wroclaw."]


def test_parallel_extraction_keeps_batch_alignment(fake_tf, fake_tavily):
    fake_tavily.add_search(
        "q",
        [
            search_result(f"https://s{i}.example", content=f"Metric {i} rose {i}0% in 2026.")
            for i in range(1, 5)
        ],
    )

    def respond(body: dict) -> httpx.Response:
        prompt = body["messages"][-1]["content"]
        facts = [
            _fact(
                f"Metric {i} rose {i}0% in 2026.", f"https://s{i}.example", f"Metric {i} rose {i}0% in 2026"
            )
            for i in range(1, 5)
            if f"URL: https://s{i}.example" in prompt
        ]
        return httpx.Response(200, json=chat_completion(content=json.dumps({"facts": facts})))

    fake_tf.add_responder(respond).add_responder(respond)
    bundle = gather_external_facts(
        QUESTION,
        tf_client=fake_tf.client(),
        tavily=fake_tavily.client(),
        queries=["q"],
        batch_size=2,
        max_workers=4,
    )
    assert [f.source for f in bundle.facts] == [f"https://s{i}.example" for i in range(1, 5)]


def test_empty_question_is_rejected(fake_tf, fake_tavily):
    with pytest.raises(ValueError):
        gather_external_facts("  ", tf_client=fake_tf.client(), tavily=fake_tavily.client())


# --- helpers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.Example.com/a/b/", "example.com/a/b"),
        ("http://example.com/a/b#section", "example.com/a/b"),
        ("example.com/a/b", "example.com/a/b"),
        ("https://example.com/a?utm_source=x&id=7&gclid=1", "example.com/a?id=7"),
        ("<https://example.com:8443/a>", "example.com:8443/a"),
        ("S3", ""),
        ("", ""),
    ],
)
def test_normalize_url(url, expected):
    assert normalize_url(url) == expected


def test_numbers_in_handles_separators_and_labels():
    assert numbers_in("Rents rose 12% to 1,200 EUR in Q2 2026 (FY2025: 11.5%)") == {12, 1200, 1.2, 2026, 11.5}
    assert numbers_in("3PL fees rose 12,5 %") == {3, 125, 12.5}


def test_is_grounded_tolerates_small_edits_only():
    text = "Prime warehouse rents in Poland rose 6.5% year over year in 2026, reaching 4.6 EUR."
    assert is_grounded("prime warehouse rents in Poland rose 6.5%", text)
    assert is_grounded("Prime warehouse rents ... rose 6.5% year-over-year", text)
    assert not is_grounded("Office rents in Germany collapsed", text)
    assert not is_grounded("", text)


def test_near_duplicates_require_same_numbers():
    assert is_near_duplicate("Rents in Poland rose 6.5% in 2026.", "rents in poland rose by 6.5% in 2026")
    assert not is_near_duplicate("Rents in Poland rose 6.5% in 2026.", "Rents in Poland rose 7% in 2026.")
    assert not is_near_duplicate("Rents in Poland rose 12% in 2026.", "Rents in Czechia rose 12% in 2026.")


def test_truncate_cuts_on_word_boundary():
    assert truncate("short", 10) == "short"
    assert truncate("alpha beta gamma delta", 12) == "alpha beta ..."


def test_merge_results_assigns_ids_in_first_seen_order():
    q1, q2 = ResearchQuery(query="a"), ResearchQuery(query="b")
    r1 = SearchResponse.model_validate(
        search_payload("a", [search_result("https://x.example/1", content="one")])
    )
    r2 = SearchResponse.model_validate(
        search_payload(
            "b",
            [
                search_result("https://y.example", content="two"),
                search_result("http://x.example/1/", content="one"),
            ],
        )
    )
    docs = merge_results([(q1, r1), (q2, r2)])
    assert [(d.source_id, d.url, d.queries) for d in docs] == [
        ("S1", "https://x.example/1", ["a", "b"]),
        ("S2", "https://y.example", ["b"]),
    ]
    assert docs[0].content == "one"  # identical snippet not repeated
