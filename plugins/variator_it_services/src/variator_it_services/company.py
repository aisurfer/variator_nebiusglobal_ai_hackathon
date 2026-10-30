"""Template ``company_model``: an IT services company for one year, as a system of cause-and-effect chains.

All factors of the owner's table (finance, clients, product and technology, operations,
people, market, external risks) are variables. The point is the links, not the list:

    GPU shortage / vendor price rise -> infra cost and latency up -> SLA down -> NPS down,
        churn up -> revenue down -> cash and runway down
    technical debt -> incidents and latency up; overdue implementations up -> churn up
    load, attrition, blockers, skills, new tools -> capacity -> overdue and roadmap -> new ARR
    deploy frequency -> time to restore -> SLA
    price change -> revenue up, churn up, win rate down; new ARR -> CAC spend
    receivables days -> cash; renewal share x low NPS -> churn

Each chain is a derived value. Shocks are risk events on variables that are 0 without the
event. Levers are 0/1 variables that the options override, so all options share one set of
formulas. The size of each lever (``hire_count``, ``debt_share``, ``gpu_reserve_cost``) and of each
placed event (``ev_*_size``) is a variable too, so a page can edit it. Every ``k_*`` variable is a link
coefficient: an assumption, not calibrated. Change it with ``case_patch`` (``set_distribution``) or on
the playground page. The limits of the rules and of the runway RAG bands (block ``limits``) are variables
too; the runway criterion takes its RAG limits from them (``Criterion.rag_from``).
"""

from __future__ import annotations

from variator.domain import (
    Criterion,
    DecisionModel,
    DecisionOption,
    Derived,
    Distribution,
    DistributionKind,
    MetricSpec,
    RagVariables,
    Risk,
    RiskEffect,
    Variable,
)


def _pert(low: float, mode: float, high: float) -> Distribution:
    return Distribution(kind=DistributionKind.PERT, low=low, mode=mode, high=high)


def _fixed(value: float) -> Distribution:
    return Distribution.fixed(value)


