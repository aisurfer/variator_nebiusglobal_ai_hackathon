"""Case B "Best year" (company_best_year): which lever gives Larchwood Systems the best year (company_model)."""

# ruff: noqa: E501 - data tables, one row per entry

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from case_calibration import calibrate
from case_spec import CaseSpec
from variator_it_services.company import PAGE, VARIABLES, company_model

FORK_DATE = "2026-10-26"
_FACTS = json.loads((Path(__file__).resolve().parents[2] / "workspace/config/company.facts.json").read_text(encoding="utf-8"))
# The inputs with a metric at T come from the history (QA m7); the others stay model assumptions.
CALIBRATED, CALIBRATED_SOURCES = calibrate(company_model(), _FACTS["metricsAtT"])
# The placed event "Meridian Freight Group gives notice" takes Meridian's real share of ARR (its ARR at T over the
# ARR at T), not the template's 10%.
MERIDIAN = next(c for c in _FACTS["keyClients"] if c["id"] == "CL-001")
ARR0 = CALIBRATED["arr0"].value
MERIDIAN_SHARE = MERIDIAN["arrAtT"] / ARR0
# PRD demo step 4: the payroll tax applies from 1 July (months after the fork date); the payroll at T is its base
PAYROLL_TAX_MONTH = round((date(2027, 7, 1) - date.fromisoformat(FORK_DATE)).days * 12 / 365, 2)
G_CHURN = next(dist.value for _b, name, _d, _u, dist, _s in VARIABLES if name == "g_churn")

