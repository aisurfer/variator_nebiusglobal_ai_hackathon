import numpy as np
import pytest

from variator.simulation import AUTO_GPU_MIN_SCENARIOS, BackendUnavailableError, get_backend, to_host
from variator.simulation import backend as backend_module


def test_numpy_backend():
    be = get_backend("numpy")
    assert be.name == "numpy" and be.xp is np


def test_auto_falls_back_to_numpy_without_gpu(monkeypatch):
    monkeypatch.setattr(backend_module, "cupy_available", lambda: False)
    assert get_backend("auto").name == "numpy"
    assert get_backend("auto", n_scenarios=10**7).name == "numpy"


def test_auto_stays_on_cpu_for_small_runs_even_with_gpu(monkeypatch):
    monkeypatch.setattr(backend_module, "cupy_available", lambda: True)
    assert get_backend("auto", n_scenarios=AUTO_GPU_MIN_SCENARIOS - 1).name == "numpy"


def test_explicit_cupy_without_gpu_raises(monkeypatch):
    monkeypatch.setattr(backend_module, "cupy_available", lambda: False)
    with pytest.raises(BackendUnavailableError, match="CuPy"):
        get_backend("cupy")


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="unknown backend"):
        get_backend("tpu")  # type: ignore[arg-type]


def test_seeded_generators_are_reproducible():
    be = get_backend("numpy")
    np.testing.assert_array_equal(be.rng(3).random(5), be.rng(3).random(5))
    assert not np.array_equal(be.rng(3).random(5), be.rng(4).random(5))


def test_to_host_passes_numpy_through():
    arr = np.arange(3.0)
    assert to_host(arr) is arr
    np.testing.assert_array_equal(to_host([1, 2]), np.array([1, 2]))
    assert to_host(np.float64(2.0)).shape == ()


def test_special_functions_on_cpu():
    be = get_backend("numpy")
    assert float(be.ndtr(0.0)) == pytest.approx(0.5)
    assert float(be.ndtri(0.975)) == pytest.approx(1.959964, abs=1e-6)
    assert float(be.betaincinv(2.0, 2.0, 0.5)) == pytest.approx(0.5)


def test_betaincinv_falls_back_to_scipy_when_backend_lacks_it(monkeypatch):
    real = backend_module._special

    def missing_betaincinv(name, fn):
        if fn == "betaincinv":
            raise AttributeError(fn)
        return real(name, fn)

    monkeypatch.setattr(backend_module, "_special", missing_betaincinv)
    be = get_backend("numpy")
    out = be.betaincinv(2.0, 3.0, np.array([0.0, 0.5, 1.0]))
    np.testing.assert_allclose(out, [0.0, 0.38572756813238945, 1.0])
