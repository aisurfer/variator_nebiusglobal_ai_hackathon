"""`kbsim.intake validate` on the English delivery fixture tests/fixtures/sample-en: no errors, the adapter check
parses every line (sim lines through the registered sim adapter), and corruptions of sim lines are caught."""
from __future__ import annotations

import gzip
import json
import shutil
import sys
from pathlib import Path

import pytest

from tests.unit.connectors.sample_en import FIXTURE, SIM, materialize

if str(FIXTURE.parents[2] / "sim") not in sys.path:  # kbsim lives in kb/sim (PYTHONPATH=app:sim adds it too)
    sys.path.append(str(FIXTURE.parents[2] / "sim"))

from kbsim import contract as K  # noqa: E402
from kbsim import intake as I  # noqa: E402

TIMELINE = Path("fixtures") / "timeline.jsonl.gz"
SAMPLE_WARNINGS = {"coverage", "density", "north_star"}  # a 6-person, 10-day sample cannot meet the §6-7 targets


@pytest.fixture(scope="module")
def delivery(tmp_path_factory) -> Path:
    return materialize(tmp_path_factory.mktemp("sample-en") / "d")


def _check(rep: I.Report, cid: str) -> I.Check:
    return next(c for c in rep.checks if c.id == cid)


def test_the_fixture_passes_intake_validate_with_no_errors(delivery):
    rep = I.validate(delivery)
    d = rep.as_dict()
    assert rep.errors == 0, [(c.id, c.items[:3]) for c in rep.checks if c.level == "error" and c.total]
    assert set(d["warned_checks"]) <= SAMPLE_WARNINGS
    adapter = _check(rep, "adapter")
    assert adapter.level == "error" and adapter.status == "ok"  # the backend was importable and parsed every line
    assert _check(rep, "sim_records").status == "ok"
    lang = _check(rep, "language")
    assert lang.status == "ok" and lang.measured["texts"] >= 40 and lang.measured["in another language"] == 0
    assert d["tenant"] == "wp3sample" and _check(rep, "manifest").status == "ok"


def test_the_contract_reads_sim_lines(delivery):
    d = K.Delivery.load(delivery)
    assert {"sim.INV-2026-0031", "sim.REQ-0003", "sim.EE-0007", "sim.DEC-001"} <= d.entities
    by_key = {(lk.effect, lk.predicate, lk.cause): lk for lk in d.declared()}
    assert by_key[("sim.REQ-0003", "caused_by", "sim.EE-0007")].expressed_as == "sim_cause"
    assert by_key[("sim.DEC-002", "supersedes", "sim.DEC-001")].expressed_as == "sim_field"
    assert all(lk.cause for lk in d.declared() if lk.expressed_as.startswith("sim_"))
    gold = [json.loads(x) for x in (FIXTURE / "gold_links.jsonl").read_text(encoding="utf-8").splitlines() if x]
    assert {g["expressed_as"] for g in gold if g["effect"].startswith("sim.")} == {"sim_cause", "sim_field"}


def _lines(root: Path) -> list[dict]:
    with gzip.open(root / TIMELINE, "rt", encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def _write(root: Path, lines: list[dict]) -> None:
    with gzip.open(root / TIMELINE, "wt", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln, ensure_ascii=False, separators=(",", ":")) + "\n")


def _first(lines: list[dict], object_type: str) -> dict:
    return next(ln for ln in lines if ln["source"] == SIM and ln["object_type"] == object_type)


def _unknown_type(lines):
    _first(lines, "cloud_bill")["object_type"] = "timesheet"


def _missing_amount(lines):
    del _first(lines, "invoice")["payload"]["fields"]["amount"]


def _bad_cause_type(lines):
    _first(lines, "deal")["payload"]["causes"].append({"type": "whim", "id": "DEC-001"})


def _dangling_cause(lines):
    _first(lines, "deal")["payload"]["causes"].append({"type": "decision", "id": "DEC-404"})


def _dangling_ref(lines):
    _first(lines, "payment")["payload"]["fields"]["settles"] = "INV-404"


def _no_container(lines):
    _first(lines, "payroll")["container"] = None


def _unknown_person(lines):
    _first(lines, "requisition")["payload"]["fields"]["owner"] = "WP-E99"


def _unknown_org(lines):
    _first(lines, "contract")["payload"]["fields"]["client"] = "org-nobody"


def _identity(lines):
    _first(lines, "incident")["external_id"] = "INC-5"


@pytest.mark.parametrize("corrupt,checks", [
    (_unknown_type, {"source_declared", "adapter"}),
    (_missing_amount, {"sim_records"}),
    (_bad_cause_type, {"sim_records", "adapter"}),
    (_dangling_cause, {"dangling"}),
    (_dangling_ref, {"dangling"}),
    (_no_container, {"line_fields"}),
    (_unknown_person, {"accounts"}),
    (_unknown_org, {"accounts"}),
    (_identity, {"identity"}),
])
def test_corrupted_sim_lines_are_caught(delivery, tmp_path, corrupt, checks):
    root = tmp_path / "d"
    shutil.copytree(delivery, root)
    lines = _lines(root)
    corrupt(lines)
    _write(root, lines)
    rep = I.validate(root)
    failed = set(rep.as_dict()["failed_checks"]) - {"manifest"}  # the timeline no longer matches its sha256
    assert failed == checks, [(c.id, c.items[:2]) for c in rep.checks if c.id in failed ^ checks]