# block, name, description, unit, distribution, source
VARIABLES = [
    ("finance", "arr0", "Annual recurring revenue at the start", "USD/year", _fixed(12_000_000), "data: finance"),
    ("finance", "opex", "Operating cost without infrastructure and CAC (salaries, office)", "USD/year",
     _pert(8_200_000, 8_800_000, 9_400_000), "data: finance"),
    ("finance", "cash0", "Cash at the start", "USD", _fixed(2_000_000), "data: finance"),
    ("finance", "budget", "Annual cost budget", "USD/year", _fixed(11_200_000), "data: finance"),
    ("finance", "acv", "Average annual contract value", "USD", _fixed(120_000), "data: CRM"),
    ("finance", "cac", "Customer acquisition cost per new client", "USD", _pert(20_000, 30_000, 45_000), "data: CRM"),
    ("finance", "ar_days", "Receivables: days until clients pay", "days", _pert(45, 60, 90), "data: finance"),
    ("clients", "base_churn", "Annual churn without the effects below", "share", _pert(0.08, 0.12, 0.18), "data: CRM"),
    ("clients", "pipeline", "Annual new ARR in the sales pipeline", "USD/year",
     _pert(2_000_000, 3_000_000, 4_500_000), "data: CRM"),
    ("clients", "win_rate", "Share of the pipeline that closes", "share", _pert(0.20, 0.25, 0.32), "data: CRM"),
    ("clients", "renewal_due", "Share of ARR with a renewal this year", "share", _pert(0.30, 0.40, 0.50), "data: CRM"),
    ("clients", "overdue_base", "Share of client implementations that are late", "share",
     _pert(0.05, 0.10, 0.18), "data: PMO"),
    ("operations", "support_quality", "Support quality score (0-1)", "score", _pert(0.7, 0.8, 0.9), "data: support"),
    ("product", "tech_debt", "Technical debt index (0 = none, 1 = severe)", "index", _pert(0.30, 0.45, 0.60),
     "data: code metrics (complexity, cycles, hot spots)"),
    ("product", "infra_base", "Annual infrastructure and GPU cost", "USD/year",
     _pert(1_200_000, 1_500_000, 1_900_000), "data: cloud bills"),
    ("product", "latency_base", "p95 API latency", "ms", _pert(150, 180, 230), "data: monitoring"),
    ("product", "latency_slo", "Latency objective in the SLA", "ms", _fixed(250), "data: contracts"),
    ("product", "sla_target", "Availability promised in the SLA", "share", _fixed(0.995), "data: contracts"),
    ("product", "incidents_base", "Incidents per month at the average debt and load", "count", _pert(2, 4, 7),
     "data: incidents"),
    ("operations", "mttr_h", "Mean time to restore", "hours", _pert(1, 2, 4), "data: incidents"),
    ("operations", "deploy_freq", "Deployments per week", "count", _pert(5, 10, 20), "data: CI/CD"),
    ("operations", "team_load", "Planned work / capacity", "ratio", _pert(0.85, 0.95, 1.10), "data: planning"),
    ("operations", "blockers", "Share of time lost to blockers", "share", _pert(0.05, 0.10, 0.20), "data: Jira"),
    ("people", "attrition", "Annual staff attrition", "share", _pert(0.10, 0.15, 0.22), "data: HR"),
    ("people", "hiring_lead", "Months to hire and onboard", "months", _pert(2, 3, 5), "data: HR"),
    ("people", "skills", "Team skills index (1 = normal)", "index", _pert(0.9, 1.0, 1.1), "data: HR"),
    ("market", "demand_growth", "Market demand growth", "share", _pert(-0.05, 0.08, 0.20), "web: market reports"),
    ("market", "new_tech_gain", "Productivity gain from new tools (for example AI assistants)", "share",
     _pert(0.0, 0.05, 0.12), "web: market reports"),
    # levers (0 = off; options set them)
    ("lever", "lever_debt", "Lever: 15% of capacity pays down technical debt", "0/1", _fixed(0), "decision"),
    ("lever", "debt_share", "Share of delivery capacity the debt work takes", "share", _fixed(0.15), "decision"),
    ("lever", "lever_hire", "Lever: hire 5 engineers", "0/1", _fixed(0), "decision"),
    ("lever", "hire_count", "Engineers the hiring lever hires", "count", _fixed(5), "decision"),
    ("lever", "lever_gpu", "Lever: reserved GPU capacity on a second cloud", "0/1", _fixed(0), "decision"),
    ("lever", "gpu_reserve_cost", "Annual cost of the reserved GPU capacity", "USD/year", _fixed(300_000), "decision"),
    ("lever", "price_change", "Lever: list price change", "share", _fixed(0), "decision"),
    # shocks (0 without the event; risk events add to them)
    ("external", "gpu_shortage", "GPU shortage severity", "share", _fixed(0), "assumption: external"),
    ("market", "vendor_price", "Price rise by NVIDIA, AWS or another supplier", "share", _fixed(0),
     "assumption: market"),
    ("external", "fx_shock", "Cost increase from currency moves", "share", _fixed(0), "assumption: external"),
    ("external", "regulation_cost", "New compliance cost", "USD/year", _fixed(0), "assumption: external"),
    ("market", "competitor_cut", "Competitor price cut", "share", _fixed(0), "assumption: market"),
    ("people", "key_person_exit", "A key engineer leaves (1 = yes)", "0/1", _fixed(0), "assumption: people"),
    ("clients", "key_account_lost", "Share of ARR lost with a key account", "share", _fixed(0), "assumption: clients"),
    ("external", "reputation_hit", "Extra churn from a reputation event", "share", _fixed(0), "assumption: external"),
    # link coefficients (assumptions)
    ("link", "k_infra_gpu", "Infra cost growth per unit of GPU shortage", "x", _fixed(0.8), "assumption: link"),
    ("link", "k_lat_gpu", "Latency growth per unit of GPU shortage", "x", _fixed(0.6), "assumption: link"),
    ("link", "k_lat_debt", "Latency growth per unit of technical debt", "x", _fixed(0.5), "assumption: link"),
    ("link", "k_inc_debt", "Incident growth per unit of technical debt", "x", _fixed(1.2), "assumption: link"),
    ("link", "k_mttr_deploy", "Elasticity of restore time to deploy frequency", "x", _fixed(0.3), "assumption: link"),
    ("link", "k_churn_sla", "Churn added per 0.1 pp of availability below the SLA", "share", _fixed(0.01),
     "assumption: link"),
    ("link", "k_churn_overdue", "Churn added per unit of overdue implementations", "x", _fixed(0.15),
     "assumption: link"),
    ("link", "k_churn_nps", "Churn added per NPS point below 30, on the renewal share", "share", _fixed(0.01),
     "assumption: link"),
    ("link", "k_churn_price", "Churn added per unit of price increase", "x", _fixed(0.6), "assumption: link"),
    ("link", "k_churn_competitor", "Churn added per unit of competitor price cut", "x", _fixed(0.5),
     "assumption: link"),
    ("link", "k_win_price", "Win-rate loss per unit of price increase", "x", _fixed(1.0), "assumption: link"),
    # timeline: when each lever starts and how long until it works; events placed at a month (0 = not placed);
    # milestone months and year-end goals. A lever acts only for the part of the year after it is ready.
    ("timeline", "m_price", "Month when the new prices are announced", "month", _fixed(0), "plan"),
    ("timeline", "price_notice", "Contract notice before new prices apply", "months", _pert(1, 1.5, 3),
     "assumption: contracts"),
    ("timeline", "m_debt", "Month when the technical-debt work starts", "month", _fixed(0), "plan"),
    ("timeline", "debt_lead", "Months until the debt work shows an effect", "months", _pert(2, 3, 5),
     "assumption: engineering"),
    ("timeline", "m_hire", "Month when the hiring starts", "month", _fixed(0), "plan"),
    ("timeline", "m_gpu", "Month when the GPU reservation is ordered", "month", _fixed(0), "plan"),
    ("timeline", "gpu_setup", "Months to set up the second cloud", "months", _pert(1, 2, 3), "assumption: vendor"),
    ("timeline", "ev_gpu", "Placed event: a GPU shortage (1 = yes)", "0/1", _fixed(0), "event"),
    ("timeline", "ev_gpu_month", "Month of the GPU shortage", "month", _fixed(4), "event"),
    ("timeline", "ev_gpu_size", "Size of the placed GPU shortage", "share", _fixed(0.4), "event"),
    ("timeline", "ev_vendor", "Placed event: a supplier raises prices by 10% (1 = yes)", "0/1", _fixed(0), "event"),
    ("timeline", "ev_vendor_month", "Month of the supplier price rise", "month", _fixed(3), "event"),
    ("timeline", "ev_vendor_size", "Price rise of the placed supplier event", "share", _fixed(0.1), "event"),
    ("timeline", "ev_account", "Placed event: a key account (10% of ARR) leaves (1 = yes)", "0/1", _fixed(0), "event"),
    ("timeline", "ev_account_month", "Month when the key account leaves", "month", _fixed(6), "event"),
    ("timeline", "ev_account_size", "Share of ARR the placed key account takes", "share", _fixed(0.1), "event"),
    ("timeline", "ev_kp", "Placed event: a key engineer leaves (1 = yes)", "0/1", _fixed(0), "event"),
    ("timeline", "ev_kp_month", "Month when the key engineer leaves", "month", _fixed(5), "event"),
    ("timeline", "ms_price", "Milestone: new prices live", "month", _fixed(3), "milestone"),
    ("timeline", "ms_debt", "Milestone: the debt work shows an effect", "month", _fixed(5), "milestone"),
    ("timeline", "ms_hire", "Milestone: the new hires are productive", "month", _fixed(4), "milestone"),
    ("timeline", "ms_gpu", "Milestone: GPU capacity on the second cloud ready", "month", _fixed(3), "milestone"),
    ("timeline", "g_cash", "Goal: cash at year end at least", "USD", _fixed(1_500_000), "goal"),
    # one churn limit: the goal, the red flag of churn_high (amber at 3/4 of it) and the risk-calendar row
    ("timeline", "g_churn", "Goal and red limit: annual churn at most", "share", _fixed(0.20), "goal"),
    # the people and the delivery cost (the MVP company of the history has 20 people)
    ("people", "headcount", "Staff at the start of the year", "count", _fixed(20), "data: HR"),
    ("people", "engineer_cost", "Annual cost of one new engineer (salary, tax and overhead)", "USD/year",
     _fixed(150_000), "data: HR"),
    ("finance", "delivery_payroll_share",
     "Share of the operating cost that pays the delivery (engineers, delivery and support staff, subcontractors)",
     "share", _fixed(0.85), "data: payroll and vendor bills"),
    # limits of the rules and the RAG bands: settings, not facts (the user tunes them)
    ("limits", "burnout_load", "Burnout: a team load after the hires above this level raises attrition", "ratio",
     _fixed(1.05), "assumption: HR"),
    ("limits", "burnout_attrition", "Burnout: attrition added above the load level", "share", _fixed(0.05),
     "assumption: HR"),
    ("limits", "runway_cap", "The most months of runway the model reports", "months", _fixed(60),
     "assumption: finance"),
    ("limits", "runway_green", "Runway RAG: green from this many months", "months", _fixed(18), "policy: finance"),
    ("limits", "runway_red", "Runway RAG and check: fewer months are red and not acceptable", "months", _fixed(6),
     "policy: finance"),
    # meaningful differences: forks closer than this on a criterion are equal on it (score and P(best))
    ("limits", "md_cash", "Meaningful difference of cash: under 1% of revenue, below one month of cost", "USD",
     _fixed(100_000), "policy: decision"),
    ("limits", "md_runway", "Meaningful difference of runway: the month is the planning step", "months", _fixed(1),
     "policy: decision"),
    ("limits", "md_churn", "Meaningful difference of churn: 0.5 pp is about $60k of ARR", "share", _fixed(0.005),
     "policy: decision"),
    ("limits", "md_sla", "Meaningful difference of availability: 0.05 pp is about 22 minutes a month", "share",
     _fixed(0.0005), "policy: decision"),
    ("limits", "md_nps", "Meaningful difference of NPS: the score is reported in whole points", "points",
     _fixed(1), "policy: decision"),
    # payroll tax (PRD demo step 4: "payroll tax +5 pp from July"); 0 without the world hypothesis
    ("people", "payroll", "Annual gross payroll of the staff (salaries before employer taxes)", "USD/year",
     _fixed(2_332_000), "data: payroll"),
    ("external", "payroll_tax_add", "Extra employer payroll tax on the gross payroll", "share", _fixed(0),
     "assumption: regulation"),
    ("timeline", "payroll_tax_month", "Month when the extra payroll tax applies", "month", _fixed(9),
     "assumption: regulation"),
]

