"""Run statistics: stages and LLM calls land in SQLite with the right links."""

import asyncio

from fastmcp import Client

from variator.agent import run_agent
from variator.llm.testing import FakeTokenFactory
from variator.mcp import Services, build_server, discover
from variator.telemetry import StatsStore


def _server(tmp_path, fake, stats):
    plugins = discover()
    services = Services(plugins, llm=fake.client(), report_dir=tmp_path, stats=stats)
    return build_server(plugins, remotes={}, services=services, all_tools=True)


def test_tool_stages_llm_calls_and_summary(tmp_path):
    stats = StatsStore(tmp_path / "stats.sqlite")
    fake = FakeTokenFactory()
    fake.add_reply(content="Nearshore is best.", prompt_tokens=300, completion_tokens=50)
    server = _server(tmp_path, fake, stats)

    async def go():
        async with Client(server) as c:
            case = (
                await c.call_tool(
                    "case_from_template", {"plugin": "it_services", "template": "delivery_model"}
                )
            ).data
            run = (await c.call_tool("simulate", {"case": case["case"], "n": 500})).data
            await c.call_tool("rank", {"run": run["run"]})
            await c.call_tool("explain", {"run": run["run"]})  # sync tool: runs in a worker thread
            await c.call_tool("simulate", {"case": "case999"}, raise_on_error=False)
            await c.call_tool("it_services_rice", {"reach": 10, "impact": 1, "confidence": 1, "effort": 2})
            return (await c.call_tool("stats_summary", {"group_by": "kind"})).data, run["run"]

    summary, run_id = asyncio.run(go())
    rows = stats.query(
        "SELECT tool, plugin, kind, ok, case_id, run_id, llm_calls, prompt_tokens, cost_usd FROM stages"
    )
    by_tool = {}
    for r in rows:
        by_tool.setdefault(r["tool"], []).append(r)
    assert by_tool["simulate"][0]["run_id"] == run_id and by_tool["simulate"][0]["case_id"] == "case1"
    assert by_tool["simulate"][1]["ok"] == 0  # unknown case -> error stage
    explain = by_tool["explain"][0]
    assert explain["kind"] == "llm" and explain["llm_calls"] == 1 and explain["prompt_tokens"] == 300
    assert explain["cost_usd"] > 0 and explain["run_id"] == run_id
    assert by_tool["rank"][0]["kind"] == "local" and by_tool["rank"][0]["cost_usd"] == 0
    assert by_tool["it_services_rice"][0]["plugin"] == "it_services"
    calls = stats.query("SELECT model, stage_id, duration_s FROM llm_calls")
    assert len(calls) == 1 and "Ultra" in calls[0]["model"] and calls[0]["duration_s"] >= 0
    kinds = {r["kind"]: r for r in summary["rows"]}
    assert kinds["llm"]["stages"] == 1 and kinds["local"]["errors"] == 1
    assert stats.by_model()[0]["calls"] == 1


def test_agent_run_is_one_session(tmp_path):
    stats = StatsStore()
    fake = FakeTokenFactory()
    fake.add_reply(
        tool_calls=[("case_from_template", {"plugin": "it_services", "template": "delivery_model"})]
    )
    fake.add_reply(tool_calls=[("simulate", {"case": "case1", "n": 300})])
    fake.add_reply(tool_calls=[("rank", {"run": "run2"})])
    fake.add_reply(content="done")
    server = _server(tmp_path, fake, stats)
    llm = server.services.llm

    asyncio.run(run_agent("Which model?", server=server, llm=llm))

    stages = stats.query("SELECT tool, session, llm_calls FROM stages ORDER BY id")
    assert [s["tool"] for s in stages] == ["agent_run", "case_from_template", "simulate", "rank"]
    assert len({s["session"] for s in stages}) == 1 and stages[0]["session"].startswith("agent-")
    assert stages[0]["llm_calls"] == 4  # the agent's own Nemotron calls
    by_session = stats.summary("session")
    assert by_session[0]["stages"] == 4 and by_session[0]["llm_calls"] == 4


def test_stats_off_means_no_middleware(tmp_path):
    fake = FakeTokenFactory()
    server = _server(tmp_path, fake, None)

    async def go():
        async with Client(server) as c:
            return await c.call_tool("stats_summary", {}, raise_on_error=False)

    result = asyncio.run(go())
    assert result.is_error and "statistics are off" in result.content[0].text
