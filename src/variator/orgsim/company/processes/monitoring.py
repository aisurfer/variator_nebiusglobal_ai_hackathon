"""P13 monitoring: the daily p95 latency, availability and error rate of each production service.

The series are datasets, not knowledge-base records (no ``sim`` lines).

- Services: the platform components that serve traffic (``SERVICES``, with their traffic
  shares). An incident takes down the services of its footprint (``footprint``): the
  service of its component, plus the service that depends on it in a SEV1 (and sometimes
  in a SEV2). An ``infrastructure`` incident takes down 1 or 2 services. On average an
  incident takes down about 10% of the traffic for its restore time, the 0.1 of the
  company_model SLA formula.
- Availability of a service on a day = 1 - downtime minutes / 1440. The downtime is the
  first ``downtime_minutes`` of each incident of the service (the incident record has the
  same ``downtime_minutes`` and ``services``).
- p95 latency = ``latency_base`` x service factor x traffic load x incidents x GPU shortage
  x noise. Traffic follows the active clients and the day of the week. A GPU capacity life
  event of the cloud provider (``ops``) adds ``k_lat_gpu`` x a shortage that decays over
  ``gpu_tau_days`` (company_model ``latency``) to ``ml-inference`` and partly to ``api``.
- ``sla_report``: the yearly SLA from these series and the ``company_model`` formula
  ``1 - incidents x mttr x 0.1 / 720 - 0.002 x max(latency - latency_slo, 0) / 50``
  with the realised incidents, restore time and latency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np

from variator.orgsim.company.model import Record, World

if TYPE_CHECKING:
    from variator.orgsim.company.engine import Engine

# production services and their share of the platform traffic (the weights of the platform SLA)
SERVICES: dict[str, float] = {
    "api": 0.30,
    "web-app": 0.22,
    "auth": 0.10,
    "integrations": 0.12,
    "billing-engine": 0.08,
    "data-pipeline": 0.08,
    "ml-inference": 0.10,
}
# p95 of a service relative to the platform p95 (normalised so the traffic-weighted mean is 1)
_LATENCY_RAW = {
    "api": 1.0,
    "web-app": 1.15,
    "auth": 0.55,
    "integrations": 1.25,
    "billing-engine": 0.9,
    "data-pipeline": 1.05,
    "ml-inference": 1.4,
}
_NORM = sum(SERVICES[s] * _LATENCY_RAW[s] for s in SERVICES)
LATENCY_FACTOR = {s: _LATENCY_RAW[s] / _NORM for s in SERVICES}
ERROR_BASE = {
    "api": 0.002,
    "web-app": 0.0015,
    "auth": 0.001,
    "integrations": 0.004,
    "billing-engine": 0.001,
    "data-pipeline": 0.003,
    "ml-inference": 0.003,
}
# the service that goes down with a service in a SEV1 (in a SEV2 with DEPENDENT_SEV2_P)
DEPENDENTS = {
    "auth": ("api",),
    "api": ("web-app",),
    "ml-inference": ("api",),
    "data-pipeline": ("billing-engine",),
}
DEPENDENT_SEV2_P = 0.1
INFRA_SERVICES = {"SEV1": 2, "SEV2": 1, "SEV3": 1}
GPU_API_SHARE = 0.3  # share of the GPU effect that reaches the api latency
WEEKEND_TRAFFIC = 0.55
MINUTES = 1440
SLOW_SYMPTOMS = ("high latency", "GPU inference timeouts")


@dataclass
class Outage:
    incident: Record
    start: datetime
    downtime: int  # minutes without service
    restore: int  # minutes until restored (degraded after the downtime)
    services: tuple[str, ...]
    slow: bool


class Monitoring:
    def __init__(self, w: World, eng: Engine):
        self.w, self.eng = w, eng
        t = w.spec.truth
        self.days = [w.start + timedelta(days=k) for k in range((w.anchor - w.start).days)]
        self.day_index = {d: k for k, d in enumerate(self.days)}
        n = len(self.days)
        self.noise = {
            s: w.rng("monitoring", s, "latency").normal(0.0, t.latency_noise_sd, n) for s in SERVICES
        }
        self.err_noise = {s: w.rng("monitoring", s, "errors").normal(0.0, 0.25, n) for s in SERVICES}
        self.outages: list[Outage] = []
        self.gpu: list[tuple[date, float, Record]] = []
        self._p95: dict[tuple[str, date], float] = {}
        self._traffic0: float | None = None

    # ------------------------------------------------------------------ inputs from operations
    def footprint(self, component: str, severity: str, r: np.random.Generator) -> list[str]:
        """The services an incident of ``component`` takes down (draws from the incident's stream)."""
        names = list(SERVICES)
        if component not in SERVICES:
            k = INFRA_SERVICES.get(severity, 1)
            idx = r.choice(len(names), size=k, replace=False)
            return sorted(names[int(j)] for j in idx)
        out = [component]
        for dep in DEPENDENTS.get(component, ()):
            if severity == "SEV1" or (severity == "SEV2" and r.random() < DEPENDENT_SEV2_P):
                out.append(dep)
        return sorted(set(out))

    def add_incident(
        self, rec: Record, start: datetime, downtime: int, restore: int, services: list[str], symptom: str
    ) -> None:
        self.outages.append(
            Outage(rec, start, int(downtime), int(restore), tuple(services), symptom in SLOW_SYMPTOMS)
        )

    def gpu_event(self, le: Record, d: date) -> None:
        t = self.w.spec.truth
        r = self.w.rng("monitoring", f"gpu-{d.isoformat()}", "hit")
        lo, hi = t.gpu_hit
        self.gpu.append((d, lo + (hi - lo) * float(r.random()), le))

    # ------------------------------------------------------------------ series
    def _bounds(self, d: date) -> tuple[datetime, datetime]:
        cal = self.w.cal
        return cal.at(d, "00:00"), cal.at(d + timedelta(days=1), "00:00")

    @staticmethod
    def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
        lo, hi = max(a0, b0), min(a1, b1)
        return max((hi - lo).total_seconds() / 60.0, 0.0)

    def minutes(self, service: str, d: date) -> tuple[float, float]:
        """Downtime and degraded minutes of ``service`` on day ``d`` (local wall-clock day)."""
        a, b = self._bounds(d)
        down = slow = 0.0
        for o in self.outages:
            if service not in o.services or o.start >= b:
                continue
            down += self._overlap(o.start, o.start + timedelta(minutes=o.downtime), a, b)
            slow += self._overlap(o.start, o.start + timedelta(minutes=o.restore), a, b)
        return down, slow

    def traffic(self, d: date) -> float:
        """Traffic of the day relative to a normal day at the start (active clients, day of the week)."""
        w = self.w

        def load(x: date) -> float:
            return sum(math.sqrt(c.profile.acv) for c in w.clients.values() if c.active_on(x))

        if self._traffic0 is None:
            self._traffic0 = load(w.start) or 1.0
        week = 1.0 if w.cal.is_workday(d) else WEEKEND_TRAFFIC
        normal = (5 + 2 * WEEKEND_TRAFFIC) / 7
        return load(d) / self._traffic0 * week / normal

    def gpu_hit(self, d: date) -> float:
        tau = self.w.spec.truth.gpu_tau_days
        return sum(h * math.exp(-(d - e).days / tau) for e, h, _ in self.gpu if e <= d)

    def p95(self, service: str, d: date) -> float:
        """p95 latency of ``service`` on day ``d``, ms (call it only for days before the current one)."""
        key = (service, d)
        if key in self._p95:
            return self._p95[key]
        t = self.w.spec.truth
        k = self.day_index[d]
        load = 1.0 + t.k_lat_load * (self.traffic(d) - 1.0)
        bump = 0.0
        a, b = self._bounds(d)
        for o in self.outages:
            if service in o.services and o.start < b:
                slow = self._overlap(o.start, o.start + timedelta(minutes=o.restore), a, b)
                if slow > 0:
                    bump += t.k_lat_incident * min(slow / (0.05 * MINUTES), 1.0) * (1.5 if o.slow else 1.0)
        g = self.gpu_hit(d)
        gpu = (
            t.k_lat_gpu
            * g
            * (1.0 if service == "ml-inference" else GPU_API_SHARE if service == "api" else 0.0)
        )
        sd = t.latency_noise_sd
        v = (
            t.latency_base
            * LATENCY_FACTOR[service]
            * max(load, 0.5)
            * (1.0 + min(bump, 3.0))
            * (1.0 + gpu)
            * math.exp(float(self.noise[service][k]) - sd * sd / 2)
        )
        self._p95[key] = v
        return v

    def platform_p95(self, d: date) -> float:
        return sum(SERVICES[s] * self.p95(s, d) for s in SERVICES)

    def rows(self) -> list[dict[str, Any]]:
        """``monitoring_daily``: one row per calendar day before T and production service."""
        out = []
        for k, d in enumerate(self.days):
            g = self.gpu_hit(d)
            for s in SERVICES:
                down, slow = self.minutes(s, d)
                err = ERROR_BASE[s] * math.exp(float(self.err_noise[s][k]) - 0.25**2 / 2)
                err += down / MINUTES + 0.03 * max(slow - down, 0.0) / MINUTES
                if s == "ml-inference":
                    err += 0.01 * g
                out.append(
                    {
                        "date": d.isoformat(),
                        "service": s,
                        "p95_ms": round(self.p95(s, d), 1),
                        "availability": round(1.0 - down / MINUTES, 6),
                        "downtime_minutes": int(round(down)),
                        "error_rate": round(min(err, 1.0), 6),
                    }
                )
        return out

    # ------------------------------------------------------------------ the yearly SLA
    def sla_report(self, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """The yearly SLA from the series and from the company_model formula (realised inputs)."""
        t = self.w.spec.truth
        rows = rows if rows is not None else self.rows()
        down: dict[str, float] = {s: 0.0 for s in SERVICES}
        platform: dict[str, float] = {}
        for x in rows:
            down[x["service"]] += x["downtime_minutes"]
            platform[x["date"]] = platform.get(x["date"], 0.0) + SERVICES[x["service"]] * x["p95_ms"]
        total = MINUTES * len(self.days)
        availability = sum(SERVICES[s] * (1.0 - down[s] / total) for s in SERVICES)
        lat = list(platform.values())
        lat_mean = float(np.mean(lat)) if lat else 0.0
        penalty_daily = (
            float(np.mean([0.002 * max(x - t.latency_slo, 0.0) / 50 for x in lat])) if lat else 0.0
        )
        sla_series = availability - penalty_daily
        incidents = self.eng.ops.incidents
        months = len(self.days) / (365.0 / 12)
        restore = [x.state.get("restore_minutes") for x in incidents if x.state.get("restore_minutes")]
        per_month = len(incidents) / months if months else 0.0
        mttr_h = float(np.mean(restore)) / 60 if restore else 0.0
        penalty_formula = 0.002 * max(lat_mean - t.latency_slo, 0.0) / 50
        sla_formula = 1 - per_month * mttr_h * 0.1 / 720 - penalty_formula
        # company_model at its defaults (debt and load factors of the template, no shocks)
        model_inc = t.incidents_base * (1 + t.k_inc_debt * t.tech_debt) * t.team_load
        model_lat = t.latency_base * (1 + t.k_lat_debt * t.tech_debt)
        model_sla = 1 - model_inc * t.mttr_h * 0.1 / 720 - 0.002 * max(model_lat - t.latency_slo, 0) / 50
        return {
            "sla_series": round(sla_series, 6),
            "sla_formula_realised": round(sla_formula, 6),
            "difference": round(sla_series - sla_formula, 6),
            "downtime_share_series": round(1 - availability, 6),
            "downtime_share_formula": round(per_month * mttr_h * 0.1 / 720, 6),
            "latency_penalty_series": round(penalty_daily, 6),
            "latency_penalty_formula": round(penalty_formula, 6),
            "availability_by_service": {s: round(1 - down[s] / total, 6) for s in SERVICES},
            "p95_platform_mean_ms": round(lat_mean, 1),
            "p95_platform_max_ms": round(max(lat), 1) if lat else 0.0,
            "days_p95_over_slo": sum(1 for x in lat if x > t.latency_slo),
            "incidents_per_month": round(per_month, 3),
            "mttr_h": round(mttr_h, 3),
            "gpu_events": len(self.gpu),
            "sla_target": t.sla_target,
            "company_model_defaults": {
                "incidents_per_month": round(model_inc, 3),
                "latency_ms": round(model_lat, 1),
                "sla": round(model_sla, 6),
            },
        }

    def weekly_sla(self, d0: date, d1: date, minutes_down: float) -> float:
        """SLA of a client over the days [d0, d1): its downtime and the platform latency term."""
        t = self.w.spec.truth
        days = [
            d0 + timedelta(days=k) for k in range((d1 - d0).days) if d0 + timedelta(days=k) in self.day_index
        ]
        if not days:
            return 1.0
        lat = float(np.mean([self.platform_p95(x) for x in days]))
        return 1 - minutes_down / (MINUTES * len(days)) - 0.002 * max(lat - t.latency_slo, 0.0) / 50
