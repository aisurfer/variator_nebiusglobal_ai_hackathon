"""Write the artifact set that ``variator.web.artifacts.ArtifactStore`` serves.

Layout: ``<out>/manifest.json`` and JSON files (``actors.json``, ``habits.json``,
``cohorts.json``, ``risk_register.json``, ``recovery.json``, ``facts/<actor>.json``). The
output is deterministic: no wall-clock time, sorted keys, fixed rounding.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from variator.actors.learn import SCHEMA, Learned

SET_TITLE = "Actors, habits, cohorts and risks learned from the history"


def dumps(doc: Any) -> str:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1, default=_default) + "\n"


def _default(x: Any) -> Any:
    if hasattr(x, "isoformat"):
        return x.isoformat()
    if hasattr(x, "item"):
        return x.item()
    raise TypeError(f"not JSON serialisable: {type(x)}")


def write_set(L: Learned, out_dir: str | Path, recovery: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write all documents and the manifest; return the manifest."""
    out = Path(out_dir)
    if (out / "facts").is_dir():
        shutil.rmtree(out / "facts")
    out.mkdir(parents=True, exist_ok=True)
    docs = L.documents()
    if recovery is not None:
        docs["recovery.json"] = recovery
    files = []
    for rel, doc in sorted(docs.items()):
        text = dumps(doc)
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        files.append(
            {
                "path": rel,
                "bytes": len(text.encode("utf-8")),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    counts: dict[str, int] = {}
    for a in L.actors:
        counts[a["kind"]] = counts.get(a["kind"], 0) + 1
    manifest = {
        "id": out.name,
        "schema": SCHEMA,
        "title": SET_TITLE,
        "as_of": L.as_of.isoformat(),
        "history": {
            "root": str(L.h.root),
            "start": L.h.start.isoformat(),
            "rows_cut_at_t": dict(sorted(L.h.dropped.items())),
            "optional_datasets": dict(sorted(L.h.optional.items())),
        },
        "producer": "variator.actors (python -m variator.actors.cli learn)",
        "llm": "none: parameters that need general knowledge are marked llm:pending",
        "source_tags": ["data:<dataset>@<T>", "group:<cohort>", "web:", "llm:pending", "assumption:"],
        "param_format": {
            "point": "posterior median",
            "low": "p1",
            "mode": "most likely value",
            "high": "p99",
            "n": "observations or events",
            "source": "source tag",
        },
        "counts": {
            "actors": counts,
            "cohorts": len(L.cohorts),
            "risks": len(L.risks),
            "facts_files": sum(1 for f in files if f["path"].startswith("facts/")),
        },
        "files": files,
    }
    if recovery is not None:
        manifest["recovery"] = recovery["summary"] | {"worst_misses": len(recovery.get("worst_misses", []))}
        manifest["recovery"].pop("by_parameter", None)
    (out / "manifest.json").write_text(dumps(manifest), encoding="utf-8")
    return manifest
