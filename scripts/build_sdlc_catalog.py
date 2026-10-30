"""Build the offline SDLC metric catalog (`variator_it_services/data/sdlc_metrics.json`).

Sources: Apache DevLake metric docs (parsed, verbatim excerpts), DORA, OpenSSF Scorecard and
CHAOSS (curated summaries). Each entry has `doc_url` (human page) and `source_url` (pinned).

Rebuild:
1. git clone --depth 1 --filter=blob:none --sparse https://github.com/apache/incubator-devlake-website <dir>
2. git -C <dir> sparse-checkout set docs/Metrics
3. uv run python scripts/build_sdlc_catalog.py <dir> <commit> <commit date>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / (
    "plugins/variator_it_services/src/variator_it_services/data/sdlc_metrics.json"
)
SCORECARD_COMMIT = "83b2069eee6e3c2fe7ae3b3eb745faa62224e618"
DEVLAKE_SITE = "https://devlake.apache.org/docs/Metrics/"
DEVLAKE_REPO = "https://github.com/apache/incubator-devlake-website/blob/{commit}/docs/Metrics/{file}"
DORA_GUIDE = "https://dora.dev/guides/dora-metrics-four-keys/"

# DevLake file -> (id, group, unit, direction, used_by). direction: lower / higher / neutral.
DEVLAKE = {
    "RequirementCount.md": ("requirement_count", "requirements", "issues", "neutral", ["jira_synth"]),
    "RequirementLeadTime.md": (
        "requirement_lead_time",
        "requirements",
        "days",
        "lower",
        ["jira_synth", "estimation"],
    ),
    "RequirementDeliveryRate.md": (
        "requirement_delivery_rate",
        "requirements",
        "share",
        "higher",
        ["estimation.release_forecast"],
    ),
    "RequirementGranularity.md": (
        "requirement_granularity",
        "requirements",
        "story points",
        "lower",
        ["estimation"],
    ),
    "BugAge.md": ("bug_age", "bugs_incidents", "days", "lower", ["jira_synth"]),
    "BugCountPer1kLinesOfCode.md": (
        "bug_count_per_1k_loc",
        "bugs_incidents",
        "bugs/kLOC",
        "lower",
        ["codebase", "delivery_model.rework_share"],
    ),
    "IncidentAge.md": ("incident_age", "bugs_incidents", "days", "lower", ["company_model"]),
    "IncidentCountPer1kLinesOfCode.md": (
        "incident_count_per_1k_loc",
        "bugs_incidents",
        "incidents/kLOC",
        "lower",
        ["company_model"],
    ),
    "CommitCount.md": ("commit_count", "code", "commits", "neutral", ["codebase.git_churn"]),
    "CommitAuthorCount.md": ("commit_author_count", "code", "authors", "neutral", ["codebase"]),
    "AddedLinesOfCode.md": ("added_loc", "code", "lines", "neutral", ["codebase.git_churn"]),
    "DeletedLinesOfCode.md": ("deleted_loc", "code", "lines", "neutral", ["codebase.git_churn"]),
    "PRCount.md": ("pr_count", "pull_requests", "PRs", "neutral", []),
    "PRCycleTime.md": ("pr_cycle_time", "pull_requests", "hours", "lower", ["delivery_model.productivity"]),
    "PRCodingTime.md": ("pr_coding_time", "pull_requests", "hours", "lower", []),
    "PRPickupTime.md": ("pr_pickup_time", "pull_requests", "hours", "lower", []),
    "PRReviewTime.md": ("pr_review_time", "pull_requests", "hours", "lower", []),
    "PRDeployTime.md": ("pr_deploy_time", "pull_requests", "hours", "lower", []),
    "PRTimeToMerge.md": ("pr_time_to_merge", "pull_requests", "hours", "lower", []),
    "PRMergeRate.md": ("pr_merge_rate", "pull_requests", "share", "higher", []),
    "PRReviewDepth.md": ("pr_review_depth", "pull_requests", "comments/PR", "higher", []),
    "PRSize.md": ("pr_size", "pull_requests", "lines/PR", "lower", []),
    "BuildCount.md": ("build_count", "builds", "builds", "neutral", []),
    "BuildDuration.md": ("build_duration", "builds", "minutes", "lower", []),
    "BuildSuccessRate.md": ("build_success_rate", "builds", "share", "higher", []),
    "DeploymentFrequency.md": (
        "dora_deployment_frequency",
        "dora",
        "deployments/period",
        "higher",
        ["delivery_model"],
    ),
    "LeadTimeForChanges.md": ("dora_lead_time_for_changes", "dora", "hours", "lower", ["delivery_model"]),
    "FailedDeploymentRecoveryTime.md": (
        "dora_failed_deployment_recovery_time",
        "dora",
        "hours",
        "lower",
        ["company_model"],
    ),
    "MTTR.md": ("dora_time_to_restore_service", "dora", "hours", "lower", ["company_model"]),
    "CFR.md": (
        "dora_change_failure_rate",
        "dora",
        "share",
        "lower",
        ["delivery_model.rework_share", "company_model"],
    ),
    "CQIssueCount.md": ("cq_issue_count", "code_quality", "issues", "lower", ["containers.findings"]),
    "CQTest.md": ("cq_test", "code_quality", "share", "higher", []),
    "CQMaintainability-Debt.md": (
        "cq_maintainability_debt",
        "code_quality",
        "days",
        "lower",
        ["codebase.file_complexity"],
    ),
    "CQDuplicatedBlocks.md": ("cq_duplicated_blocks", "code_quality", "blocks", "lower", []),
    "CQDuplicatedLines.md": ("cq_duplicated_lines", "code_quality", "lines", "lower", []),
}

# DORA guide: the one DORA metric that DevLake does not have yet.
DORA = [
    {
        "id": "dora_deployment_rework_rate",
        "name": "DORA - Deployment Rework Rate",
        "group": "dora",
        "unit": "share",
        "direction": "lower",
        "used_by": ["delivery_model.rework_share"],
        "definition": "The ratio of deployments that are unplanned but happen as a result of an incident"
        " in production.",
    },
]

# Scorecard checks: (check, risk, question). Score 0-10 for each check.
SCORECARD = [
    ("Binary-Artifacts", "high", "Is the project free of checked-in binaries?"),
    ("Branch-Protection", "high", "Does the project use branch protection?"),
    ("CI-Tests", "low", "Does the project run tests in CI?"),
    ("CII-Best-Practices", "low", "Has the project earned an OpenSSF Best Practices Badge?"),
    ("Code-Review", "high", "Does the project practice code review before merging?"),
    ("Contributors", "low", "Does the project have contributors from at least two organizations?"),
    (
        "Dangerous-Workflow",
        "critical",
        "Does the project avoid dangerous patterns in GitHub Action workflows?",
    ),
    ("Dependency-Update-Tool", "high", "Does the project use tools to update its dependencies?"),
    ("Fuzzing", "medium", "Does the project use fuzzing tools?"),
    ("License", "low", "Does the project declare a license?"),
    ("Maintained", "high", "Is the project at least 90 days old and maintained?"),
    ("Pinned-Dependencies", "medium", "Does the project declare and pin its dependencies?"),
    ("Packaging", "medium", "Does the project build and publish packages from CI/CD?"),
    ("SAST", "medium", "Does the project use static code analysis tools?"),
    ("Security-Policy", "medium", "Does the project contain a security policy?"),
    ("Signed-Releases", "high", "Does the project cryptographically sign releases?"),
    ("Token-Permissions", "high", "Does the project declare GitHub workflow tokens as read-only?"),
    ("Vulnerabilities", "high", "Does the project have unfixed vulnerabilities?"),
    (
        "Webhooks",
        "critical",
        "Does the webhook of the repository have a token to authenticate request origins?",
    ),
]

# CHAOSS: (id, slug, name, group, unit, direction, used_by, summary)
CHAOSS = [
    (
        "chaoss_contributor_absence_factor",
        "contributor-absence-factor",
        "Contributor Absence Factor (bus factor)",
        "people",
        "contributors",
        "higher",
        ["company_model", "delivery_model.ev_kp"],
        "The smallest number of contributors that make 50% of the contributions.",
    ),
    (
        "chaoss_elephant_factor",
        "elephant-factor",
        "Elephant Factor",
        "people",
        "organizations",
        "higher",
        ["company_model"],
        "The smallest number of organizations that make 50% of the contributions.",
    ),
    (
        "chaoss_contributors",
        "contributors",
        "Contributors",
        "people",
        "people",
        "neutral",
        [],
        "The number of people who contribute to the project.",
    ),
    (
        "chaoss_new_contributors",
        "new-contributors",
        "New Contributors",
        "people",
        "people",
        "neutral",
        ["company_model"],
        "The number of contributors who make a first contribution in the period.",
    ),
    (
        "chaoss_inactive_contributors",
        "inactive-contributors",
        "Inactive Contributors",
        "people",
        "people",
        "lower",
        ["company_model"],
        "The number of contributors who stop their activity in the period.",
    ),
    (
        "chaoss_time_to_first_response",
        "time-to-first-response",
        "Time to First Response",
        "flow",
        "hours",
        "lower",
        [],
        "The time from the creation of an issue or change request to the first response.",
    ),
    (
        "chaoss_change_request_closure_ratio",
        "change-request-closure-ratio",
        "Change Request Closure Ratio",
        "flow",
        "share",
        "higher",
        [],
        "The ratio of closed change requests to opened change requests in the period.",
    ),
    (
        "chaoss_change_requests_duration",
        "change-requests-duration",
        "Change Requests Duration",
        "flow",
        "days",
        "lower",
        [],
        "The time from the start of a change request to its acceptance or closure.",
    ),
    (
        "chaoss_change_request_review_duration",
        "change-request-review-duration",
        "Change Request Review Duration",
        "flow",
        "days",
        "lower",
        [],
        "The time from the start of the review of a change request to its acceptance.",
    ),
    (
        "chaoss_issue_resolution_duration",
        "issue-resolution-duration",
        "Issue Resolution Duration",
        "flow",
        "days",
        "lower",
        ["jira_synth"],
        "The time from the creation of an issue to its closure.",
    ),
    (
        "chaoss_issue_age",
        "issue-age",
        "Issue Age",
        "flow",
        "days",
        "lower",
        ["jira_synth"],
        "The time for which open issues stay open.",
    ),
    (
        "chaoss_release_frequency",
        "release-frequency",
        "Release Frequency",
        "flow",
        "releases/period",
        "higher",
        [],
        "The number of new versions that the project releases in the period.",
    ),
    (
        "chaoss_libyears",
        "libyears",
        "Libyears",
        "dependencies",
        "years",
        "lower",
        ["containers.findings"],
        "The sum of the age gaps between the used and the latest version of each dependency.",
    ),
    (
        "chaoss_upstream_code_dependencies",
        "upstream-code-dependencies",
        "Upstream Code Dependencies",
        "dependencies",
        "dependencies",
        "neutral",
        [],
        "The other projects that the project depends on.",
    ),
    (
        "chaoss_test_coverage",
        "test-coverage",
        "Test Coverage",
        "code_quality",
        "share",
        "higher",
        [],
        "The share of the code that the tests execute.",
    ),
]

# Code metrics from `variator_it_services.codebase`: (id, name, unit, direction, field, doc, summary)
CLASSIC = [
    (
        "code_nloc",
        "NLOC (lines of code without comments)",
        "lines",
        "neutral",
        "nloc",
        "https://github.com/terryyin/lizard#readme",
        "Lines of code without comments and blank lines (lizard).",
    ),
    (
        "code_cyclomatic_complexity",
        "Cyclomatic complexity (CCN)",
        "paths",
        "lower",
        "ccn_avg, ccn_max",
        "https://en.wikipedia.org/wiki/Cyclomatic_complexity",
        "The number of linearly independent paths through a function (McCabe, 1976); lizard counts it.",
    ),
    (
        "code_churn",
        "Code churn",
        "commits, lines",
        "lower",
        "commits, changed_lines",
        "https://git-scm.com/docs/git-log#Documentation/git-log.txt---numstat",
        "Commits and added plus deleted lines per file in the period (git log --numstat).",
    ),
    (
        "code_fan_in",
        "Afferent coupling (Ca, fan-in)",
        "packages",
        "neutral",
        "fan_in",
        "https://en.wikipedia.org/wiki/Software_package_metrics",
        "The number of other packages that depend on this package (R. C. Martin).",
    ),
    (
        "code_fan_out",
        "Efferent coupling (Ce, fan-out)",
        "packages",
        "lower",
        "fan_out",
        "https://en.wikipedia.org/wiki/Software_package_metrics",
        "The number of other packages that this package depends on (R. C. Martin).",
    ),
    (
        "code_instability",
        "Instability I = Ce / (Ca + Ce)",
        "0-1",
        "neutral",
        "instability",
        "https://en.wikipedia.org/wiki/Software_package_metrics",
        "0 = stable (others depend on it), 1 = unstable (it depends on others) (R. C. Martin).",
    ),
    (
        "code_dependency_cycles",
        "Dependency cycles",
        "cycles",
        "lower",
        "cycles",
        "https://en.wikipedia.org/wiki/Acyclic_dependencies_principle",
        "Groups of packages that import each other; they break the acyclic dependencies principle.",
    ),
    (
        "code_hot_spot",
        "Hot spot = churn x complexity",
        "score",
        "lower",
        "hot_spots",
        "https://pragprog.com/titles/atcrime2/your-code-as-a-crime-scene-second-edition/",
        "Files that change often and are complex; the first candidates to refactor (A. Tornhill).",
    ),
]

SOURCES = {
    "devlake": {
        "name": "Apache DevLake",
        "license": "Apache-2.0",
        "home": DEVLAKE_SITE,
        "kind": "verbatim excerpts",
    },
    "dora": {
        "name": "DORA (Google Cloud)",
        "license": "public definitions",
        "home": DORA_GUIDE,
        "kind": "verbatim definition",
    },
    "scorecard": {
        "name": "OpenSSF Scorecard",
        "license": "Apache-2.0",
        "home": "https://github.com/ossf/scorecard",
        "kind": "verbatim check questions",
        "commit": SCORECARD_COMMIT,
    },
    "chaoss": {
        "name": "CHAOSS",
        "license": "MIT (metric definitions)",
        "home": "https://chaoss.community/kbtopic/all-metrics/",
        "kind": "short paraphrase",
    },
    "classic": {
        "name": "Classic code metrics",
        "license": "public definitions",
        "home": "https://en.wikipedia.org/wiki/Software_metric",
        "kind": "short paraphrase",
    },
}


def _clean(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # links -> their text
    text = re.sub(r"<[^>]+>", "", text)  # html tags
    return re.sub(r"\s+", " ", text).strip()


def _sections(md: str) -> dict[str, str]:
    parts = re.split(r"^## +(.+?)\s*$", md, flags=re.M)
    return {parts[i].strip().rstrip("?").lower(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _items(text: str) -> list[str]:
    return [_clean(m) for m in re.findall(r"^\s*(?:\d+\.|-)\s+(.+)$", text, flags=re.M)]


def _benchmarks(calc: str) -> list[dict[str, str]]:
    """First benchmark table (newest DORA report); the older report sits in a <details> block."""
    calc = calc.split("<details>")[0]
    rows = []
    for line in calc.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].lower().endswith("performers"):
            row = {"group": cells[0], "dora": cells[1], "devlake": _clean(cells[2])}
            if len(cells) > 3:
                row["criteria"] = _clean(cells[3].replace("`", ""))
            rows.append(row)
    return rows


def devlake_entry(path: Path, commit: str) -> dict:
    md = path.read_text(encoding="utf-8")
    title = re.search(r'^title:\s*"?(.+?)"?\s*$', md, flags=re.M).group(1)
    sec = _sections(md)
    calc = sec.get("how is it calculated", "")
    head, _, rest = calc.partition("Data Sources Required")
    sources_text = re.split(r"<b>|\*\*", rest, maxsplit=1)[0] if rest else ""
    sql = re.search(r"```(?:sql)?\n(.*?)```", calc, flags=re.S)
    key, group, unit, direction, used_by = DEVLAKE[path.name]
    entry = {
        "id": key,
        "name": title,
        "source": "devlake",
        "group": group,
        "unit": unit,
        "direction": direction,
        "used_by": used_by,
        "definition": _clean(sec.get("what is this metric", "")),
        "why": _clean(sec.get("why is it important", "")),
        "calculation": _clean(re.split(r"\|", head.split("<details>")[0])[0]),
        "data_sources": _items(sources_text) or [_clean(sources_text)],
        "benchmarks": _benchmarks(calc),
        "improve": _items(sec.get("how to improve", "")),
        "sql_example": sql.group(1).strip() if sql else "",
        "doc_url": DEVLAKE_SITE + path.stem,
        "source_url": DEVLAKE_REPO.format(commit=commit, file=path.name),
    }
    if group == "dora":
        entry["see_also"] = [DORA_GUIDE]
    return entry


def build(devlake_dir: Path, commit: str, date: str) -> dict:
    files = sorted(p for p in devlake_dir.glob("*.md"))
    unknown = {p.name for p in files} - DEVLAKE.keys()
    if unknown:
        raise SystemExit(f"New DevLake metric files, add them to DEVLAKE: {sorted(unknown)}")
    metrics = [devlake_entry(p, commit) for p in files]
    metrics += [{**d, "source": "dora", "doc_url": DORA_GUIDE, "source_url": DORA_GUIDE} for d in DORA]
    checks = "https://github.com/ossf/scorecard/blob/{c}/docs/checks.md#{a}"
    metrics += [
        {
            "id": "scorecard_" + name.lower().replace("-", "_"),
            "name": f"Scorecard - {name}",
            "source": "scorecard",
            "group": "security",
            "unit": "score 0-10",
            "direction": "higher",
            "risk": risk,
            "used_by": ["containers.findings"],
            "definition": question,
            "doc_url": checks.format(c="main", a=name.lower()),
            "source_url": checks.format(c=SCORECARD_COMMIT, a=name.lower()),
        }
        for name, risk, question in SCORECARD
    ]
    metrics += [
        {
            "id": key,
            "name": name,
            "source": "chaoss",
            "group": group,
            "unit": unit,
            "direction": direction,
            "used_by": used_by,
            "definition": summary,
            "doc_url": f"https://www.chaoss.community/kb/metric-{slug}/",
            "source_url": f"https://www.chaoss.community/kb/metric-{slug}/",
        }
        for key, slug, name, group, unit, direction, used_by, summary in CHAOSS
    ]
    metrics += [
        {
            "id": key,
            "name": name,
            "source": "classic",
            "group": "code",
            "unit": unit,
            "direction": direction,
            "used_by": ["codebase"],
            "fields": [f.strip() for f in field.split(",")],
            "definition": summary,
            "doc_url": doc,
            "source_url": doc,
        }
        for key, name, unit, direction, field, doc, summary in CLASSIC
    ]
    sources = {**SOURCES, "devlake": {**SOURCES["devlake"], "commit": commit, "date": date}}
    return {"version": date, "sources": sources, "metrics": metrics}


def main() -> None:
    root, commit, date = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    catalog = build(root / "docs" / "Metrics", commit, date)
    OUT.write_text(json.dumps(catalog, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(catalog['metrics'])} metrics -> {OUT}")


if __name__ == "__main__":
    main()
