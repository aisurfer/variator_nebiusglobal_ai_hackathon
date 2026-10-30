"""Case config (scripts/mvp/export_case.py) and preview data (scripts/mvp/proto_future.py) of the case page.

The workspace files must equal a fresh build: run both scripts after a change of the template or the case.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/mvp"
CASES = ROOT / "workspace/config/cases"
SECTIONS = [
    "world",
    "forks",
    "stays",
    "month-answer",
    "month-compare",
    "month-time",
    "month-plan",
    "month-why",
    "month-details",
    "week-review",
    "week",
    "day-review",
    "day",
]
CASE_IDS = ["company_best_year", "delivery_best_way"]
EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\u2300-\u23ff\ufe0f]")
LANES = {"decisions", "people", "clients_vendors", "factors"}
MODEL_KEYS = {
    "question", "variables", "derived", "options", "criteria", "risks", "rules", "assumptions", "page",
    "scenarios", "timeline",
}  # fmt: skip
CASE_KEYS = {
    "schema", "id", "letter", "title", "company", "lead", "owner", "forkDate", "horizonMonths", "template",
    "previewFile", "defaults", "priorityPresets", "caution", "hypotheses", "forks", "people", "items",
    "shocks", "chainPath", "chainHigher", "blocks", "stays", "goals", "riskRows", "helpKeys", "help", "steps",
    "sections", "labels",
}  # fmt: skip


def _load(name: str):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def config() -> dict:
    return _load("export_case").build_config("company_best_year")


@pytest.fixture(scope="module", params=CASE_IDS)
def any_config(request) -> dict:
    return _load("export_case").build_config(request.param)


@pytest.fixture(scope="module", params=CASE_IDS)
def preview(request) -> dict:
    return _load("proto_future").build_preview(request.param)


def test_config_has_the_model_and_the_case_layer(config):
    assert set(config) == MODEL_KEYS | CASE_KEYS
    assert config["schema"] == "variator-case/1" and config["id"] == "company_best_year"
    assert [s["id"] for s in config["sections"]] == SECTIONS
    assert [s["id"] for s in config["steps"]] == ["forks", "month", "week", "day"]
    assert {s["step"] for s in config["sections"]} == {"forks", "month", "week", "day"}
    assert [s["id"] for s in config["sections"] if s["collapsed"]] == ["stays", "month-details"]
    assert {s["id"] for s in config["sections"] if s["preview"]} == {"month-time", "week", "day"}
    assert [s["help"] for s in config["steps"]] == ["step.forks", "step.month", "step.week", "step.day"]
    assert config["letter"] == "B" and config["company"] in config["question"]
    assert config["forkDate"] == "2026-10-26" and config["horizonMonths"] == 12
    d = config["defaults"]
    assert set(d) == {"weights", "riskAversion", "n", "seed", "activePresets", "edits"}
    assert d["weights"] == {c["name"]: c["weight"] for c in config["criteria"]}
    assert d["activePresets"] == [] and d["edits"] == {} and d["n"] == 4096


def test_presets_hypotheses_and_forks(config):
    pp = {p["id"]: p for p in config["priorityPresets"]}
    assert list(pp) == ["balanced", "cash_first", "clients_first", "reliability_first"]
    assert pp["balanced"]["weights"] == config["defaults"]["weights"]
    assert all(set(p["weights"]) == set(config["defaults"]["weights"]) for p in pp.values())
    assert [h["id"] for h in config["hypotheses"]] == [s["name"] for s in config["scenarios"]]
    assert all(
        h["kind"] == "hypothesis" and h["label"] and h["description"] and h["effects"]
        for h in config["hypotheses"]
    )
    assert [f["id"] for f in config["forks"]] == [o["name"] for o in config["options"]]
    assert all(f["kind"] == "decision" and f["label"] and f["description"] for f in config["forks"])
    assert config["forks"][0]["baseline"] and not any(f["baseline"] for f in config["forks"][1:])
    hire = next(f for f in config["forks"] if f["id"] == "hire_5")
    assert hire["levers"] == {"lever_hire": 1.0}
    assert {i["item"] for i in hire["interventions"]} == {"hire_start", "hires_ready"}
    assert [c["value"] for c in config["caution"]] == [0.0, 0.25, 1.0]


def test_help_covers_sections_numbers_controls_and_model_names(config):
    help_ = config["help"]
    assert all(set(h) >= {"title", "body"} and h["title"] and h["body"] for h in help_.values())
    wanted = [f"section.{s}" for s in SECTIONS] + [f"step.{s['id']}" for s in config["steps"]]
    wanted += [f"criterion.{c['name']}" for c in config["criteria"]] + [
        f"fork.{f['id']}" for f in config["forks"]
    ]
    wanted += [f"hypothesis.{h['id']}" for h in config["hypotheses"]] + [
        f"rule.{r['id']}" for r in config["rules"]
    ]
    wanted += [f"derived.{x}" for x in config["page"]["chain"]] + [
        f"variable.{v['name']}" for v in config["variables"]
    ]
    wanted += [f"timeline.{i['name']}" for i in config["timeline"]["items"]]
    wanted += [f"priority.{p['id']}" for p in config["priorityPresets"]]
    wanted += [
        "kpi.pBest", "kpi.feasible", "kpi.pMet", "kpi.fan", "kpi.riskCalendar", "control.weights",
        "control.caution", "control.resetAll", "control.suggestDates", "control.applyPlan", "metric.cash",
        "check.runway_low",
    ]  # fmt: skip
    missing = [k for k in wanted if k not in help_]
    assert not missing, missing
    assert all(s["help"] in help_ for s in config["sections"])
    assert all(o["name"] in config["labels"] for o in config["options"])


def test_workspace_config_is_fresh(any_config):
    on_disk = json.loads((CASES / f"{any_config['id']}.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(any_config)), "run: .venv/bin/python scripts/mvp/export_case.py"


# case -> (the fan metric whose last month equals a criterion, the calendar rows of every fork,
# the day metric that counts the "incident" events)
PREVIEW = {
    "company_best_year": (
        "cash",
        {"cash_negative", "runway_low", "sla_breach", "churn_high", "over_budget"},
        "incidents",
    ),
    "delivery_best_way": (
        "money",
        {"budget_overrun", "beta", "load_test_env", "go_live", "money_goal"},
        "defects",
    ),
}
KB_ID = re.compile(r"(org\.(CL-\d{3}|VN-\d{2})|person\.lw-e\d{2})")


def _l0(case: str) -> dict:
    return _load("proto_future").l0_run() if case == "company_best_year" else _load("proto_delivery").l0_run()


def test_preview_is_marked_and_matches_the_model(preview):
    case = preview["caseId"]
    end_metric, rows, _count = PREVIEW[case]
    assert preview["preview"] is True and case in CASE_IDS
    assert all(o["preview"] is True for o in preview["options"].values())
    assert all(t["preview"] is True for t in preview["weekTrees"].values())
    assert all(e["preview"] is True for exs in preview["dayExamples"].values() for e in exs)
    l0 = _l0(case)
    run, horizon = l0["run"], preview["horizonMonths"]
    milestones = {i["name"]: i for i in l0["timeline"]["items"] if i["kind"] in ("milestone", "goal")}
    for j, name in enumerate(run.option_names):
        fans = preview["options"][name]["fans"]
        assert set(fans) == {m["id"] for m in preview["metrics"]}
        assert all([p["month"] for p in f] == list(range(horizon + 1)) for f in fans.values())
        metrics = ("cash", "churn", "sla", "nps", "runway") if case == "company_best_year" else (end_metric,)
        for metric in metrics:
            end = run.values[:, j, run.criterion_index(metric)]
            want = np.quantile(end, [0.1, 0.5, 0.9])
            got = [fans[metric][horizon][k] for k in ("p10", "p50", "p90")]
            assert np.allclose(got, want, rtol=1e-4, atol=1e-3), (name, metric, got, want)
        calendar = preview["options"][name]["riskCalendar"]
        ids = {r["id"] for r in calendar}
        assert rows <= ids, (name, rows - ids)
        for item in ids - rows:  # a milestone row only for the forks of the milestone
            assert item in milestones and (
                milestones[item]["options"] is None or name in milestones[item]["options"]
            )
        for row in calendar:
            assert (
                row["kind"] in ("check", "flag", "threshold", "milestone", "goal")
                and row["label"]
                and row["consequence"]
            )
            assert len(row["pByMonth"]) == horizon and row["pByMonth"] == sorted(row["pByMonth"])
            assert row["pByMonth"][-1] == pytest.approx(row["pEver"], abs=1e-5)
            if row["kind"] in ("milestone", "goal"):
                met = run.probes[f"met__{row['id']}"][:, j]
                assert row["pEver"] == pytest.approx(1 - met.mean(), abs=1e-5)
    assert list(preview["weekTrees"]) == list(run.option_names) == list(preview["dayExamples"])


def test_week_trees_and_day_examples(preview):
    count = PREVIEW[preview["caseId"]][2]
    end_metric = PREVIEW[preview["caseId"]][0]
    for tree in preview["weekTrees"].values():
        assert tree["k"] == 3 and tree["maxNodes"] == 60 and len(tree["levels"]) == 53
        assert all(0 < len(level) <= 60 for level in tree["levels"])
        assert set(tree["bands"]) == {m["id"] for m in preview["weekMetrics"]}
        for w, level in enumerate(tree["levels"][1:], start=1):
            assert all(0 <= n["parent"] < len(tree["levels"][w - 1]) for n in level)
            assert sum(n["share"] for n in level) == pytest.approx(1.0, abs=1e-4)
            assert all(n["summary"].startswith(f"Week {w}:") for n in level)
            assert all(set(n["values"]) == {m["id"] for m in preview["weekMetrics"]} for n in level)
    for option, examples in preview["dayExamples"].items():
        assert [e["kind"] for e in examples] == ["pessimistic", "median", "optimistic"]
        ends = [e["yearEnd"][end_metric] for e in examples]
        assert ends == sorted(ends)
        for e in examples:
            assert len(e["days"]) == 60 and e["option"] == option
            dates = {d["date"] for d in e["days"]}
            assert all(ev["date"] in dates for ev in e["events"])
            assert all(set(d["values"]) == {m["id"] for m in preview["dayMetrics"]} for d in e["days"])
            assert all(KB_ID.fullmatch(ev["entityId"]) for ev in e["events"] if "entityId" in ev)
            assert all(ev["person"] == ev["entityId"][7:].upper() for ev in e["events"] if "person" in ev)
            assert sum(d["values"][count] for d in e["days"]) == sum(
                ev["type"] == "incident" for ev in e["events"]
            )


def test_preview_is_deterministic_and_fresh(preview):
    case = preview["caseId"]
    again = _load("proto_future").build_preview(case)
    assert again == preview
    on_disk = json.loads((CASES / f"{case}.preview.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(preview)), "run: .venv/bin/python scripts/mvp/proto_future.py"


def test_case_layer_items_shocks_people_and_chain(any_config):
    c = any_config
    people = {p["id"]: p for p in c["people"]}
    assert c["owner"] in people
    assert all(p["entityId"] == f"person.{pid.lower()}" for pid, p in people.items())
    names = (
        {v["name"] for v in c["variables"]}
        | {d["name"] for d in c["derived"]}
        | {k["name"] for k in c["criteria"]}
    )
    plan_event = [i for i in c["timeline"]["items"] if i["kind"] in ("plan", "event")]
    assert set(c["items"]) == {i["name"] for i in plan_event}
    risks = {r["name"] for r in c["risks"]}
    for i in plan_event:
        it = c["items"][i["name"]]
        assert it["kind"] == ("lever" if i["kind"] == "plan" else "event") and it["lane"] in LANES
        assert it["label"] and it["description"] and it["affects"] and it["source"]
        assert all(a["id"] in names and a["label"] for a in it["affects"])
        assert it["owner"] is None or it["owner"] in people
        assert it["person"] is None or it["person"] in people
        assert it["valuePath"] == (it["value"]["path"] if it["value"] else None)
        assert it["range"] is None or it["range"] in names
        if it["kind"] == "event":
            assert it["shock"] in risks and c["shocks"][it["shock"]]["event"] == i["name"]
        else:
            assert it["lane"] == "decisions" and it["shock"] is None
        if it["entityId"]:
            assert re.fullmatch(r"(org\.(CL-\d{3}|VN-\d{2})|person\.lw-e\d{2})", it["entityId"]), it[
                "entityId"
            ]
    assert set(c["shocks"]) == risks
    assert all(s["lane"] in LANES and s["affects"] for s in c["shocks"].values())
    assert all(f["recompute"] in (None, "overlay", "from_date", "structural") for f in c["forks"])
    assert [f["recompute"] is None for f in c["forks"]] == [f["baseline"] for f in c["forks"]]
    chain = set(c["page"]["chain"])
    assert c["chainPath"] and all(col and set(col) <= chain for col in c["chainPath"])
    assert {x for col in c["chainPath"] for x in col} <= set(c["chainHigher"])
    variables = {v["name"]: v for v in c["variables"]}
    assert all(variables[s]["block"] not in ("lever", "timeline") for s in c["stays"])
    assert {v["block"] for v in c["variables"]} <= {b["id"] for b in c["blocks"]}
    assert set(c["goals"]) <= {k["name"] for k in c["criteria"]}
    assert all(v in c["help"] for v in c["helpKeys"].values())
    wanted = [
        "section.stays", "section.world", "section.week-review", "section.day-review",
        "control.exampleChoice", "control.dayForks", "kpi.drivers", "kpi.range", "recompute.baseline",
        "recompute.overlay", "recompute.from_date", "recompute.structural", "lane.decisions",
        "lane.clients_vendors",
    ]  # fmt: skip
    assert not [k for k in wanted if k not in c["help"]]


def test_chain_values_have_short_labels_and_the_ceo_is_a_person(any_config):
    """WP16, case page requests: short labels on the "Why" chain; the CEO in the people of each case."""
    c = any_config
    for name in c["page"]["chain"]:
        label = c["labels"][name]
        assert len(label) <= 28 and " until " not in label, (name, label)
    if c["id"] == "delivery_best_way":
        assert c["labels"]["value"] == "Business value"
    assert "LW-E01" in {p["id"] for p in c["people"]}
    assert c["people"][0]["id"] == c["owner"]


def test_week_tree_colour_metric_varies_at_the_end(preview):
    """The first week metric colours the tree; its end values must differ between branches of some fork."""
    lead = preview["weekMetrics"][0]["id"]
    spread = [len({n["values"][lead] for n in tree["levels"][-1]}) for tree in preview["weekTrees"].values()]
    assert max(spread) > 1 and sum(s > 1 for s in spread) >= len(spread) - 1, (lead, spread)


def test_no_emoji_in_help_labels_and_the_case_layer(any_config):
    keys = ("help", "labels", "forks", "hypotheses", "items", "shocks", "sections", "steps",
            "priorityPresets", "caution", "lead", "question", "title", "goals")  # fmt: skip
    layer = {k: any_config[k] for k in keys}
    found = EMOJI.findall(json.dumps(layer, ensure_ascii=False))
    assert not found, found


def test_case_a_delivery_config(any_config):
    if any_config["id"] != "delivery_best_way":
        pytest.skip("case A only")
    c = any_config
    assert c["template"] == "delivery_model" and c["letter"] == "A" and c["horizonMonths"] == 24
    assert [f["id"] for f in c["forks"]] == ["in_house", "nearshore_tm", "offshore_fixed"]
    assert [p["id"] for p in c["priorityPresets"]] == ["balanced", "money_first", "speed_first", "low_effort"]
    assert c["priorityPresets"][0]["weights"] == c["defaults"]["weights"]
    assert [h["id"] for h in c["hypotheses"]] == [s["name"] for s in c["scenarios"]]
    assert {i["name"] for i in c["timeline"]["items"]} >= {"go_live", "money_goal", "beta", "load_test_env"}
    assert c["page"]["labels"]["offshore_fixed"] == "Offshore fixed price"


def test_index_lists_every_case_and_is_fresh():
    ec = _load("export_case")
    index = ec.build_index()
    assert index["schema"] == "variator-cases/1"
    assert [r["id"] for r in index["cases"]] == CASE_IDS
    assert all(r["status"] in ("ready", "preparing") and r["question"] and r["owner"] for r in index["cases"])
    assert all((CASES / r["config"]).is_file() for r in index["cases"])
    on_disk = json.loads((CASES / "index.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(index)), "run: .venv/bin/python scripts/mvp/export_case.py"
