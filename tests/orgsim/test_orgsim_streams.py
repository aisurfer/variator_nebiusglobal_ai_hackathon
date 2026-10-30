"""Keyed streams: labels, determinism, common random numbers, first passage, arrivals."""

import numpy as np
import pytest

from variator.orgsim.streams import (
    NEVER,
    Streams,
    arrival_counts,
    child_key,
    cumulative_hazard,
    first_passage,
    hazard_of_probability,
    slot_key,
    slot_keys,
    stream_label,
)
from variator.synthetic.company import derived_rng


def test_label_follows_the_design_and_escapes_separators():
    assert Streams(7).label("s17", "churn", "contract-042", "time") == "s17:churn:contract-042:time"
    assert stream_label("a:b", "c") != stream_label("a", "b:c")
    assert Streams(7, ("tree/0/2",)).label("hire", "x") == "tree/0/2:hire:x"
    with pytest.raises(ValueError):
        stream_label("a", "")


def test_rng_is_derived_rng_of_the_label():
    a = Streams(7).event_rng(17, "churn", "contract-042", "time").random(5)
    b = derived_rng(7, "s17:churn:contract-042:time").random(5)
    assert np.array_equal(a, b)


def test_same_seed_same_draws_other_seed_other_draws():
    ids = ["c1", "c2", "c3"]
    a = Streams(11).unit_exponentials("churn", ids, "time", 8)
    b = Streams(11).unit_exponentials("churn", ids, "time", 8)
    c = Streams(12).unit_exponentials("churn", ids, "time", 8)
    assert a.shape == (8, 3) and np.array_equal(a, b) and not np.array_equal(a, c)


def test_crn_same_key_same_draw_whatever_other_keys():
    s = Streams(5)
    full = s.unit_exponentials("churn", ["c1", "c2", "c3"], "time", 20)
    other = s.unit_exponentials("churn", ["c9", "c3", "x"], "time", [17, 3])
    assert other[0, 1] == full[17, 2] and other[1, 1] == full[3, 2]
    # another purpose or process is another stream
    assert not np.array_equal(full, s.unit_exponentials("churn", ["c1", "c2", "c3"], "amount", 20))
    assert not np.array_equal(full, s.unit_exponentials("upsell", ["c1", "c2", "c3"], "time", 20))


def test_more_draws_keep_the_first_ones():
    s = Streams(3)
    few = s.unit_exponentials("incidents", ["svc"], "arrivals", 4, count=3)
    many = s.unit_exponentials("incidents", ["svc"], "arrivals", 4, count=10)
    assert few.shape == (4, 1, 3) and np.array_equal(many[..., :3], few)
    single = s.unit_exponentials("incidents", ["svc"], "arrivals", 4)
    assert np.array_equal(single, few[..., 0])


def test_unit_exponentials_have_mean_one():
    e = Streams(1).unit_exponentials("p", [f"e{i}" for i in range(4)], "t", 2500)
    assert abs(e.mean() - 1) < 0.04 and (e > 0).all()
    u = Streams(1).uniforms("p", ["e0"], "u", 2000)
    assert 0 < u.min() and u.max() < 1 and abs(u.mean() - 0.5) < 0.03


def test_slot_and_child_keys():
    assert slot_key("hire", 3) == "hire-slot-3"
    assert slot_keys("deal", 2) == ["deal-slot-1", "deal-slot-2"]
    assert child_key("tree/0", 2) == "tree/0/2"
    with pytest.raises(ValueError):
        slot_key("hire", 0)


def test_first_passage_by_hand():
    cum = np.array([[0.1, 0.5, 0.9, 1.4], [0.0, 0.0, 0.1, 0.2]])
    e = np.array([0.5, 1.0])
    assert first_passage(cum, e).tolist() == [1, NEVER]
    assert first_passage(cum, 0.0).tolist() == [0, 0]


def test_first_passage_broadcasts_e_over_options():
    rng = np.random.default_rng(0)
    cum = cumulative_hazard(rng.uniform(0, 0.3, size=(5, 2, 3, 12)))  # (S, O, N, T)
    e = rng.exponential(size=(5, 1, 3))
    out = first_passage(cum, e)
    assert out.shape == (5, 2, 3)
    assert np.array_equal(out[:, 0], first_passage(cum[:, 0], e[:, 0]))


@pytest.mark.parametrize("seed", range(20))
def test_first_passage_is_monotone_in_the_hazard(seed):
    rng = np.random.default_rng(seed)
    base = rng.uniform(0, 0.2, size=(50, 4, 12))
    more = base + rng.uniform(0, 0.2, size=base.shape) * rng.integers(0, 2, size=base.shape)
    e = rng.exponential(size=(50, 4))
    t_base = first_passage(cumulative_hazard(base), e)
    t_more = first_passage(cumulative_hazard(more), e)
    late = lambda t: np.where(t == NEVER, 10**9, t)  # noqa: E731
    assert (late(t_more) <= late(t_base)).all()


def test_hazard_of_probability_gives_the_period_probability():
    p = 0.2
    cum = cumulative_hazard(np.full((1, 6), hazard_of_probability(p)))
    e = Streams(9).unit_exponentials("churn", ["c"], "time", 20000)[:, 0]
    first = first_passage(cum, e[:, None])[:, 0]
    assert abs((first == 0).mean() - p) < 0.015
    assert abs((first == NEVER).mean() - (1 - p) ** 6) < 0.015
    assert hazard_of_probability(0.0) == 0 and np.isinf(hazard_of_probability(1.0))
    with pytest.raises(ValueError):
        hazard_of_probability(1.5)


def test_arrival_counts_match_a_loop_and_flag_saturation():
    rng = np.random.default_rng(4)
    cum = cumulative_hazard(rng.uniform(0, 2, size=(30, 8)))
    e = rng.exponential(size=(30, 40))
    counts, saturated = arrival_counts(cum, e)
    for i in range(30):
        arrivals = np.cumsum(e[i])
        want = [
            int(((arrivals > (cum[i, t - 1] if t else -np.inf)) & (arrivals <= cum[i, t])).sum())
            for t in range(8)
        ]
        assert counts[i].tolist() == want
    assert not saturated.any()
    _, sat = arrival_counts(cum, e[:, :2])
    assert sat.any()


def test_arrival_counts_grow_with_the_hazard():
    rng = np.random.default_rng(8)
    base = rng.uniform(0, 1, size=(40, 10))
    e = rng.exponential(size=(40, 60))
    low, _ = arrival_counts(cumulative_hazard(base), e)
    high, _ = arrival_counts(cumulative_hazard(base * 1.5), e)
    assert (np.cumsum(high, axis=-1) >= np.cumsum(low, axis=-1)).all()
