"""Write the case configs of the case pages (WP13a, WP14a): workspace/config/cases/<case>.json and index.json.

Run with VARIATOR's venv:
    .venv/bin/python scripts/mvp/export_case.py               # every case config and the index
    .venv/bin/python scripts/mvp/export_case.py --reference   # + Python reference results for the TS parity tests
    .venv/bin/python scripts/mvp/export_case.py --case delivery_best_way

A config = ``report.playground.export_model`` of the case's template (rule packs, page text, scenario presets,
timeline) plus the case layer of the UI: defaults, priority presets, caution levels, world hypotheses, forks with
their recompute class, the people, the items of the timeline (lane, owner, knowledge-base entity, what it affects,
the editable value and range, the shock behind an event), the shocks, the "Why" chain, help texts, steps and
sections (Plancy Docs/variator_integration/13-MVP-UX.md §4, §5, §5a). The cases are data modules:
``case_company.py`` (B, company_model) and ``case_delivery.py`` (A, delivery_model). The TS engine
(workspace/src/variator/engine) reads the configs; the contract is engine/types.ts.
"""

# ruff: noqa: E501 - help texts and data tables, one row per entry (as the company_model template tables)

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from case_company import COMPANY  # noqa: E402
from case_delivery import DELIVERY  # noqa: E402
from case_spec import CaseSpec  # noqa: E402
from variator_it_services import plugin  # noqa: E402

from variator.domain import apply_scenarios, with_values  # noqa: E402
from variator.mcda import best_path  # noqa: E402
from variator.report.playground import export_model  # noqa: E402
from variator.rules import compile_rules  # noqa: E402
from variator.simulation import simulate_tensor  # noqa: E402
from variator.timeline import evaluate  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "workspace/config/cases"
FIXTURES = ROOT / "workspace/tests/fixtures"

SPECS: dict[str, CaseSpec] = {s.id: s for s in (COMPANY, DELIVERY)}
INDEX_ORDER = ("company_best_year", "delivery_best_way")  # the cases list (B is the first case built)
CASE_ID = COMPANY.id  # the default case (older callers)
SCHEMA = "variator-case/1"
INDEX_SCHEMA = "variator-cases/1"
DEFAULT_N = 4096
DEFAULT_SEED = 42
DEFAULT_RISK_AVERSION = 0.25  # the page and mcda.best_path default
REFERENCE_N = 4096
REFERENCE_SEEDS = (7, 42)

