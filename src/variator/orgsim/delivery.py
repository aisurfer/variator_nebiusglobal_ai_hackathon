"""Deterministic writer of a corpus delivery (``plancy-kb-corpus/1``, §1 and §11).

``write_delivery`` writes the files of a delivery from the directory data and the
timeline lines. The same input gives the same bytes: no clock, gzip with mtime 0,
sorted JSON keys, lines in a fixed order. ``verify_delivery`` reads a delivery back
and compares its files with the manifest checksums.

Layout (the minimum that Plancy's ``kbsim.intake validate`` reads):

    config/sources.yaml  config/people.yaml  fixtures/timeline.jsonl.gz
    cases.jsonl  gold_links.jsonl  dashboard.json (optional)  manifest.json
"""

from __future__ import annotations

import gzip
import io
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from variator import __version__
from variator.orgsim.events import SCHEMA_VERSION, canonical_json, content_sha256, sha256_hex

CONTRACT = "plancy-kb-corpus/1"
GENERATOR = "variator.orgsim"
TIMELINE = "fixtures/timeline.jsonl.gz"
SOURCES = "config/sources.yaml"
PEOPLE = "config/people.yaml"
CASES = "cases.jsonl"
GOLD = "gold_links.jsonl"
DASHBOARD = "dashboard.json"
MANIFEST = "manifest.json"
LINE_FIELDS = (
    "event_id",
    "sequence",
    "at",
    "world_at",
    "observed_at",
    "source",
    "object_type",
    "external_id",
    "op",
    "modified_at",
    "schema_version",
)
CONTAINER_PROVIDERS = {"jira", "confluence", "gitlab", "xwiki"}


