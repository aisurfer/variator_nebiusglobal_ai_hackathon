"""Array backend selection: NumPy on CPU, CuPy on an NVIDIA GPU.

The engine is written once against an array module ``xp`` (``numpy`` or ``cupy``).
CuPy is imported lazily so this package works without it.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal

import numpy as np

BackendName = Literal["numpy", "cupy"]
BackendChoice = Literal["auto", "numpy", "cupy"]

# Below this many scenarios "auto" stays on NumPy: GPU start-up and kernel JIT
# compilation (~1 s on first use) cost more than the whole CPU run.
AUTO_GPU_MIN_SCENARIOS = 200_000


class BackendUnavailableError(RuntimeError):
    """Raised when ``backend="cupy"`` is requested but CuPy or a CUDA device is missing."""


@functools.cache
def cupy_available() -> bool:
    """Return True if CuPy imports and at least one CUDA device is visible."""
    if importlib.util.find_spec("cupy") is None:
        return False
    try:
        import cupy

        return int(cupy.cuda.runtime.getDeviceCount()) > 0
    except Exception:  # no driver, no device, broken install: all mean "no GPU"
        return False


@dataclass(frozen=True)
class Backend:
    """An array module plus the few backend-specific operations the engine needs."""

    name: BackendName
    xp: ModuleType

    def rng(self, seed: int | None) -> Any:
        """Seeded generator with a NumPy-like ``random(size, dtype)`` method.

        NumPy and CuPy produce different streams for the same seed, and CuPy does not
        guarantee stream stability across versions.
        """
        if self.name == "cupy":
            return self.xp.random.default_rng(seed)
        return np.random.default_rng(seed)

    def asarray(self, value: Any) -> Any:
        """Move a host array (or scalar) onto this backend's device."""
        return self.xp.asarray(value)

    def ndtr(self, x: Any) -> Any:
        """Standard normal CDF."""
        return _special(self.name, "ndtr")(x)

    def ndtri(self, u: Any) -> Any:
        """Standard normal inverse CDF."""
        return _special(self.name, "ndtri")(u)

    def betaincinv(self, a: float, b: float, u: Any) -> Any:
        """Inverse of the regularized incomplete beta function in ``u``.

        On CuPy builds without ``cupyx.scipy.special.betaincinv`` the column is
        computed on the host with SciPy and copied back (slow, but correct).
        """
        try:
            fn = _special(self.name, "betaincinv")
        except (ImportError, AttributeError):
            host = _scipy_special().betaincinv(a, b, to_host(u))
            return self.xp.asarray(host)
        return fn(a, b, u)


def _scipy_special() -> ModuleType:
    return importlib.import_module("scipy.special")


@functools.cache
def _special(backend: BackendName, name: str) -> Callable[..., Any]:
    # ASSUMPTION: cupyx.scipy.special provides ndtr, ndtri and betaincinv with SciPy
    # signatures (listed at https://docs.cupy.dev/en/stable/reference/scipy_special.html);
    # betaincinv may be missing on older CuPy, hence the host fallback above.
    module = importlib.import_module("cupyx.scipy.special" if backend == "cupy" else "scipy.special")
    return getattr(module, name)


def get_backend(choice: BackendChoice = "auto", *, n_scenarios: int | None = None) -> Backend:
    """Resolve a backend choice to a ``Backend``.

    ``"auto"`` picks CuPy only if it is importable, a CUDA device is visible and the run
    is large enough to benefit (``n_scenarios >= AUTO_GPU_MIN_SCENARIOS`` when given).
    ``"cupy"`` raises ``BackendUnavailableError`` if no GPU is usable.
    """
    if choice == "numpy":
        return Backend("numpy", np)
    if choice == "cupy":
        if not cupy_available():
            raise BackendUnavailableError(
                "backend='cupy' needs CuPy (pip install 'variator[gpu]') and a visible CUDA device"
            )
        return Backend("cupy", importlib.import_module("cupy"))
    if choice != "auto":
        raise ValueError(f"unknown backend {choice!r}; use 'auto', 'numpy' or 'cupy'")
    large_enough = n_scenarios is None or n_scenarios >= AUTO_GPU_MIN_SCENARIOS
    if large_enough and cupy_available():
        return Backend("cupy", importlib.import_module("cupy"))
    return Backend("numpy", np)


def to_host(value: Any) -> np.ndarray:
    """Return ``value`` as a host NumPy array (copies from the GPU if it is a CuPy array)."""
    if isinstance(value, np.ndarray):
        return value
    get = getattr(value, "get", None)
    if callable(get) and type(value).__module__.startswith("cupy"):
        return get()
    return np.asarray(value)
