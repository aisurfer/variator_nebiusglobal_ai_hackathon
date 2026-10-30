"""Densification: plan -> pass 1 (ticket texts) -> pass 2 (comments, MRs, meetings, metrics) -> apply, on a mini world."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbsim.densify import Densifier, check_request  # noqa: E402
from kbsim.world import World  # noqa: E402
from tests.mini import build_mini  # noqa: E402

DENSIFY = """
seed: 3
tickets_per_workday: 5
projects:
  SAAS:
    weight: 1.0
    epics_per_month: 1
    components:
      - {id: api, name: "API and online payment", repo: mini/api, assignees: [dev_back], reporters: [ceo, accountant], topics: [payment, API]}
issue_mix: {Bug: 0.4, Task: 0.4, Story: 0.2}
qa_projects: [SAAS]
leads: {SAAS: ceo}
qa: ceo
reviewers: [ceo]
meetings:
  - {id: standup, title: "Standup", cadence: daily, time: "10:00", minutes: 15, space: MGMT, organizer: ceo, attendees: [ceo, dev_back], kind: standup, refs: in_progress}
  - {id: planning, title: "Planning", cadence: [mon, wed], time: "11:00", minutes: 30, space: MGMT, organizer: ceo, attendees: [ceo, dev_back, accountant], kind: planning, refs: spawn}
kpi:
  title: "Metrics"
  author: ceo
  space: MGMT
  weekday: mon
  time: "11:30"
  start: {paying: 100, active: 80, orders: 1000, mrr: 300000}
  weekly_new: [1, 2]
  weekly_churn: [0, 1]
  active_share: [0.8, 0.9]
  orders_per_active: [10, 12]
  mrr_per_paying: 3000
  online_share: {before: 0.0, pilot_from: 2026-06-16, pilot: 0.03, launch_from: 2026-09-01, launch: [0.08, 0.18]}
"""
LONG = "The context, the steps and the acceptance criteria are listed below, with environment details. " * 3


def fake_answer(req: dict) -> dict:
    ch, kind = req["checks"], req["kind"]
    if kind == "dz_tickets":
        return {"tickets": [{"key": k, "summary": f"Online payment ticket {k}", "description": LONG} for k in ch["keys"]]}
    if kind == "dz_comments":
        return {"comments": [{"slot": s, "body": f"On {s}: checked on staging, it works."} for s in ch["slots"]]}
    if kind == "dz_mrs":
        return {"mrs": [{"slot": s, "title": f"{ch['keys'][s]}: payment fixes", "description": LONG,
                         "notes": [f"review note {i + 1}" for i in range(ch["notes"][s])]} for s in ch["slots"]]}
    if kind == "dz_meetings":
        return {"meetings": [{"slot": s, "title": f"Meeting {s}", "body": LONG * 3 + " ".join(ch["refs"][s]), "decisions": [],
                              "action_items": [f"{ch['attendees'][s][0]}: create {k}" for k in ch["spawned"][s]]}
                             for s in ch["slots"]]}
    return {"reports": [{"slot": s, "commentary": f"Active customers {ch['active'][s]}. " + LONG} for s in ch["slots"]]}


def answer_all(root: Path, phase: int) -> int:
    n = 0
    for p in sorted((root / "out" / "mini" / "rounds" / f"densify-{phase}").glob("req-*.json")):
        req = json.loads(p.read_text(encoding="utf-8"))
        Path(req["response_path"]).write_text(json.dumps(fake_answer(req), ensure_ascii=False), encoding="utf-8")
        assert check_request(req)["ok"], check_request(req)
        n += 1
    return n


def test_densify_cycle(tmp_path):
    build_mini(tmp_path)  # a finished 5-day mini world
    (tmp_path / "tenants" / "mini" / "densify.yaml").write_text(DENSIFY, encoding="utf-8")
    before = World(Densifier("mini", tmp_path).s.company, tmp_path / "out" / "mini")
    before.load()

    dz = Densifier("mini", tmp_path)
    summary = dz.make_plan()
    assert summary["tickets"] >= 10 and summary["meetings"] >= 5 and summary["mrs"] >= 1
    plan = dz.load()
    keys = [t.key for t in plan.tickets]
    assert len(set(keys)) == len(keys) and all(int(k.split("-")[1]) > before.counters["ticket:SAAS"] for k in keys)
    for t in plan.tickets:  # a life cycle runs forward in time and never through an absence
        ts = [t.created] + [x.at for x in t.transitions]
        assert ts == sorted(ts), t.key

    assert dz.prepare1()["requests"] >= 1 and answer_all(tmp_path, 1) >= 1
    first = next((tmp_path / "out" / "mini" / "rounds" / "densify-1").glob("req-*.json"))
    req = json.loads(first.read_text(encoding="utf-8"))
    Path(req["response_path"]).write_text(json.dumps({"tickets": []}), encoding="utf-8")
    assert not check_request(req)["ok"]  # a missing ticket is reported, not silently dropped
    Path(req["response_path"]).write_text(json.dumps(fake_answer(req), ensure_ascii=False), encoding="utf-8")

    assert dz.prepare2()["requests"] >= 1 and answer_all(tmp_path, 2) >= 1
    assert all(v["ok"] == v["requests"] for v in dz.status().values())
    res = dz.apply()
    assert res["added_events"] > 50

    w = World(dz.s.company, tmp_path / "out" / "mini")
    w.load()
    seqs = [e.seq for e in w.events]
    assert seqs == list(range(1, len(seqs) + 1))
    assert [e.ts for e in w.events] == sorted(e.ts for e in w.events)  # one journal in time order
    assert len(w.tickets) == len(before.tickets) + len(plan.tickets)
    for t in plan.tickets:  # the final state is the planned one
        assert w.tickets[t.key].status == (t.transitions[-1].to if t.transitions else "To Do"), t.key
    spawned = [t for t in plan.tickets if (t.origin or {}).get("kind") == "meeting"]
    for t in spawned:
        link = w.tickets[t.key].remote_links[0]
        assert link["relationship"] == "informed by" and w.handles[link["target"]][0] == "page"
    merged = [m for m in w.mrs.values() if m.state == "merged" and m.closes and m.closes[0] in keys]
    assert len(merged) == sum(1 for m in plan.mrs if m.merged_at)
    kpi = [p for p in w.pages.values() if p.title.startswith("Metrics")]
    assert len(kpi) == len(plan.kpis) and "North Star" in kpi[0].body
    by_title = sorted(kpi, key=lambda p: p.created)
    assert [p.properties["Active customers"] for p in by_title] == [str(k.numbers["active"]) for k in plan.kpis]  # each week its own numbers
    assert Path(res["backup"]).exists()  # the world before densification is kept
