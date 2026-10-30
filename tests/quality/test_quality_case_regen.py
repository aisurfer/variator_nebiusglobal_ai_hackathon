"""The harness of the case re-drafting test (scripts/quality_case_regen.py): keys, tie sets, Wilson, summary."""

# ruff: noqa: E501 - records and fixtures, one per line

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from variator.metrics.cube import MetricCube, MetricInfo

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "quality_case_regen.py"
_SPEC = importlib.util.spec_from_file_location("quality_case_regen", _PATH)
qr = importlib.util.module_from_spec(_SPEC)
sys.modules["quality_case_regen"] = qr
_SPEC.loader.exec_module(qr)


def _iv(target: str, op: str = "set", value: float = 0.0, at: float = 0.0, **params) -> dict:
    return {
        "kind": "factor",
        "op": op,
        "target": target,
        "at": at,
        "value": value,
        "params": params,
        "label": "",
    }


def test_fork_key_is_the_set_of_levers():
    assert qr.fork_key([]) == "plan"
    assert qr.fork_key([_iv("price.list", "scale", 0.05)]) == "price"
    assert qr.fork_key([_iv("role:backend", "add", 3), _iv("capacity.debt_share", value=0.1)]) == "debt+hire"
    # a role without "add" and a target outside the levers are not levers
    assert qr.fork_key([_iv("role:backend", "remove", 1), _iv("deals.rate", "scale", 1.1)]) == "plan"
    assert (
        qr.fork_key([_iv("vendor:second_cloud", "add", 2e5), _iv("price.list", "scale", 0.1)]) == "gpu+price"
    )


def test_lever_params_read_the_values_as_compile_knobs_does():
    p = qr.lever_params(
        [_iv("role:backend", "add", 4, at=2, cost_per_year=120_000.0), _iv("price.list", "scale", 0.06, at=1)]
    )
    assert p == {
        "hire": {"month": 2.0, "count": 4.0, "costPerYear": 120_000.0},
        "price": {"month": 1.0, "change": 0.06},
    }


def test_wilson_interval():
    lo, hi = qr.wilson(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-3) and hi == pytest.approx(0.9433, abs=1e-3)
    assert qr.wilson(0, 0) == (0.0, 1.0)
    lo, hi = qr.wilson(100, 100)
    assert hi == pytest.approx(1.0) and lo == pytest.approx(0.963, abs=1e-3)


def test_tie_set_keeps_the_forks_first_in_ten_percent():
    assert qr.tie_set({"a": 151, "b": 30, "c": 19}, 200) == ["a", "b"]
    assert qr.tie_set({"a": 150, "b": 30, "c": 20}, 200) == ["a", "b", "c"]  # 10% exactly counts
    assert qr.tie_set({"a": 179, "b": 21}, 200) == ["a", "b"]
    assert qr.tie_set({"b": 100, "a": 100}, 200) == ["a", "b"]
    assert qr.tie_set({"a": 200}, 200) == ["a"]


def _cube(values: np.ndarray) -> MetricCube:
    S, F = values.shape
    return MetricCube(
        run="t", options=[f"f{j}" for j in range(F)], metrics=[MetricInfo(id="cash")], periods=["m0"],
        values=values.reshape(S, F, 1, 1), checks={"x": np.zeros((S, F, 1), bool)}, inputs={"u": np.arange(S, dtype=float)},
    )  # fmt: skip


def test_bootstrap_resamples_the_scenarios_of_the_cube():
    rng = np.random.default_rng(0)
    cube = _cube(rng.normal(size=(40, 3)))
    sub = qr.resample_cube(cube, np.array([3, 3, 7]))
    assert sub.values.shape == (3, 3, 1, 1) and sub.checks["x"].shape == (3, 3, 1)
    np.testing.assert_array_equal(sub.inputs["u"], [3.0, 3.0, 7.0])
    # the best mean: f1 clearly ahead, f0 and f2 close -> a clear winner and no ties
    v = np.stack([rng.normal(0, 1, 60), rng.normal(5, 1, 60), rng.normal(0, 1, 60)], 1)
    mean_first = lambda c: c.options[int(np.argmax(c.values[:, :, 0, 0].mean(0)))]  # noqa: E731
    counts = qr.first_counts(_cube(v), mean_first, n_boot=50, seed=1)
    assert counts == {"f1": 50} and qr.tie_set(counts, 50) == ["f1"]
    # two forks equal in mean: both win often -> both in the tie set
    w = rng.normal(0, 1, 60)
    v2 = np.stack([w, w + rng.normal(0, 0.01, 60), w - 3], 1)
    counts2 = qr.first_counts(_cube(v2), mean_first, n_boot=50, seed=1)
    assert set(qr.tie_set(counts2, 50)) == {"f0", "f1"} and sum(counts2.values()) == 50
    assert (
        qr.first_counts(_cube(v2), mean_first, n_boot=50, seed=1) == counts2
    )  # the seed fixes the resamples


def _rec(
    run: int, key: str, ties: list[str], p_best: float = 0.5, ok: bool = True, second: float = 0.4
) -> dict:
    if not ok:
        return {"run": run, "ok": False, "error": "invalid draft"}
    forks = [{"id": "keep", "key": "plan", "levers": {}, "unmodelled": []},
             {"id": "up", "key": "price", "levers": {"price": {"month": 0.0, "change": 0.08}}, "unmodelled": []}]  # fmt: skip
    rows = [
        {"fork": "keep" if key == "plan" else "up", "key": key, "scoreTimesFeasible": 0.5, "pBest": p_best, "cashP50": 1.0, "churnP50": 0.2, "npsP50": 30.0},
        {"fork": "up" if key == "plan" else "keep", "key": "price" if key == "plan" else "plan", "pBest": 1 - p_best,
         "scoreTimesFeasible": second, "cashP50": 2.0, "churnP50": 0.25, "npsP50": 28.0},
    ]  # fmt: skip
    return {"run": run, "ok": True, "forks": forks, "ranking": rows, "recommended": rows[0]["fork"],
            "recommendedKey": key, "tieSet": ties, "tieKeys": ties, "nLayers": 3, "nRisks": 5, "wall_s": 20.0}  # fmt: skip


def test_summary_measures():
    recs = [_rec(0, "plan", ["plan"]), _rec(1, "plan", ["plan", "price"]), _rec(2, "price", ["price", "plan"]),
            _rec(3, "price", ["price"], second=0.5), _rec(4, "plan", ["plan"]), _rec(5, "", [], ok=False)]  # fmt: skip
    s = qr.summarize(recs)
    assert s["modalKey"] == "plan" and s["completed"] == 5 and s["failed"] == 1
    assert s["strictAgreement"]["k"] == 3 and s["agreement"]["k"] == 4
    assert (
        s["agreement"]["n"] == 6 and s["strictAgreement"]["n"] == 6
    )  # a failed run counts as a disagreement
    assert s["agreementValid"]["n"] == 5 and s["strictAgreementValid"]["k"] == 3
    assert s["modalFork"]["runsWithModalKey"] == 5
    assert s["inputs"]["leverRuns"]["price"] == 5 and s["inputs"]["parameters"]["price.change"]["p50"] == 0.08
    # supplementary: run 3 ties with "plan" on score x P(feasible) to 4 decimals
    assert s["supplementary"]["agreementWithScoreTies"]["k"] == 5
    assert "Agreement" in qr.summary_md("t", s)
