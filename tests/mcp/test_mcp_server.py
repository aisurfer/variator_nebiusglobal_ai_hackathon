"""variator-core through a real MCP client (in-memory transport), fully offline."""

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp import Client
from variator_it_services import delivery_model

from variator.llm.testing import FakeTokenFactory
from variator.mcp import Services, build_server, discover
from variator.mcp.case import apply_patch

YAML = """
pack: extra
rules:
  - id: rate_red
    kind: flag
    if: rate > 13000
    then: {flag: red, criterion: money}
"""


def _data(result):
    return result.data if result.data is not None else json.loads(result.content[0].text)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def server(tmp_path: Path):
    plugins = discover()
    fake = FakeTokenFactory()
    services = Services(plugins, llm=fake.client(), report_dir=tmp_path)
    mcp = build_server(plugins, remotes={}, services=services)
    mcp.fake = fake
    return mcp


def test_plugins_are_discovered_and_mounted(server):
    async def go():
        async with Client(server) as c:
            core = {t.name for t in await c.list_tools()}
            hidden = await c.call_tool("logistics_capacity_gap", {}, raise_on_error=False)
            cards = _data(await c.call_tool("plugins_list", {}))
            enabled = _data(await c.call_tool("tools_enable", {"groups": ["logistics", "code"]}))
            names = {t.name for t in await c.list_tools()}
            gap = _data(
                await c.call_tool(
                    "logistics_capacity_gap", {"base_orders": 100, "growth": 0.5, "capacity": 120}
                )
            )
        async with Client(server) as other:  # another session keeps the core set
            other_names = {t.name for t in await other.list_tools()}
        return core, hidden, cards, enabled, names, gap, other_names

    core, hidden, cards, enabled, names, gap, other_names = _run(go())
    assert {"simulate", "rank", "rag", "tunnel", "tools_enable"} <= core
    assert not core & {"it_services_rice", "logistics_capacity_gap", "code_scan", "stats_summary"}
    assert hidden.is_error
    assert {p["name"] for p in cards["plugins"]} >= {"logistics", "it_services"}
    assert "logistics_capacity_gap" in enabled["plugin_tools"]
    assert {"logistics_capacity_gap", "code_scan", "container_status"} <= names
    assert "it_services_rice" not in names and "stats_summary" not in names
    assert gap["overflow"][0] == 30
    assert other_names == core


def test_all_tools_mode_and_unknown_group(tmp_path):
    plugins = discover()
    mcp = build_server(plugins, remotes={}, services=Services(plugins, report_dir=tmp_path), all_tools=True)

    async def go():
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
            bad = await c.call_tool("tools_enable", {"groups": ["nope"]}, raise_on_error=False)
            return names, bad

    names, bad = _run(go())
    assert {"it_services_rice", "stats_summary", "code_scan"} <= names
    assert bad.is_error and "unknown groups" in bad.content[0].text


def test_isolated_sessions_do_not_see_each_other(tmp_path):
    plugins = discover()
    mcp = build_server(plugins, remotes={}, services=Services(plugins, report_dir=tmp_path, isolate=True))
    template = {"plugin": "it_services", "template": "delivery_model"}

    async def go():
        async with Client(mcp) as a, Client(mcp) as b:
            case = _data(await a.call_tool("case_from_template", template))["case"]
            own = await a.call_tool("case_get", {"case": case}, raise_on_error=False)
            foreign = await b.call_tool("case_get", {"case": case}, raise_on_error=False)
            patch = await b.call_tool("case_patch", {"case": case, "ops": []}, raise_on_error=False)
            return own, foreign, patch

    own, foreign, patch = _run(go())
    assert not own.is_error
    assert foreign.is_error and "unknown case" in foreign.content[0].text
    assert "known: none" in foreign.content[0].text  # the other session's handles are not listed
    assert patch.is_error


def test_case_patch_version_conflict(server):
    async def go():
        async with Client(server) as c:
            case = _data(
                await c.call_tool(
                    "case_from_template", {"plugin": "it_services", "template": "delivery_model"}
                )
            )
            ops = [{"op": "set_weight", "criterion": "time", "weight": 2}]
            first = _data(await c.call_tool("case_patch", {"case": case["case"], "ops": ops, "version": 1}))
            stale = await c.call_tool(
                "case_patch", {"case": case["case"], "ops": ops, "version": 1}, raise_on_error=False
            )
            return first, stale

    first, stale = _run(go())
    assert first["version"] == 2
    assert stale.is_error and "conflict" in stale.content[0].text


