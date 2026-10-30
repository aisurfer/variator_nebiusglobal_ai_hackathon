"""Forks as dated interventions, world hypotheses as layers, and the external shocks of a case.

A fork is a set of interventions of the kinds ``factor | habit | event | actor | rule`` (09 §4), each
with a month (0 = T). A hypothesis about the world is a layer of the same interventions, marked as a
hypothesis. ``compile_knobs`` turns the interventions into the knobs that the engine reads.
"""

# ruff: noqa: E501 - the interventions of a fork, one per line

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from variator.future.params import Param

CASES_DIR = Path(__file__).resolve().parents[3] / "workspace" / "config" / "cases"  # a source checkout
CASES_ENV = "VARIATOR_CASES_DIR"  # the case configs elsewhere (for example in an image: /data/future/cases)
NEVER = 10_000  # month index of an intervention that does not happen


def cases_dir() -> Path:
    """The directory of the case configs: ``$VARIATOR_CASES_DIR`` when it is set, else the source checkout."""
    env = os.environ.get(CASES_ENV, "").strip()
    return Path(env).expanduser() if env else CASES_DIR


@dataclass(frozen=True)
class Intervention:
    kind: str  # factor | habit | event | actor | rule
    op: str  # set | scale | shift | probability | add | remove
    target: str
    at: float = 0.0  # month from T
    value: float = 0.0
    params: Mapping[str, Any] = field(default_factory=dict)
    source: str = "decision"
    label: str = ""


@dataclass(frozen=True)
class Fork:
    id: str
    label: str
    interventions: tuple[Intervention, ...] = ()
    kind: str = "decision"  # decision | hypothesis

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "interventions": [
                {
                    "kind": i.kind,
                    "op": i.op,
                    "target": i.target,
                    "at": i.at,
                    "value": i.value,
                    "params": dict(i.params),
                    "source": i.source,
                    "label": i.label,
                }
                for i in self.interventions
            ],
        }


@dataclass(frozen=True)
class Shock:
    """An external event of the case template (not in the learned risk register)."""

    name: str
    variable: str
    p: float  # probability within a year
    size: Param
    label: str = ""


def case_config(case: str) -> dict[str, Any]:
    return json.loads((cases_dir() / f"{case}.json").read_text(encoding="utf-8"))


def _var(cfg: Mapping[str, Any], name: str) -> dict[str, Any]:
    for v in cfg.get("variables", []):
        if v["name"] == name:
            return v["dist"]
    return {}


def var_param(cfg: Mapping[str, Any], name: str, default: float, note: str = "") -> Param:
    """A case variable as a parameter, with the source tag of the case."""
    src = next((v.get("source", "") for v in cfg.get("variables", []) if v["name"] == name), "")
    tag = f"case:{src.replace(': ', ':')}" if src else "case:"
    d = _var(cfg, name)
    if d.get("kind") == "pert":
        return Param.pert(name, d["low"], d["mode"], d["high"], tag, note=note)
    return Param.fixed(name, float(d.get("value", default)), tag, note=note)


# Shocks that the learned actors already carry: a key person leaves (resignation hazards of the
# people) and a key account leaves (churn hazards of the key clients). They are not drawn twice.
ACTOR_SHOCKS = {"key_person_exit", "key_account_lost"}


def case_shocks(cfg: Mapping[str, Any]) -> list[Shock]:
    out = []
    for r in cfg.get("risks", []):
        if r["variable"] in ACTOR_SHOCKS:
            continue
        m = r["magnitude"]
        size = (
            Param.pert(r["name"], m["low"], m["mode"], m["high"], "assumption:case shock")
            if m.get("kind") == "pert"
            else Param.fixed(r["name"], m.get("value", 1.0), "assumption:case shock")
        )
        out.append(Shock(r["name"], r["variable"], float(r["p"]), size, r.get("desc", r["name"])))
    return out