# name, description, unit, formula
DERIVED = [
    # timeline: the month each lever is ready, and the share of the year after a month (rest of the year)
    ("price_live", "Month when the new prices apply", "month", "m_price + price_notice"),
    ("debt_ready", "Month when the debt work shows an effect", "month", "m_debt + debt_lead"),
    ("hire_ready", "Month when the new hires join (their work and their cost start)", "month",
     "m_hire + hiring_lead"),
    ("gpu_ready", "Month when the GPU capacity is ready", "month", "m_gpu + gpu_setup"),
    ("price_eff", "Price change over the year (from the month it applies)", "share",
     "price_change * maximum(12 - price_live, 0) / 12"),
    ("gpu_total", "GPU shortage: shock events + a placed event (rest of the year)", "share",
     "gpu_shortage + ev_gpu * ev_gpu_size * maximum(12 - ev_gpu_month, 0) / 12"),
    ("vendor_total", "Supplier price rise: shock events + a placed event", "share",
     "vendor_price + ev_vendor * ev_vendor_size * maximum(12 - ev_vendor_month, 0) / 12"),
    ("account_total", "ARR lost with key accounts: shock events + a placed event", "share",
     "key_account_lost + ev_account * ev_account_size * maximum(12 - ev_account_month, 0) / 12"),
    ("kp_total", "Key engineer gone: shock events + a placed event (share of the year)", "share",
     "minimum(key_person_exit + ev_kp * maximum(12 - ev_kp_month, 0) / 12, 1)"),
    ("hire_effect", "Share of the year in which the new hires work", "share",
     "lever_hire * maximum(12 - hire_ready, 0) / 12"),
    ("debt_eff", "Technical debt after the debt lever", "index",
     "tech_debt * (1 - 0.4 * lever_debt * maximum(12 - debt_ready, 0) / 12)"),
    # the work of the year spread over the staff plus the hires for the part of the year they work
    ("load", "Team load after the hires", "ratio", "team_load * headcount / (headcount + hire_count * hire_effect)"),
    ("hire_cost", "Cost of the new hires in the year (from the month they join)", "USD/year",
     "engineer_cost * hire_count * hire_effect"),
    ("capacity", "Delivery capacity (1 = normal)", "ratio",
     "(1 - debt_share * lever_debt * maximum(12 - m_debt, 0) / 12) * (1 - 0.5 * attrition) * (1 - 0.15 * kp_total)"
     " * (1 - blockers) * skills * (1 + new_tech_gain) / load"),
    ("gpu_hit", "GPU shortage after the GPU lever", "share",
     "gpu_total * (1 - 0.7 * lever_gpu * maximum(12 - gpu_ready, 0) / 12)"),
    ("infra_cost", "Infrastructure cost", "USD/year",
     "infra_base * (1 + k_infra_gpu * gpu_hit) * (1 + vendor_total) * (1 + fx_shock)"
     " + gpu_reserve_cost * lever_gpu * maximum(12 - m_gpu, 0) / 12"),
    ("latency", "p95 latency", "ms", "latency_base * (1 + k_lat_gpu * gpu_hit) * (1 + k_lat_debt * debt_eff)"),
    ("incidents", "Incidents per month", "count", "incidents_base * (1 + k_inc_debt * debt_eff) * load"),
    ("mttr_eff", "Time to restore after deploy and people effects", "hours",
     "mttr_h * (10 / deploy_freq) ** k_mttr_deploy * (1 + 0.5 * kp_total)"),
    ("sla", "Availability (1 = always up)", "share",
     "1 - incidents * mttr_eff * 0.1 / 720 - 0.002 * maximum(latency - latency_slo, 0) / 50"),
    ("overdue", "Share of late implementations", "share",
     "minimum(overdue_base * (1 + debt_eff) / maximum(capacity, 0.3), 1)"),
    ("nps", "Net promoter score", "points",
     "40 - 2000 * maximum(sla_target - sla, 0) - 40 * overdue + 50 * (support_quality - 0.8)"),
    ("churn", "Annual churn", "share",
     "clip(base_churn + k_churn_sla * maximum(sla_target - sla, 0) * 1000 + k_churn_overdue * overdue"
     " + k_churn_nps * renewal_due * maximum(30 - nps, 0) + k_churn_price * price_eff"
     " + k_churn_competitor * competitor_cut + account_total + reputation_hit"
     " - 0.1 * (support_quality - 0.8), 0, 0.9)"),
    ("roadmap", "Share of the roadmap delivered", "share", "minimum(capacity, 1)"),
    ("new_arr", "New ARR closed in the year", "USD/year",
     "pipeline * win_rate * (1 - k_win_price * price_eff) * (1 - competitor_cut) * (1 + demand_growth)"
     " * (0.8 + 0.2 * roadmap)"),
    ("cac_cost", "Acquisition spend for the new clients", "USD/year", "new_arr / acv * cac"),
    ("revenue", "Revenue in the year (mid-year averages)", "USD/year",
     "arr0 * (1 + price_eff) * (1 - churn / 2) + new_arr / 2"),
    ("payroll_tax_cost", "Extra payroll tax in the year (from the month it applies)", "USD/year",
     "payroll_tax_add * payroll * maximum(12 - payroll_tax_month, 0) / 12"),
    ("cost", "Total cost in the year", "USD/year",
     "opex * (1 + 0.5 * fx_shock) + infra_cost + regulation_cost + hire_cost + cac_cost + payroll_tax_cost"),
    ("delivery_payroll", "Delivery payroll: the delivery share of the operating cost and the new hires", "USD/year",
     "delivery_payroll_share * opex * (1 + 0.5 * fx_shock) + hire_cost"),
    ("cash_end", "Cash at the end of the year (after the receivables change)", "USD",
     "cash0 + revenue - cost - revenue * (ar_days - 60) / 365"),
    ("burn", "Monthly net burn (0 when profitable)", "USD/month", "maximum(cost - revenue, 0) / 12"),
    ("runway", "Months of cash left at the end of the year (up to the cap, 60 by default)", "months",
     "minimum(maximum(cash_end, 0) / maximum(burn, 1), runway_cap)"),
    ("gross_margin", "Gross margin after the infrastructure and the delivery payroll", "share",
     "(revenue - infra_cost - delivery_payroll) / maximum(revenue, 1)"),
    ("ltv_cac", "Client lifetime value / acquisition cost", "ratio",
     "acv * gross_margin / maximum(churn, 0.01) / cac"),
]

