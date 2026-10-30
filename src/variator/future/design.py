"""Draft case B from the leader's question with Nemotron 3 Ultra: the forks, the world hypotheses and the
risks, in the levers that the twin models (the case re-drafting test, docs/knowledge/case-regen-test.md).

The catalogue below gives each lever, world target and shock its meaning, unit and allowed range, chosen
from the engine semantics (``forks.compile_knobs``, ``company.CompanyModel``), never from the hand-written
case. A draft is validated against the catalogue; an invalid draft is retried once with the errors
appended, and a draft still invalid is recorded as failed. ``to_case_config`` turns a valid draft into a
case config the engine reads (``forks[].interventions``, ``layers``, ``risks``).

    python -m variator.future.design generate --questions scripts/case_regen_questions.txt \\
        --per-question 20 --workers 8 --out DIR [--resume]
"""

# ruff: noqa: E501 - the catalogue, one entry per line

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from variator.future.forks import ACTOR_SHOCKS, cases_dir
from variator.llm.client import TokenFactoryClient, extract_json, recommended_sampling, user_message
from variator.llm.models import ROLE_MODEL

FACTS_PATH = Path(__file__).resolve().parents[3] / "workspace" / "config" / "company.facts.json"
CASE = "company_best_year"
MAX_TOKENS = 32_768  # reasoning on (as variator.reasoning.calls)
TIMEOUT_S = 900.0  # one Ultra call with reasoning can take minutes
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
MONTHS = (0, 11)  # month of an intervention: 0 = T (the fork date) .. 11


# ------------------------------------------------------------------------------------------ catalogue
@dataclass(frozen=True)
class Range:
    low: float
    high: float
    unit: str
    meaning: str
    integer: bool = False

    def check(self, x: float) -> bool:
        return self.low <= x <= self.high and (not self.integer or float(x).is_integer())

    def text(self) -> str:
        kind = "integer " if self.integer else ""
        return f"{kind}{_num(self.low)}..{_num(self.high)} {self.unit}: {self.meaning}"


@dataclass(frozen=True)
class Lever:
    name: str
    kind: str
    op: str
    target: str  # "role:{role}" for hire
    meaning: str
    value: Range
    params: Mapping[str, Range]


HIRE_ROLES = ("backend", "frontend", "devops", "qa", "support")

LEVERS: dict[str, Lever] = {
    "debt": Lever(
        "debt", "factor", "set", "capacity.debt_share",
        "spend a share of the engineering capacity on paying down technical debt from the month on; "
        "after a lead time the debt index falls, which lowers incidents and latency; the roadmap gets less capacity",
        Range(0.05, 0.5, "share of capacity", "share of the engineering capacity moved to debt work"),
        {"debt_cut_per_year": Range(0.1, 1.0, "share per year", "share of the technical debt index removed per year of debt work")},
    ),
    "hire": Lever(
        "hire", "actor", "add", "role:{role}",
        "open requisitions for new staff in the month; each hire joins after the hiring lead time, ramps up "
        "and adds capacity; the salary starts on the join date",
        Range(1, 10, "people", "number of hires", integer=True),
        {"cost_per_year": Range(60_000, 250_000, "USD per year per hire", "loaded salary of one hire")},
    ),
    "gpu": Lever(
        "gpu", "actor", "add", "vendor:second_cloud",
        "order a GPU reservation on a second cloud in the month; after a setup time it shields the company "
        "when GPUs run short (latency, incidents, infra cost); the reservation is paid from the order month",
        Range(50_000, 1_000_000, "USD per year", "cost of the reservation"),
        {"relief": Range(0.2, 0.95, "share", "share of the impact of GPUs running short removed once the reserve is ready")},
    ),
    "price": Lever(
        "price", "factor", "scale", "price.list",
        "raise the list prices in the month; each contract takes the new price after its notice period, new "
        "deals at once; churn and lost deals rise with the price elasticity, NPS falls",
        Range(0.01, 0.25, "relative change", "list price increase, 0.05 = +5%"),
        {},
    ),
}  # fmt: skip
HIRE_PARAMS = ("role", "cost_per_year")  # role: one of HIRE_ROLES


@dataclass(frozen=True)
class WorldTarget:
    target: (
        str  # the engine target; "{risk}" = a risk name of the draft, "{variable}", "{client}", "{person}"
    )
    kind: str
    op: str
    value: Range
    dated: bool = False  # the month matters (otherwise 0)


