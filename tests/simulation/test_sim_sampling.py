"""Sobol sampling: lower error than random sampling for the same number of scenarios."""

import numpy as np
import pytest

from variator.domain.examples import warehouse_decision
from variator.simulation import draw_uniforms, get_backend, simulate, simulate_tensor

BE = get_backend("numpy")


def test_sobol_shapes_range_and_notes():
    u, r, notes = draw_uniforms(BE, 7, 1024, 3, 4, "sobol")
    assert u.shape == (1024, 3) and r.shape == (1024, 4) and notes == []
    assert 0 <= u.min() and u.max() < 1
    _, _, notes = draw_uniforms(BE, 7, 1000, 3, 0, "sobol")
    assert "not a power of 2" in notes[0]
    u2, _, _ = draw_uniforms(BE, 7, 1024, 3, 4, "sobol")
    assert np.array_equal(u, u2)  # same seed, same points
    with pytest.raises(ValueError, match="unknown sampling"):
        draw_uniforms(BE, 7, 8, 1, 0, "lhs")


def test_sobol_columns_are_not_correlated():
    # regression: two samplers reused the same Sobol dimensions (13% bias of p5)
    u, r, _ = draw_uniforms(BE, 3, 4096, 2, 2, "sobol")
    both = np.concatenate([u, r], axis=1)
    corr = np.corrcoef(both, rowvar=False)
    assert np.abs(corr - np.eye(4)).max() < 0.05


def test_sobol_mean_error_is_smaller_than_random():
    errors = {"random": [], "sobol": []}
    for seed in range(8):
        for sampling in errors:
            u, _, _ = draw_uniforms(BE, seed, 1024, 5, 0, sampling)
            errors[sampling].append(np.abs(u.mean(axis=0) - 0.5).max())
    assert np.mean(errors["sobol"]) * 5 < np.mean(errors["random"])


def test_engines_accept_sobol():
    model = warehouse_decision()
    run = simulate_tensor(model, n_scenarios=1024, seed=1, sampling="sobol")
    ref = simulate_tensor(model, n_scenarios=100_000, seed=2)
    rel = np.abs(run.values.mean(axis=0) - ref.values.mean(axis=0)) / np.abs(ref.values.mean(axis=0))
    assert rel.max() < 0.01
    single = simulate(model, n_scenarios=1024, seed=1, sampling="sobol", sensitivity=False)
    assert single.scenarios.shape == (1024, len(model.options))


def test_random_sampling_is_unchanged():
    model = warehouse_decision()
    a = simulate_tensor(model, n_scenarios=500, seed=5)
    b = simulate_tensor(model, n_scenarios=500, seed=5, sampling="random")
    assert np.array_equal(a.values, b.values)
