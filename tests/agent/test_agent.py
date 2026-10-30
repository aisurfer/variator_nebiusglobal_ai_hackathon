"""Agent loop with a scripted fake Nemotron: tool calls go through the real MCP server."""

import asyncio
import re

from variator.agent import CORE_TOOLS, DecisionContract, run_agent
from variator.agent.loop import summarize_reply
from variator.llm.testing import FakeTokenFactory
from variator.mcp import Services, build_server, discover


def _server(llm, tmp_path):
    plugins = discover()
    return build_server(plugins, remotes={}, services=Services(plugins, llm=llm, report_dir=tmp_path))


def test_agent_calls_mcp_tools_and_answers(tmp_path):
    fake = FakeTokenFactory()
    fake.add_reply(
        tool_calls=[("case_from_template", {"plugin": "it_services", "template": "delivery_model"})]
    )
    fake.add_reply(tool_calls=[("simulate", {"case": "case1", "n": 800})], cached_tokens=60)
    fake.add_reply(tool_calls=[("rank", {"run": "run2"}), ("not_a_tool", {})])
    fake.add_reply(content="Nearshore T&M has the best weighted score.")
    llm = fake.client()
    server = _server(llm, tmp_path)

    result = asyncio.run(run_agent("Which delivery model?", server=server, llm=llm, max_tool_chars=600))

    assert result.answer == "Nearshore T&M has the best weighted score."
    assert [s.tool for s in result.steps] == ["case_from_template", "simulate", "rank", "not_a_tool"]
    assert [s.ok for s in result.steps] == [True, True, True, False]
    assert all(s.chars <= 600 + 40 for s in result.steps)  # replies are cut
    first = fake.requests[0]
    exposed = {t["function"]["name"] for t in first["tools"]}
    assert exposed <= set(CORE_TOOLS) and "it_services_rice_ranking" not in exposed
    assert first["chat_template_kwargs"]["enable_thinking"] is False
    second = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    assert second[0]["content"].startswith('{"case":"case1"') and "[cut:" in second[0]["content"]
    tool_msgs = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    old = r"\[old case_from_template reply, \d+ chars: case=case1, version=1\]"
    assert re.fullmatch(old, tool_msgs[0]["content"])
    assert "not available" in tool_msgs[-1]["content"]
    assert result.prompt_tokens == 400 and result.cached_tokens == 60 and result.cost_usd > 0


def test_stages_show_only_the_tools_of_the_stage(tmp_path):
    fake = FakeTokenFactory()
    fake.add_reply(
        tool_calls=[("case_from_template", {"plugin": "it_services", "template": "delivery_model"})]
    )
    fake.add_reply(tool_calls=[("simulate", {"case": "case1", "n": 512})])
    fake.add_reply(tool_calls=[("rank", {"run": "run2"})])
    fake.add_reply(content="done")
    llm = fake.client()
    result = asyncio.run(run_agent("q", server=_server(llm, tmp_path), llm=llm))
    build, _, analyze, _ = ({t["function"]["name"] for t in r["tools"]} for r in fake.requests)
    assert result.stages == ["build", "build", "analyze", "analyze"]
    assert "rank" not in build and "case_from_template" in build and "simulate" in build
    assert "rank" in analyze and "case_from_template" not in analyze and "case_patch" in analyze
    # a stable prefix: the order of the tools is the server order in every request
    order = [t["function"]["name"] for t in fake.requests[0]["tools"]]
    assert order == [n for n in order if n in build]


def test_agent_rejects_a_final_answer_before_simulate_and_rank(tmp_path):
    fake = FakeTokenFactory()
    fake.add_reply(content="I recommend offshore fixed price.")
    fake.add_reply(
        tool_calls=[("case_from_template", {"plugin": "it_services", "template": "delivery_model"})]
    )
    fake.add_reply(tool_calls=[("simulate", {"case": "case1", "n": 300})])
    fake.add_reply(tool_calls=[("rank", {"run": "run2"})])
    fake.add_reply(content="The ranked result recommends nearshore T&M.")
    llm = fake.client()

    result = asyncio.run(run_agent("Which model?", server=_server(llm, tmp_path), llm=llm))

    assert result.decision_ready
    assert result.answer == "The ranked result recommends nearshore T&M."
    assert [s.tool for s in result.steps] == ["case_from_template", "simulate", "rank"]


def test_agent_rejects_a_rank_for_a_different_simulation_run(tmp_path):
    fake = FakeTokenFactory()
    fake.add_reply(
        tool_calls=[("case_from_template", {"plugin": "it_services", "template": "delivery_model"})]
    )
    fake.add_reply(tool_calls=[("simulate", {"case": "case1", "n": 300})])
    fake.add_reply(tool_calls=[("rank", {"run": "run999"})])
    fake.add_reply(content="This answer must not be accepted.")
    llm = fake.client()

    result = asyncio.run(run_agent("Which model?", server=_server(llm, tmp_path), llm=llm, max_steps=4))

    assert not result.decision_ready
    assert result.answer.startswith("Decision incomplete:")


def test_fixed_contract_overrides_simulation_and_rank_inputs(tmp_path):
    fake = FakeTokenFactory()
    fake.add_reply(
        tool_calls=[("case_from_template", {"plugin": "it_services", "template": "delivery_model"})]
    )
    fake.add_reply(
        tool_calls=[
            (
                "simulate",
                {"case": "case1", "n": 123, "seed": 99, "sampling": "random"},
            )
        ]
    )
    fake.add_reply(tool_calls=[("rank", {"run": "run2", "weights": {"money": -1}})])
    fake.add_reply(content="The fixed-contract ranking is the decision.")
    llm = fake.client()
    contract = DecisionContract(
        plugin="it_services",
        template="delivery_model",
        weights={"money": 1, "time": 1, "resources": 1},
        n=128,
        seed=7,
        sampling="sobol",
    )

    result = asyncio.run(
        run_agent("Which model?", server=_server(llm, tmp_path), llm=llm, contract=contract)
    )

    assert result.decision_ready and result.ranking is not None
    simulate = next(step for step in result.steps if step.tool == "simulate")
    rank = next(step for step in result.steps if step.tool == "rank")
    assert simulate.arguments == {"case": "case1", "n": 128, "seed": 7, "sampling": "sobol"}
    assert rank.arguments["weights"] == contract.weights


def test_no_stages_shows_all_chosen_tools(tmp_path):
    fake = FakeTokenFactory()
    fake.add_reply(content="done")
    llm = fake.client()
    asyncio.run(run_agent("q", server=_server(llm, tmp_path), llm=llm, stages=False, max_steps=1))
    names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert {"rank", "case_from_template"} <= names


def test_extra_tools_are_exposed(tmp_path):
    fake = FakeTokenFactory()
    fake.add_reply(content="done")
    llm = fake.client()
    asyncio.run(
        run_agent(
            "q", server=_server(llm, tmp_path), llm=llm, extra_tools=["it_services_rice_ranking"], max_steps=1
        )
    )
    names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert "it_services_rice_ranking" in names and "simulate" in names


def test_summarize_reply_keeps_handles():
    assert (
        summarize_reply("simulate", '{"run": "run3", "options": {}}')
        == "[old simulate reply, 30 chars: run=run3]"
    )
    assert summarize_reply("rank", "error: unknown run 'run9'").endswith("error: unknown run 'run9']")
    assert summarize_reply("x", "plain") == "[old x reply, 5 chars]"
