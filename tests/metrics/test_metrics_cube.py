"""Metric cube: shapes, statuses, storage, and the bridge to TensorRun, mcda.rank and best_path."""

import numpy as np
import pytest

from variator.domain.criteria import RagColor
from variator.mcda import best_path, rank
from variator.metrics.cube import (
    STATUS_CODES,
    CriterionSpec,
    MetricCube,
    MetricInfo,
    criterion_name,
    load_cube,
    save_cube,
    to_tensor_run,
)
from variator.orgsim.risk import p_failed_by
from variator.rag import rag_report, resolve_thresholds
from variator.simulation.tensor import NO_FLAG

OPTIONS = ["base", "hire_5", "raise_prices_8"]
METRICS = [
    MetricInfo(id="ceo.cash", title="Cash", unit="kUSD", direction="higher"),
    MetricInfo(id="eng.lead-time", title="Lead time", unit="days", direction="lower"),
    MetricInfo(id="ops.headcount", title="Headcount", unit="people", direction="context"),
]
PERIODS = ["2026-11-01", "2026-12-01", "2027-01-01", "2027-02-01"]


def toy_cube(s: int = 400, seed: int = 0) -> MetricCube:
    """raise_prices_8 is best on cash and lead time; hire_5 beats base on both."""
    rng = np.random.default_rng(seed)
    t = np.arange(len(PERIODS))
    noise = rng.normal(size=(s, 1, len(PERIODS)))  # common to all options (CRN)
    cash = np.stack([100 + 2 * t, 105 + 2 * t, 130 + 2 * t])[None] + 5 * noise
    lead = np.stack([10.0 - 0 * t, 8.0 - 0 * t, 7.0 - 0 * t])[None] + 0.5 * noise
    head = np.full((s, 3, len(PERIODS)), 20.0)
    values = np.stack([cash, lead, head], axis=2)
    return MetricCube(
        run="toy",
        options=OPTIONS,
        metrics=METRICS,
        periods=PERIODS,
        values=values,
        seed=seed,
        grain="month",
        anchor="2026-11-01",
        inputs={"price_shock": rng.normal(size=s)},
    )


def test_shapes_are_checked_and_default_status_is_set():
    cube = toy_cube(10)
    assert cube.shape == (10, 3, 3, 4) and cube.n_scenarios == 10
    assert (cube.status == STATUS_CODES["neutral"]).all()
    with pytest.raises(ValueError):
        MetricCube(run="x", options=OPTIONS, metrics=METRICS, periods=PERIODS, values=np.zeros((2, 3, 3, 3)))
    with pytest.raises(ValueError):
        MetricCube(
            run="x",
            options=OPTIONS,
            metrics=METRICS,
            periods=PERIODS,
            values=np.zeros((2, 3, 3, 4)),
            checks={"c": np.zeros((2, 3, 4))},
        )  # not boolean
    v = np.zeros((2, 3, 3, 4))
    v[0, 0, 0, 0] = np.nan
    assert (
        MetricCube(run="x", options=OPTIONS, metrics=METRICS, periods=PERIODS, values=v).status[0, 0, 0, 0]
        == 4
    )
    assert (
        cube.period_index(None) == 3 and cube.period_index(-2) == 2 and cube.period_index("2026-12-01") == 1
    )


def test_status_from_bands():
    cube = toy_cube(50)
    cube.set_status_from_bands("ceo.cash", {"good": 120, "warn": 100})
    cash = cube.series("ceo.cash", "raise_prices_8")
    st = cube.status[:, 2, 0, :]
    assert ((cash >= 120) == (st == 0)).all()
    cube.set_status_from_bands("eng.lead-time", {"good": 7.5, "warn": 9})
    lead = cube.series("eng.lead-time", "base")
    assert ((lead > 9) == (cube.status[:, 0, 1, :] == 2)).all()


def test_save_and_load_round_trip_with_same_bytes(tmp_path):
    cube = toy_cube(30)
    cube.checks["cash_low"] = cube.values[:, :, 0, :] < 100
    save_cube(cube, tmp_path / "a")
    save_cube(cube, tmp_path / "b")
    assert (tmp_path / "a" / "arrays.npz").read_bytes() == (tmp_path / "b" / "arrays.npz").read_bytes()
    back = load_cube(tmp_path / "a")
    assert np.array_equal(back.values, cube.values) and np.array_equal(back.status, cube.status)
    assert np.array_equal(back.checks["cash_low"], cube.checks["cash_low"])
    assert np.array_equal(back.inputs["price_shock"], cube.inputs["price_shock"])
    assert (back.run, back.options, back.metrics, back.periods, back.seed, back.grain) == (
        cube.run,
        cube.options,
        cube.metrics,
        cube.periods,
        cube.seed,
        cube.grain,
    )
    blob = bytearray((tmp_path / "a" / "arrays.npz").read_bytes())
    blob[-30] ^= 1
    (tmp_path / "a" / "arrays.npz").write_bytes(bytes(blob))
    with pytest.raises(ValueError):
        load_cube(tmp_path / "a")


