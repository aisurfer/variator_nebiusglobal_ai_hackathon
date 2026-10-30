"""New scan recipes (fake docker), groups, findings in one file and the DefectDojo push (fake HTTP)."""

import asyncio
import json

import httpx
import pytest
from fastmcp import Client
from test_containers import SCC, TRIVY, FakeDocker  # same folder (pytest rootdir import)

from variator.containers import RECIPES, DockerCLI, run_recipe
from variator.containers.findings import DefectDojo, summary, write_scan
from variator.containers.recipes import GROUPS, expand, severity

OSV = {
    "results": [
        {
            "source": {"path": "/src/package-lock.json", "type": "lockfile"},
            "packages": [
                {
                    "package": {"name": "lodash", "version": "4.17.20", "ecosystem": "npm"},
                    "vulnerabilities": [
                        {"id": "GHSA-1", "aliases": ["CVE-9"], "summary": "Prototype pollution"},
                        {"id": "CVE-9", "aliases": ["GHSA-1"]},
                    ],
                    "groups": [{"ids": ["GHSA-1", "CVE-9"], "max_severity": "7.4"}],
                }
            ],
        }
    ]
}
JSCPD = {
    "statistics": {
        "total": {"lines": 1000, "sources": 20, "clones": 2, "duplicatedLines": 130, "percentage": 13.0},
        "formats": {"python": {"total": {"lines": 900, "percentage": 12.5}}},
    },
    "duplicates": [
        {
            "format": "python",
            "lines": 120,
            "firstFile": {"name": "a.py", "start": 1, "end": 120},
            "secondFile": {"name": "b.py", "start": 5, "end": 124},
        },
        {
            "format": "python",
            "lines": 10,
            "firstFile": {"name": "c.py", "start": 3, "end": 12},
            "secondFile": {"name": "d.py", "start": 3, "end": 12},
        },
    ],
}
DEPCRUISE = {
    "modules": [
        {
            "source": "src/a.js",
            "dependencies": [
                {"resolved": "src/b.js", "circular": True, "coreModule": False, "couldNotResolve": False},
                {"resolved": "fs", "coreModule": True, "circular": False},
                {"resolved": "node_modules/x/index.js", "circular": False, "coreModule": False},
            ],
        },
        {
            "source": "src/b.js",
            "dependencies": [
                {"resolved": "src/a.js", "circular": True, "coreModule": False},
                {"resolved": "missing", "couldNotResolve": True, "coreModule": False},
            ],
        },
    ],
    "summary": {"violations": [], "totalCruised": 3},
}
DETECT = {
    "results": {
        "cfg/settings.py": [
            {
                "type": "Secret Keyword",
                "filename": "cfg/settings.py",
                "line_number": 4,
                "hashed_secret": "h1",
                "is_verified": False,
            }
        ]
    }
}
GITLEAKS = [
    {"RuleID": "aws-access-token", "File": "deploy.sh", "StartLine": 2, "Commit": "abc", "Fingerprint": "f1"}
]
DEPCHECK = {
    "dependencies": [
        {
            "fileName": "log4j-core-2.14.jar",
            "filePath": "/src/lib/log4j-core-2.14.jar",
            "vulnerabilities": [
                {"name": "CVE-2021-44228", "severity": "CRITICAL", "cvssv3": {"baseSeverity": "CRITICAL"}}
            ],
        },
        {"fileName": "ok.jar"},
    ]
}
REPORTS = {
    "scc.json": SCC,
    "trivy.json": TRIVY,
    "osv.json": OSV,
    "jscpd-report.json": JSCPD,
    "depcruise.json": DEPCRUISE,
    "detect-secrets.json": DETECT,
    "gitleaks.json": GITLEAKS,
    "gitleaks-history.json": GITLEAKS,
    "dependency-check-report.json": DEPCHECK,
}


def _run(name, tmp_path):
    return run_recipe(
        DockerCLI(executor=FakeDocker(REPORTS)), RECIPES[name], tmp_path, out_dir=tmp_path / name
    )


def test_severity_words_and_scores():
    assert [severity(x) for x in ("CRITICAL", "moderate", "7.4", 9.8, "", None, "0")] == [
        "Critical",
        "Medium",
        "High",
        "Critical",
        "Info",
        "Info",
        "Info",
    ]


def test_groups_expand_in_order_without_repeats():
    assert expand(["sca", "trivy"]) == ["trivy", "osv_scanner"]
    assert "dependency_check" not in expand(["default"]) and set(GROUPS["default"]) <= set(RECIPES)
    with pytest.raises(ValueError, match="unknown recipe"):
        expand(["rm_rf"])


def test_osv_counts_alias_groups_once(tmp_path):
    out = _run("osv_scanner", tmp_path)
    assert out.metrics["vulnerabilities"] == {"High": 1} and out.metrics["packages_affected"] == 1
    assert out.findings[0]["vuln_id_from_tool"] == "GHSA-1" and out.findings[0]["component_name"] == "lodash"


