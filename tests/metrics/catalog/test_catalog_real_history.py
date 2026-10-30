"""One real run on the generated history of the MVP company (skipped when the history is not on this machine).

The history: env ``VARIATOR_MVP_HISTORY``, else ``/tmp/variator-1000/mvp-history-v2`` (the shipped history),
else ``/tmp/variator-1000/mvp-history`` (v1, no NPS and no monitoring).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from variator.metrics.catalog import ids
from variator.metrics.catalog.artifacts import as_of_violations, compute_all
from variator.metrics.catalog.datasets import load_history
from variator.metrics.catalog.registry import default_params
from variator.metrics.catalog.sanity import sanity
from variator.metrics.results import KbMetricResult, MetricResult

_CANDIDATES = (Path("/tmp/variator-1000/mvp-history-v2"), Path("/tmp/variator-1000/mvp-history"))
_DEFAULT = next((p for p in _CANDIDATES if (p / "datasets").is_dir()), _CANDIDATES[0])
HISTORY = Path(os.environ.get("VARIATOR_MVP_HISTORY") or _DEFAULT)
pytestmark = pytest.mark.skipif(not (HISTORY / "datasets").is_dir(), reason=f"no history at {HISTORY}")


@pytest.fixture(scope="module")
def evals():
    data = load_history(HISTORY)
    return data, compute_all(data, default_params(), computed_at="2026-10-27T00:00:00+00:00")


def test_values_at_t_match_the_generator(evals):
    data, ev = evals
    assert ev["rev.arr"].kb.value == pytest.approx(12_000_000, rel=0.01)
    assert ev["fin.cash"].kb.value == pytest.approx(2_000_000, rel=0.01)
    rows = {r["check"]: r for r in sanity(data, ev)["rows"]}
    for check in (
        "ARR at T, USD",
        "Cash at T, USD",
        "DSO at T, days",
        "Incidents per month (year)",
        "MTTR, hours (year mean)",
        "Headcount at T",
        "Clients in force at T",
        "Contracts ended by T",
    ):
        r = rows[check]
        if r["truth"] is not None:
            assert r["ours"] == pytest.approx(r["truth"], rel=0.01, abs=0.01), r


def test_every_result_is_clean(evals):
    data, ev = evals
    for e in ev.values():
        assert as_of_violations(e) == [], e.defn.id
        MetricResult.model_validate(e.result.to_json_dict())
        KbMetricResult.model_validate(e.kb.to_json_dict())
        assert len(e.kb.series) == 52
        assert all(ids.is_kb_id(i.id) for i in e.kb.items)
        # «On track · target X» only when the value meets X
        k = e.kb
        if k.value is not None and k.target is not None and k.direction != "context":
            meets = k.value >= k.target if k.direction == "higher" else k.value <= k.target
            assert meets == (k.status == "good"), (k.id, k.value, k.target, k.status)


def test_v2_metrics_are_unknown_without_v2_files(evals):
    data, ev = evals
    for mid, table in (
        ("cust.nps", "nps_responses"),
        ("ops.sla", "monitoring_daily"),
        ("ops.latency", "monitoring_daily"),
    ):
        if data.has(table):
            assert ev[mid].kb.value is not None
        else:
            assert ev[mid].kb.value is None and ev[mid].kb.status == "unknown"


def test_v2_values_match_the_generator(evals):
    data, ev = evals
    rows = [r for r in sanity(data, ev)["rows"] if r["check"].startswith(("NPS", "SLA", "Implementations"))]
    if not data.has("nps_responses"):
        pytest.skip("v1 history: no NPS and no monitoring")
    assert rows, "the v2 summary has stats.nps and stats.sla"
    for r in rows:
        assert r["ours"] == pytest.approx(r["truth"], abs=0.11), r  # NPS to 0.1 point, SLA to 0.11 point
