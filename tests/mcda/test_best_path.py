"""Best path: decision by the rank rule, where it stays best, expected result, risks."""

import asyncio

from fastmcp import Client
from variator_it_services import plugin

from variator.domain import apply_scenarios
from variator.mcda import best_path, weight_vector, what_breaks, with_ties
from variator.mcp import Services, build_server, discover
from variator.rules import compile_rules
from variator.simulation import simulate_tensor


def _run(template, active=()):
    model = apply_scenarios(plugin.templates[template](), plugin.scenarios[template], active)
    packs = [p for p in plugin.rule_packs if p.templates is None or template in p.templates]
    rules = compile_rules(
        packs,
        variables=[v.name for v in model.variables],
        options=[o.name for o in model.options],
        criteria=[c.name for c in model.effective_criteria()],
        derived=model.derived_map(),
    )
    return simulate_tensor(model, rules=rules, n_scenarios=4096, seed=7, sampling="sobol")


def test_delivery_path_follows_the_rank_rule():
    path = best_path(_run("delivery_model"), situation=["tight_budget"])
    assert path.decision == "offshore_fixed" and path.situation == ["tight_budget"]
    assert set(path.expected) == {"money", "time", "resources", "delay_months"}
    e = path.expected["money"]
    assert e["p5"] <= e["median"] <= e["p95"]
    assert any(r.startswith("Bad 5% of money") for r in path.risks)


def test_breaks_lists_single_changes_that_change_the_winner():
    # fixed anchors: the price rise leads on cash and runway; churn counted twice gives the plan back
    run = _run("company_model")
    w = weight_vector([c.name for c in run.criteria], None, [c.weight for c in run.criteria])
    breaks = what_breaks(run, w, 0.25)
    path = best_path(run)
    assert path.decision == "raise_prices_8"
    assert ("weight of churn x2", "status_quo") in breaks
    assert "weight of churn x2 -> status_quo" in path.breaks_if
    squeezed = best_path(_run("company_model", ["cash_squeeze", "price_war", "big_client_leaves"]))
    assert any("runway_low" in r for r in squeezed.risks)
    # without meaningful differences the same run still has a winner and consistent texts
    plain = best_path(with_ties(run, {c.name: 0.0 for c in run.criteria}))
    assert plain.ties == [] and plain.decision in run.option_names


def test_mcp_best_path_reads_presets_from_the_case(tmp_path):
    plugins = discover()
    server = build_server(plugins, remotes={}, services=Services(plugins, report_dir=tmp_path))

    async def go():
        async with Client(server) as c:
            case = (
                await c.call_tool(
                    "case_from_template",
                    {"plugin": "it_services", "template": "delivery_model", "scenarios": ["fixed_deadline"]},
                )
            ).data
            run = (await c.call_tool("simulate", {"case": case["case"], "n": 1024})).data
            return (await c.call_tool("best_path", {"run": run["run"]})).data

    out = asyncio.run(go())
    assert out["situation"] == ["fixed_deadline"] and out["decision"]
    assert {"stays_best", "breaks_if", "expected", "risks"} <= set(out)
