"""The case layer of one case page: what the template export does not say (read by export_case.py).

Person ids are the HR ids of the company history (LW-E06); the config adds the knowledge-base id
(person.lw-e06). Entity ids are in the exact knowledge-base form (org.CL-001, org.VN-03, person.lw-e06).
Item metadata (``items``), per timeline item name of a plan item or an event:
    kind is taken from the timeline; lane, owner, person, entity, affects (model ids), shock (risk name),
    value {variable, label, min, max, step, option?} (the editable size or amount), range (variable of the
    uncertain lead time or size), label and description (override the template text).
Shock metadata (``shocks``), per risk name: lane, owner, person, entity, affects, event (the timeline
item), label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CaseSpec:
    id: str
    letter: str
    title: str
    company: str
    #: The decision question for people (overrides the model question; names the company).
    question: str
    lead: str
    #: Person id of the case owner.
    owner: str
    template: str
    #: Page text for ``export_model`` (labels, risk headlines, chain, ...).
    page: dict[str, Any]
    #: "ready" or "preparing" (the cases list).
    status: str
    fork_date: str
    horizon: int
    #: (HR id, name, title); the owner first.
    people: list[tuple[str, str, str]]
    #: (id, label, description, weights or None = the model defaults).
    priorities: list[tuple[str, str, str, dict[str, float] | None]]
    #: Option -> recompute class (overlay, from_date, structural) or None for the baseline.
    recompute: dict[str, str | None]
    items: dict[str, dict[str, Any]]
    shocks: dict[str, dict[str, Any]]
    #: Model id -> short label for the "affects" chips.
    short_labels: dict[str, str]
    chain_path: list[list[str]]
    chain_higher: dict[str, bool]
    #: (block id, label) in display order.
    blocks: list[tuple[str, str]]
    #: Criterion -> (variable name or a number, label format with {usd}, {pct}, {pct1}, {value}).
    goals: dict[str, tuple[str | float, str]]
    #: Best-path risk ref -> risk-calendar row id.
    risk_rows: dict[str, str]
    #: Monthly preview metrics (help "metric.<id>"): id -> (title, description).
    preview_metrics: dict[str, tuple[str, str]]
    #: Risk-calendar rows (help "check.<id>"): id -> (title, description).
    calendar_rows: dict[str, tuple[str, str]]
    reference_presets: tuple[str, ...]
    fork_icons: dict[str, str] = field(default_factory=dict)
    fork_descriptions: dict[str, str] = field(default_factory=dict)
    hypothesis_icons: dict[str, str] = field(default_factory=dict)
    #: Variable -> block (for a template that exports no blocks).
    variable_blocks: dict[str, str] = field(default_factory=dict)
    #: Inputs a fork moves although no override, op, item or shock names them (for example the debt index).
    touched: tuple[str, ...] = ()
    #: Variable -> fixed default of this case (the template keeps its own default for other uses).
    defaults: dict[str, float] = field(default_factory=dict)
    #: Variable -> distribution of this case (``domain.Distribution``; for example calibrated to T).
    dists: dict[str, Any] = field(default_factory=dict)
    #: Variable -> source text of this case (the help shows it), for example "history at T: rev.churn = 0.12".
    sources: dict[str, str] = field(default_factory=dict)
    #: Section id -> overrides of title, question, summary.
    sections: dict[str, dict[str, str]] = field(default_factory=dict)
    section_help: dict[str, str] = field(default_factory=dict)
    step_help: dict[str, str] = field(default_factory=dict)
    kpi_help: dict[str, tuple[str, ...]] = field(default_factory=dict)
    control_help: dict[str, tuple[str, ...]] = field(default_factory=dict)