WORLD: tuple[WorldTarget, ...] = (
    WorldTarget("shock:{risk}", "habit", "probability", Range(0.05, 0.9, "probability", "extra probability within the year that the risk of this draft occurs (joined with its own p as a union)")),
    WorldTarget("shock:{risk}.size", "habit", "scale", Range(0.5, 3.0, "multiplier", "multiplier on the size of the risk of this draft")),
    WorldTarget("clients:key.notice_any", "habit", "probability", Range(0.05, 0.9, "probability", "probability that a key client gives notice within the horizon (joined with the learned churn as a union)")),
    WorldTarget("staff.blocked_share", "habit", "scale", Range(0.5, 3.0, "multiplier", "multiplier on the share of engineering time lost to blockers (capped at 90%)")),
    WorldTarget("demand.engineering", "factor", "scale", Range(0.7, 1.5, "multiplier", "multiplier on the engineering work demanded by clients")),
    WorldTarget("hiring.lead_months", "habit", "shift", Range(-1.0, 6.0, "months", "months added to the time to fill a role")),
    WorldTarget("staff.resign", "habit", "scale", Range(0.5, 3.0, "multiplier", "multiplier on the resignation hazard of the staff")),
    WorldTarget("cash.start", "factor", "scale", Range(0.3, 1.5, "multiplier", "multiplier on the cash at T")),
    WorldTarget("clients.pay_delay_days", "habit", "shift", Range(-15.0, 90.0, "days", "days added to the client payment delay")),
    WorldTarget("deals.rate", "habit", "scale", Range(0.3, 1.5, "multiplier", "multiplier on the arrival rate of new deals")),
    WorldTarget("tax.payroll", "factor", "shift", Range(-0.05, 0.1, "share of payroll", "change of the payroll tax rate from the month on, 0.02 = +2 pp"), dated=True),
    WorldTarget("placed:shock:{variable}", "event", "add", Range(0.0, 1e9, "the shock unit", "a certain shock of this variable in the month; the value is its size, in the range of the shock below"), dated=True),
    WorldTarget("placed:client:{client}", "event", "add", Range(0.0, 0.0, "", "this key client certainly gives notice in the month; value 0"), dated=True),
    WorldTarget("placed:person:{person}", "event", "add", Range(0.0, 0.0, "", "this person certainly resigns in the month; value 0"), dated=True),
)  # fmt: skip


@dataclass(frozen=True)
class ShockSpec:
    variable: str
    meaning: str
    size: Range


SHOCKS: dict[str, ShockSpec] = {
    s.variable: s
    for s in (
        ShockSpec("gpu_shortage", "the cloud vendor runs short of GPU capacity: latency, incidents and infra cost rise unless a GPU reserve relieves it", Range(0.05, 1.0, "severity", "share of the GPU capacity hit")),
        ShockSpec("vendor_price", "the cloud vendor raises its prices", Range(0.02, 0.6, "relative change", "cloud cost increase, 0.1 = +10%")),
        ShockSpec("fx_shock", "a currency move raises the bills of the offshore vendors", Range(0.02, 0.4, "relative change", "increase of the offshore vendor bills")),
        ShockSpec("regulation_cost", "a new regulation adds a compliance cost", Range(10_000, 500_000, "USD per year", "compliance cost")),
        ShockSpec("competitor_cut", "a competitor cuts its prices: churn rises with the price elasticity and fewer deals are won", Range(0.02, 0.4, "share", "competitor price cut")),
        ShockSpec("reputation_hit", "a public failure hurts the reputation: every client is more likely to leave", Range(0.02, 0.5, "hazard share", "extra yearly churn probability of each client")),
        ShockSpec("demand_growth", "the market turns: the arrival rate of new deals changes (negative = fewer deals)", Range(-0.6, 0.3, "relative change", "change of the new-deal rate")),
    )
}  # fmt: skip
SHOCK_P = Range(0.02, 0.9, "probability", "probability that the risk occurs within the year")
assert not set(SHOCKS) & ACTOR_SHOCKS  # the actors carry key people and key accounts leaving


def _num(x: float) -> str:
    if float(x).is_integer() and abs(x) >= 1000:
        return f"{int(x):,}"
    return f"{x:g}"