def test_full_flow_is_local_and_compact(server):
    async def go():
        async with Client(server) as c:
            call = lambda name, **kw: c.call_tool(name, kw)  # noqa: E731
            ds = _data(await call("ingest", source="vendor_rates.csv", plugin="it_services"))
            case = _data(
                await call(
                    "case_from_template",
                    plugin="it_services",
                    template="delivery_model",
                    extend=["logistics"],
                    datasets=[ds["dataset"]],
                )
            )
            added = _data(await call("rules_add", case=case["case"], yaml=YAML))
            run = _data(await call("simulate", case=case["case"], n=3000))
            ranking = _data(await call("rank", run=run["run"]))
            money_only = _data(
                await call(
                    "rank",
                    run=run["run"],
                    weights={"money": 1, "time": 0, "resources": 0, "delay_months": 0, "service_level": 0},
                )
            )
            rag = _data(await call("rag", run=run["run"]))
            tunnel = _data(await call("tunnel", run=run["run"], x="productivity", y="scope_points", grid=5))
            patched = _data(
                await call(
                    "case_patch",
                    case=case["case"],
                    ops=[{"op": "set_weight", "criterion": "time", "weight": 3}],
                )
            )
            report = _data(await call("report", run=run["run"]))
            return ds, case, added, run, ranking, money_only, rag, tunnel, patched, report

    ds, case, added, run, ranking, money_only, rag, tunnel, patched, report = _run(go())
    assert ds["dataset"].startswith("ds") and "blended_rate" in ds["numeric"]
    assert [c["name"] for c in case["criteria"]] == [
        "money",
        "time",
        "resources",
        "delay_months",
        "service_level",
    ]
    assert "outsourcing_basic:burn_over_budget" in case["rules"]
    assert "rate_red" in added["rules"]
    assert run["checks_failed"]["burn_over_budget"]["offshore_fixed"] == 0.0
    assert len(json.dumps(run)) < 3000  # compact summary, no arrays
    # each share is rounded to 4 places, so the sum can be off by a few 1e-4
    assert sum(o["prob_best"] for o in ranking["options"]) == pytest.approx(1.0, abs=1e-3)
    assert money_only["options"][0]["option"] == "offshore_fixed"
    for option in rag["options"]:
        assert sum(option["overall"].values()) == pytest.approx(1.0, abs=0.01)
    assert set(tunnel["best_area"]) == {"in_house", "nearshore_tm", "offshore_fixed"}
    assert next(c for c in patched["criteria"] if c["name"] == "time")["weight"] == 3
    html = Path(report["path"]).read_text(encoding="utf-8")
    assert "Tunnel" in html and "plotly" in html and "offshore_fixed" in html


def test_guides_are_resources(server):
    async def go():
        async with Client(server) as c:
            return (await c.read_resource("variator://guide/patch"))[0].text

    assert "set_distribution" in _run(go())


def test_errors_come_back_as_tool_errors(server):
    async def go():
        async with Client(server) as c:
            return await c.call_tool("simulate", {"case": "case999"}, raise_on_error=False)

    result = _run(go())
    assert result.is_error and "unknown case" in result.content[0].text


def test_explain_uses_ultra_once(server):
    server.fake.add_reply(content="Nearshore is the best choice.")

    async def go():
        async with Client(server) as c:
            case = _data(
                await c.call_tool(
                    "case_from_template", {"plugin": "it_services", "template": "delivery_model"}
                )
            )
            run = _data(await c.call_tool("simulate", {"case": case["case"], "n": 1000}))
            return _data(await c.call_tool("explain", {"run": run["run"]}))

    out = _run(go())
    assert out["text"] == "Nearshore is the best choice."
    assert len(server.fake.requests) == 1 and "Ultra" in server.fake.requests[0]["model"]


def test_only_loopback_hosts_without_allow_remote():
    from variator.mcp.__main__ import is_loopback

    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.5") and not is_loopback("example.com")


def test_patch_ops():
    model = delivery_model()
    out = apply_patch(
        model,
        [
            {"op": "set_distribution", "name": "team_size", "distribution": {"kind": "fixed", "value": 9}},
            {"op": "set_formula", "option": "in_house", "criterion": "time", "formula": "ramp_up_months + 6"},
            {"op": "set_correlation", "a": "rate", "b": "productivity", "rho": 0.4},
            {"op": "remove_risk", "name": "scope_creep"},
            {"op": "add_assumption", "value": "x"},
        ],
    )
    assert out.variable("team_size").distribution.value == 9
    assert out.option("in_house").metrics["time"] == "ramp_up_months + 6"
    assert len(out.correlations) == 1 and "scope_creep" not in {r.name for r in out.risks}
    with pytest.raises(ValueError, match="unknown patch op"):
        apply_patch(model, [{"op": "explode"}])
    with pytest.raises(ValueError):
        apply_patch(model, [{"op": "remove_variable", "name": "rate"}])  # formulas still use it


def test_case_from_question_ultra_designs_the_case_then_super_builds_it(server, monkeypatch):
    from variator import reasoning
    from variator.ingestion import question as framing

    calls = []

    def design(client, question, *, context_facts=(), **kw):
        calls.append(("design",))
        return framing.DecisionBrief(
            question=question,
            restated_question=question,
            options=[
                {"name": "in_house", "description": "own team"},
                {"name": "nearshore_tm", "description": "vendor"},
            ],
            metric="profit",
            horizon_months=24,
            constraints=[{"kind": "budget", "description": "at most $100k a month", "value": 100000}],
        )

    def build(client, question, facts, *, options_hint=(), constraints=(), **kw):
        calls.append(("build", list(options_hint), list(constraints)))
        return delivery_model()

    monkeypatch.setattr(framing, "parse_question", design)
    monkeypatch.setattr(reasoning, "build_decision_model", build)

    async def go():
        async with Client(server) as c:
            return _data(await c.call_tool("case_from_question", {"question": "Who builds the platform?"}))

    case = _run(go())
    assert [c[0] for c in calls] == ["design", "build"]
    assert calls[1][1:] == (["in_house", "nearshore_tm"], ["at most $100k a month"])
    assert any("case design: 2 forks, metric profit, horizon 24 months" in n for n in case["notes"])