CAUTION = [
    ("average", "On average", "Rank by the mean result of all scenarios.", 0.0),
    (
        "margin",
        "With a margin",
        "Mostly the mean, with a quarter of the weight on the worst 5% (the default).",
        0.25,
    ),
    ("worst", "Worst 5%", "Rank by the mean of the worst 5% of the scenarios only.", 1.0),
]
STEPS = [
    ("forks", 1, "Forks", "Which options and world hypotheses do we compare?"),
    ("month", 2, "Month", "Which fork gives the best result, and why?"),
    ("week", 3, "Week", "How do the weeks unfold for the chosen forks?"),
    ("day", 4, "Day", "What does it look like day by day?"),
]
# id, step, title, question, summary, collapsed, preview, resets (page order)
SECTIONS = [
    (
        "world",
        "forks",
        "World",
        "What may the world do around our decision?",
        "World hypotheses as layers: turn on the situations you want to test.",
        False,
        False,
        ["activePresets"],
    ),
    (
        "forks",
        "forks",
        "Forks",
        "Which options do we compare, and when does each start?",
        "The forks you compare and the timeline of their interventions, events and dates.",
        False,
        False,
        ["edits", "timeline"],
    ),
    (
        "stays",
        "forks",
        "As before",
        "What stays as it was?",
        "The common ground of all options: the values that no fork, hypothesis or event changes. Correct them only when the data is wrong.",
        True,
        False,
        [],
    ),
    (
        "month-answer",
        "month",
        "Answer",
        "What should we choose?",
        "The best fork in one sentence, its expected result and the three main risks.",
        False,
        False,
        [],
    ),
    (
        "month-compare",
        "month",
        "Compare",
        "How do the forks compare?",
        "Ranking, criteria per fork and your priorities; a priority change reranks at once.",
        False,
        False,
        ["prefs"],
    ),
    (
        "month-time",
        "month",
        "Over time",
        "How will it go month by month?",
        "Monthly fans per fork and the risk calendar: when things can fail.",
        False,
        True,
        [],
    ),
    (
        "month-plan",
        "month",
        "Plan",
        "Can we make it, and what do we need?",
        "Milestones and goals with their chance, the inputs in conflict and a plan of levers.",
        False,
        False,
        ["timeline", "timelineOption"],
    ),
    (
        "month-why",
        "month",
        "Why",
        "What does the result depend on?",
        "The cause-and-effect chain per fork, the main drivers and how often each rule fires.",
        False,
        False,
        [],
    ),
    (
        "month-details",
        "month",
        "Details",
        "For the analyst",
        "Box plots, RAG detail with its limits, histograms and the run data.",
        True,
        False,
        ["n", "seed"],
    ),
    (
        "week-review",
        "week",
        "Review",
        "Which forks go on to the weeks?",
        "Keep the forks worth a closer look; a fork whose range overlaps the leader is marked.",
        False,
        False,
        ["selectedForks"],
    ),
    (
        "week",
        "week",
        "Paths",
        "How do the weeks unfold?",
        "Pick a fork and see how its year can go, week by week: the common paths, the bad branches to watch, and how far the numbers can spread.",
        False,
        True,
        [],
    ),
    (
        "day-review",
        "day",
        "Review",
        "Which forks do we see day by day?",
        "Pick one or two forks for the example days.",
        False,
        False,
        [],
    ),
    (
        "day",
        "day",
        "Examples",
        "What does it look like day by day?",
        "Three example scenarios (bad, typical, good) with daily numbers and events.",
        False,
        True,
        [],
    ),
]
SECTION_HELP = {
    "world": "Typical situations of the world around the decision. Turn on one or more: they add up as layers in a fixed order, and your own edits stay on top. The forks are compared inside the world you choose.",
    "forks": "Each fork is one decision to compare. The timeline below shows when each intervention starts, and the events you can place. Drag a marker to move its date; select it to edit its value in the inspector.",
    "stays": "These inputs keep their values from the data. They still vary inside their ranges in each scenario, but no fork, hypothesis or event on this page moves them. Change them in Model settings.",
    "month-answer": "The fork with the best score times the share of feasible scenarios. The sentence says what you aim for, what to choose and when to look again. The cards show the median and the 5%-95% range of each criterion; the risks come as consequences with their odds.",
    "month-compare": "Each criterion is scaled 0-1 across the forks, weighted and summed. The order uses score x feasible share. Change the priorities or the caution level: the ranking changes at once, without a new simulation.",
    "month-time": "A preview: monthly paths drawn to end at the results of the model at the horizon. The fan shows the median and the 25-75% and 10-90% ranges. The risk calendar shows the chance that each check, limit, milestone or goal has failed by each month.",
    "month-plan": "Each milestone and goal is met in a share of the scenarios; 80% is the target. Drag a marker to move its date. When a goal is missed, the inputs in conflict and a plan of levers (what, how much, when) show what to change.",
    "month-why": "The chain shows how the inputs move through the model to the result. Values are means over all scenarios, per fork. The drivers are the inputs that move the first criterion most.",
    "month-details": "For the analyst: the spread of each criterion per fork (box plots), the RAG shares with their limits, histograms, and the number of scenarios and the seed of the run.",
    "week-review": "Choose the forks for the week and day steps. A fork whose range of results overlaps the leader is marked: better keep it, it can still win.",
    "week": "A preview: weekly paths as a tree. Each node gives 3 variants of the next week; each level keeps at most 60 nodes, so the tree stays a fair sample. A thicker branch carries more paths.",
    "day-review": "The day level shows three example scenarios of each fork you pick: pessimistic, median and optimistic. Pick one or two forks.",
    "day": "A preview: three example scenarios of a fork, from the bad, the typical and the good part of the scenarios. Each shows the first 60 working days after the fork date and the events of those days.",
}
STEP_HELP = {
    "forks": "Step 1: choose what to compare. Changes here make the later steps out of date until they run again.",
    "month": "Step 2: the horizon as a Monte Carlo of the cause-and-effect model, per fork.",
    "week": "Step 3: weekly paths for the forks you choose in the review card.",
    "day": "Step 4: example days of 1-2 forks.",
}
KPI_HELP = {
    "decision": ("Decision", "The fork with the highest score x feasible share at your priorities."),
    "strategy": (
        "Strategy",
        "What you aim for, what to choose, and when to examine the choice again, in 1-2 sentences.",
    ),
    "score": (
        "Score",
        "Each criterion scaled 0-1 on fixed anchors (the 1st to 99th percentile of the baseline fork), times its weight, summed. A change of one fork changes only its own score. A gap to the baseline counts only beyond the meaningful difference of the criterion (inputs under Limits and thresholds).",
    ),
    "pBest": (
        "P(best)",
        "Share of scenarios where the fork has the highest weighted score of all the forks compared (the same fixed anchors as the score). The label beside it says how many forks that is («of 5 forks»): the review before the week or the day lists only some forks, but each one's P(best) is still out of all of them. Two forks equal on every criterion share the scenarios they win, so the verdict gives them together («together best in»). Failed scenarios do not count for it. In each scenario, gaps below the meaningful difference of a criterion are ties.",
    ),
    "feasible": ("Feasible", "Share of scenarios where no check rule fails."),
    "overallRag": (
        "Overall RAG",
        "Per scenario, the worst colour over the weighted criteria: green, amber or red.",
    ),
    "limits": (
        "RAG limits",
        "From the baseline fork: green = its median; red = its worst 10%. Flags make a colour worse; a failed check makes it red.",
    ),
    "expected": (
        "Expected result",
        "Median and the range of 90% of the outcomes (5% to 95%) of each criterion for the chosen fork.",
    ),
    "breaks": (
        "Changes if",
        "Single changes after which another fork leads clearly (by more than 0.02 of score): one weight halved or doubled, or another caution level.",
    ),
    "risks": (
        "Risks to watch",
        "Consequences for you, most important first: the odds in words, a fork that makes it less likely, and the input to watch.",
    ),
    "odds": ("Odds", "The share of scenarios in words: 3 in 10, 1 in 12, below 1 in 50."),
    "watch": (
        "What to watch",
        "The input whose values correlate most with the bad outcome (|r| at least 0.1).",
    ),
    "tradeoff": (
        "Trade-off",
        "On which criteria the leader is better than the second fork (and by how much on average), the reverse, and where the two are equal within the meaningful difference.",
    ),
    "vsBase": (
        "Compared with the base",
        "The difference to the run without your changes: up = better, down = worse.",
    ),
    "pMet": ("P(met)", "Share of scenarios where the milestone or goal is met by its date. Target: 80%."),
    "forecastBand": (
        "Forecast",
        "In 80% of the scenarios the milestone is reached in this range of months; the line is the median month. Your date does not change the forecast.",
    ),
    "spare": (
        "Months spare",
        "Your date minus the month that 8 of 10 scenarios meet: positive = spare time, negative = more time needed.",
    ),
    "conflicts": (
        "Inputs in conflict",
        "For a missed goal, the random inputs that correlate most with the miss (C1-C3). Too high or too low gives the direction.",
    ),
    "plan": (
        "Plan",
        "Levers of the fork in order (what, how much, when). Each takes the value with the most goals met, then the latest start.",
    ),
    "chainValue": (
        "Chain value",
        "The mean of a link of the cause-and-effect chain over all scenarios, per fork.",
    ),
    "drivers": (
        "Main drivers",
        "Inputs that move the first criterion most for this fork.",
        "The correlation of each uncertain input with the first criterion over the scenarios. A bar to the right: higher values give a better result; to the left: they make it worse.",
    ),
    "range": (
        "Range",
        "Low, likely and high value of an uncertain input.",
        "Each scenario draws a value from this range. Most draws fall near the likely value; the curve shows the shape (a PERT distribution).",
    ),
    "ruleShare": ("Rule share", "Share of scenarios where the rule fired, per fork."),
    "boxplot": (
        "Box plot",
        "Box = middle 50% of the scenarios; whiskers = the range of the slider; thick line = median; dot = mean; dashed lines = RAG limits.",
    ),
    "ragTable": ("RAG detail", "Per criterion and fork, the share of green, amber and red scenarios."),
    "histogram": (
        "Histogram",
        "How often each value occurs, one line per fork; dashed lines are the RAG limits.",
    ),
    "runMeta": (
        "Run",
        "Number of scenarios, the seed and the time of the last simulation. The same seed gives the same result.",
    ),
    "fan": (
        "Fan",
        "Median line; the dark band holds 50% of the paths (25-75%), the light band 80% (10-90%). Preview data.",
    ),
    "riskCalendar": (
        "Risk calendar",
        "Chance that the check, limit, milestone or goal has failed by each month (cumulative). Preview data.",
    ),
    "medianFirstMonth": (
        "Typical month",
        "Among the scenarios where it fails, the median month of the first failure.",
    ),
    "weekTree": (
        "Week tree",
        "Weeks on the axis; a branch is a group of paths; a thicker branch carries more paths. Click a node for the week summary. Preview data.",
    ),
    "weekNode": (
        "Week node",
        "One week of one group of paths: its numbers at the end of the week and a short summary.",
    ),
    "dayExample": (
        "Example future",
        "A scenario of the model from the bad (10%), typical (50%) or good (90%) part of the first criterion at the horizon. Preview data.",
    ),
    "dayEvents": (
        "Events",
        "What happens on each day of the example. A link opens the record in the knowledge base.",
    ),
    "simulationBadge": ("Simulation", "These numbers are a simulated future, not recorded data."),
    "preview": (
        "Preview",
        "Stand-in data drawn to match the model at the horizon, until the month-by-month simulation on actors is ready.",
    ),
    "stale": ("Out of date", "An input changed after this result. It updates after the next run."),
}
CONTROL_HELP = {
    "hypotheses": (
        "World hypotheses",
        "Typical situations as layers. Turn on one or more: they add up in a fixed order. Your own edits stay on top.",
    ),
    "priorityPresets": (
        "Priorities",
        "Ready weight sets. Balanced = the model defaults. A preset sets all weights; you can then move each one.",
    ),
    "weights": ("Weights", "How much each criterion counts in the score, 0-3. Only the ratios matter."),
    "caution": (
        "How careful?",
        "On average = rank by the mean; with a margin = a quarter of the weight on the worst 5%; worst 5% = rank by the bad cases only.",
    ),
    "compare": (
        "Compare with the start",
        "Shows the difference to a run without your changes and hypotheses.",
    ),
    "timelineOption": (
        "Show for",
        "The fork whose timeline, conflicts and plan are shown; default: the best fork.",
    ),
    "eventButtons": ("Events", "Place a certain event on the timeline, then move it to its month."),
    "dragMarker": (
        "Move a date",
        "Drag a marker, or focus it and press the arrow keys (half a month; Shift: a month). Esc during a drag moves it back.",
    ),
    "suggestDates": (
        "Suggest dates",
        "Each milestone moves to the month that 8 of 10 scenarios meet, rounded up to half a month.",
    ),
    "applyPlan": ("Apply the plan", "Sets the levers of the plan (the start months) on the timeline."),
    "deleteItem": ("Delete", "Removes the milestone or goal from the evaluation. Restore it from the list."),
    "restoreItem": ("Restore", "Brings a deleted milestone or goal back."),
    "resetField": ("Back to the default", "Removes your edit of this value; a hypothesis value stays."),
    "resetSection": ("Reset the section", "Removes your changes in this section only."),
    "resetAll": (
        "Back to the start",
        "Removes all your changes and hypotheses. You can undo it in the notification.",
    ),
    "undo": ("Undo", "Restores the value before this change."),
    "modelSettings": (
        "Model settings",
        "All inputs by block, the link coefficients, the shocks and the levers. A changed value has a dot; a hypothesis value has a mark.",
    ),
    "inspector": (
        "Intervention",
        "The value and its range (low, likely, high) of the selected intervention, its source, its owner and what it changes.",
    ),
    "n": ("Scenarios", "Number of simulated scenarios. More gives smoother numbers and takes longer."),
    "seed": ("Seed", "Start of the random numbers. The same seed gives the same result."),
    "whiskers": ("Whiskers", "The range of the box-plot whiskers, from 1%-99% to 25%-75%."),
    "forkSelection": (
        "Forks for the next steps",
        "Choose the forks for the week and day steps. A fork whose range overlaps the leader is marked: better keep it.",
    ),
    "dayForks": (
        "Forks for the day level",
        "Pick one or two forks to see day by day.",
        "The day level shows three example scenarios of each fork you pick: pessimistic, median and optimistic.",
    ),
    "exampleChoice": (
        "Example future",
        "Choose the pessimistic, the median or the optimistic example.",
        "The examples are scenarios of the model from the bad (10%), the typical (50%) and the good (90%) part of the first criterion.",
    ),
    "openInKb": (
        "Open in the knowledge base",
        "Shows the brief, dossiers and questions on this example branch, marked as a simulation.",
    ),
}
RECOMPUTE_HELP = {
    "baseline": (
        "Baseline",
        "The fork every delta is measured against.",
        "The baseline is always compared, and every vs-baseline delta on the page is measured against it. Its RAG limits are the green and red lines of the other forks.",
    ),
    "overlay": (
        "Overlay",
        "Recomputed as an overlay on the same paths: instant.",
        "An overlay fork changes only prices or values on top of the same scenario paths; no actor reacts to the change by itself. The recompute is instant.",
    ),
    "from_date": (
        "Rerun from its date",
        "The paths are rerun from the start date of the fork.",
        "This fork changes how the company works from its start month. The scenarios are rerun from that month on; earlier months stay the same.",
    ),
    "structural": (
        "Structural",
        "New actors or rules: the whole horizon is rerun.",
        "This fork adds something new to the company or the project, for example a second cloud or a vendor team. The whole horizon is rerun, and the ranges are wider.",
    ),
}
LANE_HELP = {
    "decisions": ("Decisions", "What you decide and when it starts: the levers of the forks."),
    "people": ("People", "Events about people: a key engineer leaves, a hire starts later."),
    "clients_vendors": (
        "Clients and vendors",
        "Events at clients and suppliers: a client gives notice, a vendor raises prices or rotates its team.",
    ),
    "factors": ("Factors", "Outside factors: shortages, markets, currency and regulation."),
}
# page help key -> config help key (the page's short names for the "?" icons)
HELP_KEYS = {
    "pBest": "kpi.pBest", "feasible": "kpi.feasible", "score": "kpi.score", "rag": "kpi.overallRag", "priorities": "control.priorityPresets",
    "caution": "control.caution", "changesIf": "kpi.breaks", "expected": "kpi.expected", "risks": "kpi.risks", "tradeoff": "kpi.tradeoff",
    "criteriaTable": "kpi.vsBase", "fan": "kpi.fan", "calendar": "kpi.riskCalendar", "goalsMet": "kpi.pMet", "timeline": "kpi.forecastBand",
    "pmet": "kpi.pMet", "spare": "kpi.spare", "conflicts": "kpi.conflicts", "levers": "kpi.plan", "suggest": "control.suggestDates",
    "chain": "kpi.chainValue", "drivers": "kpi.drivers", "range": "kpi.range", "rules": "kpi.ruleShare", "boxplot": "kpi.boxplot",
    "ragTable": "kpi.ragTable", "review": "control.forkSelection", "dayReview": "control.dayForks", "tree": "kpi.weekTree", "weekly": "kpi.weekNode",
    "examples": "kpi.dayExample", "exampleChoice": "control.exampleChoice", "daily": "kpi.dayEvents", "hypotheses": "control.hypotheses",
    "interventions": "control.dragMarker", "events": "control.eventButtons", "inspector": "control.inspector", "compareOriginal": "control.compare",
    "returnOriginal": "control.resetAll", "modelSettings": "control.modelSettings", "simulation": "kpi.simulationBadge", "preview": "kpi.preview",
    "openInKb": "control.openInKb", "stays": "section.stays", "world": "section.world", "limits": "kpi.limits", "histogram": "kpi.histogram",
    "runMeta": "kpi.runMeta", "medianFirstMonth": "kpi.medianFirstMonth", "weights": "control.weights", "resetField": "control.resetField",
    "resetSection": "control.resetSection", "undo": "control.undo", "n": "control.n", "seed": "control.seed", "whiskers": "control.whiskers",
}  # fmt: skip
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FUNCTIONS = {"minimum", "maximum", "clip", "min", "max", "abs", "exp", "log", "sqrt", "where"}
TAGS = re.compile(r"<[^>]+>")
EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿⌀-⏿←-⇿⤀-⥿️‍⃣]")


