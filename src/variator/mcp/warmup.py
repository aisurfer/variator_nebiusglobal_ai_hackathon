"""Load native libraries in the main thread before the stdio transport starts.

On Windows, the first load of a native extension (BLAS in NumPy/SciPy, pdfium, ...)
inside a worker thread hangs while the stdio transport reads the pipe. FastMCP runs
sync tools in worker threads, so every such library must be loaded here first.
In-process tests do not show the problem; see the live test in tests/mcp.
"""

from __future__ import annotations

import importlib
import logging

log = logging.getLogger(__name__)

# Optional modules: missing ones are skipped.
MODULES = (
    "scipy.linalg",
    "scipy.special",
    "scipy.stats",
    "scipy.optimize",
    "yaml",
    "lizard",
    "openpyxl",
    "pdfplumber",
    "pypdfium2",
)


def warm_up() -> list[str]:
    """Import native-backed modules and run one BLAS call; return the modules loaded."""
    import numpy as np

    np.linalg.lstsq(np.eye(2), np.ones(2), rcond=None)  # loads BLAS/LAPACK
    np.linalg.pinv(np.eye(2))
    loaded = ["numpy.linalg"]
    for name in MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            continue
        loaded.append(name)
    log.debug("warm-up loaded %s", loaded)
    return loaded