def test_duplication_architecture_and_secrets(tmp_path):
    dup = _run("jscpd", tmp_path)
    assert dup.metrics["duplicated_percent"] == 13.0 and dup.metrics["top"][0]["a"] == "a.py:1"
    assert [f["severity"] for f in dup.findings] == ["Medium", "Low"]
    arch = _run("depcruise", tmp_path)
    assert arch.metrics["circular_dependencies"] == 2 and arch.metrics["modules_in_cycles"] == 2
    assert (
        arch.metrics["dependencies"] == 2 and arch.metrics["unresolved"] == 1
    )  # core and node_modules left out
    sec = _run("detect_secrets", tmp_path)
    assert sec.metrics["secrets"] == 1 and sec.findings[0]["line"] == 4
    hist = _run("gitleaks_history", tmp_path)
    assert hist.metrics["commits"] == 1 and hist.findings[0]["unique_id_from_tool"] == "f1"


def test_dependency_check_is_optional_and_gets_the_nvd_key(tmp_path, monkeypatch):
    monkeypatch.setenv("NVD_API_KEY", "k-1")
    fake = FakeDocker(REPORTS)
    out = run_recipe(DockerCLI(executor=fake), RECIPES["dependency_check"], tmp_path, out_dir=tmp_path / "dc")
    assert out.metrics["vulnerabilities"] == {"Critical": 1} and out.metrics["dependencies_affected"] == 1
    run = fake.calls[-1]
    assert run[-2:] == ["--nvdApiKey", "k-1"] and any(a.endswith(":/cache") for a in run)


def test_missing_image_is_built_from_the_package(tmp_path):
    class NoImages(FakeDocker):
        def __call__(self, args, timeout):
            done = super().__call__(args, timeout)
            if args[1:3] == ["image", "inspect"]:
                done.returncode = 1
            return done

    fake = NoImages(REPORTS)
    run_recipe(DockerCLI(executor=fake), RECIPES["jscpd"], tmp_path, out_dir=tmp_path / "j")
    build = next(c for c in fake.calls if c[1] == "build")
    assert "variator/jscpd:4.0.5" in build and build[build.index("-f") + 1].endswith("jscpd.Dockerfile")


def test_findings_file_is_defectdojo_generic(tmp_path):
    outcomes = [_run(n, tmp_path) for n in ("trivy", "jscpd", "depcruise")]
    write_scan(tmp_path, outcomes)
    data = json.loads((tmp_path / "findings.json").read_text(encoding="utf-8"))
    allowed = {
        "title",
        "severity",
        "description",
        "file_path",
        "line",
        "component_name",
        "component_version",
        "vuln_id_from_tool",
        "unique_id_from_tool",
        "fix_version",
        "tags",
        "static_finding",
    }
    assert all({"title", "severity", "description"} <= set(f) <= allowed for f in data["findings"])
    s = summary(tmp_path)
    assert s["by_category"] == {"vulnerability": 2, "secret": 1, "duplication": 2, "architecture": 2}
    assert s["by_tool"]["jscpd"] == 2 and s["by_severity"]["Critical"] == 1


def test_code_scan_default_group_and_defectdojo_push(tmp_path):
    from variator.mcp import Services, build_server

    sent = []

    def dojo(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", "replace")
        sent.append((request.headers["Authorization"], body))
        return httpx.Response(201, json={"test": len(sent)})

    src = tmp_path / "code"
    src.mkdir()
    services = Services({}, runner=DockerCLI(executor=FakeDocker(REPORTS)), report_dir=tmp_path / "r")
    services.defectdojo = DefectDojo(
        "http://dojo", "t-1", http=httpx.Client(transport=httpx.MockTransport(dojo))
    )
    server = build_server({}, remotes={}, services=services, all_tools=True)

    async def go(monkey_env):
        async with Client(server) as c:
            scan = (await c.call_tool("code_scan", {"source": str(src), "recipes": ["default"]})).data
            pushed = (await c.call_tool("findings_export", {"scan": scan["scan"], "product": "acme"})).data
            return scan, pushed

    import os

    os.environ["DEFECTDOJO_URL"], os.environ["DEFECTDOJO_TOKEN"] = "http://dojo", "t-1"
    try:
        scan, pushed = asyncio.run(go(None))
    finally:
        del os.environ["DEFECTDOJO_URL"], os.environ["DEFECTDOJO_TOKEN"]
    assert set(scan["scans"]) == set(GROUPS["default"]) and all(v["ok"] for v in scan["scans"].values())
    assert scan["findings"]["by_category"]["secret"] == 4  # trivy 1, gitleaks 1, history 1, detect-secrets 1
    types = [i["scan_type"] for i in pushed["imports"]]
    assert {
        "Trivy Scan",
        "OSV Scan",
        "Gitleaks Scan",
        "Detect-secrets Scan",
        "Generic Findings Import",
    } <= set(types)
    assert "scc" not in [i["recipe"] for i in pushed["imports"]]  # metrics only, no findings
    assert all(auth == "Token t-1" for auth, _ in sent) and "auto_create_context" in sent[0][1]
