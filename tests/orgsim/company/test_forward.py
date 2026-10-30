"""Forward run (day level, WP19): state at T, learned parameters, determinism, common random numbers,
week-tree paths, branch delivery with 0 intake errors, texts packets.

Needs the final history and the actor artifacts in /tmp (skipped without them).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HISTORY = Path("/tmp/variator-1000/mvp-history-mgmt")
ACTORS = Path("/tmp/variator-1000/mvp-artifacts/actors")
ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(
    not (HISTORY / "delivery" / "manifest.json").is_file() or not (ACTORS / "habits.json").is_file(),
    reason="no final history or actor artifacts in /tmp",
)


@pytest.fixture(scope="module")
def st():
    from variator.orgsim.company.forward.state import StateAtT

    return StateAtT(HISTORY, ACTORS)


@pytest.fixture(scope="module")
def case():
    from variator.orgsim.company.forward.forks import Case

    return Case()


def _run(st, case, fork, k, path=None, months=3):
    from variator.orgsim.company.forward.pipeline import one_run

    return one_run(st, case, fork, k, 7, months, path=path)


def _lines_hash(run) -> str:
    from variator.orgsim.company.forward.deliver import future_lines

    lines = future_lines(run, lean=False)
    return hashlib.sha256("".join(json.dumps(x, sort_keys=True) for x in lines).encode()).hexdigest()


# ------------------------------------------------------------------------------------------ state and params
def test_state_at_t_is_the_end_of_the_history(st):
    assert st.anchor.isoformat() == "2026-10-27"
    assert len(st.people) == 20
    assert round(sum(c.acv for c in st.clients.values() if c.status != "signed")) == 12_001_200
    assert st.cash_at_t == 2_000_000.0
    ids = st.snapshot_ids()
    assert "PROC-PAYROLL" in ids and "RULE-BURNOUT" in ids
    assert all(st.sim_last[i]["at"] < "2026-10-27" for i in ids)


def test_every_parameter_has_a_source_tag(st):
    import numpy as np

    from variator.orgsim.company.forward.params import build_params

    P = build_params(st, np.random.default_rng(1))
    assert P.values and set(P.values) == set(P.sources)
    for k, src in P.sources.items():
        assert re.match(r"^(data:|group:|case:|rule:|assumption:)", src), (k, src)


def test_the_forward_code_never_reads_the_hidden_truth():
    src = "\n".join(p.read_text() for p in (ROOT / "src/variator/orgsim/company/forward").glob("*.py"))
    banned = [
        r"[\"']truth\.json[\"']",
        r"\bperson_habits\(",
        r"\bclient_habits\(",
        r"\bdefault_spec\(",
        r"\.truth\b",
        r"from variator\.orgsim\.company\.spec import",
    ]
    for pat in banned:
        assert not re.search(pat, src), pat


# ------------------------------------------------------------------------------------------ the run
def test_same_scenario_same_records(st, case):
    assert _lines_hash(_run(st, case, "pay_tech_debt", 4)) == _lines_hash(_run(st, case, "pay_tech_debt", 4))


def test_a_fork_changes_only_what_it_changes(st, case):
    """Common random numbers: before its first intervention a fork has the records of the baseline."""
    base = _run(st, case, "status_quo", 5)
    gpu = _run(st, case, "reserve_gpu", 5)
    first = min(v.ts for r in gpu.book.records.values() for v in r.versions if r.otype == "decision")

    def before(run):
        return sorted(
            (r.id, v.kind, v.ts) for r in run.book.records.values() for v in r.versions if v.ts < first
        )

    assert before(base) == before(gpu)
    assert base.summary()["incidents"] == gpu.summary()["incidents"]  # GPU reserve does not change incidents


def test_nothing_before_t_and_ids_after_the_history(st, case):
    run = _run(st, case, "pay_tech_debt", 2)
    hist = set(st.sim_last)
    for r in run.book.records.values():
        if getattr(r, "adopted", False):
            assert r.id in hist
            continue
        assert r.id not in hist, r.id
        assert all(v.ts.date() >= st.anchor for v in r.versions)


def test_series_and_lane(st, case):
    run = _run(st, case, "pay_tech_debt", 3)
    assert run.series[0]["date"] == "2026-10-27"
    assert len(run.series) == len(run.days)
    v = run.series[0]["values"]
    assert set(v) == {"cash", "load", "incidents", "arr", "headcount", "availability", "p95_ms"}
    assert 0.7 < sum(x["values"]["load"] for x in run.series) / len(run.series) < 1.15
    types = {e["type"] for e in run.lane}
    assert {"decision", "incident"} <= types
    assert all(e["entityId"].startswith(("sim.", "person.", "org.")) for e in run.lane if "entityId" in e)


def test_week_tree_path_is_followed(st, case):
    from variator.orgsim.company.forward.pipeline import WEEK_TREE, leaves, path_check

    if not WEEK_TREE.is_file():
        pytest.skip("no week tree")
    forks, paths = leaves(WEEK_TREE)
    leaf = paths[forks[0]]["median"]
    run = _run(st, case, forks[0], leaf["scenario"], path=leaf["events"], months=6)
    chk = path_check(run, leaf)
    assert chk["incidents_per_week_equal"]
    assert chk["applied"] >= chk["path_events_in_window"] - 2


# ------------------------------------------------------------------------------------- delivery and texts
def test_branch_delivery_passes_intake_with_no_errors(st, case, tmp_path):
    from variator.narrative.branch import packets
    from variator.narrative.facts import Packet, placeholder_text
    from variator.orgsim.company.forward.deliver import write_branch

    run = _run(st, case, "pay_tech_debt", 3)
    run.tenant = "company-b-test"
    texts = {}
    for a in packets(run, "t"):
        pk = Packet.from_json(a["packet"])
        texts[run.book.records[a["record"]].text_slots["description"]] = {
            "description": placeholder_text(pk, "description")
        }
    man = write_branch(run, tmp_path, tenant="company-b-test", label="test", texts=texts)
    assert man["counts"]["by_object_type"]["sim:decision"] >= 1
    with gzip.open(tmp_path / "delivery" / "fixtures" / "timeline.jsonl.gz", "rt") as fh:
        lines = [json.loads(x) for x in fh]
    future = [ln for ln in lines if ln["payload"]["run"] == "day"]
    assert future and all(ln["payload"]["option"] == "pay_tech_debt" for ln in future)
    assert any("description" in ln["payload"]["fields"] for ln in future)
    kb = ROOT / "kb"
    if not (kb / "sim" / "kbsim" / "intake.py").is_file():
        pytest.skip("no kb/ copy")
    env = {**os.environ, "KB_ENV": "test", "PYTHONPATH": f"{kb / 'sim'}{os.pathsep}{kb / 'app'}"}
    out = tmp_path / "intake"
    subprocess.run(
        [sys.executable, "-m", "kbsim.intake", "validate", str(tmp_path / "delivery"), "--out", str(out)],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    rep = json.loads((out / "validate.json").read_text())
    assert rep["errors"] == 0, rep["failed_checks"]


def test_text_packets_pass_the_checker_with_placeholder_texts(st, case):
    from variator.narrative.branch import packets, plan
    from variator.narrative.check import Names, check_artifact
    from variator.narrative.facts import Packet, placeholder_text
    from variator.orgsim.company.forward.deliver import directory

    run = _run(st, case, "pay_tech_debt", 3, months=6)
    arts = packets(run, "t")
    assert {a["type"] for a in arts} >= {"postmortem", "leadership_review", "decision_rationale"}
    _, ppl = directory(run, "x", "x")
    names = Names.from_json(plan(arts, ppl)["names"])
    for a in arts:
        pk = Packet.from_json(a["packet"])
        assert check_artifact(pk, {s: placeholder_text(pk, s) for s in pk.slots}, names) == [], a["id"]


def test_write_texts_maps_texts_to_their_records(st, case, tmp_path):
    from variator.narrative.branch import write_texts

    run = _run(st, case, "reserve_gpu", 2)
    texts, planned, summary = write_texts({"company-b-x": run}, tmp_path, lambda s: None, mode="placeholder")
    assert summary["mode"] == "placeholder" and planned["company-b-x"] == len(texts["company-b-x"]) > 0
    slots = {aid for r in run.book.records.values() for aid in r.text_slots.values()}
    assert set(texts["company-b-x"]) <= slots


def test_a_changed_packet_loses_its_old_text(tmp_path):
    from variator.narrative.branch import forget_changed

    (tmp_path / "requests").mkdir()
    (tmp_path / "responses").mkdir()
    (tmp_path / "plan.json").write_text(
        json.dumps({"artifacts": [{"id": "a", "packet": 1}, {"id": "b", "packet": 2}]})
    )
    (tmp_path / "texts.json").write_text(
        json.dumps({"a": {"status": "accepted"}, "b": {"status": "accepted"}})
    )
    (tmp_path / "requests" / "r0-x.json").write_text(json.dumps({"artifacts": ["b"]}))
    (tmp_path / "responses" / "r0-x.json").write_text("{}")
    assert forget_changed(tmp_path, [{"id": "a", "packet": 1}, {"id": "b", "packet": 3}]) == ["b"]
    assert set(json.loads((tmp_path / "texts.json").read_text())) == {"a"}
    assert not (tmp_path / "responses" / "r0-x.json").exists()


def test_every_signal_and_notice_has_declared_causes(st, case):
    """The "why" of a branch rests on declared links: no signal or client notice without a cause."""
    from variator.orgsim.company.forward.pipeline import WEEK_TREE, leaves

    runs = [_run(st, case, f, k, months=6) for f, k in (("status_quo", 3), ("pay_tech_debt", 7))]
    if WEEK_TREE.is_file():
        forks, paths = leaves(WEEK_TREE)
        leaf = paths[forks[-1]]["pessimistic"]
        runs.append(_run(st, case, forks[-1], leaf["scenario"], path=leaf["events"], months=6))
    seen = 0
    for run in runs:
        for r in run.book.records.values():
            title = str(r.state.get("title", ""))
            if r.id.startswith("SIG-") or title.endswith("gives notice"):
                seen += 1
                assert r.versions[0].causes, (r.id, title)
            if r.id.startswith("SIG-"):
                assert re.match(
                    r"^(Incidents rose|Incidents fell|Cash fell|Engineering load over capacity) in ", title
                )
    assert seen > 0


def test_the_metric_catalogue_reads_a_forward_run(st, case, tmp_path):
    """A metric defined over records works on the future: catalogue cash = the run's end-of-day cash."""
    from variator.metrics.catalog import registry
    from variator.metrics.catalog.datasets import load_history
    from variator.metrics.catalog.engine import run as metric_run
    from variator.orgsim.company.forward.datasets import write_datasets

    run = _run(st, case, "status_quo", 2)
    write_datasets(run, tmp_path)
    data = load_history(tmp_path, as_of_anchor=run.end)
    params = registry.default_params()
    cash = registry.BY_ID["fin.cash"]
    for k in (0, len(run.days) // 2, len(run.days) - 1):
        assert metric_run(cash, data, params, run.days[k]).value == run.series[k]["values"]["cash"]
    assert metric_run(registry.BY_ID["rev.arr"], data, params, run.days[-1]).value > 0


def test_new_prices_are_in_the_contract_records(st, case, tmp_path):
    """Raise prices: the catalogue ARR from the contract records = the run's ARR, before and after the new
    prices go live (the run bills acv x price factor; the records carry the price step)."""
    from variator.metrics.catalog import registry
    from variator.metrics.catalog.datasets import load_history
    from variator.metrics.catalog.engine import run as metric_run
    from variator.orgsim.company.forward.datasets import write_datasets

    run = _run(st, case, "raise_prices_8", 21, months=4)
    live = run.fork.dates["price_live"]
    assert run.days[0] < live < run.days[-1] and run.prices_live
    write_datasets(run, tmp_path)
    data = load_history(tmp_path, as_of_anchor=run.end)
    params, arr = registry.default_params(), registry.BY_ID["rev.arr"]
    after = [k for k, d in enumerate(run.days) if d >= live]
    for k in (0, after[0] - 1, after[0], after[len(after) // 2], len(run.days) - 1):
        got = metric_run(arr, data, params, run.days[k]).value
        assert abs(got - run.series[k]["values"]["arr"]) < 1, run.days[k]


def test_the_hires_of_the_run_are_named_in_the_lane(st, case):
    actors = json.loads((ACTORS / "actors.json").read_text(encoding="utf-8"))["actors"]
    known = {a["id"] for a in actors if a.get("kind") == "person"}
    run = _run(st, case, "hire_5", 2, months=4)
    named = [e for e in run.lane if e.get("person") and e["person"] not in known]
    assert named and all(e.get("personName") for e in named)
    assert not any("personName" in e for e in run.lane if e.get("person") in known)


def test_select_takes_the_p10_p50_p90_runs():
    from variator.orgsim.company.forward.pipeline import select

    rows = [{"scenario": k, "cash_end": float(100 - k)} for k in range(1, 12)]
    sel = select(rows)
    assert sel["pessimistic"] == (10, 10) and sel["median"] == (6, 50) and sel["optimistic"] == (2, 90)
