"""The dated interventions of a fork of case B (``workspace/config/cases/company_best_year.json``).

Each fork of the case config names its lever (``levers``) and its timeline items (start month and
the milestone when it takes effect). The sizes come from the case variables; a variable with a PERT
is drawn once per scenario (the same draw in every fork: common random numbers). The effect
formulas follow the ``derived`` formulas of the case, applied day by day:

- ``pay_tech_debt``: ``debt_share`` of the capacity goes to debt work from ``m_debt``; from
  ``m_debt + debt_lead`` the debt index falls by 40%: incidents x (1 + k_inc_debt x debt) and latency
  x (1 + k_lat_debt x debt) fall with it (ramp of 20 working days);
- ``hire_5``: ``hire_count`` requisitions open at ``m_hire``; each hire starts after a learned
  hiring lead; the planned load falls by 1.2% per productive hire (case ``load``);
- ``reserve_gpu``: + ``gpu_reserve_cost`` a year from ``m_gpu``; from ``m_gpu + gpu_setup`` a GPU
  shortage hits 70% less;
- ``raise_prices_8``: invoices x (1 + price_change) from ``m_price + price_notice``; churn hazard
  + k_churn_price x price_change; win rate x (1 - k_win_price x price_change).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from variator.orgsim.calendar import add_months
from variator.orgsim.company.forward.params import pert

DEFAULT_CASE = (
    Path(__file__).resolve().parents[5] / "workspace" / "config" / "cases" / "company_best_year.json"
)


def month_date(anchor: date, months: float) -> date:
    """The date ``months`` after ``anchor`` (fraction of a month = 30.4 days)."""
    whole = int(np.floor(months))
    d = add_months(anchor, whole)
    return d + timedelta(days=int(round((months - whole) * 30.4)))


@dataclass
class Fork:
    id: str
    label: str
    description: str
    levers: dict[str, float]
    vars: dict[str, float] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    dates: dict[str, date] = field(default_factory=dict)

    @property
    def baseline(self) -> bool:
        return not self.levers

    def lever(self, name: str) -> float:
        return float(self.levers.get(name, 0.0))

    # ------------------------------------------------------------------ effects by day
    def _ramp(self, d: date, start: date | None, bdays: int = 20) -> float:
        if start is None or d < start:
            return 0.0
        return min(1.0, ((d - start).days + 1) / (bdays * 7 / 5))

    def debt_share(self, d: date) -> float:
        """Share of the engineering capacity that goes to debt work on day ``d``."""
        if self.lever("lever_debt") <= 0 or d < self.dates["debt_start"]:
            return 0.0
        return self.vars["debt_share"] * self.lever("lever_debt")

    def debt_index(self, d: date) -> float:
        base = self.vars["tech_debt"]
        if self.lever("lever_debt") <= 0:
            return base
        return base * (1 - 0.4 * self.lever("lever_debt") * self._ramp(d, self.dates["debt_ready"]))

    def incident_factor(self, d: date) -> float:
        k, base = self.vars["k_inc_debt"], self.vars["tech_debt"]
        return (1 + k * self.debt_index(d)) / (1 + k * base)

    def latency_factor(self, d: date) -> float:
        k, base = self.vars["k_lat_debt"], self.vars["tech_debt"]
        return (1 + k * self.debt_index(d)) / (1 + k * base)

    def gpu_factor(self, d: date) -> float:
        """Multiplier of a GPU shortage hit (the reserved capacity takes 70% of it once ready)."""
        if self.lever("lever_gpu") <= 0:
            return 1.0
        return 1 - 0.7 * self.lever("lever_gpu") * self._ramp(d, self.dates["gpu_ready"], 5)

    def gpu_reserve_per_year(self, d: date) -> float:
        if self.lever("lever_gpu") <= 0 or d < self.dates["gpu_start"]:
            return 0.0
        return self.vars["gpu_reserve_cost"] * self.lever("lever_gpu")

    def price_factor(self, d: date) -> float:
        if self.lever("price_change") == 0 or d < self.dates["price_live"]:
            return 1.0
        return 1 + self.lever("price_change")

    def churn_add(self, d: date) -> float:
        """Annual churn probability added by the price change (from the announcement)."""
        if self.lever("price_change") == 0 or d < self.dates["price_start"]:
            return 0.0
        return self.vars["k_churn_price"] * self.lever("price_change")

    def win_factor(self, d: date) -> float:
        if self.lever("price_change") == 0 or d < self.dates["price_live"]:
            return 1.0
        return 1 - self.vars["k_win_price"] * self.lever("price_change")

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "levers": self.levers,
            "vars": {k: round(v, 4) for k, v in sorted(self.vars.items())},
            "sources": dict(sorted(self.sources.items())),
            "dates": {k: v.isoformat() for k, v in sorted(self.dates.items())},
        }


class Case:
    """The case config: forks, variables and their distributions."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_CASE
        self.cfg = json.loads(self.path.read_text(encoding="utf-8"))
        self.id = self.cfg["id"]
        self.variables = {v["name"]: v for v in self.cfg["variables"]}
        self.forks = {f["id"]: f for f in self.cfg["forks"]}

    def var(self, name: str, rng: np.random.Generator | None) -> tuple[float, str]:
        v = self.variables[name]
        dist = v["dist"]
        src = f"case:{name} ({v.get('source', '')})"
        if dist["kind"] == "fixed":
            return float(dist["value"]), src
        if dist["kind"] == "pert":
            if rng is None:
                return float(dist["mode"]), src + " mode"
            return pert(rng, dist["low"], dist["mode"], dist["high"]), src + " PERT draw"
        raise ValueError(f"case variable {name}: unknown distribution {dist['kind']}")

    def fork(self, fork_id: str, anchor: date, streams: Any, *, draw: bool = True) -> Fork:
        """The fork with its dates for one scenario; ``streams`` gives the scenario's keyed generators."""
        f = self.forks[fork_id]
        fk = Fork(f["id"], f["label"], f.get("description", ""), dict(f.get("levers") or {}))

        def v(name: str) -> float:
            rng = streams.rng("fork", "case", name) if draw else None
            x, src = self.var(name, rng)
            fk.vars[name] = x
            fk.sources[name] = src
            return x

        for name in (
            "tech_debt",
            "k_inc_debt",
            "k_lat_debt",
            "debt_share",
            "hire_count",
            "gpu_reserve_cost",
            "k_churn_price",
            "k_win_price",
        ):
            if name in self.variables:
                v(name)
        if fk.lever("lever_debt") > 0:
            fk.dates["debt_start"] = month_date(anchor, v("m_debt"))
            fk.dates["debt_ready"] = month_date(anchor, fk.vars["m_debt"] + v("debt_lead"))
        if fk.lever("lever_hire") > 0:
            fk.dates["hire_start"] = month_date(anchor, v("m_hire"))
        if fk.lever("lever_gpu") > 0:
            fk.dates["gpu_start"] = month_date(anchor, v("m_gpu"))
            fk.dates["gpu_ready"] = month_date(anchor, fk.vars["m_gpu"] + v("gpu_setup"))
        if fk.lever("price_change") != 0:
            fk.dates["price_start"] = month_date(anchor, v("m_price"))
            fk.dates["price_live"] = month_date(anchor, fk.vars["m_price"] + v("price_notice"))
        return fk
