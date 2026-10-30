"""Hygiene and reproducibility of the corpus (T07 A6): plan versions and random streams, the same bytes from the
same seed and writer answers, audit thresholds that stop the export, provenance of every timeline line."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from kbsim import densify as DZ
from kbsim.export import ExportBlocked, Paths, export, read_timeline
from synth import TENANT, T, world_events, write_tenant
from tests.mini import build_mini
from tests.test_densify import DENSIFY, answer_all

# the version-1 plan of the mini world (tests/mini.py): plan_version 1 must keep these bytes (re-pinned for this copy:
# the mini world is written through the World API instead of Plancy's engine, and the plan texts are English)
V1_PLAN = "9161375fb6f7f613e2e8f98c664b44a466d392c63d11e7dace3d5576e5227e43"


def _mini(root: Path, densify: str = DENSIFY) -> DZ.Densifier:
    build_mini(root)
    (root / "tenants" / "mini" / "densify.yaml").write_text(densify, encoding="utf-8")
    return DZ.Densifier("mini", root)


def test_plan_version_1_keeps_its_bytes(tmp_path):
    dz = _mini(tmp_path)
    dz.make_plan()
    assert hashlib.sha256(dz.plan_path.read_bytes()).hexdigest() == V1_PLAN
    assert "version" not in json.loads(dz.plan_path.read_text(encoding="utf-8"))


def _shape(plan: DZ.Plan) -> list[tuple]:
    """What the tickets and life-cycle streams decide (not the origins)."""
    return [(t.project, t.itype, t.component, t.created, t.priority, t.assignee,
             [(x.at, x.to) for x in t.transitions]) for t in plan.tickets]


@pytest.mark.parametrize("version,isolated", [(1, False), (2, True)])
def test_a_new_feature_of_the_planner_leaves_the_other_streams(tmp_path, monkeypatch, version, isolated):
    cfg = DENSIFY + f"plan_version: {version}\n"
    base = DZ.Planner(_mini(tmp_path / "a", cfg).s, yaml.safe_load(cfg)).plan()
    original = DZ.Planner.maybe_origin

    def with_new_feature(self, t, t0):  # a new kind of origin that draws two more numbers from its stream
        self.rng("origins").random()
        self.rng("origins").random()
        return original(self, t, t0)

    monkeypatch.setattr(DZ.Planner, "maybe_origin", with_new_feature)
    changed = DZ.Planner(_mini(tmp_path / "b", cfg).s, yaml.safe_load(cfg)).plan()
    assert (_shape(base) == _shape(changed)) is isolated
    if version == 2:
        assert base.version == 2 and [k.numbers for k in base.kpis] == [k.numbers for k in changed.kpis]


def _densified_export(root: Path) -> Paths:
    dz = _mini(root, DENSIFY + "plan_version: 2\n")
    dz.make_plan()
    dz.prepare1()
    answer_all(root, 1)
    dz.prepare2()
    answer_all(root, 2)
    dz.apply()
    paths = Paths("mini", root, root / "kb")
    export(paths)
    return paths


def test_same_seed_and_answers_give_the_same_bytes(tmp_path):
    a, b = _densified_export(tmp_path / "a"), _densified_export(tmp_path / "b")
    assert a.timeline.read_bytes() == b.timeline.read_bytes()
    assert (a.out_dir / "densify" / "plan.json").read_bytes() == (b.out_dir / "densify" / "plan.json").read_bytes()
    assert (a.fixtures_dir / "manifest.json").read_bytes() == (b.fixtures_dir / "manifest.json").read_bytes()
    lines = read_timeline(a.timeline)
    origins = {ln["provenance"]["origin"] for ln in lines}
    assert {"densify", "scenario"} <= origins  # densified tickets and the beats of the scenario
    dz = next(ln for ln in lines if ln["provenance"]["origin"] == "densify" and ln["object_type"] == "issue")
    assert dz["provenance"]["stub"] and dz["provenance"]["writer"].startswith("Nemotron")
    man = json.loads((a.fixtures_dir / "manifest.json").read_text(encoding="utf-8"))
    assert man["contract"] == "plancy-kb-corpus/1" and man["counts"]["events"] == len(lines)
    assert man["files_sha256"]["timeline.jsonl.gz"] == hashlib.sha256(a.timeline.read_bytes()).hexdigest()


def test_provenance_of_scenario_agent_and_engine_lines(tmp_path):
    paths = write_tenant(tmp_path)
    export(paths)
    lines = read_timeline(paths.timeline)
    by = {(ln["object_type"], ln["external_id"], ln["provenance"]["world_seq"]): ln["provenance"] for ln in lines}
    first = next(p for (ot, ext, _), p in by.items() if ext == "MENU-1")
    assert first["origin"] == "scenario" and first["beat"] == "PAY-01"
    assert next(p for (ot, ext, _), p in by.items() if ext == "MENU-2")["origin"] == "agent"
    assert any(p["origin"] == "engine" for (ot, _, _), p in by.items() if ot == "commit")  # the merge commit
    from kb.ingest.replay import event_envelope  # the backend ignores the field

    env = event_envelope("t", paths.fixtures_dir, lines[0])
    assert env.payload == lines[0]["payload"]


def test_export_stops_over_the_audit_thresholds(tmp_path):
    world = world_events()
    leak = {"seq": len(world) + 1, "ts": T("2026-06-20 10:00"), "day": 6, "actor": "dev", "kind": "ticket.commented",
            "artifact_type": "ticket", "artifact_id": "MENU-1", "beat_id": None, "handle": None,
            "data": {"comment": {"id": "comment-0099", "author": "dev", "body": "I heard the new hire gets 250,000.",
                                 "created": T("2026-06-20 10:00")}}}
    paths = write_tenant(tmp_path, world=world + [leak])
    tdir = paths.sim_dir / "tenants" / TENANT
    (tdir / "delivery.yaml").write_text("audit_thresholds: {confidential: 0}\n", encoding="utf-8")
    with pytest.raises(ExportBlocked) as e:
        export(paths)
    assert e.value.over["confidential"]["found"] >= 1 and not paths.timeline.exists()
    (tdir / "delivery.yaml").write_text("audit_thresholds: {confidential: 5}\n", encoding="utf-8")
    assert export(paths)["lines"] > 0
    with gzip.open(paths.timeline, "rt", encoding="utf-8") as fh:
        assert any("250,000" in x for x in fh)


def test_working_hours_past_the_calendar_do_not_loop(tmp_path):
    """add_hours used to loop for ever when a long task ran past the calendar's horizon (found by plan_version 2)."""
    dz = _mini(tmp_path)
    cal = DZ.Calendar(dz.s)
    t = cal.add_hours(cal.end, 400)
    assert t > cal.end
