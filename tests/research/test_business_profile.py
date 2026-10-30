import asyncio
from pathlib import Path

from fastmcp import Client

from variator.llm.testing import FakeTokenFactory
from variator.mcp import Services, build_server, discover
from variator.research import business_profile_queries
from variator.research.testing import FakeTavily, search_result


def _data(result):
    return result.data


def test_business_profile_queries_cover_the_business_lenses():
    queries = business_profile_queries("IT services", geography="United Kingdom", max_queries=24)

    assert len(queries) == 24
    assert {q.purpose for q in queries[:12]} == {
        "business model",
        "customers",
        "competitors",
        "regulation",
        "operations",
        "labour",
        "risks",
        "benchmarks",
        "market",
        "technology",
        "customers pain",
        "economics",
    }
    assert all("IT services" in q.query and "United Kingdom" in q.query for q in queries)


def test_mcp_research_business_stores_cited_facts(tmp_path: Path):
    business = "IT services"
    geography = "United Kingdom"
    query = business_profile_queries(business, geography=geography, max_queries=1)[0].query

    fake_tavily = FakeTavily().add_search(
        query,
        [
            search_result(
                "https://example.com/report",
                title="Industry report",
                content="UK IT services spending grew 12% in 2026.",
            )
        ],
    )
    fake_llm = FakeTokenFactory().add_json_reply(
        {
            "facts": [
                {
                    "statement": "UK IT services spending grew 12% in 2026.",
                    "source_url": "https://example.com/report",
                    "evidence": "UK IT services spending grew 12% in 2026.",
                    "value": 12,
                    "unit": "%",
                    "period": "2026",
                    "confidence": 0.9,
                }
            ]
        }
    )
    server = build_server(
        discover(),
        remotes={},
        services=Services(
            discover(),
            llm=fake_llm.client(),
            tavily=fake_tavily.client(),
            report_dir=tmp_path,
        ),
    )

    async def go():
        async with Client(server) as client:
            tools = {tool.name for tool in await client.list_tools()}
            summary = _data(
                await client.call_tool(
                    "research_business",
                    {
                        "business": business,
                        "geography": geography,
                        "max_queries": 1,
                        "results_per_query": 1,
                        "extract_top": 0,
                        "max_facts": 10,
                    },
                )
            )
            full = _data(
                await client.call_tool("research_get", {"research": summary["research"], "full": True})
            )
            return tools, summary, full

    tools, summary, full = asyncio.run(go())
    assert {"research_business", "research_get"} <= tools
    assert summary["queries"] == 1
    assert summary["sources"] == 1
    assert summary["facts"] == 1
    assert summary["tavily_credits"] == 1
    assert full["facts"][0]["fact"]["source"] == "https://example.com/report"