def _instant(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    if t.tzinfo is None:
        raise ValueError(f"time {text!r} has no offset (§2.1 needs ISO 8601 with an offset)")
    return t


def finalize_lines(lines: Iterable[Mapping[str, Any]], tenant: str) -> list[dict[str, Any]]:
    """Sort the lines and fill what the writer owns.

    Order: ``at`` (as an instant), then the given ``sequence``, else the input position
    (so the input order sets the causal order of equal times). Filled when missing:
    ``sequence`` (1..N), ``version`` and ``previous_event_id`` (per object, in order),
    ``event_id``, ``world_at``, ``observed_at``, ``modified_at``, ``schema_version`` and
    ``content_sha256`` (upsert only).
    """
    rows = [dict(ln) for ln in lines]
    order = sorted(
        range(len(rows)),
        key=lambda i: (
            _instant(rows[i]["at"]),
            rows[i].get("sequence") if rows[i].get("sequence") is not None else i,
        ),
    )
    out: list[dict[str, Any]] = []
    last: dict[tuple[str, str, str], tuple[int, str]] = {}
    for n, i in enumerate(order, start=1):
        ln = rows[i]
        obj = (ln["source"], ln["object_type"], str(ln["external_id"]))
        prev_version, prev_id = last.get(obj, (0, None))
        ln["sequence"] = n
        if ln.get("modified_at") is None:
            ln["modified_at"] = ln["at"]
        if ln.get("world_at") is None:
            ln["world_at"] = ln["modified_at"]
        if ln.get("observed_at") is None:
            ln["observed_at"] = ln["at"]
        if ln.get("version") is None:
            ln["version"] = prev_version + 1
        if ln.get("previous_event_id") is None and prev_id is not None:
            ln["previous_event_id"] = prev_id
        if ln.get("event_id") is None:
            ln["event_id"] = f"{tenant}:{obj[0]}:{obj[1]}:{obj[2]}:{ln['version']}"
        if ln.get("schema_version") is None:
            ln["schema_version"] = SCHEMA_VERSION
        ln.setdefault("container", None)
        ln.setdefault("acl", None)
        if ln.get("op", "upsert") == "upsert":
            ln["op"] = "upsert"
            ln["content_sha256"] = ln.get("content_sha256") or content_sha256(ln)
        last[obj] = (int(ln["version"]), ln["event_id"])
        out.append(ln)
    return out


def check_lines(lines: Sequence[Mapping[str, Any]], sources: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Structural self-checks of §13 part A that the writer can make (for the manifest)."""
    declared = {s["id"]: s for s in sources.get("sources") or []}
    containers = {(s["id"], k) for s in declared.values() for k in (s.get("containers") or {})}
    found: dict[str, list[int]] = {
        k: []
        for k in (
            "line_fields",
            "source_declared",
            "containers",
            "order",
            "observe_lag",
            "event_ids",
            "version_chain",
        )
    }
    seen_ids: set[str] = set()
    prev_at: datetime | None = None
    last: dict[tuple[str, str, str], str] = {}
    for i, ln in enumerate(lines):
        if any(ln.get(k) in (None, "") for k in LINE_FIELDS):
            found["line_fields"].append(i)
        src = declared.get(ln.get("source", ""))
        if src is None:
            found["source_declared"].append(i)
        elif (
            src.get("provider") in CONTAINER_PROVIDERS
            and (ln["source"], ln.get("container")) not in containers
        ):
            found["containers"].append(i)
        at, mod = _instant(ln["at"]), _instant(ln["modified_at"])
        if (prev_at and at < prev_at) or at < mod:
            found["order"].append(i)
        if (at - mod).total_seconds() > 300:
            found["observe_lag"].append(i)
        prev_at = at
        if ln["event_id"] in seen_ids:
            found["event_ids"].append(i)
        seen_ids.add(ln["event_id"])
        obj = (ln["source"], ln["object_type"], str(ln["external_id"]))
        if ln.get("previous_event_id") != last.get(obj):
            found["version_chain"].append(i)
        last[obj] = ln["event_id"]
    return {
        k: {"status": "fail" if v else "ok", "count": len(v), "lines": [i + 1 for i in v[:20]]}
        for k, v in found.items()
    }


def _json_line(obj: Any) -> str:
    return canonical_json(obj) + "\n"


def _gzip_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(text.encode("utf-8"))
    return buf.getvalue()


def _yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def counters(
    lines: Sequence[Mapping[str, Any]],
    people: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    gold: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Counters of §11, computed from the delivered data."""
    ats = [ln["at"] for ln in lines]
    return {
        "events": len(lines),
        "by_source": dict(sorted(Counter(ln["source"] for ln in lines).items())),
        "by_object_type": dict(
            sorted(Counter(f"{ln['source']}:{ln['object_type']}" for ln in lines).items())
        ),
        "ops": dict(sorted(Counter(ln.get("op", "upsert") for ln in lines).items())),
        "objects": len({(ln["source"], ln["object_type"], str(ln["external_id"])) for ln in lines}),
        "period": {"first_at": min(ats) if ats else None, "last_at": max(ats) if ats else None},
        "links": {
            "declared": sum(1 for g in gold if g.get("explicit", True)),
            "implicit": sum(1 for g in gold if not g.get("explicit", True)),
        },
        "cases": {
            "total": len(cases),
            "by_category": dict(sorted(Counter(str(c.get("category")) for c in cases).items())),
        },
        "directory": {k: len(people.get(k) or []) for k in ("people", "organizations", "contacts", "groups")},
    }


def write_delivery(
    root: str | Path,
    *,
    tenant: str,
    sources: Mapping[str, Any],
    people: Mapping[str, Any],
    lines: Iterable[Mapping[str, Any]],
    seed: int,
    language: str = "en",
    name: str | None = None,
    dashboard: Mapping[str, Any] | None = None,
    cases: Sequence[Mapping[str, Any]] = (),
    gold_links: Sequence[Mapping[str, Any]] = (),
    sample_of: str | None = None,
    generator_version: str = __version__,
    extra_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a delivery to ``root`` and return its manifest.

    ``sources`` and ``people`` are the YAML documents of §2.3 and §4.1. ``lines`` are
    timeline lines (for example from ``Event.to_timeline_line``); ``finalize_lines``
    orders and completes them. ``sample_of`` marks a slice of a larger delivery (§10).
    """
    root = Path(root)
    if (sources.get("tenant") or {}).get("id") != tenant:
        raise ValueError("sources.yaml tenant.id must equal tenant")
    final = finalize_lines(lines, tenant)
    texts: dict[str, bytes] = {
        SOURCES: _yaml(dict(sources)).encode("utf-8"),
        PEOPLE: _yaml(dict(people)).encode("utf-8"),
        CASES: "".join(_json_line(c) for c in cases).encode("utf-8"),
        GOLD: "".join(_json_line(g) for g in gold_links).encode("utf-8"),
    }
    timeline_text = "".join(_json_line(ln) for ln in final)
    texts[TIMELINE] = _gzip_bytes(timeline_text)
    if dashboard is not None:
        texts[DASHBOARD] = (json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    records = {
        SOURCES: 1,
        PEOPLE: 1,
        CASES: len(cases),
        GOLD: len(gold_links),
        TIMELINE: len(final),
        DASHBOARD: 1,
    }
    unpacked = {k: (timeline_text.encode("utf-8") if k == TIMELINE else v) for k, v in texts.items()}
    config_hash = sha256_hex(texts[SOURCES] + texts[PEOPLE])
    manifest: dict[str, Any] = {
        "contract": CONTRACT,
        "tenant": tenant,
        "name": name or (sources.get("tenant") or {}).get("name") or tenant,
        "language": language,
        "generator": {"name": GENERATOR, "version": generator_version},
        "generator_version": generator_version,
        "seed": seed,
        "run_id": f"{tenant}-{seed}-{config_hash[:12]}",
        "config_hash": config_hash,
        "schema_versions": {"timeline": SCHEMA_VERSION},
        "datasets": [],
        "mappings": {},
        "time_shifts": {},
        "legitimate_propagation": [],
        "counts": counters(final, people, cases, gold_links),
        "checks": check_lines(final, sources),
        "licenses": {"texts": "synthetic, generated by VARIATOR", "third_party": []},
        "commercial_use_allowed": True,
        "files_sha256": {k: sha256_hex(v) for k, v in sorted(texts.items())},
        "files": {
            k: {
                "sha256_unpacked": sha256_hex(unpacked[k]),
                "bytes": len(unpacked[k]),
                "records": records[k],
                "schema_version": SCHEMA_VERSION if k == TIMELINE else None,
            }
            for k in sorted(texts)
        },
        **({"sample_of": sample_of} if sample_of else {}),
        **dict(extra_manifest or {}),
    }
    for rel, data in texts.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def read_timeline(root: str | Path) -> list[dict[str, Any]]:
    with gzip.open(Path(root) / TIMELINE, "rt", encoding="utf-8") as fh:
        return [json.loads(raw) for raw in fh if raw.strip()]


def verify_delivery(root: str | Path) -> list[str]:
    """Problems of a written delivery (read-only): missing files, checksums, order. Empty when valid."""
    root = Path(root)
    path = root / MANIFEST
    if not path.is_file():
        return [f"{MANIFEST} is missing"]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    for rel, sha in manifest.get("files_sha256", {}).items():
        p = root / rel
        if not p.is_file():
            problems.append(f"{rel} is missing")
        elif sha256_hex(p.read_bytes()) != sha:
            problems.append(f"{rel}: sha256 differs from the manifest")
    if not problems:
        sources = yaml.safe_load((root / SOURCES).read_text(encoding="utf-8"))
        for check, res in check_lines(read_timeline(root), sources).items():
            if res["status"] != "ok":
                problems.append(f"{check}: {res['count']} lines, first {res['lines'][:5]}")
    return problems
