"""IT outsourcing toolkit through MCP: synthetic Jira -> estimates -> forecast -> RICE -> RAID -> case."""

import asyncio
import json
import math
from dataclasses import asdict
from pathlib import Path

import pytest
from fastmcp import Client
from variator_it_services.estimation import actual_hours, estimate, fit_effort, release_forecast, rice_ranking
from variator_it_services.jira_synth import generate
from variator_it_services.raid import raid_patch, raid_summary

from variator.mcp import Services, build_server, discover

REPO = Path(__file__).resolve().parents[2]


def _rows(items):
    return [{k: "" if v is None else str(v) for k, v in asdict(i).items()} for i in items]


def test_generator_is_deterministic_and_consistent():
    a, b = generate(seed=3), generate(seed=3)
    assert [i.key for i in a.issues] == [i.key for i in b.issues]
    done = {i.key for i in a.issues if i.status == "Done"}
    assert {w.issue for w in a.worklogs} == done
    assert all(i.assignee.startswith("dev_") for i in a.issues if i.status == "Done")
    assert any(i.story_points is None for i in a.issues if i.status != "Done")


def test_effort_model_recovers_the_generating_relation():
    export = generate(seed=11, sprints=20)
    issues = _rows(export.issues)
    model = fit_effort(issues, actual_hours(_rows(export.worklogs)))
    beta = dict(zip(model.columns, model.beta, strict=True))
    assert beta["log_points"] == pytest.approx(1.0, abs=0.15)  # hours grow about linearly with points
    assert model.name in ("M0", "M1")  # no metrics -> M2 not tried
    estimates = estimate(model, issues)
    unpointed = [e for e in estimates if e.points is None]
    pointed = [e for e in estimates if e.points is not None]
    assert unpointed and min(e.sd for e in unpointed) > max(e.sd for e in pointed)  # unknown size = wider
    assert all(e.p10 < e.p50 < e.p90 for e in estimates)


def test_forecast_and_rice_are_probabilistic():
    export = generate(seed=5)
    issues = _rows(export.issues)
    estimates = estimate(fit_effort(issues, actual_hours(_rows(export.worklogs))), issues)
    forecast = release_forecast(estimates, export.sprints[0].capacity_hours)
    assert forecast["sprints"]["p10"] <= forecast["sprints"]["p50"] <= forecast["sprints"]["p90"]
    assert sum(e.mean for e in estimates) == pytest.approx(sum(forecast["total_hours"].values()) / 3, rel=0.2)
    ranking = rice_ranking(issues, estimates, top_k=5)
    assert len(ranking) == len(estimates)
    # 5 places per sample; each value is rounded to 3 decimals
    assert sum(r["p_top_5"] for r in ranking) == pytest.approx(5.0, abs=0.0005 * len(ranking))
    assert ranking[0]["expected_rank"] <= ranking[-1]["expected_rank"]


def test_raid_summary_and_patch():
    rows = _rows(generate(seed=7).raid)
    summary = raid_summary(rows)
    assert summary["open"] == sum(1 for r in rows if r["status"] == "open")
    ops = raid_patch(rows)
    assert len(ops) == summary["open"]
    issue_ops = [o for o in ops if o["value"]["description"].startswith("issue:")]
    assert all(o["value"]["probability"] == 1.0 for o in issue_ops)
    assert all(o["value"]["variable"] == "risk_delay_months" for o in ops)


def _data(result):
    return result.data if result.data is not None else json.loads(result.content[0].text)


def test_full_flow_through_mcp(tmp_path):
    plugins = discover()
    server = build_server(
        plugins, remotes={}, services=Services(plugins, report_dir=tmp_path), all_tools=True
    )

    async def go():
        async with Client(server) as c:
            call = lambda name, **kw: c.call_tool(name, kw)  # noqa: E731
            files = _data(await call("it_services_jira_synthetic", out_dir=str(tmp_path / "jira")))["files"]
            est = _data(
                await call(
                    "it_services_estimate_backlog",
                    issues=files["issues"],
                    worklogs=files["worklogs"],
                    repo=str(REPO),
                    out=str(tmp_path / "est.csv"),
                )
            )
            forecast = _data(
                await call(
                    "it_services_release_forecast",
                    issues=files["issues"],
                    worklogs=files["worklogs"],
                    capacity_hours=288,
                )
            )
            rice = _data(
                await call("it_services_rice_ranking", issues=files["issues"], worklogs=files["worklogs"])
            )
            raid = _data(await call("it_services_raid_summary", path=files["raid"]))
            ops = _data(await call("it_services_raid_patch", path=files["raid"]))["ops"]
            arch = _data(await call("it_services_architecture", repo=str(REPO)))
            case = _data(await call("case_from_template", plugin="it_services", template="delivery_model"))
            before = _data(await call("simulate", case=case["case"], n=3000))
            patched = _data(await call("case_patch", case=case["case"], ops=ops))
            after = _data(await call("simulate", case=case["case"], n=3000))
            ds = _data(await call("ingest", source=str(tmp_path / "est.csv")))
            return est, forecast, rice, raid, ops, arch, before, patched, after, ds

    est, forecast, rice, raid, ops, arch, before, patched, after, ds = asyncio.run(go())
    assert est["model"]["model"] in ("M0", "M1", "M2") and est["open_issues"] == 40
    assert len(json.dumps(est)) < 2500  # compact reply
    assert forecast["sprints"]["p50"] >= 1
    assert len(rice["ranking"]) == 10
    assert raid["open"] == len(ops) and len(patched["risks"]) == 3 + len(ops)
    assert arch["cycles"] == []  # the repository has no package cycles
    t0 = before["options"]["in_house"]["time"][0]
    t1 = after["options"]["in_house"]["time"][0]
    expected = raid["expected_delay_days"] / 21.0
    assert t1 - t0 == pytest.approx(expected, rel=0.25)  # RAID delays move the go-live
    assert ds["rows"] == 40 and "p90" in ds["numeric"]
    assert not math.isnan(ds["numeric"]["p50"]["median"])


def test_factors_from_metrics_and_today_dates():
    from datetime import date

    from variator_it_services.jira_synth import factors_from_metrics, start_for_today

    f = factors_from_metrics({"a": {"ccn_avg": 1, "commits": 1}, "b": {"ccn_avg": 3, "commits": 9}})
    assert f["b"] > 1 > f["a"] and f["a"] * f["b"] == pytest.approx(1.0, abs=0.01)
    start = start_for_today(12, today=date(2026, 10, 25))
    assert start.weekday() == 0 and (date(2026, 10, 25) - start).days in range(168, 175)