CRITERIA = [
    Criterion(name="cash", description="Cash at the end of the year", unit="USD", higher_is_better=True,
              weight=1.0, source="it_services", tie_from="md_cash"),
    # fixed RAG limits (variables): the baseline median is the 60-month cap, so it cannot make bands
    Criterion(name="runway", description="Months of cash at the end of the year", unit="months",
              higher_is_better=True, weight=0.5, source="it_services",
              rag_from=RagVariables(green="runway_green", red="runway_red"), tie_from="md_runway"),
    Criterion(name="churn", description="Annual client churn", unit="share", higher_is_better=False,
              weight=1.0, source="it_services", tie_from="md_churn"),
    Criterion(name="sla", description="Availability", unit="share", higher_is_better=True, weight=0.75,
              source="it_services", tie_from="md_sla"),
    Criterion(name="nps", description="Net promoter score", unit="points", higher_is_better=True, weight=0.5,
              source="it_services", tie_from="md_nps"),
]
METRICS = {"runway": "runway", "churn": "churn", "sla": "sla", "nps": "nps"}

# name, description, lever overrides
OPTIONS = [
    ("status_quo", "Keep the current plan", {}),
    ("pay_tech_debt", "Use 15% of capacity for technical debt for a year", {"lever_debt": 1}),
    ("hire_5", "Hire 5 engineers (cost from the month they join)", {"lever_hire": 1}),
    ("reserve_gpu", "Reserve GPU capacity on a second cloud (+0.3M per year)", {"lever_gpu": 1}),
    ("raise_prices_8", "Raise list prices by 8%", {"price_change": 0.08}),
]

