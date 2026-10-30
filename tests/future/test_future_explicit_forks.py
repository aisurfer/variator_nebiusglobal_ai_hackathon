"""Explicit forks and layers of a drafted case B: the hand-written case in the explicit form gives the same
result, and arbitrary fork ids run through the month Monte Carlo, the ranking, the week tree and the export."""

# ruff: noqa: E501 - records and fixtures, one per line

from __future__ import annotations

import json

import numpy as np
import pytest

from variator.future import export
from variator.future.forks import (
    builtin_forks,
    builtin_layers,
    case_config,
    company_forks,
    company_layers,
    compile_knobs,
    explicit_forks,
)

N = 120
SEED = 5


def _rows(forks) -> list[dict]:
    return [
        {"id": f.id, "label": f.label,
         "interventions": [{k: v for k, v in i.items() if k != "source"} for i in f.as_dict()["interventions"]]}
        for f in forks
    ]  # fmt: skip


def explicit(cfg: dict) -> dict:
    """The hand-written case with its 5 forks and 8 layers written out as explicit interventions."""
    return dict(cfg, forks=_rows(builtin_forks(cfg)), layers=_rows(builtin_layers(cfg)))


def drafted(cfg: dict) -> dict:
    """A drafted case: other fork ids, a combined fork, one layer, other risks."""
    rows = [
        {"id": "keep_course", "label": "Keep course", "interventions": []},
        {"id": "lift_prices_5", "label": "Prices +5%", "interventions": [
            {"kind": "factor", "op": "scale", "target": "price.list", "at": 1, "value": 0.05, "params": {}, "label": "up"}]},
        {"id": "hire_and_fix", "label": "Hire 3, fix debt", "interventions": [
            {"kind": "actor", "op": "add", "target": "role:backend", "at": 0, "value": 3,
             "params": {"cost_per_year": 140000, "cohort": "future:backend"}, "label": "hire"},
            {"kind": "factor", "op": "set", "target": "capacity.debt_share", "at": 2, "value": 0.1,
             "params": {"debt_cut_per_year": 0.3}, "label": "debt"}]},
    ]  # fmt: skip
    layers = [{"id": "slow_market", "label": "Slow market", "interventions": [
        {"kind": "habit", "op": "scale", "target": "deals.rate", "at": 0, "value": 0.9, "params": {}, "label": ""}]}]  # fmt: skip
    risks = [r for r in cfg["risks"] if r["variable"] != "fx_shock"]
    return dict(cfg, forks=rows, layers=layers, risks=risks)


@pytest.fixture(scope="module")
def cfg() -> dict:
    return case_config("company_best_year")


def test_the_hand_written_case_stays_on_the_built_in_path(cfg):
    assert not explicit_forks(cfg["forks"])  # its forks carry timeline items, not engine interventions
    assert "layers" not in cfg
    assert company_forks(cfg) == builtin_forks(cfg)
    assert company_layers(cfg) == builtin_layers(cfg)
    assert [f.id for f in company_forks(cfg)] == [
        "status_quo",
        "pay_tech_debt",
        "hire_5",
        "reserve_gpu",
        "raise_prices_8",
    ]
    assert len(company_layers(cfg)) == 8


def test_the_explicit_form_builds_the_same_forks_and_layers(cfg):
    x = json.loads(json.dumps(explicit(cfg)))  # through JSON, as a config file
    assert explicit_forks(x["forks"])
    assert company_forks(x) == builtin_forks(cfg)
    assert company_layers(x) == builtin_layers(cfg)
    assert [f.as_dict() for f in company_forks(x)] == [f.as_dict() for f in builtin_forks(cfg)]
    assert [f.as_dict() for f in company_layers(x)] == [f.as_dict() for f in builtin_layers(cfg)]


