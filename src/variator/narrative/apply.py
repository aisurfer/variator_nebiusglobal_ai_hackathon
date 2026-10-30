"""Merge the overlay with the base delivery and write a new history directory.

``apply`` reads the base history (never changes it), renders the accepted artifacts, adds
the new Confluence spaces to ``sources.yaml``, and writes ``<out>/delivery`` with the orgsim
delivery writer (sequence, version chains, checksums and the manifest are recomputed), a
``sample/`` slice of the last days, ``datasets/`` and ``truth.json`` copied from the base,
and a ``summary.json`` with the overlay counters.
"""

from __future__ import annotations

import copy
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from variator.narrative import OVERLAY_VERSION
from variator.narrative.base import Base
from variator.narrative.planner import SPACES
from variator.narrative.render import render
from variator.orgsim.delivery import verify_delivery, write_delivery


def merged_sources(b: Base) -> dict[str, Any]:
    src = copy.deepcopy(b.sources)
    for s in src["sources"]:
        if s["provider"] == "confluence":
            for key, sp in SPACES.items():
                s["containers"].setdefault(key, {"name": sp["name"], "acl": list(sp["acl"])})
    return src


def apply(
    b: Base,
    plan: dict[str, Any],
    out: str | Path,
    *,
    texts: dict[str, dict[str, str]] | None = None,
    keep: set[str] | None = None,
    sample_days: int = 10,
) -> dict[str, Any]:
    out = Path(out)
    if out.resolve() == b.root.resolve():
        raise ValueError("the output must be a new directory, not the base history")
    overlay, gold_new, stats = render(b, plan, texts, keep)
    base_lines = [dict(ln) for ln in b.lines]
    lines = _sort_instants(base_lines + overlay)  # base order kept for equal instants
    sources = merged_sources(b)
    seen = {(g["effect"], g["predicate"], g.get("cause")) for g in b.gold}
    gold = list(b.gold) + [g for g in gold_new if (g["effect"], g["predicate"], g.get("cause")) not in seen]
    hist = dict(b.manifest.get("history") or {})
    kept = [a for a in plan["artifacts"] if keep is None or a["id"] in keep]
    narrative = {
        "version": OVERLAY_VERSION,
        "base": str(b.root),
        "base_run_id": b.manifest.get("run_id"),
        "artifacts": dict(sorted(Counter(a["type"] for a in kept).items())),
        "lines": len(overlay),
        "gold_links": len(gold) - len(b.gold),
        **stats,
    }
    delivery = out / "delivery"
    manifest = write_delivery(
        delivery,
        tenant=b.tenant,
        sources=sources,
        people=b.people_yaml,
        lines=lines,
        seed=int(b.manifest.get("seed", 0)),
        language=b.manifest.get("language", "en"),
        name=b.manifest.get("name"),
        dashboard=b.dashboard,
        cases=b.cases,
        gold_links=gold,
        extra_manifest={"history": hist, "narrative": narrative},
    )
    if sample_days:
        days = sorted({ln["at"][:10] for ln in lines})
        first = days[-sample_days] if len(days) > sample_days else None
        sample = [
            {k: v for k, v in ln.items() if k not in ("sequence",)}
            for ln in lines
            if first is None or ln["at"][:10] >= first
        ]
        for ln in sample:
            ln["sequence"] = None
            ln["version"] = None
            ln["previous_event_id"] = None
            ln["event_id"] = None
        write_delivery(
            delivery / "sample",
            tenant=b.tenant,
            sources=sources,
            people=b.people_yaml,
            lines=sample,
            seed=int(b.manifest.get("seed", 0)),
            language=b.manifest.get("language", "en"),
            name=f"{b.manifest.get('name')} (sample)",
            dashboard=b.dashboard,
            sample_of=b.tenant,
        )
    for name in ("datasets",):
        src = b.root / name
        if src.is_dir():
            dst = out / name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
    for name in ("truth.json",):
        if (b.root / name).is_file():
            shutil.copy2(b.root / name, out / name)
    base_summary = {}
    if (b.root / "summary.json").is_file():
        base_summary = json.loads((b.root / "summary.json").read_text(encoding="utf-8"))
    summary = {
        **base_summary,
        "manifest_counts": manifest["counts"],
        "checks": manifest["checks"],
        "narrative": narrative,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    problems = verify_delivery(delivery)
    return {"out": str(out), "narrative": narrative, "verify": problems, "counts": manifest["counts"]}


def _sort_instants(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by the instant of ``at`` (offsets differ), keeping the given order for equal instants."""
    from variator.narrative.base import parse_time

    keyed = sorted(enumerate(lines), key=lambda x: (parse_time(x[1]["at"]), x[0]))
    out = []
    for _, ln in keyed:
        ln = dict(ln)
        ln["sequence"] = None
        out.append(ln)
    return out