# --------------------------------------------------------------------------------------------- schema
class DraftIntervention(BaseModel):
    lever: Literal["debt", "hire", "gpu", "price"]
    month: int = Field(description="month from T when the action starts, 0..11")
    value: float = Field(description="the lever value in the unit of the catalogue")
    params: dict[str, float | str] = Field(default_factory=dict, description="the lever's params")
    label: str = Field(default="", description="one line: what is done")


class DraftFork(BaseModel):
    id: str = Field(description="snake_case id")
    label: str = Field(description="short name, at most 6 words")
    rationale: str = Field(description="why this fork answers the question; which numbers are assumptions")
    interventions: list[DraftIntervention] = Field(default_factory=list, max_length=4)


class DraftEffect(BaseModel):
    target: str = Field(description="a target of the world catalogue, placeholders filled in")
    value: float
    month: int = Field(default=0, description="month from T, 0..11; matters for dated targets only")


class DraftHypothesis(BaseModel):
    id: str = Field(description="snake_case id")
    label: str
    rationale: str
    effects: list[DraftEffect] = Field(min_length=1, max_length=4)


class Magnitude(BaseModel):
    kind: Literal["pert"] = "pert"
    low: float
    mode: float
    high: float


class DraftRisk(BaseModel):
    name: str = Field(description="snake_case name, unique")
    variable: str = Field(description="a shock variable of the catalogue, each at most once")
    p: float = Field(description="probability within the year")
    magnitude: Magnitude
    desc: str


class CaseDraft(BaseModel):
    """What Nemotron 3 Ultra drafts from the question: forks, world hypotheses, risks."""

    forks: list[DraftFork] = Field(min_length=3, max_length=6)
    hypotheses: list[DraftHypothesis] = Field(min_length=3, max_length=8)
    risks: list[DraftRisk] = Field(min_length=3, max_length=8)


