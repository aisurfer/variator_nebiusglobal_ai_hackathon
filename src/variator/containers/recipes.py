"""Tool recipes: a pinned image, a command and a local parser to compact metrics.

The parsers read the tool's JSON report and keep only counts and the top items, so
an LLM gets a few hundred characters instead of the full report. Each recipe also
turns its report into findings (``findings`` below) for one place of record
(``variator.containers.findings``, DefectDojo).

Images without an official build come from ``images/<name>.Dockerfile``; ``run_recipe``
builds them on first use. ``GROUPS`` names the default sets; Dependency-Check is in no
group (optional: Java-heavy clients or an OWASP report on request).

# ASSUMPTION: image tags and report formats are from the tools' documentation and the
# DefectDojo parsers (checked 2026-10-26); they are not verified live (no Docker yet).
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from variator.containers.runner import OUT_DIR, ContainerResult, ContainerSpec, Mount, Runner

SRC = "/src"
CACHE = "/cache"
TOP = 10
IMAGES = Path(__file__).parent / "images"
ENV_SCAN_CACHE = "VARIATOR_SCAN_CACHE"  # host folder for scanner databases; default .cache/scanners
GENERIC = "Generic Findings Import"  # DefectDojo scan type for our normalized findings.json
SEVERITIES = ("Critical", "High", "Medium", "Low", "Info")
# git refuses a repository owned by another user; the scanners run as another user
TRUST_REPO = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}

Finding = dict[str, Any]  # DefectDojo generic fields + "category"


@dataclass(frozen=True)
class Recipe:
    name: str
    description: str
    image: str
    command: tuple[str, ...]
    output: str  # file name in /out
    parse: Callable[[Any], dict[str, Any]]
    network: str = "none"
    env: dict[str, str] = field(default_factory=dict)
    findings: Callable[[Any], list[Finding]] | None = None
    scan_type: str = GENERIC  # DefectDojo parser for the raw report (GENERIC: use findings.json)
    import_file: str | None = None  # raw report for DefectDojo when it differs from ``output``
    dockerfile: str | None = None  # images/<file> when there is no official image
    cache: bool = False  # mount a persistent host folder at /cache (vulnerability databases)

    def spec(
        self, source: str | Path, *, timeout_s: float = 900, cache_root: Path | None = None
    ) -> ContainerSpec:
        mounts = [Mount(str(source), SRC, read_only=True)]
        if self.cache:
            host = (cache_root or Path(os.environ.get(ENV_SCAN_CACHE, ".cache/scanners"))) / self.name
            host.mkdir(parents=True, exist_ok=True)
            mounts.append(Mount(str(host), CACHE, read_only=False))
        return ContainerSpec(
            image=self.image,
            command=self.command,
            env=self.env,
            mounts=tuple(mounts),
            workdir=SRC,
            network=self.network,  # type: ignore[arg-type]
            timeout_s=timeout_s,
        )


def severity(text: Any) -> str:
    """Tool severity words or a CVSS base score -> a DefectDojo severity."""
    try:
        score = float(text)
    except (TypeError, ValueError):
        word = str(text or "").upper()
        return {
            "CRITICAL": "Critical",
            "HIGH": "High",
            "MEDIUM": "Medium",
            "MODERATE": "Medium",
            "LOW": "Low",
        }.get(word, "Info")
    return (
        "Critical"
        if score >= 9
        else "High"
        if score >= 7
        else "Medium"
        if score >= 4
        else "Low"
        if score > 0
        else "Info"
    )


def _finding(category: str, title: str, sev: str, description: str = "", **extra: Any) -> Finding:
    out = {"category": category, "title": title[:250], "severity": sev, "description": description or title}
    out.update({k: v for k, v in extra.items() if v not in (None, "")})
    return out


# --- size ----------------------------------------------------------------------------


def _scc(report: Any) -> dict[str, Any]:
    languages = sorted(report, key=lambda r: r.get("Code", 0), reverse=True)
    return {
        "files": sum(r.get("Count", 0) for r in languages),
        "code_lines": sum(r.get("Code", 0) for r in languages),
        "complexity": sum(r.get("Complexity", 0) for r in languages),
        "languages": {
            r["Name"]: {"files": r.get("Count", 0), "code": r.get("Code", 0)} for r in languages[:TOP]
        },
    }


# --- dependencies (SCA) --------------------------------------------------------------------


def _trivy(report: Any) -> dict[str, Any]:
    severities: Counter[str] = Counter()
    secrets = misconfig = 0
    top: list[dict[str, str]] = []
    for result in report.get("Results") or []:
        for vuln in result.get("Vulnerabilities") or []:
            severities[vuln.get("Severity", "UNKNOWN")] += 1
            if vuln.get("Severity") in ("CRITICAL", "HIGH") and len(top) < TOP:
                top.append(
                    {
                        "id": vuln.get("VulnerabilityID", ""),
                        "package": vuln.get("PkgName", ""),
                        "fixed": vuln.get("FixedVersion", ""),
                        "severity": vuln["Severity"],
                    }
                )
        secrets += len(result.get("Secrets") or [])
        misconfig += len(result.get("Misconfigurations") or [])
    return {
        "vulnerabilities": dict(severities),
        "secrets": secrets,
        "misconfigurations": misconfig,
        "top": top,
    }


def _trivy_findings(report: Any) -> list[Finding]:
    out = []
    for result in report.get("Results") or []:
        target = result.get("Target", "")
        for v in result.get("Vulnerabilities") or []:
            vid, pkg = v.get("VulnerabilityID", ""), v.get("PkgName", "")
            out.append(
                _finding(
                    "vulnerability",
                    f"{vid} in {pkg}",
                    severity(v.get("Severity")),
                    v.get("Title", ""),
                    file_path=target,
                    component_name=pkg,
                    component_version=v.get("InstalledVersion"),
                    vuln_id_from_tool=vid,
                    fix_version=v.get("FixedVersion"),
                )
            )
        for s in result.get("Secrets") or []:
            out.append(
                _finding(
                    "secret",
                    f"Secret: {s.get('RuleID', '')}",
                    "High",
                    file_path=target,
                    line=s.get("StartLine"),
                )
            )
        for m in result.get("Misconfigurations") or []:
            out.append(
                _finding(
                    "misconfiguration",
                    f"{m.get('ID', '')}: {m.get('Title', '')}",
                    severity(m.get("Severity")),
                    m.get("Description", ""),
                    file_path=target,
                )
            )
    return out


def _osv_groups(report: Any) -> Iterable[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """(package, group, first vulnerability) per alias group, so one CVE with a GHSA alias counts once."""
    for result in report.get("results") or []:
        for item in result.get("packages") or []:
            vulns = {v.get("id"): v for v in item.get("vulnerabilities") or []}
            groups = item.get("groups") or [{"ids": [i]} for i in vulns]
            for group in groups:
                ids = group.get("ids") or []
                yield item.get("package") or {}, group, vulns.get(ids[0] if ids else None) or {}


def _osv(report: Any) -> dict[str, Any]:
    severities: Counter[str] = Counter()
    ecosystems: Counter[str] = Counter()
    packages: set[tuple[str, str]] = set()
    top: list[dict[str, str]] = []
    for package, group, _ in _osv_groups(report):
        sev = severity(group.get("max_severity"))
        severities[sev] += 1
        ecosystems[package.get("ecosystem", "")] += 1
        packages.add((package.get("name", ""), package.get("version", "")))
        if sev in ("Critical", "High") and len(top) < TOP:
            top.append(
                {"id": (group.get("ids") or [""])[0], "package": package.get("name", ""), "severity": sev}
            )
    return {
        "vulnerabilities": dict(severities),
        "packages_affected": len(packages),
        "ecosystems": dict(ecosystems),
        "top": top,
    }


def _osv_findings(report: Any) -> list[Finding]:
    out = []
    for package, group, vuln in _osv_groups(report):
        ids = group.get("ids") or []
        name = package.get("name", "")
        out.append(
            _finding(
                "vulnerability",
                f"{ids[0] if ids else '?'} in {name}",
                severity(group.get("max_severity")),
                vuln.get("summary", ""),
                component_name=name,
                component_version=package.get("version"),
                vuln_id_from_tool=ids[0] if ids else None,
            )
        )
    return out


def _depcheck_vulns(report: Any) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    for dep in report.get("dependencies") or []:
        for vuln in dep.get("vulnerabilities") or []:
            yield dep, vuln


def _depcheck_severity(vuln: dict[str, Any]) -> str:
    cvss3 = vuln.get("cvssv3") or {}
    return severity(cvss3.get("baseSeverity") or vuln.get("severity"))


def _dependency_check(report: Any) -> dict[str, Any]:
    severities: Counter[str] = Counter()
    deps: set[str] = set()
    top: list[dict[str, str]] = []
    for dep, vuln in _depcheck_vulns(report):
        sev = _depcheck_severity(vuln)
        severities[sev] += 1
        deps.add(dep.get("fileName", ""))
        if sev in ("Critical", "High") and len(top) < TOP:
            top.append({"id": vuln.get("name", ""), "dependency": dep.get("fileName", ""), "severity": sev})
    return {
        "vulnerabilities": dict(severities),
        "dependencies_scanned": len(report.get("dependencies") or []),
        "dependencies_affected": len(deps),
        "top": top,
    }


def _dependency_check_findings(report: Any) -> list[Finding]:
    return [
        _finding(
            "vulnerability",
            f"{vuln.get('name', '')} in {dep.get('fileName', '')}",
            _depcheck_severity(vuln),
            vuln.get("description", ""),
            file_path=dep.get("filePath"),
            vuln_id_from_tool=vuln.get("name"),
        )
        for dep, vuln in _depcheck_vulns(report)
    ]


# --- secrets ---------------------------------------------------------------------------------


def _gitleaks(report: Any) -> dict[str, Any]:
    rules = Counter(item.get("RuleID", "unknown") for item in report or [])
    files = Counter(item.get("File", "") for item in report or [])
    commits = {item.get("Commit") for item in report or [] if item.get("Commit")}
    return {
        "leaks": sum(rules.values()),
        "rules": dict(rules.most_common(TOP)),
        "files": [f for f, _ in files.most_common(TOP)],
        **({"commits": len(commits)} if commits else {}),
    }


def _gitleaks_findings(report: Any) -> list[Finding]:
    return [
        _finding(
            "secret",
            f"Secret: {item.get('RuleID', '')}",
            "High",
            item.get("Description", ""),
            file_path=item.get("File"),
            line=item.get("StartLine"),
            unique_id_from_tool=item.get("Fingerprint"),
        )
        for item in report or []
    ]


def _detect_secrets_items(report: Any) -> Iterable[tuple[str, dict[str, Any]]]:
    for path, items in (report.get("results") or {}).items():
        for item in items:
            yield path, item


def _detect_secrets(report: Any) -> dict[str, Any]:
    types: Counter[str] = Counter()
    files: Counter[str] = Counter()
    for path, item in _detect_secrets_items(report):
        types[item.get("type", "unknown")] += 1
        files[path] += 1
    return {
        "secrets": sum(types.values()),
        "types": dict(types.most_common(TOP)),
        "files": [f for f, _ in files.most_common(TOP)],
    }


def _detect_secrets_findings(report: Any) -> list[Finding]:
    return [
        _finding(
            "secret",
            f"Secret: {item.get('type', '')}",
            "High",
            file_path=item.get("filename") or path,
            line=item.get("line_number"),
            unique_id_from_tool=item.get("hashed_secret"),
        )
        for path, item in _detect_secrets_items(report)
    ]


# --- duplication -------------------------------------------------------------------------------


def _place(file: dict[str, Any] | None) -> str:
    return f"{(file or {}).get('name', '')}:{(file or {}).get('start', '')}"


def _jscpd(report: Any) -> dict[str, Any]:
    stats = report.get("statistics") or {}
    total = stats.get("total") or {}
    formats = stats.get("formats") or {}
    clones = sorted(report.get("duplicates") or [], key=lambda d: d.get("lines", 0), reverse=True)
    return {
        "duplicated_percent": round(float(total.get("percentage", 0.0)), 2),
        "duplicated_lines": total.get("duplicatedLines", 0),
        "lines": total.get("lines", 0),
        "clones": total.get("clones", len(clones)),
        "files": total.get("sources", 0),
        "formats": {
            name: round(float((f.get("total") or {}).get("percentage", 0.0)), 2)
            for name, f in sorted(
                formats.items(), key=lambda kv: -((kv[1].get("total") or {}).get("lines", 0))
            )[:TOP]
        },
        "top": [
            {"lines": d.get("lines", 0), "a": _place(d.get("firstFile")), "b": _place(d.get("secondFile"))}
            for d in clones[:TOP]
        ],
    }


def _jscpd_findings(report: Any) -> list[Finding]:
    out = []
    for d in report.get("duplicates") or []:
        a, b, lines = d.get("firstFile") or {}, d.get("secondFile") or {}, int(d.get("lines", 0))
        out.append(
            _finding(
                "duplication",
                f"Duplicated block: {lines} lines",
                "Medium" if lines >= 100 else "Low",
                f"Same code in {a.get('name')}:{a.get('start')}-{a.get('end')} "
                f"and {b.get('name')}:{b.get('start')}-{b.get('end')} ({d.get('format', '')}).",
                file_path=a.get("name"),
                line=a.get("start"),
            )
        )
    return out


# --- architecture --------------------------------------------------------------------------------


def _internal_edges(report: Any) -> Iterable[tuple[str, dict[str, Any]]]:
    for module in report.get("modules") or []:
        for dep in module.get("dependencies") or []:
            if (
                not dep.get("coreModule")
                and not dep.get("couldNotResolve")
                and "node_modules" not in dep.get("resolved", "")
            ):
                yield module.get("source", ""), dep


def _depcruise(report: Any) -> dict[str, Any]:
    modules = [m for m in report.get("modules") or [] if "node_modules" not in m.get("source", "")]
    fan_out: Counter[str] = Counter()
    fan_in: Counter[str] = Counter()
    in_cycles: set[str] = set()
    circular = 0
    for source, dep in _internal_edges(report):
        fan_out[source] += 1
        fan_in[dep.get("resolved", "")] += 1
        if dep.get("circular"):
            circular += 1
            in_cycles |= {source, dep.get("resolved", "")}
    unresolved = sum(
        1
        for m in modules
        for d in m.get("dependencies") or []
        if d.get("couldNotResolve") and not d.get("coreModule")
    )
    folders: defaultdict[str, int] = defaultdict(int)
    for m in in_cycles:
        folders[m.rsplit("/", 1)[0] if "/" in m else "."] += 1
    return {
        "modules": len(modules),
        "dependencies": sum(fan_out.values()),
        "circular_dependencies": circular,
        "modules_in_cycles": len(in_cycles),
        "cycle_folders": dict(sorted(folders.items(), key=lambda kv: -kv[1])[:TOP]),
        "unresolved": unresolved,
        "top_fan_in": dict(fan_in.most_common(5)),
        "top_fan_out": dict(fan_out.most_common(5)),
    }


def _depcruise_findings(report: Any) -> list[Finding]:
    return [
        _finding(
            "architecture",
            f"Import cycle: {source} -> {dep.get('resolved', '')}",
            "Medium",
            "This dependency is part of a circular import chain.",
            file_path=source,
        )
        for source, dep in _internal_edges(report)
        if dep.get("circular")
    ]


RECIPES: dict[str, Recipe] = {
    r.name: r
    for r in (
        Recipe(
            name="scc",
            description="Size and complexity per language",
            image="ghcr.io/boyter/scc:v3.5.0",
            command=("scc", "--format", "json", "--output", f"{OUT_DIR}/scc.json", SRC),
            output="scc.json",
            parse=_scc,
        ),
        Recipe(
            name="trivy",
            description="Dependency vulnerabilities, secrets and misconfigurations",
            image="aquasec/trivy:0.67.2",
            command=(
                "fs",
                "--scanners",
                "vuln,secret,misconfig",
                "--format",
                "json",
                "--output",
                f"{OUT_DIR}/trivy.json",
                SRC,
            ),
            output="trivy.json",
            parse=_trivy,
            findings=_trivy_findings,
            scan_type="Trivy Scan",
            network="bridge",  # downloads the vulnerability database
            env={"TRIVY_CACHE_DIR": CACHE},
            cache=True,
        ),
        Recipe(
            name="osv_scanner",
            description="Dependency vulnerabilities from lockfiles (OSV database, exact versions)",
            image="ghcr.io/google/osv-scanner:v2.7.0",
            command=("scan", "source", "-r", "--format", "json", "--output", f"{OUT_DIR}/osv.json", SRC),
            output="osv.json",
            parse=_osv,
            findings=_osv_findings,
            scan_type="OSV Scan",
            network="bridge",  # sends package names and versions to api.osv.dev, never the code
        ),
        Recipe(
            name="dependency_check",
            description="OWASP Dependency-Check (optional): CPE matching, NVD; set NVD_API_KEY",
            image="owasp/dependency-check:13.0.0",
            command=(
                "--scan",
                SRC,
                "--project",
                "variator-scan",
                "--format",
                "JSON",
                "--format",
                "XML",  # DefectDojo reads only the XML report
                "--out",
                OUT_DIR,
                "--data",
                CACHE,
                "--disableOssIndex",
            ),
            output="dependency-check-report.json",
            import_file="dependency-check-report.xml",
            parse=_dependency_check,
            findings=_dependency_check_findings,
            scan_type="Dependency Check Scan",
            network="bridge",  # updates the NVD data in /cache
            cache=True,
        ),
        Recipe(
            name="gitleaks",
            description="Secrets in the working tree",
            image="ghcr.io/gitleaks/gitleaks:v8.28.0",
            command=(
                "dir",
                SRC,
                "--report-format",
                "json",
                "--report-path",
                f"{OUT_DIR}/gitleaks.json",
                "--exit-code",
                "0",
            ),
            output="gitleaks.json",
            parse=_gitleaks,
            findings=_gitleaks_findings,
            scan_type="Gitleaks Scan",
        ),
        Recipe(
            name="gitleaks_history",
            description="Secrets in the git history (all commits), also removed ones",
            image="ghcr.io/gitleaks/gitleaks:v8.28.0",
            command=(
                "git",
                SRC,
                "--report-format",
                "json",
                "--report-path",
                f"{OUT_DIR}/gitleaks-history.json",
                "--exit-code",
                "0",
            ),
            output="gitleaks-history.json",
            parse=_gitleaks,
            findings=_gitleaks_findings,
            scan_type="Gitleaks Scan",
            env=TRUST_REPO,
        ),
        Recipe(
            name="detect_secrets",
            description="Secret patterns and high-entropy strings (Yelp detect-secrets)",
            image="variator/detect-secrets:1.5.0",
            dockerfile="detect-secrets.Dockerfile",
            command=(
                "detect-secrets scan --all-files --exclude-files '(^|/)(\\.git|node_modules)/' . "
                f"> {OUT_DIR}/detect-secrets.json",
            ),
            output="detect-secrets.json",
            parse=_detect_secrets,
            findings=_detect_secrets_findings,
            scan_type="Detect-secrets Scan",
        ),
        Recipe(
            name="jscpd",
            description="Duplicated code blocks (copy/paste), 150+ languages",
            image="variator/jscpd:4.0.5",
            dockerfile="jscpd.Dockerfile",
            command=(
                SRC,
                "--reporters",
                "json",
                "--output",
                OUT_DIR,
                "--ignore",
                "**/node_modules/**,**/.git/**,**/dist/**,**/build/**,**/*.min.js",
                "--silent",
            ),
            output="jscpd-report.json",
            parse=_jscpd,
            findings=_jscpd_findings,
        ),
        Recipe(
            name="depcruise",
            description="JavaScript/TypeScript module graph: cycles, fan-in, fan-out",
            image="variator/depcruise:18.7.0",
            dockerfile="depcruise.Dockerfile",
            command=(
                "--no-config",
                "--do-not-follow",
                "node_modules",
                "--exclude",
                "(^|/)(node_modules|dist|build|coverage)/",
                "--output-type",
                "json",
                "--output-to",
                f"{OUT_DIR}/depcruise.json",
                ".",
            ),
            output="depcruise.json",
            parse=_depcruise,
            findings=_depcruise_findings,
        ),
    )
}

GROUPS: dict[str, tuple[str, ...]] = {
    "sca": ("trivy", "osv_scanner"),
    "secrets": ("gitleaks", "gitleaks_history", "detect_secrets"),
    "duplication": ("jscpd",),
    "architecture": ("depcruise",),
}
GROUPS["default"] = (
    "scc",
    *GROUPS["sca"],
    *GROUPS["secrets"],
    *GROUPS["duplication"],
    *GROUPS["architecture"],
)


def expand(names: Iterable[str]) -> list[str]:
    """Recipe and group names -> recipe names, in order, without repeats."""
    out: list[str] = []
    for name in names:
        for item in GROUPS.get(name, (name,)):
            if item not in RECIPES:
                raise ValueError(
                    f"unknown recipe or group {item!r}; known: {sorted(RECIPES)} and {sorted(GROUPS)}"
                )
            if item not in out:
                out.append(item)
    return out


@dataclass
class ScanOutcome:
    recipe: str
    ok: bool
    metrics: dict[str, Any]
    elapsed_s: float
    error: str | None = None
    findings: list[Finding] = field(default_factory=list)
    out_dir: Path | None = None


def ensure_image(runner: Runner, recipe: Recipe) -> None:
    """Build a recipe image from images/ when the runner can build and the image is missing."""
    if recipe.dockerfile is None or not hasattr(runner, "image_exists") or not hasattr(runner, "build"):
        return
    if not runner.image_exists(recipe.image):
        runner.build(IMAGES, recipe.image, dockerfile=str(IMAGES / recipe.dockerfile))


def run_recipe(
    runner: Runner, recipe: Recipe, source: str | Path, *, out_dir: Path | None = None
) -> ScanOutcome:
    env_key = os.environ.get("NVD_API_KEY")
    if recipe.name == "dependency_check" and env_key:
        recipe = Recipe(**{**recipe.__dict__, "command": (*recipe.command, "--nvdApiKey", env_key)})
    ensure_image(runner, recipe)
    result: ContainerResult = runner.run(recipe.spec(source), out_dir=out_dir)
    if result.out_dir is None or recipe.output not in result.files:
        detail = result.stderr.strip()[-500:] or f"exit code {result.exit_code}"
        return ScanOutcome(recipe.name, False, {}, result.elapsed_s, f"no {recipe.output}: {detail}")
    report = result.read_json(recipe.output)
    found = recipe.findings(report) if recipe.findings else []
    return ScanOutcome(
        recipe.name, True, recipe.parse(report), result.elapsed_s, findings=found, out_dir=result.out_dir
    )
