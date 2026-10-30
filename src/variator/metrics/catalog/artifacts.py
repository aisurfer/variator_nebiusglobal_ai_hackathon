"""Compute the whole catalogue and write the artifact set that ``variator.web`` serves.

Layout of ``<out>`` (one set of ``variator.web.artifacts``; every file is JSON):

- ``manifest.json``: what the set holds (the brief, roles, metrics, files with sizes and hashes);
- ``catalog.json``: the definitions as the UI reads them and the editable parameters;
- ``briefs/ceo.json``: ``KbBrief`` of the one MVP role, the CEO, with every metric in a top row and three
  areas (``workspace/src/kb/types.ts``);
- ``metrics/<metric id>.json``: ``{"metric": KbMetricResult, "result": MetricResult, "params": {...}}``
  (card, drill-down and the current parameters);
- ``sanity.json``: the values at T against the generator's numbers.

Every document is validated against the contracts before it is written.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from variator.metrics.catalog.contracts import CatalogEntry, MetricArtifact
from variator.metrics.catalog.core import MVP_ROLE, RoleId, tile_value
from variator.metrics.catalog.datasets import HistoryData
from variator.metrics.catalog.engine import (
    ENGINE_VERSION,
    SPEC_VERSION,
    Evaluation,
    definition,
    evaluate,
)
from variator.metrics.catalog.ids import is_kb_id
from variator.metrics.catalog.params import CatalogParams
from variator.metrics.catalog.registry import AREAS, CATALOG, ROLES, brief_layout, metrics_for
from variator.metrics.results import (
    KbBrief,
    KbBriefArea,
    KbMetricResult,
    KbRole,
    LineageNode,
    MetricResult,
)

SET_KIND = "history-metrics"
SCHEMA = 2  # 2: one brief (the CEO) with top row and areas; metric files carry their parameters
UI_ROLES: tuple[RoleId, ...] = (MVP_ROLE,)


def compute_all(
    data: HistoryData, params: CatalogParams, as_of: date | None = None, *, computed_at: str | None = None
) -> dict[str, Evaluation]:
    return {m.id: evaluate(m, data, params, as_of, computed_at=computed_at) for m in CATALOG}


# ---------------------------------------------------------------------------------------------- checks
def _walk(node: LineageNode):
    yield node
    for c in node.children:
        yield from _walk(c)


def as_of_violations(ev: Evaluation) -> list[str]:
    """Records or points after the as-of date, and record ids that are not KB entity ids (empty: clean)."""
    out = []
    d = ev.as_of.isoformat()
    for p in ev.kb.series:
        if p.date > d:
            out.append(f"{ev.defn.id}: series point {p.date} after {d}")
    for i in ev.computation.items:
        if i.date and i.date[:10] > d:
            out.append(f"{ev.defn.id}: record {i.kb_id} dated {i.date} after {d}")
        if i.event and i.event[1][:10] > (ev.as_of + timedelta(days=1)).isoformat():
            out.append(f"{ev.defn.id}: record {i.kb_id} known at {i.event[1]} after {d}")
        if not is_kb_id(i.kb_id):
            out.append(f"{ev.defn.id}: {i.kb_id!r} is not a KB entity id")
    for node in _walk(ev.result.lineage):
        for r in node.records or []:
            if not is_kb_id(r.id):
                out.append(f"{ev.defn.id}: lineage record {r.id!r} is not a KB entity id")
        if node.cause is not None and not is_kb_id(node.cause.id):
            out.append(f"{ev.defn.id}: cause {node.cause.id!r} is not a KB entity id")
    return out


# ---------------------------------------------------------------------------------------------- briefs
def _name(title: str) -> str:
    """A metric title inside a sentence: «New ARR · 90 days» -> «new ARR», «ARR» stays."""
    t = title.split(" · ")[0]
    return t if t[:2].isupper() else t[:1].lower() + t[1:]


_STATE = {"bad": " (needs attention)", "warn": " (watch)"}


def _phrase(e: Evaluation, state: bool = False) -> str:
    """«cash $2.00M»; a value with a display phrase reads as that phrase («runway over 5 years»)."""
    shown = (e.computation.display or {}).get("phrase")
    text = shown or f"{_name(e.kb.title)} {tile_value(e.kb.value, e.kb.unit)}"
    return text + _STATE.get(e.kb.status, "") if state else text


def headline(evals: list[Evaluation], top: list[str]) -> list[str]:
    """The CEO headline: one sentence per area (its key numbers with their state, then the other metrics
    that need attention or a watch), and a last line with the metrics this history cannot compute."""
    lines = []
    for a in AREAS:
        mine = [e for e in evals if e.defn.area == a["id"]]
        key = [_phrase(e, True) for e in mine if e.defn.id in top and e.kb.value is not None]
        rest = [e for e in mine if e.defn.id not in top]
        bad = [_phrase(e) for e in rest if e.kb.status == "bad"]
        warn = [_phrase(e) for e in rest if e.kb.status == "warn"]
        parts = [", ".join(key)] if key else []
        parts += [f"needs attention: {', '.join(bad)}"] if bad else []
        parts += [f"to watch: {', '.join(warn)}"] if warn else []
        if not bad and not warn:
            parts.append(f"every {'other ' if key else ''}metric with a target is on track")
        label = a["title"].split(" ")[0]
        lines.append(f"{label}: {'; '.join(parts)}.")
    unknown = [e.kb.title for e in evals if e.kb.status == "unknown"]
    if unknown:
        lines.append(f"Unknown (no data in this history): {', '.join(unknown)}.")
    return lines


def organization(data: HistoryData) -> dict[str, Any]:
    org = dict(data.organization or {})
    org.setdefault("id", data.tenant)
    org.setdefault("tenant", data.tenant)
    org.setdefault("name", data.company)
    org.setdefault("currency", data.currency)
    org.setdefault("timezone", str(data.tz))
    org["roles"] = [r for r in org.get("roles") or [] if r.get("id") in UI_ROLES]
    return org


def role_doc(data: HistoryData, role: RoleId = MVP_ROLE) -> KbRole:
    for r in organization(data)["roles"]:
        if r.get("id") == role:
            return KbRole(
                id=role,
                label=str(r.get("label") or ROLES[role]["label"]),
                title=str(r.get("title") or ""),
                name=str(r.get("name") or ""),
                person_id=str(r.get("personId") or ""),
                principal=str(r.get("principal") or "user:admin"),
                **({"metadata": bool(r["metadata"])} if "metadata" in r else {}),
            )
    return KbRole(
        id=role, label=ROLES[role]["label"], title="", name="", person_id="", principal="user:admin"
    )


def brief(data: HistoryData, evals: dict[str, Evaluation], built_at: str) -> KbBrief:
    """The CEO brief: every metric once, in the order of the page (the top row, then the areas)."""
    top, areas = brief_layout([m.id for m in metrics_for(MVP_ROLE) if m.id in evals])
    order = top + [i for a in areas for i in a["metrics"]]
    mine = [evals[i] for i in order]
    as_of = mine[0].as_of if mine else data.anchor
    return KbBrief(
        organization=organization(data),
        role=role_doc(data),
        as_of=as_of.isoformat(),
        range={"start": data.start.isoformat(), "end": (data.anchor - timedelta(days=1)).isoformat()},
        weeks=[p.date for p in mine[0].kb.series] if mine else [as_of.isoformat()],
        headline=headline(mine, top),
        metrics=[e.kb for e in mine],
        top=top,
        areas=[KbBriefArea(**a) for a in areas],
        built_at=built_at,
        watermark=None,
    )


def catalog_doc(params: CatalogParams) -> dict[str, Any]:
    entries = [
        CatalogEntry(
            definition=definition(m, params.of(m.id)),
            gen_unit=m.gen_unit,
            family=m.family,
            needs=list(m.needs),
            params=params.of(m.id).model_dump(mode="json"),
        ).to_json_dict()
        for m in CATALOG
    ]
    return {
        "engineVersion": ENGINE_VERSION,
        "specVersion": SPEC_VERSION,
        "paramsDigest": params.digest(),
        "params": params.model_dump(mode="json"),
        "roles": {r: {**ROLES[r], "metrics": [m.id for m in metrics_for(r)]} for r in UI_ROLES},
        "areas": [{**a, "metrics": [m.id for m in CATALOG if m.area == a["id"]]} for a in AREAS],
        "metrics": entries,
    }


# ---------------------------------------------------------------------------------------------- writing
def _dump(path: Path, doc: Any, *, compact: bool = False) -> dict[str, Any]:
    text = (
        json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        if compact
        else json.dumps(doc, ensure_ascii=False, indent=1)
    )
    data = (text + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def metric_file(metric_id: str) -> str:
    return f"metrics/{metric_id}.json"


def write_set(
    out: str | Path,
    data: HistoryData,
    params: CatalogParams,
    evals: dict[str, Evaluation],
    *,
    built_at: str | None = None,
    sanity_doc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the artifact set; files of an earlier run that this run does not write are removed."""
    out = Path(out)
    built_at = built_at or datetime.now(UTC).isoformat(timespec="seconds")
    files: dict[str, dict[str, Any]] = {}
    metrics: dict[str, Any] = {}
    for mid, ev in evals.items():
        art = MetricArtifact(metric=ev.kb, result=ev.result, params=params.of(mid).model_dump(mode="json"))
        doc = art.to_json_dict()
        MetricArtifact.model_validate(doc)  # the contracts parse what is written
        MetricResult.model_validate(doc["result"])
        rel = metric_file(mid)
        files[rel] = _dump(out / rel, doc, compact=True)
        metrics[mid] = {
            "file": rel,
            "title": ev.kb.title,
            "area": ev.defn.area,
            "unit": ev.kb.unit,
            "value": ev.kb.value,
            "status": ev.kb.status,
            "records": ev.result.coverage.records,
        }
    # one brief: the CEO (the one MVP role) with every metric
    doc = brief(data, evals, built_at).to_json_dict()
    KbBrief.model_validate(doc)
    for m in doc["metrics"]:
        KbMetricResult.model_validate(m)
    rel = f"briefs/{MVP_ROLE}.json"
    files[rel] = _dump(out / rel, doc)
    roles: dict[str, Any] = {
        MVP_ROLE: {
            "file": rel,
            "label": ROLES[MVP_ROLE]["label"],
            "metrics": [m["id"] for m in doc["metrics"]],
        }
    }
    brief_entry = {"role": MVP_ROLE, "file": rel, "top": doc["top"], "areas": doc["areas"]}
    files["catalog.json"] = _dump(out / "catalog.json", catalog_doc(params))
    if sanity_doc is not None:
        files["sanity.json"] = _dump(out / "sanity.json", sanity_doc)
    for old in list(out.glob("metrics/*.json")) + list(out.glob("briefs/*.json")):
        if old.relative_to(out).as_posix() not in files:
            old.unlink()
    as_of = next(iter(evals.values())).as_of if evals else data.anchor
    manifest = {
        "title": f"History metrics of {data.company or data.tenant}",
        "kind": SET_KIND,
        "schema": SCHEMA,
        "engineVersion": ENGINE_VERSION,
        "specVersion": SPEC_VERSION,
        "asOf": as_of.isoformat(),
        "builtAt": built_at,
        "history": {
            "dir": str(data.root),
            "runId": data.run_id,
            "tenant": data.tenant,
            "company": data.company,
            "anchor": data.anchor.isoformat(),
            "start": data.start.isoformat(),
            "timezone": str(data.tz),
            "currency": data.currency,
            "tables": sorted(data.present),
            "missingOptional": [x for x in ("monitoring_daily", "nps_responses") if not data.has(x)],
            "droppedAfterT": data.dropped_after_t,
        },
        "params": {"file": "catalog.json", "digest": params.digest()},
        "catalog": "catalog.json",
        "sanity": "sanity.json" if sanity_doc is not None else None,
        "brief": brief_entry,
        "roles": roles,
        "metrics": metrics,
        "files": [{"path": k, **v} for k, v in sorted(files.items())],
    }
    files["manifest.json"] = _dump(out / "manifest.json", manifest)
    return manifest