# ----------------------------------------------------------------------------------------- validation
def load_facts(path: Path = FACTS_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _ids(facts: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    clients = {str(c["id"]) for c in facts.get("keyClients", [])}
    people = {str(p["id"]) for p in facts.get("people", [])}
    return clients, people


def _resolve(
    target: str, risks: Mapping[str, str], facts: Mapping[str, Any]
) -> tuple[WorldTarget | None, str]:
    """The catalogue entry of a hypothesis target, or None and the reason."""
    clients, people = _ids(facts)
    for w in WORLD:
        if "{" not in w.target:
            if target == w.target:
                return w, ""
            continue
        prefix, rest = w.target.split("{", 1)
        suffix = rest.split("}", 1)[1]
        if not (target.startswith(prefix) and target.endswith(suffix)) or len(target) <= len(prefix) + len(
            suffix
        ):
            continue
        key = target[len(prefix) : len(target) - len(suffix)]
        if "{risk}" in w.target and "." in key and not suffix:
            continue  # "shock:x.size" belongs to the size entry
        if "{risk}" in w.target:
            return (w, "") if key in risks else (None, f"{target}: no risk named {key!r} in this draft")
        if "{variable}" in w.target:
            return (w, "") if key in SHOCKS else (None, f"{target}: unknown shock variable {key!r}")
        if "{client}" in w.target:
            return (w, "") if key in clients else (None, f"{target}: unknown key client {key!r}")
        if "{person}" in w.target:
            return (w, "") if key in people else (None, f"{target}: unknown person {key!r}")
    return None, f"{target}: not a target of the world catalogue"


def validate_draft(draft: CaseDraft, facts: Mapping[str, Any]) -> list[str]:
    """Every way the draft leaves the catalogue; empty when the engine can run it as drafted."""
    err: list[str] = []

    def ids(kind: str, values: Sequence[str]) -> None:
        for v in values:
            if not ID_RE.match(v):
                err.append(f"{kind} {v!r}: id must be snake_case (a-z, 0-9, _), 2..48 characters")
        if len(set(values)) != len(values):
            err.append(f"{kind} ids must be unique: {list(values)}")

    # forks
    ids("fork", [f.id for f in draft.forks])
    keep = [f.id for f in draft.forks if not f.interventions]
    if len(keep) != 1:
        err.append(f"exactly one fork must keep the plan (no interventions); found {len(keep)}: {keep}")
    for f in draft.forks:
        seen: set[str] = set()
        for i in f.interventions:
            where = f"fork {f.id}, lever {i.lever}"
            if i.lever in seen:
                err.append(f"{where}: at most one intervention per lever in a fork (the twin keeps one)")
            seen.add(i.lever)
            if not MONTHS[0] <= i.month <= MONTHS[1]:
                err.append(f"{where}: month {i.month} outside {MONTHS[0]}..{MONTHS[1]}")
            lever = LEVERS[i.lever]
            if not lever.value.check(i.value):
                err.append(f"{where}: value {i.value:g} outside {lever.value.text()}")
            allowed = set(lever.params) | ({"role"} if i.lever == "hire" else set())
            for k in sorted(set(i.params) - allowed):
                err.append(f"{where}: unknown param {k!r} (allowed: {sorted(allowed) or 'none'})")
            for k in sorted(allowed - set(i.params)):
                err.append(f"{where}: missing param {k!r}")
            for k, r in lever.params.items():
                x = i.params.get(k)
                if k in i.params and (not isinstance(x, int | float) or not r.check(float(x))):
                    err.append(f"{where}: param {k} = {x!r} outside {r.text()}")
            if i.lever == "hire" and "role" in i.params and i.params["role"] not in HIRE_ROLES:
                err.append(f"{where}: role {i.params['role']!r} not one of {list(HIRE_ROLES)}")
    # risks
    ids("risk", [r.name for r in draft.risks])
    variables = [r.variable for r in draft.risks]
    for r in draft.risks:
        where = f"risk {r.name}"
        spec = SHOCKS.get(r.variable)
        if spec is None:
            err.append(f"{where}: unknown variable {r.variable!r} (allowed: {sorted(SHOCKS)})")
            continue
        if variables.count(r.variable) > 1:
            err.append(f"{where}: variable {r.variable} used by more than one risk (the twin keeps one)")
        if not SHOCK_P.check(r.p):
            err.append(f"{where}: p {r.p:g} outside {SHOCK_P.text()}")
        m = r.magnitude
        if not (m.low <= m.mode <= m.high and m.low < m.high):
            err.append(f"{where}: magnitude needs low <= mode <= high and low < high")
        for k, x in (("low", m.low), ("mode", m.mode), ("high", m.high)):
            if not spec.size.check(x):
                err.append(f"{where}: magnitude {k} {x:g} outside {spec.size.text()}")
    # hypotheses
    ids("hypothesis", [h.id for h in draft.hypotheses])
    risks = {r.name: r.variable for r in draft.risks}
    for h in draft.hypotheses:
        for e in h.effects:
            where = f"hypothesis {h.id}, target {e.target}"
            w, why = _resolve(e.target, risks, facts)
            if w is None:
                err.append(f"hypothesis {h.id}: {why}")
                continue
            if not MONTHS[0] <= e.month <= MONTHS[1]:
                err.append(f"{where}: month {e.month} outside {MONTHS[0]}..{MONTHS[1]}")
            if w.target == "placed:shock:{variable}":
                size = SHOCKS[e.target.split(":", 2)[2]].size
                if not size.check(e.value):
                    err.append(f"{where}: value {e.value:g} outside {size.text()}")
            elif not w.value.check(e.value):
                err.append(f"{where}: value {e.value:g} outside {w.value.text()}")
    return err


# --------------------------------------------------------------------------------------------- config
def _lever_row(i: DraftIntervention) -> dict[str, Any]:
    lever = LEVERS[i.lever]
    params: dict[str, Any] = {k: float(i.params[k]) for k in lever.params}
    target = lever.target
    value = float(i.value)
    if i.lever == "hire":
        role = str(i.params["role"])
        target = f"role:{role}"
        params["cohort"] = f"future:{role}"
        value = float(int(value))
    return {
        "kind": lever.kind, "op": lever.op, "target": target, "at": float(i.month), "value": value,
        "params": params, "source": "decision", "label": i.label,
    }  # fmt: skip


def _effect_row(
    e: DraftEffect, risks: Mapping[str, str], facts: Mapping[str, Any], label: str
) -> dict[str, Any]:
    w, _ = _resolve(e.target, risks, facts)
    assert w is not None, e.target
    return {
        "kind": w.kind, "op": w.op, "target": e.target, "at": float(e.month if w.dated else 0), "value": float(e.value),
        "params": {}, "source": "hypothesis" if w.kind != "event" else "event", "label": label,
    }  # fmt: skip


def to_case_config(
    draft: CaseDraft, base_cfg: Mapping[str, Any], facts: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """A copy of the case config with the drafted ``forks``, a new ``layers`` list and the drafted ``risks``;
    everything else (criteria, weights, presets, people, ...) unchanged."""
    facts = load_facts() if facts is None else facts
    cfg = copy.deepcopy(dict(base_cfg))
    risks = {r.name: r.variable for r in draft.risks}
    cfg["forks"] = [
        {"id": f.id, "label": f.label, "interventions": [_lever_row(i) for i in f.interventions]}
        for f in draft.forks
    ]
    cfg["layers"] = [
        {
            "id": h.id,
            "label": h.label,
            "interventions": [_effect_row(e, risks, facts, h.label) for e in h.effects],
        }
        for h in draft.hypotheses
    ]
    cfg["risks"] = [
        {
            "name": r.name, "variable": r.variable, "p": float(r.p), "desc": r.desc,
            "magnitude": {"kind": "pert", "low": float(r.magnitude.low), "mode": float(r.magnitude.mode), "high": float(r.magnitude.high)},
        }
        for r in draft.risks
    ]  # fmt: skip
    return cfg


# --------------------------------------------------------------------------------------------- prompt
def render_facts(facts: Mapping[str, Any]) -> str:
    m = facts.get("metricsAtT", {})
    depts: dict[str, int] = {}
    for p in facts.get("people", []):
        depts[p.get("department", "")] = depts.get(p.get("department", ""), 0) + 1
    lines = [
        f"Company: {facts.get('company')}; the fork date T = {facts.get('asOf')} (month 0); horizon 12 months.",
        f"Metrics at T: ARR {_num(m.get('rev.arr', 0))} USD; cash {_num(m.get('fin.cash', 0))} USD; "
        f"yearly churn {m.get('rev.churn', 0):.1%}; SLA {m.get('ops.sla', 0):.3%}; latency {m.get('ops.latency', 0):g} ms; "
        f"NPS {m.get('cust.nps', 0):g}; incidents {m.get('ops.incidents', 0):g} per month; MTTR {m.get('ops.mttr', 0):g} h; "
        f"deploys {m.get('ops.deploys', 0):g} per month; engineering load {m.get('ops.team_load', 0):.0%} of capacity; "
        f"DSO {m.get('fin.dso', 0):g} days.",
        f"Payroll at T: {_num(facts.get('payrollAtT', 0))} USD per year; headcount {len(facts.get('people', []))} "
        f"({', '.join(f'{k} {v}' for k, v in depts.items())}).",
        "People (id, title): " + "; ".join(f"{p['id']} {p['title']}" for p in facts.get("people", [])) + ".",
        "Key clients (id, name, ACV USD, ARR at T USD, renewal in months): "
        + "; ".join(
            f"{c['id']} {c['name']}, {_num(c.get('acv', 0))}, {_num(c.get('arrAtT', 0))}, {c.get('renewalInMonths') if c.get('renewalInMonths') is not None else 'n/a'}"
            for c in facts.get("keyClients", [])
        )
        + f"; plus {facts.get('otherClients', 0)} other clients.",
        "Vendors (id, name, kind): " + "; ".join(f"{v['id']} {v['name']}, {v['kind']}" for v in facts.get("vendors", [])) + ".",
    ]  # fmt: skip
    return "\n".join(f"- {x}" for x in lines)


def render_catalogue() -> str:
    out = ["Levers of a fork (one intervention per lever and fork; month 0..11):"]
    for lv in LEVERS.values():
        ps = "; ".join(f"{k} {r.text()}" for k, r in lv.params.items())
        if lv.name == "hire":
            ps = f"role one of {', '.join(HIRE_ROLES)}; " + ps
        out.append(f"- {lv.name}: {lv.meaning}. value {lv.value.text()}. params: {ps or 'none ({})'}.")
    out.append("World targets of a hypothesis (value range; month only where dated, else 0):")
    for w in WORLD:
        what = w.value.meaning if w.kind == "event" else w.value.text()
        out.append(f"- {w.target}: {what}{' (dated)' if w.dated else ''}")
    out.append(
        "Shock variables of a risk (each at most once; magnitude low/mode/high in this range; p "
        f"{SHOCK_P.low:g}..{SHOCK_P.high:g} per year):"
    )
    for s in SHOCKS.values():
        out.append(f"- {s.variable}: {s.meaning}. size {s.size.text()}")
    return "\n".join(out)


RULES = """Rules:
- The digital twin models only the levers, world targets and shock variables of the catalogue. Use nothing else.
- Draft 3 to 6 forks that answer the question. Exactly one fork changes nothing: it has no interventions.
  A fork may combine levers only if the question asks for a combination.
- Every number comes from the question or the facts; where neither gives it, choose an assumption inside the
  catalogue range and say in the rationale that it is an assumption.
- Draft 3 to 8 world hypotheses: what may be true about the world in the next 12 months (not decisions), each
  with 1 to 4 effects on world targets. Fill the placeholders: {risk} = a risk name of this draft,
  {variable} = a shock variable, {client} = a key client id, {person} = a person id.
- Draft 3 to 8 risks: external events that may hit within the year, each on its own shock variable, with a
  probability p and a PERT magnitude (kind "pert", low <= mode <= high, low < high).
- Ids and risk names are snake_case."""


def build_prompt(question: str, facts: Mapping[str, Any]) -> str:
    """The prompt of one draft: the question, the facts at T, the catalogue and the rules (no case values)."""
    return "\n\n".join(
        [
            "You design the decision case for a digital twin of an IT services company. The leader asks a question; "
            "draft the forks (the options to compare), the world hypotheses and the risks the twin will simulate "
            "month by month for 12 months.",
            "Question of the leader:\n" + question.strip(),
            "Facts at T:\n" + render_facts(facts),
            "Catalogue:\n" + render_catalogue(),
            RULES,
        ]
    )


# ---------------------------------------------------------------------------------------------- draft
@dataclass
class DraftResult:
    draft: CaseDraft | None
    raw: str  # the last reply
    attempts: list[dict[str, Any]]  # per call: latency_s, tokens, finish_reason, errors
    errors: list[str]  # of the last attempt; empty when valid
    model: str = ""

    @property
    def ok(self) -> bool:
        return self.draft is not None and not self.errors


def _parse(content: str, facts: Mapping[str, Any]) -> tuple[CaseDraft | None, list[str]]:
    try:
        draft = CaseDraft.model_validate_json(extract_json(content))
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(x) for x in e['loc']) or 'json'}: {e['msg']}"
            for e in exc.errors(include_url=False)
        ][:30]
    except ValueError as exc:
        return None, [f"json: {exc}"]
    return draft, validate_draft(draft, facts)