def no_emoji(text: str) -> str:
    """Plain text for people: no emoji, no double spaces."""
    return re.sub(r"\s{2,}", " ", EMOJI.sub("", text or "")).strip()


def _plain(text: str) -> str:
    return no_emoji(TAGS.sub("", text or ""))


def _num(x: float) -> str:
    """A number for people: 12000000 -> 12M, 0.12 -> 0.12, 45 -> 45."""
    a = abs(x)
    if a >= 1e6:
        return f"{x / 1e6:.3g}M"
    if a >= 1e4:
        return f"{x / 1e3:.3g}k"
    return f"{x:.4g}"


def _dist_text(d: dict[str, Any], unit: str) -> str:
    kind = d["kind"]
    if kind == "fixed":
        return f"{_num(d['value'])} {unit}".strip()
    if kind in ("pert", "triangular"):
        return f"likely {_num(d['mode'])}, from {_num(d['low'])} to {_num(d['high'])} {unit}".strip()
    if kind == "uniform":
        return f"from {_num(d['low'])} to {_num(d['high'])} {unit}".strip()
    if kind in ("normal", "lognormal"):
        return f"mean {_num(d['mean'])}, std {_num(d['std'])} {unit}".strip()
    return f"probability {d['p']:.2f}"


def _spec(case: str | CaseSpec | None) -> CaseSpec:
    if isinstance(case, CaseSpec):
        return case
    return SPECS[case or CASE_ID]