# name, description, probability, variable, magnitude
RISKS = [
    ("gpu_shortage_event", "GPU shortage at the cloud provider", 0.25, "gpu_shortage", _pert(0.2, 0.4, 0.7)),
    ("vendor_price_event", "NVIDIA, AWS or a supplier raises prices", 0.20, "vendor_price",
     _pert(0.05, 0.10, 0.25)),
    ("fx_event", "Currency move raises offshore cost", 0.20, "fx_shock", _pert(0.03, 0.08, 0.15)),
    ("regulation_event", "New regulation needs compliance work", 0.15, "regulation_cost",
     _pert(100_000, 250_000, 500_000)),
    ("competitor_event", "A competitor cuts prices", 0.30, "competitor_cut", _pert(0.05, 0.10, 0.20)),
    ("key_person_event", "A key engineer leaves", 0.20, "key_person_exit", _fixed(1)),
    ("key_account_event", "A key account leaves", 0.10, "key_account_lost", _pert(0.05, 0.10, 0.15)),
    ("reputation_event", "A public incident hurts the reputation", 0.10, "reputation_hit", _pert(0.01, 0.03, 0.06)),
    ("macro_event", "Macro downturn lowers demand", 0.20, "demand_growth", _pert(-0.15, -0.10, -0.05)),
]


