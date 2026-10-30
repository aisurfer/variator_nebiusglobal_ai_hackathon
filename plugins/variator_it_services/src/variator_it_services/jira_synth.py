"""Synthetic Jira-like export for demos and tests (there is no real Jira access yet).

Deterministic for a seed. Files: ``issues.csv``, ``worklogs.csv``, ``sprints.csv``,
``raid.csv``. Actual hours depend on story points and on a complexity factor per
component, so an estimator can learn the relation. Default components are the
packages of this repository, so the stories link to real code metrics.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from variator.synthetic.company import derived_rng

DEFAULT_COMPONENTS: dict[str, list[str]] = {
    name: [f"src/variator/{name}/"]
    for name in (
        "adapters",
        "rules",
        "simulation",
        "mcda",
        "rag",
        "quality",
        "mcp",
        "ingestion",
        "reasoning",
        "optimization",
        "llm",
        "report",
    )
}
# Relative effort per component (1.0 = average). A demo can replace these with values from code metrics.
DEFAULT_FACTORS = {
    "adapters": 0.9,
    "rules": 1.1,
    "simulation": 1.3,
    "mcda": 0.9,
    "rag": 1.0,
    "quality": 1.0,
    "mcp": 1.2,
    "ingestion": 1.4,
    "reasoning": 1.2,
    "optimization": 1.5,
    "llm": 0.8,
    "report": 0.7,
}
POINTS = np.array([1, 2, 3, 5, 8, 13])
POINT_WEIGHTS = np.array([0.12, 0.22, 0.26, 0.22, 0.13, 0.05])
HOURS_PER_POINT = 6.0
SIGMA = 0.35  # log-normal noise of actual hours
TEAM = [f"dev_{i}" for i in range(1, 7)]
VERBS = ("Add", "Change", "Fix", "Refactor", "Speed up", "Validate", "Document", "Extend")
OBJECTS = (
    "export",
    "import",
    "limits",
    "cache",
    "report view",
    "error messages",
    "API",
    "settings",
    "metrics",
)


@dataclass
class Issue:
    key: str
    type: str
    summary: str
    component: str
    story_points: int | None
    status: str
    sprint: str
    created: str
    resolved: str
    assignee: str
    priority: str
    epic: str
    reach: int
    impact: float
    confidence: float


@dataclass
class Worklog:
    issue: str
    author: str
    day: str
    hours: float


@dataclass
class Sprint:
    name: str
    start: str
    end: str
    capacity_hours: float


@dataclass
class RaidItem:
    id: str
    type: str  # risk | assumption | issue | dependency
    title: str
    component: str
    status: str  # open | closed
    owner_role: str
    probability: float  # risks: 0..1; others: 1
    impact_days: float
    due: str


@dataclass
class JiraExport:
    issues: list[Issue] = field(default_factory=list)
    worklogs: list[Worklog] = field(default_factory=list)
    sprints: list[Sprint] = field(default_factory=list)
    raid: list[RaidItem] = field(default_factory=list)

    def write(self, out_dir: str | Path) -> dict[str, str]:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths = {}
        for name, rows, cls in (
            ("issues", self.issues, Issue),
            ("worklogs", self.worklogs, Worklog),
            ("sprints", self.sprints, Sprint),
            ("raid", self.raid, RaidItem),
        ):
            path = out / f"{name}.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(cls.__dataclass_fields__))
                writer.writeheader()
                writer.writerows(asdict(r) for r in rows)
            paths[name] = str(path)
        return paths


def generate(
    *,
    seed: int = 7,
    sprints: int = 12,
    open_stories: int = 40,
    start: date = date(2026, 1, 5),
    components: Mapping[str, float] | None = None,
    sprint_days: int = 14,
) -> JiraExport:
    """A team of six with ``sprints`` finished sprints and a backlog of ``open_stories``."""
    rng = derived_rng(seed, "it_jira_synth")  # own stream label (openspec/project.md)
    factors = dict(components or DEFAULT_FACTORS)
    names = sorted(factors)
    export = JiraExport()
    capacity = len(TEAM) * 6.0 * sprint_days * 5 / 7 * 0.8  # 6 focus hours per working day, 80% on stories
    counter = 1
    for s in range(sprints + 1):
        begin = start + timedelta(days=s * sprint_days)
        sprint = Sprint(
            f"S{s + 1}",
            begin.isoformat(),
            (begin + timedelta(days=sprint_days - 1)).isoformat(),
            round(capacity, 1),
        )
        export.sprints.append(sprint)
        if s == sprints:
            break  # the next sprint has no finished work yet
        used = 0.0
        while used < capacity * rng.uniform(0.85, 1.0):
            issue, hours = _issue(rng, counter, names, factors, sprint, begin, sprint_days, done=True)
            counter += 1
            used += hours
            export.issues.append(issue)
            export.worklogs.extend(_worklogs(rng, issue, hours, begin, sprint_days))
    for _ in range(open_stories):
        issue, _ = _issue(
            rng,
            counter,
            names,
            factors,
            export.sprints[-1],
            start + timedelta(days=sprints * sprint_days),
            sprint_days,
            done=False,
        )
        counter += 1
        export.issues.append(issue)
    export.raid = _raid(rng, names, start + timedelta(days=sprints * sprint_days))
    return export


def _issue(rng, n, names, factors, sprint, begin, sprint_days, *, done):
    kind = rng.choice(["Story", "Bug", "Task"], p=[0.7, 0.2, 0.1])
    component = str(rng.choice(names))
    points = int(rng.choice(POINTS, p=POINT_WEIGHTS))
    hours = float(points * HOURS_PER_POINT * factors[component] * rng.lognormal(0, SIGMA))
    if kind == "Bug":
        hours *= 0.8
    summary = f"{rng.choice(VERBS)} {rng.choice(OBJECTS)} in {component}"
    created = begin - timedelta(days=int(rng.integers(1, 40)))
    resolved = begin + timedelta(days=int(rng.integers(1, sprint_days))) if done else None
    unpointed = not done and rng.random() < 0.15  # some backlog items have no estimate yet
    return Issue(
        key=f"VAR-{n}",
        type=str(kind),
        summary=summary,
        component=component if done or rng.random() > 0.1 else "",  # some backlog items have no component
        story_points=None if unpointed else points,
        status="Done" if done else str(rng.choice(["To Do", "To Do", "In Progress"])),
        sprint=sprint.name if done else "",
        created=created.isoformat(),
        resolved=resolved.isoformat() if resolved else "",
        assignee=str(rng.choice(TEAM)) if done else "",
        priority=str(rng.choice(["Low", "Medium", "High", "Highest"], p=[0.2, 0.45, 0.25, 0.1])),
        epic=f"EPIC-{int(rng.integers(1, 6))}",
        reach=int(rng.choice([50, 100, 250, 500, 1000, 2500])),
        impact=float(rng.choice([0.25, 0.5, 1.0, 2.0, 3.0])),
        confidence=float(rng.choice([0.5, 0.8, 1.0])),
    ), hours


def _worklogs(rng, issue, hours, begin, sprint_days):
    parts = int(rng.integers(1, 5))
    shares = rng.dirichlet(np.ones(parts))
    return [
        Worklog(
            issue.key,
            issue.assignee,
            (begin + timedelta(days=int(rng.integers(0, sprint_days)))).isoformat(),
            round(float(hours * share), 2),
        )
        for share in shares
    ]


def _raid(rng, names, today):
    titles = {
        "risk": [
            "Key engineer leaves",
            "Vendor API changes",
            "Load grows faster than planned",
            "Security audit fails",
        ],
        "assumption": ["Client provides test data", "Staging stays available"],
        "issue": ["Flaky integration tests", "Slow code review"],
        "dependency": ["Payment provider sandbox", "Client SSO setup"],
    }
    items = []
    n = 1
    for kind, options in titles.items():
        for title in options:
            due = today + timedelta(days=int(rng.integers(-20, 60)))
            items.append(
                RaidItem(
                    id=f"RAID-{n}",
                    type=kind,
                    title=title,
                    component=str(rng.choice(names)),
                    status=str(rng.choice(["open", "open", "closed"])),
                    owner_role=str(rng.choice(["PM", "TL", "QA"])),
                    probability=round(float(rng.uniform(0.1, 0.6)), 2) if kind == "risk" else 1.0,
                    impact_days=float(rng.choice([2, 5, 10, 15, 20])),
                    due=due.isoformat(),
                )
            )
            n += 1
    return items


def factors_from_metrics(
    metrics: Mapping[str, Mapping[str, float]], *, strength: float = 0.25
) -> dict[str, float]:
    """Synthetic "true" effort factor per component from real code metrics.

    factor = exp(strength * (z(ccn_avg) + z(commits))), scaled to a geometric mean of 1.
    A demo on a client codebase then has history that depends on its real complexity and churn.
    """
    names = sorted(metrics)
    if not names:
        return {}
    cols = np.array(
        [[float(metrics[n].get("ccn_avg", 0)), float(metrics[n].get("commits", 0))] for n in names]
    )
    std = cols.std(axis=0)
    z = np.where(std > 0, (cols - cols.mean(axis=0)) / np.where(std > 0, std, 1.0), 0.0)
    log_f = strength * z.sum(axis=1)
    log_f -= log_f.mean()
    return {n: round(float(np.exp(v)), 3) for n, v in zip(names, log_f, strict=True)}


def start_for_today(sprints: int, sprint_days: int = 14, today: date | None = None) -> date:
    """The first sprint start such that the finished history ends today (Monday-aligned)."""
    today = today or date.today()
    start = today - timedelta(days=sprints * sprint_days)
    return start - timedelta(days=start.weekday())