def _source(case: str | CaseSpec | None = None) -> tuple[Any, list, dict, list, Any]:
    """(model, rule packs, page text, scenario presets, timeline) of the case's template."""
    spec = _spec(case)
    model = with_values(plugin.templates[spec.template](), spec.defaults)  # the case's own defaults
    if spec.dists or spec.sources:  # the case's own distributions and sources (keep the block before " | ")
        variables = [
            v.model_copy(
                update={
                    "distribution": spec.dists.get(v.name, v.distribution),
                    "source": f"{(v.source or '').partition(' | ')[0]} | {spec.sources[v.name]}"
                    if v.name in spec.sources
                    else v.source,
                }
            )
            for v in model.variables
        ]
        model = model.model_copy(update={"variables": variables})
    packs = [p for p in plugin.rule_packs if p.templates is None or spec.template in p.templates]
    return (
        model,
        packs,
        spec.page,
        plugin.scenarios.get(spec.template, []),
        plugin.timelines.get(spec.template),
    )


def _labels(data: dict[str, Any], short: dict[str, str] | None = None) -> dict[str, str]:
    """Names for people: page labels first, then the short labels of the "Why" chain values (``short``, the
    case's ``short_labels``), then descriptions (as ``lbl`` on the page), then the id."""
    page = data["page"].get("labels", {})
    out: dict[str, str] = {}
    for v in data["variables"]:
        out[v["name"]] = v["desc"]
    for d in data["derived"]:
        out[d["name"]] = d["desc"]
    for r in data["risks"]:
        out[r["name"]] = r["desc"]
    for r in data["rules"]:
        out[r["id"]] = r["desc"] or r["id"].replace("_", " ").capitalize()
    for o in data["options"]:
        out[o["name"]] = o["desc"]
    for c in data["criteria"]:
        out[c["name"]] = c["name"].replace("_", " ").capitalize()
    if data["timeline"]:
        for it in data["timeline"]["items"]:
            out[it["name"]] = it["label"]
        for lv in data["timeline"]["levers"]:
            out.setdefault(lv["variable"], lv["label"])
    out.update({k: v for k, v in (short or {}).items() if k in data["page"].get("chain", [])})
    out.update(page)
    return {k: no_emoji(v) for k, v in out.items()}