# ---------------------------------------------------------------------------------------------- forks
def _number(x: Any) -> float:
    return x if isinstance(x, int | float) and not isinstance(x, bool) else float(x or 0.0)


def _intervention(d: Mapping[str, Any], source: str) -> Intervention:
    return Intervention(
        kind=str(d.get("kind", "factor")), op=str(d.get("op", "set")), target=str(d["target"]),
        at=_number(d.get("at", 0.0)), value=_number(d.get("value", 0.0)),
        params=dict(d.get("params") or {}), source=str(d.get("source") or source), label=str(d.get("label") or ""),
    )  # fmt: skip


def explicit_forks(rows: Any) -> bool:
    """True when every fork of the config carries its own engine interventions (``{kind, op, target, at,
    value, params, label}``), as a drafted case does. The forks of the hand-written case carry timeline items
    (``{item, kind: plan | milestone, monthVar}``), so they stay on the built-in path."""
    if not isinstance(rows, list) or not rows:
        return False
    items = []
    for f in rows:
        if not isinstance(f, Mapping) or not isinstance(f.get("interventions"), list):
            return False
        items.extend(f["interventions"])
    return bool(items) and all(isinstance(i, Mapping) and "target" in i and "op" in i for i in items)


def forks_from_rows(rows: Sequence[Mapping[str, Any]], kind: str = "decision") -> list[Fork]:
    """Forks (or layers, ``kind="hypothesis"``) from their explicit rows ``{id, label, interventions}``."""
    return [
        Fork(str(r["id"]), str(r.get("label") or r["id"]), tuple(_intervention(i, kind) for i in r.get("interventions") or ()), kind)
        for r in rows
    ]  # fmt: skip


def company_forks(cfg: Mapping[str, Any]) -> list[Fork]:
    """The forks of case B as dated interventions: the explicit interventions of a drafted case when every
    fork carries them (``explicit_forks``), else the five built-in forks (03-REQUIREMENTS §8 fixes 1 and 5)."""
    if explicit_forks(cfg.get("forks")):
        # the ranking anchors its scale and its ties on the first fork (mcda ``run_anchors``): keep the plan
        # (the fork without interventions) goes first, the others keep their order
        forks = forks_from_rows(cfg["forks"])
        return sorted(forks, key=lambda f: bool(f.interventions))
    return builtin_forks(cfg)


def builtin_forks(cfg: Mapping[str, Any]) -> list[Fork]:
    """The five forks of the hand-written case B as dated interventions."""
    v = {x["name"]: x["dist"] for x in cfg.get("variables", [])}

    def val(name: str, default: float) -> float:
        d = v.get(name, {})
        return float(d.get("value", d.get("mode", default)))

    m_debt, m_hire, m_gpu, m_price = (val(n, 0.0) for n in ("m_debt", "m_hire", "m_gpu", "m_price"))
    labels = {f["id"]: f["label"] for f in cfg.get("forks", [])}
    return [
        Fork("status_quo", labels.get("status_quo", "Keep the plan")),
        Fork(
            "pay_tech_debt",
            labels.get("pay_tech_debt", "Reduce tech debt"),
            (
                Intervention(
                    "factor", "set", "capacity.debt_share", m_debt, val("debt_share", 0.15),
                    {"debt_cut_per_year": 0.4}, label=f"{val('debt_share', 0.15):.0%} of the engineering capacity goes to technical debt",
                ),
            ),
        ),
        Fork(
            "hire_5",
            labels.get("hire_5", "Hire 5 engineers"),
            (
                Intervention(
                    "actor", "add", "role:backend", m_hire, val("hire_count", 5),
                    {"cost_per_year": val("engineer_cost", 150_000.0), "cohort": "future:backend"},
                    label="Hire engineers; the cost starts on the join date",
                ),
            ),
        ),
        Fork(
            "reserve_gpu",
            labels.get("reserve_gpu", "Reserve GPU capacity"),
            (
                Intervention(
                    "actor", "add", "vendor:second_cloud", m_gpu, val("gpu_reserve_cost", 300_000),
                    {"relief": 0.7}, label="Reserve GPU capacity on a second cloud; the cost starts on the order date",
                ),
            ),
        ),
        Fork(
            "raise_prices_8",
            labels.get("raise_prices_8", "Raise prices 8%"),
            (
                Intervention(
                    "factor", "scale", "price.list", m_price, override(cfg, "raise_prices_8", "price_change", 0.08),
                    label="List prices up; each contract takes the new price after its notice period",
                ),
            ),
        ),
    ]  # fmt: skip