def test_the_explicit_form_gives_the_same_month_result(company, cfg):
    x = json.loads(json.dumps(explicit(cfg)))
    a = company.run(compile_knobs(company_forks(cfg)), N, SEED)
    b = company.run(compile_knobs(company_forks(x)), N, SEED)
    for k, v in a.metrics.items():
        np.testing.assert_array_equal(v, b.metrics[k], err_msg=k)
    da = export.decide(export.build_cube(a, "t", company.world.anchor, SEED), cfg)
    db = export.decide(export.build_cube(b, "t", company.world.anchor, SEED), x)
    assert da["options"] == db["options"]
    for la, lb in zip(company_layers(cfg), company_layers(x), strict=True):
        ka, kb = compile_knobs(company_forks(cfg), [la]), compile_knobs(company_forks(x), [lb])
        assert vars(ka).keys() == vars(kb).keys()
        for key, va in vars(ka).items():
            np.testing.assert_array_equal(
                np.asarray(va, dtype=object), np.asarray(vars(kb)[key], dtype=object), err_msg=key
            )


def test_arbitrary_fork_ids_run_through_month_ranking_tree_and_export(company, cfg):
    from variator.future import tree as week_tree

    d = drafted(cfg)
    forks = company_forks(d)
    assert [f.id for f in forks] == ["keep_course", "lift_prices_5", "hire_and_fix"]
    k = compile_knobs(forks, company_layers(d))
    assert not k.unmodelled and k.deals_scale == pytest.approx(0.9)
    assert k.hire_count.tolist() == [0, 0, 3] and k.debt_share.tolist() == [0.0, 0.0, 0.1]
    res = company.run(compile_knobs(forks), N, SEED)
    dec = export.decide(export.build_cube(res, "t", company.world.anchor, SEED), d)
    assert {o["option"] for o in dec["options"]} == {f.id for f in forks}
    assert (
        export.rank_options(export.build_cube(res, "t", company.world.anchor, SEED), d).options[0].option
        == dec["options"][0]["option"]
    )
    doc = export.month_json(
        res, case_id="company_best_year", cfg=d, params=company.params, decision=dec, provenance={"seed": SEED},
        anchor=company.world.anchor, labels={f.id: f.label for f in forks}, forks=[f.as_dict() for f in forks],
    )  # fmt: skip
    # milestones follow the levers of a fork that no plan item names
    assert {m["id"] for m in doc["options"]["hire_and_fix"]["milestones"]} >= {"hires_ready", "debt_done"}
    assert doc["options"]["keep_course"]["milestones"] == []
    best = [o["option"] for o in dec["options"][:2]]
    t = week_tree.build_trees(company, forks, best, seed=SEED, weeks=3, k=2, max_nodes=8)
    assert list(t["weekTrees"]) == best and set(t["dayExamples"]) == set(best)


def test_the_built_in_milestones_are_unchanged(cfg):
    done = {"price_live": np.full(10, 2.0), "debt_done": np.full(10, np.nan)}
    rows = export.milestone_rows(cfg, "raise_prices_8", done, 12)
    assert [r["id"] for r in rows] == ["price_live"]
    assert export.milestone_rows(cfg, "status_quo", {"price_live": np.full(10, np.nan)}, 12) == []


def test_the_plan_fork_goes_first_whatever_the_draft_order(company, cfg):
    """The ranking anchors on the first fork: a draft with keep the plan last ranks as with it first."""
    first = drafted(cfg)
    last = dict(first, forks=first["forks"][1:] + first["forks"][:1])
    assert [f["id"] for f in last["forks"]] == ["lift_prices_5", "hire_and_fix", "keep_course"]
    assert [f.id for f in company_forks(last)] == ["keep_course", "lift_prices_5", "hire_and_fix"]
    assert company_forks(last) == company_forks(first)
    mid = dict(first, forks=[first["forks"][1], first["forks"][0], first["forks"][2]])
    assert [f.id for f in company_forks(mid)] == ["keep_course", "lift_prices_5", "hire_and_fix"]
    decided = []
    for c in (first, last):
        res = company.run(compile_knobs(company_forks(c)), N, SEED)
        decided.append(export.decide(export.build_cube(res, "t", company.world.anchor, SEED), c)["options"])
    assert decided[0] == decided[1]
