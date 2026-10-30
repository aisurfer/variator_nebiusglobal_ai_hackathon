"""Fixtures of the simulator tests: run with `cd kb/sim && PYTHONPATH=.:../app python -m pytest -q tests`.

The synthetic tenant (synth.py) is exported once per session into a temp dir. No test needs a database: Plancy's
integration test of kbsim (a load into PostgreSQL + the evaluation harness) is not part of this copy.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
KB = HERE.parents[1]
for p in (HERE, KB / "sim", KB / "app"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
os.environ.setdefault("KB_ENV", "test")


@pytest.fixture(scope="session")
def exported(tmp_path_factory):
    """(paths, report) of the synthetic tenant exported with adapter validation."""
    from kbsim.export import export
    from synth import write_tenant

    paths = write_tenant(tmp_path_factory.mktemp("simt"))
    report = export(paths)
    return paths, report


@pytest.fixture(scope="session")
def timeline(exported):
    from kbsim.export import read_timeline

    return read_timeline(exported[0].timeline)


@pytest.fixture(scope="session")
def resolved(exported):
    import json

    return json.loads(exported[0].resolved.read_text(encoding="utf-8"))