def _users(data: dict[str, Any]) -> dict[str, list[str]]:
    """Name -> derived values and criteria whose formula uses it directly."""
    users: dict[str, list[str]] = {}
    for d in data["derived"]:
        for n in sorted(set(IDENT.findall(d["formula"]))):
            users.setdefault(n, []).append(d["name"])
    first = data["options"][0]
    formulas = [first["formula"]] + [first["metrics"][c["name"]] for c in data["criteria"][1:]]
    for c, f in zip(data["criteria"], formulas, strict=True):
        for n in sorted(set(IDENT.findall(f))):
            users.setdefault(n, []).append(f"criterion {c['name']}")
    return users


def _op_text(op: dict[str, Any], data: dict[str, Any], labels: dict[str, str]) -> str:
    kind, target = op.get("kind", "variable"), op["target"]
    name = labels.get(target, target)
    if kind == "weight":
        return f"Weight of {name}: x {op['scale']:g}"
    if kind == "risk":
        risk = next(r for r in data["risks"] if r["name"] == target)
        if op.get("probability") is not None:
            after = 1 - (1 - risk["p"]) * (1 - op["probability"])
            return f"{name}: chance {risk['p']:.0%} -> {after:.0%}"
        return f"{name}: size x {op['scale']:g}"
    var = next(v for v in data["variables"] if v["name"] == target)
    who = f" ({', '.join(labels.get(o, o) for o in op['options'])})" if op.get("options") else ""
    if op.get("scale") is not None:
        return f"{name}: x {op['scale']:g}{who}"
    sign = "+" if op["shift"] >= 0 else "-"
    return f"{name}: {sign} {_num(abs(op['shift']))} {var['unit']}{who}".rstrip()


def _rule_text(rule: dict[str, Any]) -> str:
    def act(a: dict[str, Any]) -> str:
        if a.get("set"):
            return ", ".join(f"{k} = {v}" for k, v in a["set"].items())
        if a.get("fail"):
            return f"fail: {a['fail']}"
        return f"{a['flag']}" + (f" on {a['criterion']}" if a.get("criterion") else "")

    lines = [
        f"{'if' if i == 0 else 'elif'} {b['if']}: {act(b['then'])}" for i, b in enumerate(rule["branches"])
    ]
    if rule.get("else"):
        lines.append(f"else: {act(rule['else'])}")
    return "\n".join(lines)


def _sections(spec: CaseSpec) -> list[dict[str, Any]]:
    keys = ("id", "step", "title", "question", "summary", "collapsed", "preview", "resets")
    return [{**dict(zip(keys, s, strict=True)), **spec.sections.get(s[0], {})} for s in SECTIONS]


