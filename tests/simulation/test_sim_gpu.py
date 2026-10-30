"""CuPy backend checks. Skipped unless CuPy and an NVIDIA GPU are available (see conftest)."""

import time

import numpy as np
import pytest
from scipy import stats

from variator.domain import Correlation, Distribution, DistributionKind
from variator.simulation import (
    AUTO_GPU_MIN_SCENARIOS,
    build_copula,
    compile_formula,
    get_backend,
    ppf,
    simulate,
)

pytestmark = pytest.mark.gpu
pytest.importorskip("cupy")

DISTRIBUTIONS = [
    Distribution(kind=DistributionKind.NORMAL, mean=9.0, std=1.2),
    Distribution(kind=DistributionKind.LOGNORMAL, mean=100.0, std=40.0),
    Distribution(kind=DistributionKind.UNIFORM, low=1.0, high=3.0),
    Distribution(kind=DistributionKind.TRIANGULAR, low=1.0, mode=2.0, high=5.0),
    Distribution(kind=DistributionKind.PERT, low=2.6, mode=3.0, high=3.8),
    Distribution(kind=DistributionKind.BERNOULLI, p=0.3),
]


@pytest.fixture(scope="module")
def gpu():
    return get_backend("cupy")


@pytest.mark.parametrize("dist", DISTRIBUTIONS, ids=lambda d: d.kind.value)
def test_gpu_inverse_cdf_matches_cpu_for_the_same_uniforms(gpu, dist):
    # ASSUMPTION: cupyx.scipy.special ndtri/betaincinv agree with SciPy to ~1e-9.
    u = np.random.default_rng(0).random(100_000)
    on_gpu = ppf(dist, gpu.asarray(u), gpu).get()
    np.testing.assert_allclose(on_gpu, ppf(dist, u), rtol=1e-9, atol=1e-12)


def test_gpu_formula_matches_cpu(gpu):
    rng = np.random.default_rng(1)
    env = {"a": rng.normal(size=1000), "b": rng.normal(size=1000)}
    formula = compile_formula("min(a, b, 0.5) + where(a > 0, exp(a), -b) * (a > b) + clip(a, -1, 1) ** 2")
    gpu_env = {k: gpu.asarray(v) for k, v in env.items()}
    np.testing.assert_allclose(formula.evaluate(gpu_env, gpu.xp).get(), formula.evaluate(env, np), rtol=1e-12)


def test_gpu_copula_achieves_target_spearman(gpu):
    copula = build_copula(["a", "b"], [Correlation(a="a", b="b", rho=0.7)])
    u = gpu.rng(3).random((200_000, 2))
    out = copula.apply(u, gpu).get()
    assert stats.spearmanr(out).statistic == pytest.approx(0.7, abs=0.01)


def test_gpu_simulation_agrees_with_cpu(decision):
    n = 400_000
    gpu_run = simulate(decision, n_scenarios=n, seed=7, backend="cupy")
    cpu_run = simulate(decision, n_scenarios=n, seed=7, backend="numpy")
    assert gpu_run.result.backend == "cupy"
    assert isinstance(gpu_run.scenarios, np.ndarray) and gpu_run.scenarios.shape == (n, 3)
    for g, c in zip(gpu_run.result.options, cpu_run.result.options, strict=True):
        # Different random streams: compare within Monte Carlo error.
        assert g.mean == pytest.approx(c.mean, rel=0.01)
        assert g.p5 == pytest.approx(c.p5, rel=0.02)
        assert g.prob_best == pytest.approx(c.prob_best, abs=0.01)
        # ASSUMPTION: cupy.histogram closes the last bin on the right like NumPy.
        assert sum(b.count for b in g.histogram) == n
    assert gpu_run.result.sensitivities
    for name, rate in gpu_run.result.risk_hit_rates.items():
        assert rate == pytest.approx(cpu_run.result.risk_hit_rates[name], abs=0.01)


def test_gpu_seed_reproducibility(decision):
    a = simulate(decision, n_scenarios=50_000, seed=1, backend="cupy")
    b = simulate(decision, n_scenarios=50_000, seed=1, backend="cupy")
    np.testing.assert_array_equal(a.scenarios, b.scenarios)


def test_auto_uses_gpu_for_large_runs():
    assert get_backend("auto", n_scenarios=AUTO_GPU_MIN_SCENARIOS).name == "cupy"


def test_gpu_throughput_1m_scenarios(decision):
    simulate(decision, n_scenarios=10_000, seed=0, backend="cupy")  # warm up kernels
    start = time.perf_counter()
    run = simulate(decision, n_scenarios=1_000_000, seed=0, backend="cupy")
    elapsed = time.perf_counter() - start
    print(f"\n1M scenarios on GPU: {elapsed:.2f}s (engine reported {run.result.elapsed_s:.2f}s)")
    assert run.scenarios.shape == (1_000_000, 3)
