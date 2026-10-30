"""Scenario presets: relative ops, combination, and the MCP parameter."""

import asyncio

import pytest
from fastmcp import Client
from pydantic import ValidationError
from variator_it_services import delivery_model
from variator_it_services.scenarios import COMPANY, DELIVERY

from variator.domain import Scenario, ScenarioOp, apply_scenarios
from variator.mcp import Services, build_server, discover


def _scn(name, *ops):
    return Scenario(name=name, label=name, description=name, ops=list(ops))


def test_ops_scale_shift_probability_and_weight():
    base = delivery_model()
    scenarios = [
        _scn("a", ScenarioOp(target="scope_points", scale=2.0)),
        _scn("b", ScenarioOp(target="target_months", shift=-2)),
        _scn("c", ScenarioOp(kind="risk", target="scope_creep", probability=0.5)),
        _scn("d", ScenarioOp(kind="weight", target="delay_months", scale=2)),
    ]
    m = apply_scenarios(base, scenarios, ["a", "b", "c", "d"])
    assert m.variable("scope_points").distribution.mode == 1200
    assert m.variable("target_months").distribution.value == 7
    risk = next(r for r in m.risks if r.name == "scope_creep")
    assert risk.probability == pytest.approx(1 - 0.65 * 0.5)
    w = {c.name: c.weight for c in m.criteria}
    assert w["delay_months"] == 2 * {c.name: c.weight for c in base.criteria}["delay_months"]


def test_option_scope_and_overrides():
    m = apply_scenarios(delivery_model(), DELIVERY, ["vendor_rates_rise"])
    assert m.variable("rate").distribution.mode == 12_500  # in-house stays
    assert m.option("nearshore_tm").overrides["rate"].mode == pytest.approx(10_500 * 1.1)
    p = apply_scenarios(delivery_model(), DELIVERY, ["priorities_shift"])
    assert p.option("offshore_fixed").overrides["productivity"].mode == pytest.approx(
        6.5 * 0.9
    )  # overrides too


def test_presets_combine_in_definition_order():
    a = apply_scenarios(delivery_model(), DELIVERY, ["requirements_change", "priorities_shift"])
    b = apply_scenarios(delivery_model(), DELIVERY, ["priorities_shift", "requirements_change"])
    assert a.model_dump() == b.model_dump()
    assert a.variable("rework_share").distribution.mode == pytest.approx(0.08 * 1.15 * 1.2)


def test_invalid_ops_and_names():
    with pytest.raises(ValidationError):
        ScenarioOp(target="x", scale=2, shift=1)
    with pytest.raises(ValidationError):
        ScenarioOp(target="x", kind="weight", shift=1)
    with pytest.raises(ValueError, match="unknown scenarios"):
        apply_scenarios(delivery_model(), DELIVERY, ["nope"])
    assert {s.name for s in COMPANY} >= {"price_war", "cash_squeeze"}


def test_mcp_case_from_template_with_presets(tmp_path):
    plugins = discover()
    server = build_server(plugins, remotes={}, services=Services(plugins, report_dir=tmp_path))

    async def go():
        async with Client(server) as c:
            cards = (await c.call_tool("plugins_list", {})).data
            case = (
                await c.call_tool(
                    "case_from_template",
                    {"plugin": "it_services", "template": "delivery_model", "scenarios": ["tight_budget"]},
                )
            ).data
            return cards, case

    cards, case = asyncio.run(go())
    card = next(p for p in cards["plugins"] if p["name"] == "it_services")
    assert "tight_budget" in card["scenarios"]["delivery_model"]
    assert case["variables"]["budget_per_month"].startswith("fixed(value=8.5e+04")
    assert any("scenarios: tight_budget" in n for n in case["notes"])