def test_bridge_builds_the_tensor():
    cube = toy_cube()
    run = to_tensor_run(
        cube, [("ceo.cash", "higher", 1.0), CriterionSpec(metric="eng.lead-time", weight=2.0)]
    )
    assert run.values.shape == (400, 3, 2) and run.option_names == OPTIONS
    assert [c.name for c in run.criteria] == ["ceo_cash", "eng_lead_time"]
    assert [c.higher_is_better for c in run.criteria] == [True, False] and run.criteria[1].weight == 2.0
    assert np.array_equal(run.values[:, :, 0], cube.values[:, :, 0, 3])
    assert not run.infeasible_mask().any() and (run.flags == NO_FLAG).all()
    assert run.probes["in:price_shock"].shape == (400, 3)
    assert np.array_equal(run.probes["in:price_shock"][:, 1], cube.inputs["price_shock"])
    early = to_tensor_run(cube, [{"metric": "ceo.cash", "reduce": "min"}], period="2026-12-01")
    assert np.array_equal(early.values[:, :, 0], cube.values[:, :, 0, :2].min(axis=-1))


def test_rank_and_best_path_on_a_constructed_case():
    cube = toy_cube()
    criteria = [("ceo.cash", "higher", 1.0), ("eng.lead-time", "lower", 1.0)]
    run = to_tensor_run(cube, criteria)
    ranking = rank(run)
    assert [o.option for o in ranking.options] == ["raise_prices_8", "hire_5", "base"]
    # fixed anchors (mcda, QA M3): in a few extreme scenarios two forks reach the end of the scale and tie
    assert ranking.options[0].prob_best > 0.95
    assert best_path(run).decision == "raise_prices_8"
    report = rag_report(run, resolve_thresholds(run))
    assert {o.option for o in report.options} == set(OPTIONS)


def test_a_path_check_moves_the_winner():
    cube = toy_cube()
    rng = np.random.default_rng(1)
    mask = np.zeros((400, 3, 4), dtype=bool)
    mask[:, 2, 1] = rng.random(400) < 0.95  # raise_prices_8 breaks a covenant in month 2 in 95% of scenarios
    cube.checks["covenant"] = mask
    run = to_tensor_run(cube, [("ceo.cash", "higher", 1.0), ("eng.lead-time", "lower", 1.0)])
    assert run.check_failures["covenant"] == pytest.approx(p_failed_by(mask)[:, 3].tolist())
    assert rank(run).options[0].option == "hire_5"
    path = best_path(run)
    assert path.decision == "hire_5"
    assert run.probes["first_fail:covenant"][mask[:, 2, 1], 2].tolist() == [1.0] * int(mask[:, 2, 1].sum())
    at_end = to_tensor_run(
        cube, [("ceo.cash", "higher", 1.0)], checks="at"
    )  # the failure was in month 2 only
    assert not at_end.infeasible_mask().any()
    before = to_tensor_run(cube, [("ceo.cash", "higher", 1.0)], period=0)  # no failure by month 1
    assert not before.infeasible_mask().any()


def test_status_gives_flags():
    cube = toy_cube(100)
    cube.set_status_from_bands("ceo.cash", {"good": 120, "warn": 100})
    run = to_tensor_run(cube, [("ceo.cash", "higher", 1.0)])
    status = cube.status[:, :, 0, 3]
    assert ((status == 1) == (run.flags[:, :, 0] == int(RagColor.AMBER))).all()
    assert ((status == 2) == (run.flags[:, :, 0] == int(RagColor.RED))).all()
    assert ((status == 0) == (run.flags[:, :, 0] == NO_FLAG)).all()
    plain = to_tensor_run(cube, [("ceo.cash", "higher", 1.0)], flags_from_status=False)
    assert (plain.flags == NO_FLAG).all()


def test_unknown_values_raise_or_become_infeasible():
    cube = toy_cube(50)
    cube.values[3, 1, 0, 3] = np.nan
    with pytest.raises(ValueError, match="unknown"):
        to_tensor_run(cube, [("ceo.cash", "higher", 1.0)])
    run = to_tensor_run(cube, [("ceo.cash", "higher", 1.0)], unknown="infeasible")
    assert np.isfinite(run.values).all() and run.infeasible_mask().sum() == 1 and run.infeasible_mask()[3, 1]
    assert run.values[3, 1, 0] == np.nanmin(cube.values[:, 1, 0, 3])
    assert rank(run).options[0].option == "raise_prices_8"


def test_criteria_rules():
    cube = toy_cube(10)
    with pytest.raises(ValueError, match="context"):
        to_tensor_run(cube, [("ops.headcount",)])
    run = to_tensor_run(cube, [("ops.headcount", True), ("ceo.cash",), ("ceo.cash", "lower")])
    assert [c.name for c in run.criteria] == ["ops_headcount", "ceo_cash", "ceo_cash_2"]
    with pytest.raises(ValueError):
        to_tensor_run(cube, [("ceo.cash", "up")])
    with pytest.raises(ValueError):
        to_tensor_run(cube, [])
    assert criterion_name("2026.cash-runway") == "m_2026_cash_runway" and criterion_name("class") == "class_"
