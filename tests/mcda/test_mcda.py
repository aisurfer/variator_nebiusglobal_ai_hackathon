import numpy as np
import pytest

from variator.domain import Criterion
from variator.mcda import normalize, pareto_mask, rank, topsis, weight_robustness, weight_vector
from variator.simulation import TensorRun

HIGHER = np.array([True, False])


def test_rank_reversal_check():
    from variator.mcda.core import rank_reversals

    higher, w = np.array([True, True]), np.array([0.5, 0.5])
    m = np.array([[1.0, 0.0], [2.0, 6.0], [5.0, 2.0], [9.0, 7.0]])  # with d: b > c; without d: c > b
    assert rank_reversals(m, higher, w, ["a", "b", "c", "d"]) == ["d"]
    dominant = np.array([[3.0, 3.0], [2.0, 2.0], [1.0, 1.0]])
    assert rank_reversals(dominant, higher, w, ["a", "b", "c"]) == []


def test_normalize_respects_direction_and_ties():
    m = np.array([[10.0, 1.0], [20.0, 3.0], [15.0, 3.0]])
    n = normalize(m, HIGHER)
    assert n[:, 0].tolist() == [0.0, 1.0, 0.5]
    assert n[:, 1].tolist() == [1.0, 0.0, 0.0]
    assert normalize(np.ones((2, 2)), HIGHER).tolist() == [[1.0, 1.0], [1.0, 1.0]]


def test_pareto_front():
    m = np.array([[10.0, 1.0], [20.0, 3.0], [9.0, 2.0]])  # third is dominated by the first
    assert pareto_mask(m, HIGHER).tolist() == [True, True, False]


def test_topsis_prefers_dominant_option():
    m = np.array([[10.0, 1.0], [5.0, 5.0]])
    close = topsis(m, HIGHER, np.array([0.5, 0.5]))
    assert close[0] == pytest.approx(1.0) and close[1] == pytest.approx(0.0)


def test_weight_vector_validation():
    assert weight_vector(["a", "b"], {"a": 3}, [1, 1]).tolist() == [0.75, 0.25]
    with pytest.raises(ValueError, match="unknown"):
        weight_vector(["a"], {"z": 1}, [1])
    with pytest.raises(ValueError, match="> 0"):
        weight_vector(["a"], {"a": 0}, [1])


def test_robustness_is_one_for_dominant_option():
    m = np.array([[10.0, 1.0], [5.0, 5.0]])
    shares = weight_robustness(m, HIGHER, np.array([0.5, 0.5]))
    assert shares.tolist() == [1.0, 0.0]


def _run(values: np.ndarray) -> TensorRun:
    return TensorRun(
        criteria=[Criterion(name="money"), Criterion(name="time", higher_is_better=False)],
        option_names=["fast", "cheap"],
        values=values,
        flags=np.full(values.shape, -1, dtype=np.int8),
        rule_hits={},
        check_failures={},
        seed=0,
        backend="numpy",
        elapsed_s=0.0,
    )


def test_weights_flip_the_ranking():
    rng = np.random.default_rng(0)
    s = 2_000
    values = np.empty((s, 2, 2))
    values[:, 0, 0] = rng.normal(100, 5, s)  # fast: less money
    values[:, 1, 0] = rng.normal(130, 5, s)  # cheap: more money
    values[:, 0, 1] = rng.normal(3, 0.3, s)  # fast: short time
    values[:, 1, 1] = rng.normal(9, 0.3, s)
    run = _run(values)
    money_first = rank(run, {"money": 1, "time": 0.1})
    time_first = rank(run, {"money": 0.1, "time": 1})
    assert money_first.options[0].option == "cheap"
    assert time_first.options[0].option == "fast"
    assert all(o.pareto for o in money_first.options)
    assert money_first.options[0].prob_best > 0.9
    assert any("better" in t for t in money_first.tradeoffs)
