"""Export a decision model (+ rule packs + page text) as JSON for the generic playground page.

The page (``docs/playground/model-simulator.html``) parses the same formula syntax, calculates
derived values in dependency order, applies risks and rules with the Python semantics, and
runs the Monte Carlo in the browser. One template gives one page; parity comes by design.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from variator.domain import Criterion, DecisionModel, Distribution, Scenario, Timeline
from variator.rules import RulePack

PLACEHOLDER = "/*__MODEL__*/null"


def _dist(d: Distribution) -> dict[str, Any]:
    return {k: v for k, v in d.model_dump(mode="json").items() if v is not None}


def _criterion(c: Criterion) -> dict[str, Any]:
    """A criterion for the page. ``rag`` (only when set): fixed band limits, each a number or a variable name
    (the page takes the median of that variable, as ``simulation.tensor.resolved_criteria``)."""
    out: dict[str, Any] = {
        "name": c.name,
        "desc": c.description,
        "unit": c.unit,
        "higher": c.higher_is_better,
        "weight": c.weight,
    }
    if c.rag_from is not None:
        out["rag"] = c.rag_from.model_dump()
    elif c.rag is not None:
        out["rag"] = c.rag.model_dump()
    # meaningful difference (only when set): a number or a variable name (the page takes its median)
    if c.tie_from is not None:
        out["tie"] = c.tie_from
    elif c.tie_within > 0:
        out["tie"] = c.tie_within
    return out


def export_model(
    model: DecisionModel,
    packs: Iterable[RulePack] = (),
    page: Mapping[str, Any] | None = None,
    scenarios: Iterable[Scenario] = (),
    timeline: Timeline | None = None,
) -> dict[str, Any]:
    """JSON-ready dict: variables (with block), derived, options, criteria, risks, rules, page text,
    scenario presets (the page applies their ops like ``domain.apply_scenarios``) and the timeline
    (the page draws it and mirrors ``variator.timeline``)."""
    variables = []
    for v in model.variables:
        block, _, source = (v.source or "").partition(" | ")
        variables.append(
            {
                "name": v.name,
                "desc": v.description,
                "unit": v.unit,
                "block": block if source else "inputs",
                "source": source or (v.source or ""),
                "dist": _dist(v.distribution),
            }
        )
    rules = []
    for pack in packs:
        for r in pack.rules:
            rules.append(
                {
                    "id": r.id,
                    "pack": pack.pack,
                    "kind": r.kind.value,
                    "desc": r.description,
                    "branches": [
                        {"if": cond, "then": act.model_dump(mode="json")} for cond, act in r.branches()
                    ],
                    "else": r.else_.model_dump(mode="json") if r.else_ else None,
                    "options": r.options,
                }
            )
    return {
        "question": model.question,
        "variables": variables,
        "derived": [
            {"name": d.name, "desc": d.description, "unit": d.unit, "formula": d.formula}
            for d in model.derived
        ],
        "options": [
            {
                "name": o.name,
                "desc": o.description,
                "formula": o.formula,
                "metrics": o.metrics,
                "overrides": {k: _dist(v) for k, v in o.overrides.items()},
            }
            for o in model.options
        ],
        "criteria": [_criterion(c) for c in model.effective_criteria()],
        "risks": [
            {
                "name": r.name,
                "desc": r.description,
                "p": r.probability,
                "variable": r.variable,
                "effect": r.effect.value,
                "options": r.options,
                "magnitude": _dist(r.magnitude),
            }
            for r in model.risks
        ],
        "rules": rules,
        "assumptions": model.assumptions,
        "page": dict(page or {}),
        "scenarios": [s.model_dump(mode="json", exclude_none=True) for s in scenarios],
        "timeline": timeline.model_dump(mode="json") if timeline else None,
    }


def build_page(template: str | Path, data: Mapping[str, Any], out: str | Path) -> Path:
    """Write the template with the model JSON in place of ``PLACEHOLDER``."""
    html = Path(template).read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        raise ValueError(f"{template}: no {PLACEHOLDER} placeholder")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    target = Path(out)
    target.write_text(html.replace(PLACEHOLDER, payload), encoding="utf-8")
    return target