def company_layers(cfg: Mapping[str, Any]) -> list[Fork]:
    """World hypotheses of case B as layers: the explicit ``layers`` of a drafted case when the config has
    them, else the case presets mapped to actor habits and factors (``builtin_layers``)."""
    if isinstance(cfg.get("layers"), list):
        return forks_from_rows(cfg["layers"], "hypothesis")
    return builtin_layers(cfg)


def builtin_layers(cfg: Mapping[str, Any]) -> list[Fork]:
    """World hypotheses of the hand-written case B as layers (the case presets mapped to actor habits and factors)."""
    ops = {s["name"]: s["ops"] for s in cfg.get("scenarios", [])}

    def op(name: str, target: str, key: str, default: float) -> float:
        for o in ops.get(name, []):
            if o["target"] == target and key in o:
                return float(o[key])
        return default

    h = "hypothesis"
    out = [
        Fork("price_war", "Price war", (
            Intervention("habit", "probability", "shock:competitor_event", 0, op("price_war", "competitor_event", "probability", 0.5), source=h),
            Intervention("habit", "scale", "shock:competitor_event.size", 0, op("price_war", "competitor_event", "scale", 1.5), source=h),
        ), h),
        Fork("gpu_crunch", "GPU shortage", (
            Intervention("habit", "probability", "shock:gpu_shortage_event", 0, op("gpu_crunch", "gpu_shortage_event", "probability", 0.5), source=h),
            Intervention("habit", "probability", "shock:vendor_price_event", 0, op("gpu_crunch", "vendor_price_event", "probability", 0.3), source=h),
        ), h),
        Fork("big_client_leaves", "Big client may leave", (
            Intervention("habit", "probability", "clients:key.notice_any", 0, op("big_client_leaves", "key_account_event", "probability", 0.6), source=h,
                         label="P(a key client gives notice in the horizon) rises by the union with this probability"),
        ), h),
        Fork("client_requirements_churn", "Clients change requirements", (
            Intervention("habit", "scale", "staff.blocked_share", 0, op("client_requirements_churn", "blockers", "scale", 1.5), source=h),
            Intervention("factor", "scale", "demand.engineering", 0, op("client_requirements_churn", "team_load", "scale", 1.08), source=h),
            Intervention("habit", "scale", "clients.overdue_implementations", 0, op("client_requirements_churn", "overdue_base", "scale", 1.3), source=h),
        ), h),
        Fork("hiring_freeze", "Hard to hire", (
            Intervention("habit", "shift", "hiring.lead_months", 0, op("hiring_freeze", "hiring_lead", "shift", 4.0), source=h),
            Intervention("habit", "scale", "staff.resign", 0, op("hiring_freeze", "attrition", "scale", 1.2), source=h),
        ), h),
        Fork("cash_squeeze", "Cash squeeze", (
            Intervention("factor", "scale", "cash.start", 0, op("cash_squeeze", "cash0", "scale", 0.7), source=h),
            Intervention("habit", "shift", "clients.pay_delay_days", 0, op("cash_squeeze", "ar_days", "shift", 30.0), source=h),
        ), h),
        Fork("downturn", "Market downturn", (
            Intervention("habit", "probability", "shock:macro_event", 0, op("downturn", "macro_event", "probability", 0.5), source=h),
            Intervention("habit", "scale", "deals.rate", 0, 1.0 + op("downturn", "demand_growth", "shift", -0.05), source=h),
        ), h),
        Fork("payroll_tax_july", "Payroll tax +5 pp from July", (
            Intervention("factor", "shift", "tax.payroll", 9, 0.05, source=h, label="10-MVP-PRD §4: payroll tax +5 pp from July"),
        ), h),
    ]  # fmt: skip
    return out