def _help(
    spec: CaseSpec,
    data: dict[str, Any],
    labels: dict[str, str],
    forks: list,
    hypotheses: list,
    sections: list,
) -> dict[str, dict]:
    page = data["page"]
    users = _users(data)
    out: dict[str, dict[str, Any]] = {}

    def put(key: str, title: str, body: str, **extra: Any) -> None:
        clean = {k: (no_emoji(v) if k == "more" else v) for k, v in extra.items() if v}
        out[key] = {"title": no_emoji(title), "body": no_emoji(body), **clean}

    def entry(key: str, value: tuple) -> None:
        title, body, *more = value
        put(key, title, body, more=more[0] if more else "")

    for sec in sections:
        sid, title, summary = sec["id"], sec["title"], sec["summary"]
        more = ""
        if sid == "forks":
            more = _plain(page.get("lead", "")) + " " + " ".join(_plain(x) for x in page.get("change", []))
        elif sid == "month-why":
            more = " ".join(_plain(x) for x in page.get("real", []))
        elif sid == "month-details":
            more = " ".join(_plain(x) for x in page.get("metrics", []))
        put(
            f"section.{sid}",
            title,
            spec.section_help.get(sid, SECTION_HELP[sid]),
            more=more.strip() or summary,
        )
    for sid, _i, title, _q in STEPS:
        put(f"step.{sid}", title, spec.step_help.get(sid, STEP_HELP[sid]))
    for key, value in {**KPI_HELP, **spec.kpi_help}.items():
        entry(f"kpi.{key}", value)
    for key, value in {**CONTROL_HELP, **spec.control_help}.items():
        entry(f"control.{key}", value)
    for key, value in RECOMPUTE_HELP.items():
        entry(f"recompute.{key}", value)
    for key, value in LANE_HELP.items():
        entry(f"lane.{key}", value)
    first = data["options"][0]
    for k, c in enumerate(data["criteria"]):
        formula = first["formula"] if k == 0 else first["metrics"][c["name"]]
        put(
            f"criterion.{c['name']}",
            labels[c["name"]],
            f"{c['desc']}, {c['unit']}; {'higher' if c['higher'] else 'lower'} is better. Default weight {c['weight']:g}.",
            more=page.get("risks", {}).get(c["name"], ""),
            formula=f"{c['name']} = {formula}",
            metric=c["name"],
        )
    for f in forks:
        lev = (
            ", ".join(f"{labels.get(k, k)} = {v:g}" for k, v in f["levers"].items())
            or "no lever: the baseline"
        )
        if not f["levers"] and not f["baseline"]:
            lev = "its own team, rates and terms (option overrides)"
        put(f"fork.{f['id']}", f["label"], f"{f['description']}.", more=f"Sets: {lev}.")
    for h in hypotheses:
        put(f"hypothesis.{h['id']}", h["label"], h["description"], more="; ".join(h["effects"]) + ".")
    for pid, label, desc, _w in spec.priorities:
        put(f"priority.{pid}", label, desc)
    for cid, label, desc, _v in CAUTION:
        put(f"caution.{cid}", label, desc)
    kinds = {
        "effect": "An effect rule changes an input when its condition is true.",
        "check": "A check rule: when it fails, the scenario is not acceptable (infeasible).",
        "flag": "A flag rule sets a RAG colour on a criterion.",
    }
    for r in data["rules"]:
        body = f"{r['desc'] + '. ' if r['desc'] else ''}{kinds[r['kind']]}"
        put(f"rule.{r['id']}", labels[r["id"]], body, formula=_rule_text(r))
    for d in data["derived"]:
        ins = sorted(set(IDENT.findall(d["formula"])) - FUNCTIONS)
        put(
            f"derived.{d['name']}",
            labels[d["name"]],
            f"{d['desc']}, {d['unit']}. A link of the cause-and-effect chain.",
            more=f"Inputs: {', '.join(labels.get(n, n) for n in ins)}. Used by: "
            f"{', '.join(labels.get(n, n) for n in users.get(d['name'], [])) or 'none'}.",
            formula=f"{d['name']} = {d['formula']}",
            metric=d["name"],
        )
    for v in data["variables"]:
        shocks = [r["desc"] for r in data["risks"] if r["variable"] == v["name"]]
        more = f"Used by: {', '.join(labels.get(n, n) for n in users.get(v['name'], [])) or 'none'}."
        if shocks:
            more += f" Shocks that change it: {'; '.join(shocks)}."
        put(
            f"variable.{v['name']}",
            labels[v["name"]],
            f"Default: {_dist_text(v['dist'], v['unit'])}. Source: {v['source'] or '-'}.",
            more=more,
            metric=v["name"],
        )
    for r in data["risks"]:
        verb = "multiplies" if r["effect"] == "multiply" else "adds"
        what = f"{_dist_text(r['magnitude'], '')}" + (" (relative)" if r["effect"] == "multiply" else "")
        who = f" Only for: {', '.join(labels.get(o, o) for o in r['options'])}." if r.get("options") else ""
        put(
            f"risk.{r['name']}",
            r["desc"],
            f"Chance {r['p']:.0%} in the horizon. When it occurs, it {verb} {what} to "
            f"{labels.get(r['variable'], r['variable'])}.{who}",
        )
    tl = data["timeline"] or {"items": [], "levers": []}
    for it in tl["items"]:
        month = f"month variable {it['month']}" if isinstance(it["month"], str) else f"month {it['month']:g}"
        body = (
            it["description"]
            or {
                "start": "The value at the start.",
                "plan": "When this intervention starts. Drag it to another month.",
                "event": "A certain event from its month. Place it, then move it.",
                "milestone": "Met when the forecast month is not later than the marker. Target: 80% of the scenarios.",
                "goal": "Met when the condition holds at the horizon. Target: 80% of the scenarios.",
            }[it["kind"]]
        )
        put(
            f"timeline.{it['name']}",
            it["label"],
            body,
            formula=it["condition"] or it["show"] or "",
            more=month,
        )
    for lv in tl["levers"]:
        put(
            f"lever.{lv['variable']}",
            lv["label"],
            f"From {lv['low']:g} to {lv['high']:g} {lv['unit']} in steps of {lv['step']:g}; the plan prefers the "
            f"{'later' if lv['prefer'] == 'high' else 'lower'} value among equal results.",
        )
    for mid, (title, desc) in spec.preview_metrics.items():
        put(f"metric.{mid}", title, desc + " Preview data.")
    for cid, (title, desc) in spec.calendar_rows.items():
        put(f"check.{cid}", title, desc + " Preview data.")
    return out


def _affects(spec: CaseSpec, ids: list[str]) -> list[dict[str, str]]:
    return [{"id": i, "label": spec.short_labels[i]} for i in ids]


def _value(spec: CaseSpec, meta: dict[str, Any], variables: dict[str, dict]) -> dict[str, Any] | None:
    v = meta.get("value")
    if not v:
        return None
    var = v["variable"]
    if var not in variables:
        raise KeyError(f"{spec.id}: the value variable {var} is not in the model")
    path = f"o|{v['option']}|{var}|value" if v.get("option") else f"v|{var}|value"
    return {
        "path": path,
        "variable": var,
        "label": v["label"],
        "unit": variables[var]["unit"],
        "min": v["min"],
        "max": v["max"],
        "step": v["step"],
    }


def _items(spec: CaseSpec, data: dict[str, Any], labels: dict[str, str]) -> dict[str, dict[str, Any]]:
    variables = {v["name"]: v for v in data["variables"]}
    risks = {r["name"]: r for r in data["risks"]}
    out: dict[str, dict[str, Any]] = {}
    for it in data["timeline"]["items"] if data["timeline"] else []:
        if it["kind"] not in ("plan", "event"):
            continue
        meta = spec.items[it["name"]]
        value = _value(spec, meta, variables)
        rng = meta.get("range")
        if rng and rng not in variables:
            raise KeyError(f"{spec.id}: range variable {rng} is not in the model")
        lever = it["kind"] == "plan"
        if lever:
            tag = variables[rng]["source"] if rng else "plan"
        else:
            shock = risks.get(meta.get("shock") or "")
            tag = variables[shock["variable"]]["source"] if shock else "timeline"
        out[it["name"]] = {
            "kind": "lever" if lever else "event",
            "lane": meta["lane"],
            "label": no_emoji(meta.get("label") or it["label"]),
            "description": no_emoji(
                meta.get("description") or it["description"] or labels.get(it["name"], "")
            ),
            "owner": meta.get("owner"),
            "person": meta.get("person"),
            "entityId": meta.get("entity"),
            "affects": _affects(spec, meta["affects"]),
            "shock": meta.get("shock"),
            "valuePath": value["path"] if value else None,
            "value": value,
            "range": rng,
            "source": meta.get("source") or f"{'plan' if lever else 'event'} · {tag.split(': ', 1)[-1]}",
        }
    return out


