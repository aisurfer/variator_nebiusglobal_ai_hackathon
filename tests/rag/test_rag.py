import numpy as np
import pytest

from variator.domain import (
    Criterion,
    DecisionModel,
    DecisionOption,
    Distribution,
    MetricSpec,
    RagThresholds,
    Variable,
)
from variator.rag import classify, default_thresholds, rag_report, resolve_thresholds, tunnel
from variator.simulation import simulate_tensor


def test_classify_both_directions():
    criteria = [Criterion(name="m"), Criterion(name="t", higher_is_better=False)]
    limits = {"m": RagThresholds(green=10, red=0), "t": RagThresholds(green=3, red=6)}
    values = np.array([[12.0, 2.0], [5.0, 4.0], [-1.0, 7.0]])
    assert classify(values, criteria, limits).tolist() == [[0, 0], [1, 1], [2, 2]]


def test_wrong_limit_order_is_rejected():
    with pytest.raises(ValueError, match="green >= red"):
        Criterion(name="m", rag=RagThresholds(green=0, red=10))


def _model() -> DecisionModel:
    u = Distribution(kind="uniform", low=0, high=1)
    return DecisionModel(
        question="q",
        metric=MetricSpec(name="v"),
        variables=[
            Variable(name="a", description="a", distribution=u),
            Variable(name="b", description="b", distribution=u),
        ],
        options=[
            DecisionOption(name="safe", description="s", formula="0.5 + 0 * a"),
            DecisionOption(name="bet", description="b", formula="a + b - 0.5"),
        ],
    )


def test_default_limits_and_report():
    run = simulate_tensor(_model(), n_scenarios=10_000, seed=1)
    limits = default_thresholds(run, baseline="safe")
    assert limits["value"].green == pytest.approx(0.5) and limits["value"].red == pytest.approx(0.5)
    report = rag_report(run, resolve_thresholds(run, {"value": RagThresholds(green=0.8, red=0.2)}))
    bet = next(o for o in report.options if o.option == "bet")
    # a + b - 0.5 >= 0.8  <=> a + b >= 1.3: P = 0.7^2 / 2 = 0.245
    assert bet.overall.green == pytest.approx(0.245, abs=0.02)
    assert bet.overall.green + bet.overall.amber + bet.overall.red == pytest.approx(1.0)


def test_tunnel_grid_shows_where_option_goes_red():
    model = _model()
    limits = {"value": RagThresholds(green=0.8, red=0.2)}
    tm = tunnel(
        model, x="a", y="b", limits=limits, grid=5, samples_per_cell=50, x_range=(0, 1), y_range=(0, 1)
    )
    assert len(tm.green["bet"]) == 5 and len(tm.green["bet"][0]) == 5
    assert tm.red["bet"][0][0] == 1.0  # a = b = 0 -> -0.5, red
    assert tm.green["bet"][4][4] == 1.0  # a = b = 1 -> 1.5, green
    assert tm.best[4][4] == "bet" and tm.best[0][0] == "safe"