PEOPLE = [
    ("LW-E01", "Avery Collins", "Chief Executive Officer"),
    ("LW-E02", "Morgan Reyes", "Head of Finance"),
    ("LW-E03", "Jordan Patel", "Head of Delivery"),
    ("LW-E06", "Daniel Kowalski", "Lead Backend Engineer"),
    ("LW-E22", "Victor Osei", "DevOps Engineer"),  # in the DevOps seat of Kwame Mensah (LW-E12, left before T)
    ("LW-E16", "Grace Whitfield", "Senior Support Engineer"),
    ("LW-E18", "Natalie Brooks", "Senior Account Manager"),
]
SHORT = {
    "price_eff": "Price change", "revenue": "Revenue", "churn": "Client churn", "new_arr": "New ARR", "debt_eff": "Technical debt",
    "latency": "Latency", "incidents": "Incidents", "overdue": "Late implementations", "capacity": "Delivery capacity",
    "load": "Team load", "cost": "Cost", "infra_cost": "Infrastructure cost", "gpu_hit": "GPU shortage", "sla": "Availability",
    "mttr_eff": "Time to restore", "cash_end": "Cash", "nps": "NPS", "roadmap": "Roadmap done", "runway": "Runway",
    "ltv_cac": "LTV / CAC", "hire_cost": "Cost of new hires", "delivery_payroll": "Delivery payroll", "gross_margin": "Gross margin",
}  # fmt: skip
ITEMS = {
    "price_start": {
        "lane": "decisions", "owner": "LW-E02", "affects": ["price_eff", "revenue", "churn", "new_arr"],
        "value": {"variable": "price_change", "option": "raise_prices_8", "label": "Price change", "min": 0.02, "max": 0.15, "step": 0.01},
        "range": "price_notice",
        "description": "The new list prices are announced to the clients; they apply after the contract notice.",
    },
    "debt_start": {
        "lane": "decisions", "owner": "LW-E06", "affects": ["debt_eff", "latency", "incidents", "overdue", "capacity"],
        "value": {"variable": "debt_share", "label": "Capacity on debt", "min": 0.05, "max": 0.3, "step": 0.01},
        "range": "debt_lead",
        "description": "Part of the engineering capacity goes to technical debt from this month; the effect shows after a lead time.",
    },
    "hire_start": {
        "lane": "decisions", "owner": "LW-E03", "affects": ["load", "hire_cost", "cost", "incidents", "capacity"],
        "value": {"variable": "hire_count", "label": "Engineers to hire", "min": 1, "max": 12, "step": 1},
        "range": "hiring_lead",
        "description": "Requisitions for new engineers open. The new engineers join after the hiring lead time; their work and their cost start then.",
    },
    "gpu_start": {
        "lane": "decisions", "owner": "LW-E22", "affects": ["infra_cost", "gpu_hit", "latency", "sla"],
        "value": {"variable": "gpu_reserve_cost", "label": "Reservation cost per year", "min": 100_000, "max": 600_000, "step": 25_000},
        "range": "gpu_setup",
        "description": "GPU capacity is reserved on a second cloud; it is ready after the setup time.",
    },
    "gpu_event": {
        "lane": "factors", "owner": "LW-E22", "entity": "org.VN-03", "affects": ["gpu_hit", "infra_cost", "latency", "sla"],
        "shock": "gpu_shortage_event", "label": "GPU shortage at Nimbus Cloud",
        "value": {"variable": "ev_gpu_size", "label": "Shortage size", "min": 0.1, "max": 0.8, "step": 0.05},
        "description": "A 40% GPU shortage at the cloud provider from that month: infrastructure cost and latency rise.",
    },
    "vendor_event": {
        "lane": "clients_vendors", "owner": "LW-E02", "entity": "org.VN-03", "affects": ["infra_cost", "cost"],
        "shock": "vendor_price_event", "label": "Nimbus Cloud raises prices",
        "value": {"variable": "ev_vendor_size", "label": "Price rise", "min": 0.05, "max": 0.3, "step": 0.01},
        "description": "The cloud provider raises its prices by 10% from that month.",
    },
    "account_event": {
        "lane": "clients_vendors", "owner": "LW-E18", "entity": "org.CL-001", "affects": ["churn", "revenue"],
        "shock": "key_account_event", "label": "Meridian Freight Group gives notice",
        "value": {"variable": "ev_account_size", "label": "Share of ARR lost", "min": 0.02, "max": 0.2, "step": 0.01},
        "description": f"{MERIDIAN['name']} (about {MERIDIAN_SHARE:.0%} of ARR) gives notice; its ARR leaves from that month.",
    },
    "kp_event": {
        "lane": "people", "owner": "LW-E03", "person": "LW-E06", "entity": "person.lw-e06", "affects": ["mttr_eff", "capacity"],
        "shock": "key_person_event", "label": "Daniel Kowalski leaves",
        "description": "The lead backend engineer leaves from that month: restore times grow and delivery capacity falls.",
    },
}  # fmt: skip
SHOCKS = {
    "gpu_shortage_event": {"lane": "factors", "owner": "LW-E22", "entity": "org.VN-03", "affects": ["gpu_hit", "infra_cost", "latency"], "event": "gpu_event"},
    "vendor_price_event": {"lane": "clients_vendors", "owner": "LW-E02", "entity": "org.VN-03", "affects": ["infra_cost"], "event": "vendor_event"},
    "fx_event": {"lane": "factors", "owner": "LW-E02", "affects": ["cost", "infra_cost"]},
    "regulation_event": {"lane": "factors", "owner": "LW-E01", "affects": ["cost"]},
    "competitor_event": {"lane": "factors", "owner": "LW-E18", "affects": ["churn", "new_arr"]},
    "key_person_event": {"lane": "people", "owner": "LW-E03", "affects": ["mttr_eff", "capacity"], "event": "kp_event"},
    "key_account_event": {"lane": "clients_vendors", "owner": "LW-E18", "affects": ["churn", "revenue"], "event": "account_event"},
    "reputation_event": {"lane": "factors", "owner": "LW-E16", "affects": ["churn"]},
    "macro_event": {"lane": "factors", "owner": "LW-E01", "affects": ["new_arr"]},
}  # fmt: skip