def override(cfg: Mapping[str, Any], option: str, variable: str, default: float) -> float:
    """The value of an option override of the case (``o|<option>|<variable>|value`` on the page)."""
    for o in cfg.get("options", []):
        if o.get("name") == option and variable in (o.get("overrides") or {}):
            d = o["overrides"][variable]
            return float(d.get("value", d.get("mode", default)))
    return default


# placed timeline events of case B: item -> (target, month variable, size variable)
PLACED = {
    "gpu_event": ("placed:shock:gpu_shortage", "ev_gpu_month", "ev_gpu_size"),
    "vendor_event": ("placed:shock:vendor_price", "ev_vendor_month", "ev_vendor_size"),
    "account_event": ("placed:client", "ev_account_month", "ev_account_size"),
    "kp_event": ("placed:person", "ev_kp_month", None),
}


def placed_events(cfg: Mapping[str, Any], events: Sequence[str]) -> Fork:
    """The placed events of the page as event interventions on every fork (a world layer of certain events).
    A key account leaves = its client gives notice; a key person leaves = the person resigns (the actors of
    the item, ``items[].entityId``)."""
    v = {x["name"]: x["dist"] for x in cfg.get("variables", [])}
    items = cfg.get("items", {})

    def val(name: str | None, default: float) -> float:
        d = v.get(name or "", {})
        return float(d.get("value", d.get("mode", default)))

    out = []
    for e in events:
        if e not in PLACED:
            out.append(Intervention("event", "add", f"unknown:{e}", source="event"))
            continue
        target, month_var, size_var = PLACED[e]
        entity = str(items.get(e, {}).get("entityId") or "")
        if target == "placed:client":
            target = f"placed:client:{entity.split('.', 1)[-1]}"
        elif target == "placed:person":
            target = f"placed:person:{entity.split('.', 1)[-1].upper()}"
        out.append(
            Intervention(
                "event", "add", target, val(month_var, 0.0), val(size_var, 0.0), source="event", label=e
            )
        )
    return Fork("placed_events", "Placed events", tuple(out), "event")


# ---------------------------------------------------------------------------------------------- knobs
@dataclass
class Knobs:
    """Per fork (axis 0) the levers and the time-varying factors; per run the world layer."""

    forks: list[str]
    debt_month: np.ndarray  # first month of the debt work (NEVER = none)
    debt_share: np.ndarray
    debt_cut: np.ndarray  # share of the debt index removed per year of debt work after the lead time
    hire_month: np.ndarray
    hire_count: np.ndarray  # int
    hire_cost: np.ndarray  # USD per year per hire
    gpu_month: np.ndarray
    gpu_cost: np.ndarray  # USD per year
    gpu_relief: np.ndarray
    price_month: np.ndarray
    price_change: np.ndarray
    # world layer (the same for all forks of a run)
    shock_p_union: dict[str, float] = field(default_factory=dict)  # shock -> extra probability (union)
    shock_size_scale: dict[str, float] = field(default_factory=dict)
    key_notice_union: float = 0.0
    blocked_scale: float = 1.0
    demand_scale: float = 1.0
    hiring_lead_shift: float = 0.0
    resign_scale: float = 1.0
    cash_scale: float = 1.0
    pay_delay_shift: float = 0.0
    deals_scale: float = 1.0
    payroll_tax_shift: float = 0.0
    payroll_tax_month: int = NEVER
    placed_shocks: list[tuple[str, float, float]] = field(
        default_factory=list
    )  # (shock variable, month, size)
    placed_notices: list[tuple[str, float]] = field(default_factory=list)  # (client id, month)
    placed_quits: list[tuple[str, float]] = field(default_factory=list)  # (person id, month)
    seconded: list[str] = field(default_factory=list)  # people on another project (no capacity here)
    seconded_until: float | np.ndarray = 0.0  # month when they come back (one value or per scenario)
    unmodelled: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.forks)


