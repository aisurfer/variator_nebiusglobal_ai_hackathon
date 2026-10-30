"""One place for scan findings: a scan folder per ``code_scan`` call and an optional DefectDojo push.

Layout: ``<root>/<scan id>/<recipe>/<raw report>`` and ``<root>/<scan id>/findings.json``.
``findings.json`` uses the DefectDojo "Generic Findings Import" format (keys ``title``,
``severity``, ``description`` + allowed extras); our ``category`` goes into ``tags``.
``summary`` gives the counts that an LLM or a model input needs.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from variator.containers.recipes import GENERIC, RECIPES, SEVERITIES, Finding, ScanOutcome

FINDINGS_FILE = "findings.json"


def to_generic(findings: Iterable[Finding]) -> dict[str, Any]:
    """DefectDojo generic JSON: unknown keys raise an error there, so ``category`` becomes a tag."""
    out = []
    for f in findings:
        item = {k: v for k, v in f.items() if k not in ("category", "tool")}
        item["tags"] = [f.get("category", "other"), f.get("tool", "")]
        item["static_finding"] = True
        out.append(item)
    return {"findings": out}


def write_scan(folder: Path, outcomes: list[ScanOutcome]) -> Path:
    findings = [{**f, "tool": o.recipe} for o in outcomes for f in o.findings]
    path = folder / FINDINGS_FILE
    path.write_text(json.dumps(to_generic(findings), ensure_ascii=False), encoding="utf-8")
    return path


def summary(folder: Path) -> dict[str, Any]:
    """Counts by category, severity and tool for one scan folder."""
    data = json.loads((folder / FINDINGS_FILE).read_text(encoding="utf-8"))
    by_cat: Counter[str] = Counter()
    by_sev: Counter[str] = Counter()
    by_tool: Counter[str] = Counter()
    for f in data["findings"]:
        category, tool = (f.get("tags") or ["other", ""])[:2]
        by_cat[category] += 1
        by_sev[f.get("severity", "Info")] += 1
        by_tool[tool] += 1
    return {
        "findings": sum(by_cat.values()),
        "by_category": dict(by_cat),
        "by_severity": {s: by_sev[s] for s in SEVERITIES if by_sev[s]},
        "by_tool": dict(by_tool),
    }


class DefectDojo:
    """Minimal DefectDojo API v2 client: import each raw report with its native parser.

    Env: ``DEFECTDOJO_URL`` and ``DEFECTDOJO_TOKEN`` (API v2 key). ``auto_create_context``
    creates the product and the engagement on the first import.
    """

    def __init__(self, url: str, token: str, *, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.http = http or httpx.Client(timeout=120)
        self.headers = {"Authorization": f"Token {token}"}

    def import_scan(
        self, path: Path, scan_type: str, *, product: str, engagement: str, test_title: str | None = None
    ) -> dict[str, Any]:
        data = {
            "scan_type": scan_type,
            "product_name": product,
            "engagement_name": engagement,
            "product_type_name": "VARIATOR",
            "auto_create_context": "true",
            "close_old_findings": "false",
            "active": "true",
            "verified": "false",
            **({"test_title": test_title} if test_title else {}),
        }
        with path.open("rb") as fh:
            response = self.http.post(
                f"{self.url}/api/v2/import-scan/",
                headers=self.headers,
                data=data,
                files={"file": (path.name, fh)},
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"DefectDojo import of {path.name} failed ({response.status_code}): {response.text[:300]}"
            )
        body = response.json()
        return {"test": body.get("test") or body.get("test_id"), "scan_type": scan_type}

    def push_scan(self, folder: Path, *, product: str, engagement: str) -> list[dict[str, Any]]:
        """Native parsers for tools DefectDojo knows; our findings.json for the rest (jscpd, depcruise)."""
        results, generic_tools = [], []
        for sub in sorted(p for p in folder.iterdir() if p.is_dir()):
            recipe = RECIPES.get(sub.name)
            if recipe is None:
                continue
            if recipe.scan_type == GENERIC:
                generic_tools.append(sub.name)
                continue
            report = sub / (recipe.import_file or recipe.output)
            if report.is_file():
                results.append(
                    {
                        "recipe": sub.name,
                        **self.import_scan(report, recipe.scan_type, product=product, engagement=engagement),
                    }
                )
        if generic_tools:
            data = json.loads((folder / FINDINGS_FILE).read_text(encoding="utf-8"))
            keep = [f for f in data["findings"] if (f.get("tags") or ["", ""])[1] in generic_tools]
            subset = folder / "findings-generic.json"
            subset.write_text(json.dumps({"findings": keep}, ensure_ascii=False), encoding="utf-8")
            results.append(
                {
                    "recipe": ",".join(generic_tools),
                    **self.import_scan(
                        subset,
                        GENERIC,
                        product=product,
                        engagement=engagement,
                        test_title="VARIATOR code quality",
                    ),
                }
            )
        return results
