"""Cases (model + rules + quality) and small patches.

The ``Store`` that keeps them is in ``variator.mcp.store``. ``apply_patch`` changes a
case with small operations, so an LLM sends a delta instead of the full model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from variator.adapters import Dataset
from variator.domain import DecisionModel, Distribution, Fact, FactOrigin
from variator.quality import score_series
from variator.router import Resolver, TypedQuestion, match_choice
from variator.rules import RulePack, RuleSet, compile_rules

_ORIGIN_Q = {"assumption": 0.4, "web": 0.7, "data": 0.8}


@dataclass
class Case:
    id: str
    model: DecisionModel
    packs: list[RulePack] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    quality: dict[str, float] = field(default_factory=dict)  # variable -> Q
    notes: list[str] = field(default_factory=list)
    version: int = 1

    def rules(self) -> RuleSet:
        m = self.model
        return compile_rules(
            self.packs,
            variables=[v.name for v in m.variables],
            options=[o.name for o in m.options],
            criteria=[c.name for c in m.effective_criteria()],
            derived={d.name: d.formula for d in m.derived},
        )

    def summary(self) -> dict[str, Any]:
        m = self.model
        return {
            "case": self.id,
            "version": self.version,
            "question": m.question,
            "criteria": [
                {
                    "name": c.name,
                    "unit": c.unit,
                    "better": "higher" if c.higher_is_better else "lower",
                    "weight": c.weight,
                }
                for c in m.effective_criteria()
            ],
            "options": [o.name for o in m.options],
            "variables": {v.name: _dist_short(v.distribution) for v in m.variables},
            "derived": {d.name: d.formula for d in m.derived},
            "risks": [r.name for r in m.risks],
            "correlations": len(m.correlations),
            "rules": [f"{p.pack}:{r.id}" for p in self.packs for r in p.rules],
            "plugins": self.plugins,
            "low_quality": sorted(k for k, q in self.quality.items() if q < 0.5),
            "notes": self.notes[-10:],
        }


def _dist_short(d: Distribution) -> str:
    params = {k: v for k, v in d.model_dump().items() if k != "kind" and v is not None}
    return f"{d.kind}(" + ", ".join(f"{k}={v:.4g}" for k, v in params.items()) + ")"


# --- patches -------------------------------------------------------------------------

_LIST_KEYS = {
    "variable": "variables",
    "option": "options",
    "risk": "risks",
    "criterion": "criteria",
    "derived": "derived",
}


def apply_patch(model: DecisionModel, ops: list[dict[str, Any]]) -> DecisionModel:
    """Apply small operations and validate the result. Supported ops:

    ``set_variable|set_option|set_risk|set_criterion|set_derived`` {"value": full object} (add or replace);
    ``remove_variable|remove_option|remove_risk|remove_criterion|remove_derived`` {"name"};
    ``set_distribution`` {"name", "distribution"}; ``set_formula`` {"option", "criterion"?, "formula"};
    ``set_weight`` {"criterion", "weight"}; ``set_correlation`` {"a", "b", "rho"} (rho 0 removes it);
    ``set_question`` {"value"}; ``add_assumption`` {"value"}.
    """
    data = model.model_dump(mode="json")
    criteria = [c["name"] for c in data["criteria"]] or ["value"]
    for op in ops:
        kind = op.get("op", "")
        action, _, target = kind.partition("_")
        if action in ("set", "remove") and target in _LIST_KEYS:
            items = data[_LIST_KEYS[target]]
            name = op["value"]["name"] if action == "set" else op["name"]
            kept = [i for i in items if i["name"] != name]
            if action == "remove" and len(kept) == len(items):
                raise ValueError(f"{kind}: no {target} {name!r}")
            if action == "set":
                old = next((i for i in items if i["name"] == name), None)
                position = items.index(old) if old else len(kept)
                kept.insert(position, op["value"])
            data[_LIST_KEYS[target]] = kept
        elif kind == "set_distribution":
            var = _find(data["variables"], op["name"], "variable")
            var["distribution"] = op["distribution"]
        elif kind == "set_formula":
            option = _find(data["options"], op["option"], "option")
            crit = op.get("criterion") or criteria[0]
            if crit == criteria[0]:
                option["formula"] = op["formula"]
            else:
                option.setdefault("metrics", {})[crit] = op["formula"]
        elif kind == "set_weight":
            _find(data["criteria"], op["criterion"], "criterion")["weight"] = float(op["weight"])
        elif kind == "set_correlation":
            pair = {op["a"], op["b"]}
            data["correlations"] = [c for c in data["correlations"] if {c["a"], c["b"]} != pair]
            if float(op["rho"]) != 0:
                data["correlations"].append({"a": op["a"], "b": op["b"], "rho": float(op["rho"])})
        elif kind == "set_question":
            data["question"] = op["value"]
        elif kind == "add_assumption":
            data["assumptions"].append(op["value"])
        else:
            raise ValueError(f"unknown patch op {kind!r}")
    return DecisionModel.model_validate(data)


def _find(items: list[dict[str, Any]], name: str, kind: str) -> dict[str, Any]:
    for item in items:
        if item["name"] == name:
            return item
    raise ValueError(f"no {kind} {name!r}")


# --- quality and facts from datasets (local, no LLM) --------------------------------


def variable_quality(
    model: DecisionModel, datasets: list[Dataset], resolver: Resolver
) -> tuple[dict[str, float], list[str]]:
    """Q per variable: from a matching dataset column (T0 name match), else from its source text."""
    columns = {(d.name, c): (d, v) for d in datasets for c, v in d.numeric().items()}
    choices = [f"{d}.{c}" for d, c in columns]
    q: dict[str, float] = {}
    notes: list[str] = []
    for var in model.variables:
        if var.distribution.kind == "fixed":
            continue
        if len(choices) >= 2:
            # T0 only: a quality hint does not justify an LLM call.
            question = TypedQuestion(
                kind="choice", question=var.name, choices=choices, state={"text": var.name}, task="column_map"
            )
            decision = _column_match(question)
            if decision is not None and decision.confidence >= resolver.threshold:
                ds_name, _, col = str(decision.value).partition(".")
                dataset, values = columns[(ds_name, col)]
                q[var.name] = score_series(values, origin=dataset.origin).q
                notes.append(f"{var.name}: quality from column {decision.value}")
                continue
        source = (var.source or "assumption").lower()
        origin = (
            "web"
            if source.startswith("http")
            else "data"
            if source.startswith(("[f", "data"))
            else "assumption"
        )
        q[var.name] = _ORIGIN_Q[origin]
    return q, notes


def _column_match(question: TypedQuestion) -> Any:
    """T0 match on the column part of 'dataset.column' choices."""
    stripped = question.model_copy(update={"choices": [c.partition(".")[2] for c in question.choices]})
    decision = match_choice(stripped)
    if decision is None:
        return None
    index = stripped.choices.index(str(decision.value))
    decision.value = question.choices[index]
    return decision


def dataset_facts(datasets: list[tuple[str, Dataset]], limit: int = 40) -> list[Fact]:
    """Facts from the dataset summaries (median and range), made by code. 0 tokens."""
    facts: list[Fact] = []
    for key, dataset in datasets:
        for column, stats in dataset.summary()["numeric"].items():
            if stats["n"] == 0:
                continue
            facts.append(
                Fact(
                    statement=f"{dataset.name}.{column}: median {stats['median']}, range {stats['min']} to "
                    f"{stats['max']} over {stats['n']} values.",
                    value=stats["median"],
                    origin=FactOrigin.DATA,
                    source=f"{key}:{column}",
                )
            )
    return facts[:limit]