def compile_knobs(forks: Sequence[Fork], layers: Sequence[Fork] = ()) -> Knobs:
    f = len(forks)
    k = Knobs(
        forks=[x.id for x in forks],
        debt_month=np.full(f, NEVER), debt_share=np.zeros(f), debt_cut=np.zeros(f),
        hire_month=np.full(f, NEVER), hire_count=np.zeros(f, dtype=np.int64), hire_cost=np.zeros(f),
        gpu_month=np.full(f, NEVER), gpu_cost=np.zeros(f), gpu_relief=np.zeros(f),
        price_month=np.full(f, NEVER), price_change=np.zeros(f),
    )  # fmt: skip
    for j, fork in enumerate(forks):
        for i in fork.interventions:
            at = int(round(i.at))
            if i.target == "capacity.debt_share":
                k.debt_month[j], k.debt_share[j] = at, i.value
                k.debt_cut[j] = float(i.params.get("debt_cut_per_year", 0.4))
            elif i.target.startswith("role:") and i.op == "add":
                k.hire_month[j], k.hire_count[j] = at, int(i.value)
                k.hire_cost[j] = float(i.params.get("cost_per_year", 150_000.0))
            elif i.target == "vendor:second_cloud":
                k.gpu_month[j], k.gpu_cost[j] = at, i.value
                k.gpu_relief[j] = float(i.params.get("relief", 0.7))
            elif i.target == "price.list":
                k.price_month[j], k.price_change[j] = at, i.value
            else:
                k.unmodelled.append(f"{fork.id}: {i.kind} {i.op} {i.target}")
    for layer in layers:
        for i in layer.interventions:
            t = i.target
            if t.startswith("placed:shock:"):
                k.placed_shocks.append((t[len("placed:shock:") :], float(i.at), float(i.value)))
            elif t.startswith("placed:client:"):
                k.placed_notices.append((t[len("placed:client:") :], float(i.at)))
            elif t.startswith("placed:person:"):
                k.placed_quits.append((t[len("placed:person:") :], float(i.at)))
            elif t.startswith("shock:") and t.endswith(".size"):
                name = t[len("shock:") : -len(".size")]
                k.shock_size_scale[name] = k.shock_size_scale.get(name, 1.0) * i.value
            elif t.startswith("shock:"):
                name = t[len("shock:") :]
                q = k.shock_p_union.get(name, 0.0)
                k.shock_p_union[name] = 1 - (1 - q) * (1 - i.value)
            elif t == "clients:key.notice_any":
                k.key_notice_union = 1 - (1 - k.key_notice_union) * (1 - i.value)
            elif t == "staff.blocked_share":
                k.blocked_scale *= i.value
            elif t == "demand.engineering":
                k.demand_scale *= i.value
            elif t == "hiring.lead_months":
                k.hiring_lead_shift += i.value
            elif t == "staff.resign":
                k.resign_scale *= i.value
            elif t == "cash.start":
                k.cash_scale *= i.value
            elif t == "clients.pay_delay_days":
                k.pay_delay_shift += i.value
            elif t == "deals.rate":
                k.deals_scale *= i.value
            elif t == "tax.payroll":
                k.payroll_tax_shift += i.value
                k.payroll_tax_month = min(k.payroll_tax_month, int(round(i.at)))
            else:
                k.unmodelled.append(f"{layer.id}: {i.kind} {i.op} {t}")
    return k
