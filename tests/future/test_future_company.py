"""Evidence for the month engine of case B: determinism, the no-op fork and the sanity laws (paired runs)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from variator.future import export
from variator.future.forks import Fork, Intervention, company_forks, company_layers, compile_knobs

N = 160
SEED = 11


@pytest.fixture(scope="module")
def forks(company):
    return company_forks(company.cfg)


@pytest.fixture(scope="module")
def base(company, forks):
    return company.run(compile_knobs(forks), N, SEED)


def _fork(fid: str, *interventions: Intervention) -> Fork:
    return Fork(fid, fid, tuple(interventions))


def test_state_at_t_matches_the_learned_levels(company):
    w = company.world
    assert w.arr0 == pytest.approx(company.habits.habits["company"]["arr_at_t"]["point"])
    assert w.cash0 == pytest.approx(company.habits.habits["company"]["cash_at_t"]["point"])
    assert w.people.n == 20 and w.people.engineer.sum() == 10


def test_same_seed_gives_the_same_cube(company, forks, base):
    again = company.run(compile_knobs(forks), N, SEED)
    for k, v in base.metrics.items():
        np.testing.assert_array_equal(v, again.metrics[k], err_msg=k)
    for k, v in base.checks.items():
        np.testing.assert_array_equal(v, again.checks[k], err_msg=k)
    a = export.build_cube(base, "t", company.world.anchor, SEED)
    b = export.build_cube(again, "t", company.world.anchor, SEED)
    np.testing.assert_array_equal(a.values, b.values)


def test_other_seed_gives_other_values(company, forks, base):
    other = company.run(compile_knobs(forks), N, SEED + 1)
    assert not np.array_equal(base.metrics["cash"], other.metrics["cash"])


def test_scenarios_are_prefix_stable_in_n(company, forks, base):
    small = company.run(compile_knobs(forks), N // 2, SEED)
    np.testing.assert_array_equal(small.metrics["cash"], base.metrics["cash"][:, : N // 2])


def test_a_fork_without_interventions_equals_the_baseline(company):
    k = compile_knobs([_fork("status_quo"), _fork("nothing")])
    r = company.run(k, N, SEED)
    for name, v in r.metrics.items():
        np.testing.assert_array_equal(v[0], v[1], err_msg=name)


def test_forks_share_the_scenarios_before_their_first_intervention(company):
    late = _fork(
        "late_hire", Intervention("actor", "add", "role:backend", 6, 5, {"cost_per_year": 150_000.0})
    )
    r = company.run(compile_knobs([_fork("status_quo"), late]), N, SEED)
    np.testing.assert_array_equal(r.metrics["cash"][0, :, :6], r.metrics["cash"][1, :, :6])
    assert not np.array_equal(r.metrics["cash"][0, :, 8:], r.metrics["cash"][1, :, 8:])


def test_more_hires_never_lower_capacity(company):
    forks = [
        _fork(f"hire_{n}", Intervention("actor", "add", "role:backend", 0, n, {"cost_per_year": 150_000.0}))
        for n in (0, 2, 5)
    ]
    r = company.run(compile_knobs(forks), N, SEED)
    cap = r.metrics["capacity"][:, :, 1:].mean(axis=(1, 2))
    assert cap[0] <= cap[1] <= cap[2]
    head = r.metrics["headcount"][:, :, -1].mean(axis=1)
    assert head[0] <= head[1] <= head[2]


def test_a_bigger_cost_shock_never_gives_more_cash(company):
    """Vendor price and regulation shocks only add cost: pathwise, per scenario."""
    k1 = compile_knobs([_fork("status_quo")])
    k2 = compile_knobs([_fork("status_quo")])
    k2.shock_size_scale = {"vendor_price_event": 2.0, "regulation_event": 2.0}
    a = company.run(k1, N, SEED).metrics["cash"][0]
    b = company.run(k2, N, SEED).metrics["cash"][0]
    assert (b <= a + 1e-6).all()
    assert (b < a - 1).any()


def test_a_likelier_risk_never_helps(company, forks):
    layers = {x.id: x for x in company_layers(company.cfg)}
    base = company.run(compile_knobs(forks[:1]), N, SEED)
    for lid, metric in (
        ("price_war", "arr"),
        ("gpu_crunch", "cash"),
        ("big_client_leaves", "arr"),
        ("downturn", "new_arr"),
    ):
        worse = company.run(compile_knobs(forks[:1], [layers[lid]]), N, SEED)
        assert worse.metrics[metric][0, :, -1].mean() <= base.metrics[metric][0, :, -1].mean() + 1e-6, lid


def test_a_higher_churn_hazard_never_gives_more_arr(company):
    p = company.params["churn_p"]
    th_lo = {k: np.full(N, v.point) for k, v in company.params.items()}
    th_hi = dict(th_lo) | {"churn_p": np.full(N, p.point * 1.5)}
    k = compile_knobs([_fork("status_quo")])
    lo = company.run(k, N, SEED, theta=th_lo).metrics
    hi = company.run(k, N, SEED, theta=th_hi).metrics
    assert (hi["churn"][0, :, -1] >= lo["churn"][0, :, -1] - 1e-9).mean() > 0.95
    assert hi["arr"][0, :, -1].mean() < lo["arr"][0, :, -1].mean()


def test_the_case_forks_move_their_own_levers(base):
    j = {f: i for i, f in enumerate(base.forks)}
    m = base.metrics
    end = lambda name, f: m[name][j[f], :, -1].mean()  # noqa: E731
    assert end("headcount", "hire_5") > end("headcount", "status_quo") + 3
    assert m["cost"][j["reserve_gpu"], :, 1:].sum() > m["cost"][j["status_quo"], :, 1:].sum()
    assert end("arr", "raise_prices_8") > end("arr", "status_quo")
    assert end("churn", "raise_prices_8") > end("churn", "status_quo")
    assert m["incidents"][j["pay_tech_debt"], :, 1:].sum() <= m["incidents"][j["status_quo"], :, 1:].sum()
    assert end("debt", "pay_tech_debt") < end("debt", "status_quo")


def test_cube_goes_through_the_existing_ranking(company, base):
    cube = export.build_cube(base, "company_best_year:test", company.world.anchor, SEED)
    dec = export.decide(cube, company.cfg)
    assert {o["option"] for o in dec["options"]} == set(base.forks)
    assert sum(o["pBest"] for o in dec["options"]) == pytest.approx(1.0, abs=1e-3)  # 4 decimals each
    assert dec["bestPath"]["decision"] == dec["options"][0]["option"]


def test_meaningful_differences_of_the_case_reach_the_ranking(company, base):
    from variator.metrics.cube import to_tensor_run

    ties = export.criterion_ties(company.cfg)
    if not ties:
        pytest.skip("the case config has no meaningful differences")
    cube = export.build_cube(base, "t", company.world.anchor, SEED)
    run = export.with_case_ties(to_tensor_run(cube, export.criteria_specs(company.cfg)), company.cfg)
    assert {c.name: c.tie_within for c in run.criteria} == ties
    huge = {
        **company.cfg,
        "variables": [
            {**v, "dist": {"kind": "fixed", "value": 1e12}} if v["name"].startswith("md_") else v
            for v in company.cfg["variables"]
        ],
    }
    dec = export.decide(cube, huge)  # every difference is a tie: every fork scores the same
    assert len({o["score"] for o in dec["options"]}) == 1


def test_a_sub_threshold_difference_does_not_change_the_actor_ranking(company, base):
    """pay_tech_debt 20k or 60k below status_quo on cash in every scenario: both are inside the meaningful
    difference (md_cash = 100k), so the ranking is the same. 150k is above it: the ranking changes."""
    ties = export.criterion_ties(company.cfg)
    if ties.get("cash", 0) < 70_000:
        pytest.skip("needs a meaningful difference of cash above 70k")
    # two forks only: with more forks a group can chain to a third fork in some scenarios (single linkage)
    pair = company.run(
        compile_knobs([f for f in company_forks(company.cfg) if f.id in ("status_quo", "pay_tech_debt")]),
        N,
        SEED,
    )
    j = {f: i for i, f in enumerate(pair.forks)}
    k = export.build_cube(pair, "t", company.world.anchor, SEED).metric_index("cash")

    def decided(gap: float, cfg) -> list[tuple]:
        cube = export.build_cube(pair, "t", company.world.anchor, SEED)
        cube.values[:, j["pay_tech_debt"], k, :] = cube.values[:, j["status_quo"], k, :] - gap
        return [(o["option"], o["score"], o["pBest"]) for o in export.decide(cube, cfg)["options"]]

    small = decided(20_000, company.cfg)
    assert small == decided(60_000, company.cfg)
    assert small != decided(150_000, company.cfg)
    no_ties = {**company.cfg, "criteria": [{**c, "tie": None} for c in company.cfg["criteria"]]}
    assert decided(20_000, no_ties) != small  # without the ties the 20k gap counts on cash


def test_risk_calendar_rows_are_monotone(company, base):
    cal = export.risk_calendar(base, company.cfg)
    for rows in cal:
        for r in rows:
            assert len(r["pByMonth"]) == 12
            assert all(b >= a - 1e-12 for a, b in zip(r["pByMonth"], r["pByMonth"][1:], strict=False))
            assert r["pEver"] == pytest.approx(r["pByMonth"][-1])


def test_month_json_has_the_page_schema(company, forks, base):
    cube = export.build_cube(base, "t", company.world.anchor, SEED)
    doc = export.month_json(
        base, case_id="company_best_year", cfg=company.cfg, params=company.params,
        decision=export.decide(cube, company.cfg), provenance={"seed": SEED}, anchor=company.world.anchor,
        labels={f.id: f.label for f in forks},
    )  # fmt: skip
    assert doc["source"] == "actors" and doc["preview"] is False
    for key in (
        "caseId",
        "note",
        "seed",
        "n",
        "anchor",
        "horizonMonths",
        "metrics",
        "weekMetrics",
        "dayMetrics",
        "options",
        "weekTrees",
        "dayExamples",
    ):
        assert key in doc
    opt = doc["options"]["status_quo"]
    assert set(opt["fans"]) >= {"cash", "churn", "sla", "nps", "runway"}
    point = opt["fans"]["cash"][0]
    assert set(point) >= {"month", "p10", "p25", "p50", "p75", "p90", "p5", "p95"}
    assert len(opt["fans"]["cash"]) == 13
    assert all(p["source"] for p in doc["parameters"])


def test_parameters_come_from_the_learned_habits(company):
    """Behaviour parameters are learned (data: or group:) unless marked case: or assumption:."""
    for p in company.params.values():
        assert p.source.split(":")[0] in {"data", "group", "case", "assumption", "llm"}, p
    learned = [p for p in company.params.values() if p.source.startswith(("data:", "group:"))]
    assert len(learned) >= 30


def test_key_clients_pool_toward_their_own_history(company):
    """No key client gave notice in the history: the pooled factor is below 1 but stays uncertain."""
    p = company.params["key_churn_factor"]
    assert p.source.startswith("data:") and "pooled" in p.source
    assert 0.3 < p.point < 1.0 and p.low < p.point < p.high and p.high > 1.0


def test_engine_does_not_read_the_generator_truth():
    import variator.future as fut

    src = "".join(p.read_text() for p in __import__("pathlib").Path(fut.__file__).parent.glob("*.py"))
    assert "orgsim.company" not in src
    assert "spec.py" not in src


def test_replace_keeps_knobs_independent(company, forks):
    k = compile_knobs(forks)
    k2 = replace(k, cash_scale=0.5)
    assert k.cash_scale == 1.0 and k2.cash_scale == 0.5
