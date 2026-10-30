"""A real variator-mcp process over stdio: catches defects that in-process tests cannot see.

Regression: on Windows, the first native-library load (BLAS) in a FastMCP worker thread
hung while the stdio transport read the pipe; ``variator.mcp.warmup`` fixes it.
"""

import asyncio
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from variator.telemetry import StatsStore

REPO = Path(__file__).resolve().parents[2]


def test_numeric_tools_answer_over_stdio(tmp_path):
    stats_path = tmp_path / "stats.sqlite"
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "variator.mcp", "--store", "memory"],
        cwd=str(REPO),
        env={**os.environ, "VARIATOR_STATS": str(stats_path)},
    )

    async def go():
        async with Client(transport) as c:
            await c.call_tool("tools_enable", {"groups": ["it_services"]})  # plugin tools are hidden
            files =(await c.call_tool("it_services_jira_synthetic", {"out_dir": str(tmp_path)})).data[
                "files"
            ]
            call = c.call_tool(
                "it_services_estimate_backlog", {"issues": files["issues"], "worklogs": files["worklogs"]}
            )
            est = await asyncio.wait_for(call, 60)  # scipy + BLAS in a worker thread
            case = (
                await c.call_tool(
                    "case_from_template", {"plugin": "it_services", "template": "delivery_model"}
                )
            ).data
            run = await asyncio.wait_for(c.call_tool("simulate", {"case": case["case"], "n": 500}), 60)
            return est.data, run.data

    est, run = asyncio.run(go())
    assert est["open_issues"] == 40
    assert set(run["options"]) == {"in_house", "nearshore_tm", "offshore_fixed"}
    stages = StatsStore(stats_path).query("SELECT tool, plugin, ok, session FROM stages ORDER BY id")
    assert [s["tool"] for s in stages] == [
        "tools_enable",
        "it_services_jira_synthetic",
        "it_services_estimate_backlog",
        "case_from_template",
        "simulate",
    ]
    assert all(s["ok"] == 1 for s in stages) and len({s["session"] for s in stages}) == 1
    assert stages[1]["plugin"] == "it_services"
