"""The SQLite store keeps handles across a server restart."""

import asyncio
import json

import numpy as np
from fastmcp import Client

from variator.adapters import read_csv
from variator.mcp import Services, Store, build_server, discover


def _data(result):
    return result.data if result.data is not None else json.loads(result.content[0].text)


def _server(path, tmp_path):
    plugins = discover()
    return build_server(
        plugins, remotes={}, services=Services(plugins, store=Store(path), report_dir=tmp_path)
    )


def test_handles_survive_a_restart(tmp_path):
    db = tmp_path / "store.sqlite"

    async def first():
        async with Client(_server(db, tmp_path)) as c:
            ds = _data(await c.call_tool("ingest", {"source": "vendor_rates.csv", "plugin": "it_services"}))
            case = _data(
                await c.call_tool(
                    "case_from_template",
                    {"plugin": "it_services", "template": "delivery_model", "datasets": [ds["dataset"]]},
                )
            )
            await c.call_tool(
                "case_patch",
                {"case": case["case"], "ops": [{"op": "set_weight", "criterion": "time", "weight": 2.5}]},
            )
            run = _data(await c.call_tool("simulate", {"case": case["case"], "n": 1500}))
            rag = _data(await c.call_tool("rag", {"run": run["run"]}))
            await c.call_tool(
                "tunnel", {"run": run["run"], "x": "productivity", "y": "scope_points", "grid": 5}
            )
            ranking = _data(await c.call_tool("rank", {"run": run["run"]}))
            return ds["dataset"], case["case"], run["run"], rag, ranking

    ds_id, case_id, run_id, rag, ranking = asyncio.run(first())

    async def second():
        async with Client(_server(db, tmp_path)) as c:  # new process state, same file
            case = _data(await c.call_tool("case_get", {"case": case_id}))
            again = _data(await c.call_tool("rank", {"run": run_id}))
            report = _data(await c.call_tool("report", {"run": run_id}))
            new_case = _data(
                await c.call_tool(
                    "case_from_template",
                    {"plugin": "it_services", "template": "delivery_model", "datasets": [ds_id]},
                )
            )
            return case, again, report, new_case

    case, again, report, new_case = asyncio.run(second())
    assert case["version"] == 2
    assert next(c for c in case["criteria"] if c["name"] == "time")["weight"] == 2.5
    assert "outsourcing_basic:late_go_live" in case["rules"]
    assert [o["option"] for o in again["options"]] == [o["option"] for o in ranking["options"]]
    assert again["options"][0]["score"] == ranking["options"][0]["score"]
    html = open(report["path"], encoding="utf-8").read()
    assert '"rag": {"limits"' in html  # RAG report reloaded from SQLite
    assert '"tunnels": [{"x": "productivity"' in html  # tunnel map reloaded from SQLite
    assert set(rag["limits"]) == {"money", "time", "resources", "delay_months"}
    assert int(new_case["case"].removeprefix("case")) > int(run_id.removeprefix("run"))  # ids continue


def test_dataset_columns_round_trip(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    key = store.add_dataset(read_csv("vendor,rate\nacme,10.5\nbeta,\n", name="t"))
    loaded = Store(tmp_path / "s.sqlite").dataset(key)
    assert loaded.columns["vendor"].tolist() == ["acme", "beta"]
    assert np.isnan(loaded.columns["rate"][1]) and loaded.columns["rate"][0] == 10.5
    assert loaded.summary() == store.dataset(key).summary()


def test_memory_store_is_not_persistent():
    assert Store().persistent is False