def draft_case(
    client: TokenFactoryClient,
    question: str,
    facts: Mapping[str, Any],
    *,
    model: str = ROLE_MODEL["case_design"],
    reasoning: bool = True,
    max_attempts: int = 2,
    max_tokens: int = MAX_TOKENS,
) -> DraftResult:
    """One draft: an Ultra call; an invalid reply is retried once with the errors appended."""
    schema = CaseDraft.model_json_schema()
    system = (
        "Reply with a single JSON object that matches this JSON Schema. No prose, no code fences.\n"
        + json.dumps(schema, ensure_ascii=False)
    )
    convo: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        user_message(build_prompt(question, facts)),
    ]
    attempts: list[dict[str, Any]] = []
    draft, errors, content, model_id = None, ["no reply"], "", client.models.model_id(model)
    for _ in range(max_attempts):
        started = time.perf_counter()
        res = client._structured_call(  # noqa: SLF001 - the json_schema / json_object switch of the client
            model, convo, "CaseDraft", schema, reasoning=reasoning, max_tokens=max_tokens,
            **recommended_sampling(reasoning=reasoning),
        )  # fmt: skip
        latency = time.perf_counter() - started
        content, model_id = res.content, res.model or model_id
        draft, errors = _parse(content, facts)
        attempts.append(
            {
                "latency_s": round(latency, 2),
                "prompt_tokens": res.usage.prompt_tokens,
                "completion_tokens": res.usage.completion_tokens,
                "finish_reason": res.finish_reason,
                "errors": errors,
            }
        )
        if draft is not None and not errors:
            break
        convo = [
            *convo,
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": "The draft is invalid:\n- "
                + "\n- ".join(errors)[:4000]
                + "\nReturn the corrected JSON object only.",
            },
        ]
    return DraftResult(draft if not errors else None, content, attempts, errors, model_id)