PAGE = {
    "title": "IT Company Simulator",
    "eyebrow": "VARIATOR · IT services company · one year",
    "lead": "An IT services company (12M USD ARR, 20 people) plans the next year. Which lever gives the best year: "
    "status quo, pay down technical debt, hire 5 engineers, reserve GPU capacity, or raise prices by 8%? "
    "The model links finance, clients, product, operations, people, market and external risks.",
    "scope": "🏢 <b>Company level (strategy).</b> This page compares levers for the whole company over one year. "
    "To choose how to deliver one project or release (in-house, nearshore or offshore), use the case "
    "<b>Best way to deliver</b>.",
    "change": [
        "Your preference: weights of cash, runway, churn, SLA and NPS; risk aversion.",
        "Any factor of the 7 blocks: a fixed value or a range (low, likely, high).",
        "Shocks: the probability and the size of each event (GPU shortage, vendor prices, FX, regulation, competitor, key person, key account, reputation, macro).",
        "Links: each k coefficient sets how strongly one factor moves the next one.",
        "Levers of each option (for example the price change).",
    ],
    "real": [
        "Each block has a real data source: finance, CRM, PMO, monitoring, incident tool, CI/CD, HR, cloud bills, market reports.",
        "The numbers here are a plausible example company, not real data. Put in your own numbers to model your company.",
        "The chains matter more than the list: a GPU shortage raises infra cost and latency, the SLA falls, churn rises, revenue and cash fall.",
        "The k coefficients are assumptions. Calibrate them with your history (for example churn after past SLA breaches).",
    ],
    "metrics": [
        "<b>Outputs:</b> cash at year end, runway (months), churn, SLA (availability), NPS; score, P(best), feasible share, RAG. Runway has fixed RAG limits: green from 18 months, red at 6 months or less.",
        "<b>Chain values:</b> capacity, infra cost, latency, incidents, time to restore, SLA, overdue, NPS, churn, roadmap, new ARR, CAC spend, revenue, cost, burn, runway, gross margin, LTV/CAC.",
        "<b>Rules:</b> burnout (a team load after the hires above 1.05 adds 5 points of attrition), runway below 6 months fails, SLA and churn flags, budget flag. The limits are inputs under “Limits and thresholds”.",
        "<b>Not modelled yet:</b> month-by-month dynamics, several products, sales seasonality.",
    ],
    # names for people on the page (ids stay in the model and the formulas)
    "labels": {
        "status_quo": "Keep the plan", "pay_tech_debt": "Reduce tech debt", "hire_5": "Hire 5 engineers",
        "reserve_gpu": "Reserve GPU capacity", "raise_prices_8": "Raise prices 8%",
        "cash": "Cash at year end", "runway": "Runway", "churn": "Client churn", "sla": "Availability (SLA)",
        "nps": "NPS", "burnout": "Burnout raises attrition", "runway_low": "Runway below 6 months",
        "sla_breach": "SLA breach", "over_budget": "Cost above budget", "churn_high": "High churn",
    },
    # risk headline per criterion or check: the consequence for you (the page adds odds, evidence, choice, watch)
    "risks": {
        "cash": "💰 You may need a credit line or cost cuts by year end",
        "runway": "⏳ You get less time to react: fundraising or cuts come sooner",
        "churn": "🚪 You lose more clients and their recurring revenue",
        "sla": "🛠️ SLA penalties and angry clients are possible",
        "nps": "📉 Fewer referrals and renewals; churn can grow",
        "runway_low": "⛔ Cash runs out within 6 months: the plan is not acceptable then",
    },
    # guided tour (docs/prd/mvp-prd.md §7): the reviewer path in about 10 minutes
    "tour": [
        {"target": "#lead", "min": "0–2 min", "title": "🧩 The question and 5 forks",
         "body": "An IT services company (12M USD ARR, 20 people) plans the next year. The forks: Keep the plan, "
         "Reduce tech debt, Hire 5 engineers, Reserve GPU capacity, Raise prices 8%."},
        {"target": "#presets", "min": "2–4 min", "title": "🌍 World hypotheses",
         "body": "Situations outside your control. They add up as layers, and all forks calculate again at once.",
         "try": "Turn on “GPU shortage” and “Big client may leave”, then read the Best path."},
        {"target": "#timeline", "min": "4–6 min", "title": "⏱️ Timeline",
         "body": "Plans, events, milestones and goals over 12 months. P(met) shows how often a goal is met "
         "in the simulated futures.",
         "try": "Place the “Key account leaves” event, or choose “Hire 5 engineers” in “Show for” and move "
         "“Hiring starts”."},
        {"target": "#answer", "min": "6–8 min", "title": "🧭 The answer",
         "body": "The decision, how often it is best, the expected result with its spread, and the numbered risks.",
         "try": "Read R1: the consequence, its odds, the choice that changes it and what to watch."},
        {"target": "nav.pages", "min": "8–10 min", "title": "📦 Same engine, another domain",
         "body": "The IT delivery page uses the same engine for one project: in-house, nearshore or offshore.",
         "try": "Open “IT delivery · one project”."},
    ],
    "chain": ["gpu_hit", "infra_cost", "latency", "incidents", "mttr_eff", "sla", "nps", "overdue", "capacity",
              "churn", "roadmap", "new_arr", "revenue", "cost", "cash_end", "runway", "ltv_cac"],
}


def company_model() -> DecisionModel:
    variables = [
        Variable(name=n, description=d, unit=u, distribution=dist, source=f"{block} | {s}")
        for block, n, d, u, dist, s in VARIABLES
    ]
    derived = [Derived(name=n, description=d, unit=u, formula=f) for n, d, u, f in DERIVED]
    options = [
        DecisionOption(
            name=name,
            description=desc,
            formula="cash_end",
            metrics=dict(METRICS),
            overrides={k: _fixed(v) for k, v in levers.items()},
        )
        for name, desc, levers in OPTIONS
    ]
    risks = [
        Risk(name=n, description=d, probability=p, variable=v, effect=RiskEffect.ADD, magnitude=m)
        for n, d, p, v, m in RISKS
    ]
    return DecisionModel(
        question="Which lever gives the IT services company the best year: status quo, pay down technical debt, "
        "hire, reserve GPU capacity, or raise prices?",
        metric=MetricSpec(name="cash at the end of the year", unit="USD"),
        variables=variables,
        derived=derived,
        options=options,
        criteria=CRITERIA,
        risks=risks,
        assumptions=[
            "One year, mid-year averages; no month-by-month dynamics.",
            "All k_* link coefficients are assumptions; calibrate them with the company's own history.",
        ],
    )