COMPANY = CaseSpec(
    id="company_best_year",
    letter="B",
    title="Best year",
    company="Larchwood Systems",
    question="Which lever gives Larchwood Systems the best year: keep the plan, pay down technical debt, hire, "
    "reserve GPU capacity, or raise prices?",
    lead="Larchwood Systems (12M USD ARR) plans the twelve months after 26 October 2026. Which lever gives the best "
    "year: keep the plan, reduce tech debt, hire 5 engineers, reserve GPU capacity, or raise prices by 8%?",
    owner="LW-E01",
    template="company_model",
    page=PAGE,
    status="ready",
    fork_date=FORK_DATE,
    horizon=12,
    people=PEOPLE,
    priorities=[
        (
            "balanced",
            "Balanced",
            "The model defaults: cash and churn first, then availability, runway and NPS.",
            None,
        ),
        (
            "cash_first",
            "Cash first",
            "Cash at year end and runway count most; clients and reliability count less.",
            {"cash": 2.0, "runway": 1.5, "churn": 0.5, "sla": 0.25, "nps": 0.25},
        ),
        (
            "clients_first",
            "Clients first",
            "Client churn and NPS count most; cash counts less.",
            {"cash": 0.5, "runway": 0.25, "churn": 2.0, "sla": 1.0, "nps": 1.5},
        ),
        (
            "reliability_first",
            "Reliability first",
            "Availability (SLA) counts most, then churn; cash counts less.",
            {"cash": 0.5, "runway": 0.25, "churn": 1.0, "sla": 2.0, "nps": 0.5},
        ),
    ],
    recompute={
        "status_quo": None,
        "pay_tech_debt": "from_date",
        "hire_5": "from_date",
        "reserve_gpu": "structural",
        # Clients react to a price rise through churn; an overlay has no reactions.
        "raise_prices_8": "from_date",
    },
    items=ITEMS,
    shocks=SHOCKS,
    short_labels=SHORT,
    chain_path=[
        ["gpu_hit"],
        ["infra_cost", "latency"],
        ["sla"],
        ["nps"],
        ["churn"],
        ["revenue"],
        ["cash_end"],
    ],
    chain_higher={
        "gpu_hit": False,
        "infra_cost": False,
        "latency": False,
        "incidents": False,
        "mttr_eff": False,
        "sla": True,
        "nps": True,
        "overdue": False,
        "capacity": True,
        "churn": False,
        "roadmap": True,
        "new_arr": True,
        "revenue": True,
        "cost": False,
        "cash_end": True,
        "runway": True,
        "ltv_cac": True,
    },  # fmt: skip
    blocks=[
        ("finance", "Finance"),
        ("clients", "Clients"),
        ("product", "Product and technology"),
        ("operations", "Operations"),
        ("people", "People"),
        ("market", "Market"),
        ("external", "External risks"),
        ("link", "Links between factors"),
        ("limits", "Limits and thresholds"),
        ("lever", "Levers"),
        ("timeline", "Timeline and events"),
    ],  # fmt: skip
    goals={
        "cash": ("g_cash", "Goal {usd}"),
        "churn": ("g_churn", "Goal {pct}"),
        "sla": ("sla_target", "SLA {pct1}"),
        "runway": ("runway_red", "Check: {value:g} months"),
    },
    risk_rows={
        "fragile": "runway_low",
        "runway_low": "runway_low",
        "runway": "runway_low",
        "cash": "cash_negative",
        "cash_goal": "cash_negative",
        "sla": "sla_breach",
        "sla_breach": "sla_breach",
        "churn": "churn_high",
        "churn_high": "churn_high",
        "churn_goal": "churn_high",
        "over_budget": "over_budget",
        "price_live": "price_live",
        "debt_done": "debt_done",
        "hires_ready": "hires_ready",
        "gpu_ready": "gpu_ready",
    },  # fmt: skip
    preview_metrics={
        "cash": ("Cash", "Cash balance at the end of each month, USD."),
        "churn": ("Churn, year to date", "Share of ARR lost since the start of the year (year to date)."),
        "sla": ("Availability, year to date", "Availability since the start of the year (year to date)."),
        "nps": ("NPS", "Net promoter score at the end of each month."),
        "runway": ("Runway", "Months of cash left at the burn rate of the year (capped at 60 months)."),
    },
    calendar_rows={
        "cash_negative": ("Cash below zero", "Chance that cash goes below zero by each month."),
        "runway_low": (
            "Runway below 6 months",
            "Chance that the runway falls below 6 months by each month (the check fails).",
        ),
        "sla_breach": (
            "SLA breach",
            "Chance of at least one month with availability below the SLA target by each month.",
        ),
        "churn_high": (
            f"Churn above {G_CHURN:.0%}",
            f"Chance that churn since the start of the year passes {G_CHURN:.0%} (the churn goal) by each month.",
        ),
        "over_budget": (
            "Cost above budget",
            "Chance that the cost to date runs above the budget to date by each month.",
        ),
        "price_live": (
            "New prices late",
            "Chance that the new prices are not live by their milestone month.",
        ),
        "debt_done": ("Debt work late", "Chance that the debt work shows no effect by its milestone month."),
        "hires_ready": (
            "Hires late",
            "Chance that the new hires are not productive by their milestone month.",
        ),
        "gpu_ready": (
            "GPU capacity late",
            "Chance that the GPU capacity on the second cloud is not ready by its milestone month.",
        ),
    },
    reference_presets=("price_war", "cash_squeeze"),
    fork_icons={
        "status_quo": "circle-dot",
        "pay_tech_debt": "wrench",
        "hire_5": "user-plus",
        "reserve_gpu": "server",
        "raise_prices_8": "tag",
    },
    hypothesis_icons={
        "price_war": "swords",
        "gpu_crunch": "cpu",
        "big_client_leaves": "user-minus",
        "client_requirements_churn": "repeat",
        "hiring_freeze": "snowflake",
        "cash_squeeze": "wallet",
        "downturn": "trending-down",
        "payroll_tax_july": "receipt",
    },  # fmt: skip
    touched=("tech_debt", "infra_base", "latency_base"),
    defaults={"ev_account_size": MERIDIAN_SHARE, "payroll": _FACTS["payrollAtT"], "payroll_tax_month": PAYROLL_TAX_MONTH},
    dists=CALIBRATED,
    sources=CALIBRATED_SOURCES | {"payroll": "history at T: gross salaries of the staff (company facts)"},
    sections={
        "world": {
            "summary": "World hypotheses as layers: price war, GPU shortage, a big client leaving and more."
        },
        "month-time": {
            "summary": "Monthly fans per fork and the risk calendar: when cash, runway, SLA, churn, cost or a milestone can fail."
        },
        "month-why": {
            "summary": "The cause-and-effect chain per fork from GPU capacity to cash, the main drivers and the rules."
        },
        "day": {
            "summary": "Three example scenarios (bad, typical, good): daily cash, load, incidents and events."
        },
    },
    section_help={
        "stays": "Inputs that no fork changes. The inputs with a metric at T start from the history (the company facts): ARR, cash, receivables days, team load and deploys as measured; time to restore, incidents, latency, support quality (from the NPS) and the base churn so that the plan without shocks gives the measured values. Every other input, the k links and the limits are model assumptions. The forecast differs from T: the shocks, the uncertainty and the links act over the year.",
        "month-time": f"A preview: monthly paths drawn to end at the year-end results of the model. The fan shows the median and the 25-75% and 10-90% ranges. The risk calendar shows the chance, by each month, that cash goes below zero, the runway falls below 6 months, a month breaks the SLA, churn passes {G_CHURN:.0%}, cost runs above the budget, or a milestone is late.",
        "month-why": "The chain shows how a shock moves through the company: GPU shortage, infrastructure cost and latency, availability, NPS, churn, revenue and cash. Values are means over all scenarios, per fork. The drivers are the inputs that move cash most.",
        "day": "A preview: three example scenarios of a fork, from the bad, the typical and the good part of the scenarios. Each shows daily cash, team load and incidents for the first 60 working days, and the events.",
    },
    kpi_help={
        "feasible": (
            "Feasible",
            "Share of scenarios where no check rule fails (for example: runway below 6 months).",
        ),
        "limits": (
            "RAG limits",
            "Runway has fixed limits: green from 18 months, red at 6 months or less (inputs under Limits and thresholds). The other criteria take them from the baseline fork (keep the plan): green = its median; red = its worst 10%. Flags make a colour worse; a failed check makes it red.",
        ),
        "drivers": (
            "Main drivers",
            "Inputs that move cash most for this fork.",
            "The correlation of each uncertain input with cash at year end over the scenarios. A bar to the right: higher values give more cash; to the left: they cost cash.",
        ),
        "weekNode": (
            "Week node",
            "One week of one group of paths: cash, team load and incidents, and a short summary.",
        ),
        "dayExample": (
            "Example future",
            "A scenario of the model from the bad (10%), typical (50%) or good (90%) part of cash at year end. Preview data.",
        ),
        "dayEvents": (
            "Events",
            "What happens on each day of the example: incidents, payments, shocks and decisions. A link opens the record in the knowledge base.",
        ),
    },
    control_help={
        "eventButtons": (
            "Events",
            "Place a certain event (GPU shortage, supplier price rise, key account or key engineer leaves) on the timeline, then move it to its month.",
        ),
    },
)