def _shocks(spec: CaseSpec, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for r in data["risks"]:
        meta = spec.shocks[r["name"]]
        out[r["name"]] = {
            "label": no_emoji(meta.get("label") or r["desc"]),
            "lane": meta["lane"],
            "owner": meta.get("owner"),
            "person": meta.get("person"),
            "entityId": meta.get("entity"),
            "affects": _affects(spec, meta["affects"]),
            "event": meta.get("event"),
        }
    return out


def _stays(spec: CaseSpec, data: dict[str, Any], items: dict, shocks: dict) -> list[str]:
    """Inputs that no fork, hypothesis, item or shock moves (the "As before" section), in block order."""
    touched: set[str] = set(spec.touched)
    for o in data["options"]:
        touched |= set(o["overrides"])
    risk_var = {r["name"]: r["variable"] for r in data["risks"]}
    for s in data["scenarios"]:
        for op in s["ops"]:
            kind = op.get("kind", "variable")
            if kind == "variable":
                touched.add(op["target"])
            elif kind == "risk":
                touched.add(risk_var[op["target"]])
    touched |= set(risk_var.values())
    for r in data["rules"]:  # effect rules change inputs (burnout raises attrition)
        if r["kind"] == "effect":
            for act in [b["then"] for b in r["branches"]] + ([r["else"]] if r["else"] else []):
                touched |= set(act.get("set") or {})
    for it in items.values():
        if it["value"]:
            touched.add(it["value"]["variable"])
        if it["range"]:
            touched.add(it["range"])
    tl = data["timeline"] or {"items": [], "levers": []}
    for it in tl["items"]:
        if isinstance(it["month"], str):
            touched.add(it["month"])
        touched |= set(it["place"])
    touched |= {lv["variable"] for lv in tl["levers"]}
    order = {b[0]: i for i, b in enumerate(spec.blocks)}
    # limits of the rules and the RAG bands are settings, not facts of the world
    keep = [
        v
        for v in data["variables"]
        if v["name"] not in touched and v["block"] not in ("lever", "timeline", "limits")
    ]
    return [v["name"] for v in sorted(keep, key=lambda v: order.get(v["block"], len(order)))]


def _goals(spec: CaseSpec, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    variables = {v["name"]: v for v in data["variables"]}
    out = {}
    for crit, (source, text) in spec.goals.items():
        if isinstance(source, str):
            d = variables[source]["dist"]
            value = d.get("value", d.get("mode"))
        else:
            value = source
        out[crit] = {
            "value": value,
            "label": text.format(
                value=value, usd=_usd(value), pct=f"{value:.0%}", pct1=f"{value * 100:.1f}%"
            ),
        }
    return out


def _usd(x: float) -> str:
    a = abs(x)
    s = f"${a / 1e6:.2f}M" if a >= 1e6 else f"${a / 1e3:.0f}k" if a >= 1e3 else f"${a:.0f}"
    return ("−" if x < 0 else "") + s


def _apply_blocks(spec: CaseSpec, data: dict[str, Any]) -> None:
    """Case blocks of the inputs (a template without blocks exports "inputs" for every variable)."""
    for v in data["variables"]:
        v["block"] = spec.variable_blocks.get(v["name"], v["block"])


def build_config(case: str | CaseSpec | None = None) -> dict[str, Any]:
    spec = _spec(case)
    model, packs, page, scenarios, timeline = _source(spec)
    data = export_model(model, packs, page, scenarios, timeline)
    _apply_blocks(spec, data)
    if spec.question:
        data["question"] = spec.question
    labels = _labels(data, spec.short_labels)
    weights = {c["name"]: c["weight"] for c in data["criteria"]}
    tl_items = data["timeline"]["items"] if data["timeline"] else []
    var_value = {v["name"]: v["dist"].get("value") for v in data["variables"]}
    forks = []
    for j, o in enumerate(data["options"]):
        mine = [it for it in tl_items if it["options"] and o["name"] in it["options"]]
        forks.append(
            {
                "id": o["name"],
                "kind": "decision",
                "label": labels[o["name"]],
                "description": no_emoji(spec.fork_descriptions.get(o["name"], o["desc"])),
                "icon": spec.fork_icons.get(o["name"], "git-branch"),
                "color": j % 5,
                "levers": {k: d.get("value", d.get("mode")) for k, d in o["overrides"].items()},
                "interventions": [
                    {
                        "item": it["name"],
                        "label": no_emoji(it["label"]),
                        "kind": it["kind"],
                        "monthVar": it["month"] if isinstance(it["month"], str) else None,
                        "month": var_value[it["month"]] if isinstance(it["month"], str) else it["month"],
                    }
                    for it in mine
                ],
                "baseline": j == 0,
                "recompute": spec.recompute[o["name"]],
            }
        )
    hypotheses = [
        {
            "id": s["name"],
            "kind": "hypothesis",
            "label": no_emoji(s["label"]),
            "description": no_emoji(s["description"]),
            "emoji": "",  # no emoji on the case layer (the model's scenarios keep theirs); the UI shows `icon`
            "icon": spec.hypothesis_icons.get(s["name"], "layers"),
            "effects": [no_emoji(_op_text(op, data, labels)) for op in s["ops"]],
        }
        for s in data["scenarios"]
    ]
    items = _items(spec, data, labels)
    shocks = _shocks(spec, data)
    sections = _sections(spec)
    return {
        "schema": SCHEMA,
        "id": spec.id,
        "letter": spec.letter,
        "title": spec.title,
        "company": spec.company,
        "lead": spec.lead,
        "owner": spec.owner,
        "forkDate": spec.fork_date,
        "horizonMonths": data["timeline"]["horizon"] if data["timeline"] else spec.horizon,
        "template": spec.template,
        "previewFile": f"{spec.id}.preview.json",
        **data,
        "defaults": {
            "weights": weights,
            "riskAversion": DEFAULT_RISK_AVERSION,
            "n": DEFAULT_N,
            "seed": DEFAULT_SEED,
            "activePresets": [],
            "edits": {},
        },
        "priorityPresets": [
            {"id": pid, "label": label, "description": desc, "weights": dict(w or weights)}
            for pid, label, desc, w in spec.priorities
        ],
        "caution": [
            {"id": cid, "label": label, "description": d, "value": v} for cid, label, d, v in CAUTION
        ],
        "hypotheses": hypotheses,
        "forks": forks,
        "people": [
            {"id": pid, "name": name, "title": title, "entityId": f"person.{pid.lower()}"}
            for pid, name, title in spec.people
        ],
        "items": items,
        "shocks": shocks,
        "chainPath": spec.chain_path,
        "chainHigher": spec.chain_higher,
        "blocks": [{"id": b, "label": label} for b, label in spec.blocks],
        "stays": _stays(spec, data, items, shocks),
        "goals": _goals(spec, data),
        "riskRows": spec.risk_rows,
        "helpKeys": HELP_KEYS,
        "help": _help(spec, data, labels, forks, hypotheses, sections),
        "steps": [
            {"id": sid, "index": i, "title": t, "question": q, "help": f"step.{sid}"}
            for sid, i, t, q in STEPS
        ],
        "sections": [
            {
                "id": s["id"],
                "step": s["step"],
                "title": s["title"],
                "question": s["question"],
                "summary": s["summary"],
                "help": f"section.{s['id']}",
                "collapsed": s["collapsed"],
                "preview": s["preview"],
                "resets": s["resets"],
            }
            for s in sections
        ],
        "labels": labels,
    }


def build_index(configs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """The cases list (config/cases/index.json)."""
    configs = configs or {cid: build_config(cid) for cid in INDEX_ORDER}
    rows = []
    for cid in INDEX_ORDER:
        spec, cfg = SPECS[cid], configs[cid]
        owner = next(name for pid, name, _t in spec.people if pid == spec.owner)
        rows.append(
            {
                "id": cid,
                "letter": spec.letter,
                "title": spec.title,
                "question": cfg["question"],
                "template": spec.template,
                "status": spec.status,
                "config": f"{cid}.json",
                "preview": cfg["previewFile"],
                "owner": owner,
                "forks": len(cfg["forks"]),
                "hypotheses": len(cfg["hypotheses"]),
            }
        )
    return {"schema": INDEX_SCHEMA, "cases": rows}


def _reference_run(model: Any, packs: list, timeline: Any, seed: int, n: int) -> dict[str, Any]:
    rules = compile_rules(
        packs,
        variables=[v.name for v in model.variables],
        options=[o.name for o in model.options],
        criteria=[c.name for c in model.effective_criteria()],
        derived=model.derived_map(),
    )
    run = simulate_tensor(model, rules=rules, n_scenarios=n, seed=seed)
    feasible = run.feasible_share()
    path = best_path(run)
    out: dict[str, Any] = {
        "seed": seed,
        "n": n,
        "options": list(run.option_names),
        "criteria": [c.name for c in run.criteria],
        "mean": {
            o: {c.name: float(run.values[:, j, k].mean()) for k, c in enumerate(run.criteria)}
            for j, o in enumerate(run.option_names)
        },
        "std": {
            o: {c.name: float(run.values[:, j, k].std()) for k, c in enumerate(run.criteria)}
            for j, o in enumerate(run.option_names)
        },
        "p50": {
            o: {c.name: float(sorted(run.values[:, j, k])[n // 2]) for k, c in enumerate(run.criteria)}
            for j, o in enumerate(run.option_names)
        },
        "feasible": {o: float(feasible[j]) for j, o in enumerate(run.option_names)},
        "bestPath": {
            "decision": path.decision,
            "runnerUp": path.runner_up,
            "breaks": path.breaks_if,
            "pBest": path.prob_best,
            "pFeasible": path.p_feasible,
        },
    }
    if timeline is not None:
        tl = evaluate(model, timeline, rules, n_scenarios=n, seed=seed)
        out["timeline"] = {
            "pMet": {i.name: i.p_met for i in tl.items if i.target is not None and i.p_met},
            "spread": {i.name: {o: list(s) for o, s in i.spread.items()} for i in tl.items if i.spread},
        }
    return out


def build_reference(case: str | CaseSpec | None = None) -> dict[str, Any]:
    spec = _spec(case)
    model, packs, _page, scenarios, timeline = _source(spec)
    runs = [_reference_run(model, packs, timeline, seed, REFERENCE_N) for seed in REFERENCE_SEEDS]
    layered = apply_scenarios(model, scenarios, spec.reference_presets)
    preset_run = _reference_run(layered, packs, timeline, REFERENCE_SEEDS[0], REFERENCE_N)
    preset_run["activePresets"] = list(spec.reference_presets)
    return {
        "caseId": spec.id,
        "template": spec.template,
        "rule": "z = |mean_ts - mean_py| / (std * sqrt(2 / n)) < 4; shares: z for two proportions < 4",
        "runs": runs,
        "presetRuns": [preset_run],
    }


def _write(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--case", choices=[*SPECS, "all"], default="all")
    ap.add_argument("--reference", action="store_true", help="also write the Python reference results")
    args = ap.parse_args(argv)
    ids = list(INDEX_ORDER) if args.case == "all" else [args.case]
    configs = {}
    for cid in ids:
        configs[cid] = build_config(cid)
        print(_write(CASES / f"{cid}.json", configs[cid]), f"({len(configs[cid]['help'])} help keys)")
        if args.reference:
            print(_write(FIXTURES / f"{cid}.reference.json", build_reference(cid)))
    if args.case == "all":
        print(_write(CASES / "index.json", build_index(configs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
