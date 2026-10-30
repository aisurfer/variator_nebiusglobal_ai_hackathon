"""Offline SDLC metric catalog: complete, unique IDs, each metric cites a verifiable source."""

import asyncio
import json

import pytest
from fastmcp import Client
from variator_it_services import ARCH_REFS, CODE_REFS
from variator_it_services.metrics_catalog import cite, load_catalog, metric, references, search

from variator.mcp import Services, build_server, discover

DORA_DEVLAKE = {
    "dora_deployment_frequency",
    "dora_lead_time_for_changes",
    "dora_failed_deployment_recovery_time",
    "dora_time_to_restore_service",
    "dora_change_failure_rate",
}


def test_catalog_is_complete_and_every_metric_has_a_source():
    cat = load_catalog()
    ids = [m["id"] for m in cat["metrics"]]
    assert len(ids) == len(set(ids))
    counts = {s: sum(m["source"] == s for m in cat["metrics"]) for s in cat["sources"]}
    assert counts["devlake"] == 35 and counts["scorecard"] == 19 and counts["dora"] == 1
    for m in cat["metrics"]:
        assert m["definition"], m["id"]
        assert m["doc_url"].startswith("https://") and m["source_url"].startswith("https://"), m["id"]
        assert m["direction"] in ("lower", "higher", "neutral"), m["id"]
    assert "commit" in cat["sources"]["devlake"]


def test_devlake_entries_keep_the_calculation_details():
    cfr = metric("dora_change_failure_rate")
    assert "/total number of deployments" in cfr["calculation"]
    assert [b["group"] for b in cfr["benchmarks"]][0] == "Elite performers"
    assert "cicd_deployment_commits" in cfr["sql_example"]
    assert cfr["source_url"].endswith("/docs/Metrics/CFR.md")
    df = metric("dora_deployment_frequency")
    assert len(df["benchmarks"]) == 4 and "Deployment Days" in df["benchmarks"][0]["criteria"]
    assert DORA_DEVLAKE <= {m["id"] for m in search(group="dora")}


def test_search_cite_and_unknown_id():
    assert {m["id"] for m in search("bus factor")} == {"chaoss_contributor_absence_factor"}
    assert all(m["source"] == "scorecard" for m in search(source="scorecard"))
    assert search(used_by="codebase")
    ref = cite("pr_pickup_time")
    assert ref["source"] == "Apache DevLake" and ref["doc_url"].endswith("/PRPickupTime")
    assert len(references(["code_churn", "code_churn", "code_nloc"])) == 2
    with pytest.raises(KeyError, match="close"):
        metric("lead_time")


def test_code_tools_return_references(tmp_path):
    for key in ARCH_REFS:
        metric(key)  # every reference ID exists
    assert set(CODE_REFS) <= set(ARCH_REFS)
    plugins = discover()
    server = build_server(
        plugins, remotes={}, services=Services(plugins, report_dir=tmp_path), all_tools=True
    )

    async def go():
        async with Client(server) as c:
            found = await c.call_tool("it_services_metric_catalog", {"text": "change failure"})
            one = await c.call_tool("it_services_metric_ref", {"metric_id": "dora_change_failure_rate"})
            return [json.loads(r.content[0].text) for r in (found, one)]

    found, one = asyncio.run(go())
    assert found["count"] >= 1 and "devlake" in found["sources"]
    assert one["cite"]["doc_url"] == "https://devlake.apache.org/docs/Metrics/CFR"
