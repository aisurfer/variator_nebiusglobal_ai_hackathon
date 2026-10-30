"""Learned habits -> the parameters of the forward processes, each with a source tag.

The values come from the WP10 artifacts (``habits.json``, ``cohorts.json``) through ``StateAtT``.
A company-level parameter with an interval is drawn once per scenario from its PERT
(low = p1, mode, high = p99) on the scenario's own stream: the day examples then differ in
parameters as well as in events. Actor-level parameters use their point (posterior median).

Source tags (as in WP10): ``data:<dataset>`` (learned from the history), ``group:<cohort>`` (pooled),
``case:<variable>`` (the case config ``company_best_year.json``), ``rule:<id>`` (a delivered company rule),
``assumption:<what>`` (not learned; the reason is in ``ASSUMPTIONS``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from variator.orgsim.company.forward.state import StateAtT

SERVICES = ("api", "web-app", "auth", "integrations", "billing-engine", "data-pipeline", "ml-inference")
SEVERITIES = ("SEV1", "SEV2", "SEV3")

# Parameters that the history does not let us learn (or only weakly), with the reason.
ASSUMPTIONS: dict[str, tuple[Any, str]] = {
    "restore_log_sd": (
        0.6,
        "spread of one restore time around its severity mean; 3-16 incidents per severity",
    ),
    "incident_churn_tau_days": (
        60.0,
        "decay of an incident bump of the churn hazard; WP10 model downtime_decay_60d",
    ),
    "gpu_tau_days": (7.0, "days a GPU shortage lasts; the history shows single-week latency spikes"),
    "gpu_hit": (0.4, "size of a GPU shortage; case variable ev_gpu_size"),
    "impl_fte_days_new": (
        15.0,
        "engineering days of an onboarding project; go-live dates are not in the records",
    ),
    "impl_fte_days_upsell": (8.0, "engineering days of an upsell implementation"),
    "incident_fte_days": ({"SEV1": 3.0, "SEV2": 1.5, "SEV3": 0.5}, "unplanned engineering days per incident"),
    "ramp": ([(20, 0.5), (40, 0.75)], "pace of a new hire: 50% for 20 working days, 75% for the next 20"),
    "hire_roles": (
        ["backend", "backend", "frontend", "devops", "qa"],
        "role mix of the 5 hires (team mix at T)",
    ),
    "nps_response_rate": (0.42, "share of contacts that answer a survey (WP9: 38-48%)"),
    "nps_sla_points": (8.0, "NPS points one SEV1 day of a client costs (company_model k_nps_sla, scaled)"),
    "commission_rate": (0.0, "sales commission is not separated in the payroll records"),
    "price_notice_letters": (True, "clients get a letter when the new prices are announced"),
}


def pert(rng: np.random.Generator, low: float, mode: float, high: float) -> float:
    """A draw of the beta-PERT distribution (lambda 4)."""
    if not high > low:
        return float(mode)
    mode = min(max(mode, low), high)
    a = 1 + 4 * (mode - low) / (high - low)
    b = 1 + 4 * (high - mode) / (high - low)
    return float(low + (high - low) * rng.beta(a, b))


@dataclass
class Params:
    """All numbers of the forward processes for one scenario."""

    values: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, k: str) -> Any:
        return self.values[k]

    def get(self, k: str, default: Any = None) -> Any:
        return self.values.get(k, default)

    def set(self, k: str, v: Any, source: str) -> Any:
        self.values[k] = v
        self.sources[k] = source
        return v

    def to_json(self) -> dict[str, Any]:
        def js(v: Any) -> Any:
            if isinstance(v, float):
                return round(v, 6)
            if isinstance(v, dict):
                return {str(a): js(b) for a, b in v.items()}
            if isinstance(v, (list, tuple)):
                return [js(x) for x in v]
            return v

        return {k: {"value": js(self.values[k]), "source": self.sources[k]} for k in sorted(self.values)}


def _p(st: StateAtT, group: str, name: str) -> dict[str, Any]:
    v = st.param(group, name)
    if v is None:
        raise KeyError(f"learned parameter {group}.{name} is missing")
    return v


def build_params(st: StateAtT, rng: np.random.Generator, *, draw: bool = True) -> Params:
    """Map the learned parameters onto the processes; draw the uncertain company ones (``draw``)."""
    P = Params()

    def learned(key: str, group: str, name: str, *, drawn: bool = False) -> float:
        v = _p(st, group, name)
        x = float(v["point"])
        if drawn and draw and v.get("low") is not None and v.get("high") is not None:
            x = pert(rng, float(v["low"]), float(v.get("mode", x)), float(v["high"]))
        return P.set(key, x, str(v.get("source")) + (" (PERT draw)" if drawn and draw else ""))

    # ---------------------------------------------------------------- operations
    learned("incidents_per_month", "company", "incidents_per_month", drawn=True)
    shares = [float(_p(st, "company", f"severity_share_{s}")["point"]) for s in SEVERITIES]
    P.set(
        "severity_mix", [x / sum(shares) for x in shares], _p(st, "company", "severity_share_SEV1")["source"]
    )
    P.set(
        "restore_hours",
        {s: float(_p(st, "company", f"restore_hours_{s}")["point"]) for s in SEVERITIES},
        _p(st, "company", "restore_hours_SEV2")["source"],
    )
    P.set(
        "downtime_share",
        {
            s: min(
                1.0,
                float(_p(st, "company", f"downtime_hours_{s}")["point"])
                / max(float(_p(st, "company", f"restore_hours_{s}")["point"]), 1e-6),
            )
            for s in SEVERITIES
        },
        "data:sim_incident (downtime / restore time by severity)",
    )
    P.set(
        "affected_clients",
        {s: float(_p(st, "company", f"affected_clients_{s}")["point"]) for s in SEVERITIES},
        _p(st, "company", "affected_clients_SEV2")["source"],
    )
    learned("deploys_per_week", "company", "deploys_per_week")
    learned("incident_deploy_share", "company", "incident_deploy_share")
    learned("incident_vendor_share", "company", "incident_vendor_share")
    learned("deploy_rollback_share", "company", "deploy_rollback_share")
    lat = {}
    for s in SERVICES:
        v = st.param("company", f"latency_p95_ms_{s}_now") or _p(st, "company", f"latency_p95_ms_{s}")
        lat[s] = float(v["point"])
    P.set("latency_now", lat, "data:monitoring_daily (last 63 days)")
    learned("latency_slo_breach_share", "company", "sla_breach_day_share")
    learned("team_load", "company", "team_load", drawn=True)
    learned("team_load_now", "company", "team_load_now")
    learned("overload_sprint_share", "company", "overload_sprint_share")
    # ---------------------------------------------------------------- people
    learned("resignation_hazard", "staff", "resignation_hazard", drawn=True)
    learned("sick_days_per_spell", "staff", "sick_days_per_spell")
    P.set(
        "vacation_month_share",
        {m: float(_p(st, "staff", f"vacation_month_share_{m:02d}")["point"]) for m in range(1, 13)},
        _p(st, "staff", "vacation_month_share_07")["source"],
    )
    tp = st.cohorts["engineers"]["params"]["throughput_engineers"]
    P.set("throughput_engineers", float(tp["point"]), tp["source"])
    hl = st.cohorts["future:backend"]["params"]["hiring_lead_months"]
    P.set("hiring_lead_log_mu", float(hl["extra"]["log_mu"]), hl["source"])
    P.set("hiring_lead_log_sd", float(hl["extra"]["log_sd"]), hl["source"])
    rg = st.habits["regulators"]["TA-UK"]
    P.set("payroll_tax_rate", float(rg["payroll_tax_rate"]["point"]), rg["payroll_tax_rate"]["source"])
    P.set(
        "employee_deduction_rate",
        float(rg["employee_deduction_rate"]["point"]),
        rg["employee_deduction_rate"]["source"],
    )
    P.set("burnout_load", 1.05, "rule:RULE-BURNOUT (a load above 1.05 raises attrition)")
    P.set("burnout_add", 0.05, "assumption:burnout (+5 points of the annual resignation probability)")
    # ---------------------------------------------------------------- clients
    base = _p(st, "client_base", "churn_hazard_base")
    churn_draw = (
        pert(rng, base["low"], base.get("mode", base["point"]), base["high"]) if draw else base["point"]
    )
    P.set("churn_scale", churn_draw / float(base["point"]), f"{base['source']} (PERT draw / point)")
    learned("churn_incident_k", "client_base", "churn_incident_k")
    learned("upsell_size_share", "client_base", "upsell_size_share")
    learned("downgrade_size_share", "client_base", "downgrade_size_share")
    P.values["downgrade_size_share"] = min(max(P["downgrade_size_share"], 0.05), 0.4)
    learned("late_extra_days", "client_base", "late_extra_days")
    learned("late_spell_days", "client_base", "late_spell_days")
    learned("renewal_uplift", "client_base", "renewal_uplift")
    learned("support_mult_after_deal", "client_base", "support_mult_after_deal")
    learned("upsell_win_share", "company", "upsell_win_share")
    learned("support_escalation_share", "company", "support_escalation_share")
    learned("support_resolve_hours", "company", "support_resolve_hours")
    learned("csat", "company", "csat")
    learned("nps_score_sd_latent", "company", "nps_score_sd_latent")
    learned("nps_level_typical", "company", "nps_level_typical_client")
    # ---------------------------------------------------------------- sales
    pr = st.cohorts["prospects"]["params"]
    for key, name in (("deals_per_month", "deals_per_month"), ("win_rate", "win_rate")):
        v = pr[name]
        x = pert(rng, v["low"], v.get("mode", v["point"]), v["high"]) if draw else float(v["point"])
        P.set(key, x, v["source"] + (" (PERT draw)" if draw else ""))
    P.set("deal_value_median", float(pr["deal_value_median"]["point"]), pr["deal_value_median"]["source"])
    P.set("deal_value_log_sd", float(pr["deal_value_log_sd"]["point"]), pr["deal_value_log_sd"]["source"])
    sc = pr["sales_cycle_days"]
    P.set("sales_cycle_log_mu", float(sc["extra"]["log_mu"]), sc["source"])
    P.set("sales_cycle_log_sd", float(sc["extra"]["log_sd"]), sc["source"])
    # ---------------------------------------------------------------- money
    learned("cloud_cost_per_year_now", "company", "cloud_cost_per_year_now")
    learned("cloud_cost_growth_per_year", "company", "cloud_cost_growth_per_year")
    learned("gpu_share", "company", "gpu_share")
    if st.corp_tax:
        P.set("corp_tax_instalment", float(st.corp_tax["amount"]), "data:sim_tax_payment (last instalment)")
    for k, (v, why) in ASSUMPTIONS.items():
        P.set(k, v, f"assumption:{why}")
    return P


def annual_to_daily(p: float, bdays: float = 252.0) -> float:
    """Hazard per working day of an annual probability."""
    p = min(max(p, 0.0), 0.999)
    return -math.log1p(-p) / bdays
