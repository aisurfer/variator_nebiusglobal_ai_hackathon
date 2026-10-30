"""Case A on actors: determinism, the team actors and the sanity laws (paired runs)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from variator.future import delivery

N = 300
SEED = 3
AS_OF = date(2026, 10, 27)


@pytest.fixture(scope="module")
def params(company):
    from variator.future.forks import case_config

    return delivery.delivery_params(company.habits, case_config(delivery.CASE))


@pytest.fixture(scope="module")
def base(params):
    return delivery.simulate(params, N, SEED, as_of=AS_OF)


def test_same_seed_same_result(params, base):
    again = delivery.simulate(params, N, SEED, as_of=AS_OF)
    for k, v in base.metrics.items():
        np.testing.assert_array_equal(v, again.metrics[k], err_msg=k)


def test_team_actors(base):
    j = {f: i for i, f in enumerate(base.forks)}
    fte = base.metrics["fte"]
    assert (fte[j["in_house"], :, 1] < 1.0).mean() > 0.9  # the hires are not there in month 1
    assert fte[j["in_house"]].max() <= 8 * 2.5  # 8 seats (pace spread around 1)
    spend = base.metrics["spend"][j["offshore_fixed"], :, 1]
    assert (spend >= 0.2 * base.theta["fixed_quote"] - 1).all()  # 20% of the fixed price at signing
    assert (base.metrics["progress"] <= 1 + 1e-9).all()


def test_more_scope_never_shortens_the_project(params, base):
    more = delivery.simulate(params, N, SEED, as_of=AS_OF, layer={"scope_scale": 1.3})
    assert (more.criteria["time"] >= base.criteria["time"] - 1e-9).all()


def test_more_attrition_never_helps_the_in_house_team(params, base):
    worse = delivery.simulate(params, N, SEED, as_of=AS_OF, layer={"resign_scale": 2.0})
    assert worse.criteria["money"][0].mean() <= base.criteria["money"][0].mean() + 1e-6


def test_ranking_goes_through_the_cube(company, base):
    from variator.future.forks import case_config

    cube = delivery.build_cube(base, AS_OF, SEED)
    dec = delivery.decide(cube, case_config(delivery.CASE))
    assert {o["option"] for o in dec["options"]} == set(base.forks)


def test_the_slowdown_of_other_work_is_inside_the_in_house_money(company, params):
    team = delivery.crew(company.habits)
    assert len(team["ids"]) == delivery.CREW and team["pace_share"] > 0.5
    res = delivery.simulate(params, N, SEED, as_of=AS_OF, crew_=team)
    before = {k: v.copy() for k, v in res.criteria.items()}
    line = delivery.charge_slowdown(res, company.habits, company.world, SEED)
    j = res.forks.index("in_house")
    loss = res.metrics["other_loss"][j, :, -1]
    np.testing.assert_allclose(res.criteria["money"][j], before["money"][j] - loss)
    assert np.median(loss) > 0 and line["loss_to_month_24"]["p50"] > 0
    for f in ("nearshore_tm", "offshore_fixed"):
        k = res.forks.index(f)
        np.testing.assert_array_equal(res.criteria["money"][k], before["money"][k])
        assert not res.metrics["other_loss"][k].any()