# ------------------------------------------------------------------------------------------------ CLI
def _write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = data if isinstance(data, str) else json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run_one(client: TokenFactoryClient, question: str, run_dir: Path, base_cfg: Mapping[str, Any],
            facts: Mapping[str, Any], model: str) -> dict[str, Any]:  # fmt: skip
    """One run directory: question.txt, draft.json, config.json (valid only), llm.json (written last)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    _write(run_dir / "question.txt", question.strip() + "\n")
    started = time.perf_counter()
    info: dict[str, Any] = {"model_key": model, "question": question.strip()}
    try:
        res = draft_case(client, question, facts, model=model)
    except Exception as exc:  # noqa: BLE001 - a transport failure is recorded, and retried on --resume
        info.update(status="error", error=f"{type(exc).__name__}: {exc}"[:2000],
                    latency_s=round(time.perf_counter() - started, 2))  # fmt: skip
        _write(run_dir / "llm.json", info)
        return info
    if res.ok:
        assert res.draft is not None
        _write(run_dir / "draft.json", res.draft.model_dump(mode="json"))
        _write(run_dir / "config.json", to_case_config(res.draft, base_cfg, facts))
    else:
        try:
            raw: Any = json.loads(extract_json(res.raw))
        except ValueError:
            raw = res.raw
        _write(run_dir / "draft.json", {"invalid": True, "reply": raw})
    info.update(
        status="ok" if res.ok else "invalid",
        model=res.model,
        sampling=recommended_sampling(reasoning=True),
        attempts=len(res.attempts),
        latency_s=round(sum(a["latency_s"] for a in res.attempts), 2),
        prompt_tokens=sum(a["prompt_tokens"] for a in res.attempts),
        completion_tokens=sum(a["completion_tokens"] for a in res.attempts),
        calls=res.attempts,
        errors=res.errors,
    )
    _write(run_dir / "llm.json", info)
    return info


def _done(run_dir: Path) -> bool:
    """A run that needs no new call: valid or invalid (a failed draft is never re-drawn); transport errors are retried."""
    p = run_dir / "llm.json"
    if not p.is_file():
        return False
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("status") in ("ok", "invalid")
    except ValueError:
        return False


def generate(args: argparse.Namespace) -> int:
    from variator.config import Settings

    questions = [
        q.strip() for q in Path(args.questions).read_text(encoding="utf-8").splitlines() if q.strip()
    ]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base_cfg = json.loads((cases_dir() / f"{CASE}.json").read_text(encoding="utf-8"))
    facts = load_facts()
    jobs = [
        (qi * args.per_question + k, q) for qi, q in enumerate(questions) for k in range(args.per_question)
    ]
    if args.limit:
        jobs = jobs[: args.limit]
    todo = [(n, q) for n, q in jobs if not (args.resume and _done(out / f"run-{n:03d}"))]
    if not args.resume and any((out / f"run-{n:03d}" / "llm.json").exists() for n, _ in todo):
        print(f"{out}: runs exist; pass --resume", file=sys.stderr)
        return 2
    settings = Settings.from_env()
    if not os.environ.get("VARIATOR_TIMEOUT_S"):
        settings = replace(settings, request_timeout_s=TIMEOUT_S)
    client = TokenFactoryClient(settings)
    lock = threading.Lock()
    counts = {"ok": 0, "invalid": 0, "error": 0}
    print(
        f"{len(jobs)} runs, {len(jobs) - len(todo)} done, {len(todo)} to draft, {args.workers} workers",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {
            pool.submit(run_one, client, q, out / f"run-{n:03d}", base_cfg, facts, args.model): n
            for n, q in todo
        }
        for fut in as_completed(futs):
            info = fut.result()
            with lock:
                counts[info["status"]] += 1
                print(f"run-{futs[fut]:03d} {info['status']} {info.get('latency_s', 0):.0f}s "
                      f"attempts={info.get('attempts', '-')} {counts}", flush=True)  # fmt: skip
    return 0 if counts["error"] == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m variator.future.design", description=__doc__.split("\n\n")[0]
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="draft the case from each question, per-question times")
    g.add_argument("--questions", required=True, help="text file, one question per line")
    g.add_argument("--per-question", type=int, default=20)
    g.add_argument("--workers", type=int, default=8)
    g.add_argument("--out", required=True)
    g.add_argument("--resume", action="store_true", help="skip runs with a valid or invalid draft")
    g.add_argument("--limit", type=int, default=0, help="only the first N runs (a smoke test)")
    g.add_argument("--model", default=ROLE_MODEL["case_design"])
    p = sub.add_parser("prompt", help="print the prompt of a question (no model call)")
    p.add_argument("question")
    args = ap.parse_args(argv)
    if args.cmd == "prompt":
        print(build_prompt(args.question, load_facts()))
        return 0
    return generate(args)


if __name__ == "__main__":
    sys.exit(main())
