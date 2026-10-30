"""The catalogue: every metric definition with its default parameters, and the metrics of each role.

Texts may name parameters in braces (``{window_days}``, ``{target}``, ``{opt.slo_ms}``): the engine fills
them from the current parameters, so the text and the formula stay the same (PRD D11).
"""

from __future__ import annotations

from variator.metrics.catalog import delivery as dl
from variator.metrics.catalog import finance as fi
from variator.metrics.catalog import growth as gr
from variator.metrics.catalog.core import MetricDef, MetricRef, RoleId
from variator.metrics.catalog.params import BandsParam, CatalogParams, MetricParams


def _p(target=None, good=None, warn=None, window=None, **options) -> MetricParams:
    bands = BandsParam(good=good, warn=warn) if good is not None and warn is not None else None
    return MetricParams(target=target, bands=bands, window_days=window, options=options)


BANK = "Bank entries (sim BankEntry)"
# traffic share of each production service: the weights of the platform SLA and p95 (company parameters)
TRAFFIC = {
    "api": 0.30,
    "web-app": 0.22,
    "auth": 0.10,
    "integrations": 0.12,
    "billing-engine": 0.08,
    "data-pipeline": 0.08,
    "ml-inference": 0.10,
}
INVOICES = "Invoices and vendor bills (sim Invoice)"
CONTRACTS = "Client contracts (sim Contract)"

CATALOG: tuple[MetricDef, ...] = (
    # ------------------------------------------------------------------ money
    MetricDef(
        id="fin.cash",
        title="Cash",
        question="How much cash do we have?",
        definition="The balance of the operating bank account after the last bank entry booked by the date.",
        how_calculated=(
            "Take the bank entries known by the end of the date.",
            "The value is the balance after the latest entry.",
            "Check: the balance {window_days} days earlier + credits − debits since then"
            " gives the same value.",
            "Breakdown: net flow of the last {window_days} days by counterparty.",
        ),
        unit="money",
        gen_unit="usd",
        direction="higher",
        view="kpi",
        sources=(BANK,),
        needs=("sim_bank_entry",),
        defaults=_p(2_000_000, 2_000_000, 1_000_000, 30),
        compute=fi.cash,
        reference=MetricRef("Cash and cash equivalents", "https://en.wikipedia.org/wiki/Cash_and_cash_equivalents"),
        items_label="Bank entries of the last {window_days} days",
        family="finance",
    ),
    MetricDef(
        id="fin.runway",
        title="Runway",
        question="When does the money run out?",
        definition="Months the cash lasts at the average net burn of the last {window_days} days; "
        "capped at {opt.cap_months} months when cash grows.",
        how_calculated=(
            "Cash = the balance after the latest bank entry.",
            "Monthly net burn = (debits − credits) of the last {window_days} days × 30.44 / days.",
            "Runway = min(cash / max(burn, 1), {opt.cap_months}) months, as in company_model.",
        ),
        unit="months",
        gen_unit="months",
        direction="higher",
        view="kpi",
        sources=(BANK,),
        needs=("sim_bank_entry",),
        defaults=_p(12, 12, 6, 90, cap_months=60, min_days=28),
        compute=fi.runway,
        reference=MetricRef("Burn rate and runway", "https://en.wikipedia.org/wiki/Burn_rate"),
        items_label="Cash and the largest debits behind the burn",
        digits=1,
        family="finance",
    ),
    MetricDef(
        id="fin.burn",
        title="Net burn per month",
        question="How much cash do we lose a month?",
        definition="Average monthly net outflow of the bank account over the last {window_days} days: "
        "debits minus "
        "credits. A negative value means more cash came in than went out.",
        how_calculated=(
            "Bank entries booked in the last {window_days} days (the opening balance is not a flow).",
            "Net burn = (debits − credits) × 30.44 / days in the window.",
            "Breakdown: the monthly contribution of each counterparty (receipts are negative).",
        ),
        unit="money",
        gen_unit="usd",
        direction="lower",
        view="kpi",
        sources=(BANK,),
        needs=("sim_bank_entry",),
        defaults=_p(0, 0, 100_000, 90, min_days=28),
        compute=fi.burn,
        reference=MetricRef("Burn rate", "https://en.wikipedia.org/wiki/Burn_rate"),
        items_label="Bank entries of the window, largest first",
        family="finance",
    ),
    MetricDef(
        id="fin.revenue",
        title="Revenue (last closed month)",
        question="How much did we earn in the last closed month?",
        definition="Client revenue of the reporting month: the latest month before the date with all "
        "its cost records known. Invoices are spread over their service months (a quarterly invoice "
        "gives one third a month).",
        how_calculated=(
            "Find the reporting month: payroll, the cloud bill and a bill of each active vendor are known.",
            "Take the client invoices known by the date whose service period covers that month.",
            "Revenue = sum(amount / months of the period).",
        ),
        unit="money",
        gen_unit="usd",
        direction="higher",
        view="kpi",
        sources=(INVOICES,),
        needs=("sim_invoice", "sim_payroll", "sim_cloud_bill"),
        defaults=_p(1_000_000, 1_000_000, 900_000),
        compute=fi.revenue,
        reference=MetricRef("Revenue", "https://en.wikipedia.org/wiki/Revenue"),
        items_label="Invoices of the month",
        family="finance",
    ),
    MetricDef(
        id="fin.costs",
        title="Costs (last closed month)",
        question="What did the company spend in the last closed month?",
        definition="Costs of the reporting month: gross payroll, employer payroll tax, "
        "vendor bills for the month "
        "and the cloud bill. Corporation tax is not a cost here.",
        how_calculated=(
            "The reporting month as for revenue.",
            "Payroll record of the month: gross pay + employer tax.",
            "Vendor bills whose period is the month, and the cloud bill of the month.",
            "Breakdown by category and vendor.",
        ),
        unit="money",
        gen_unit="usd",
        direction="context",
        view="kpi",
        sources=(INVOICES, "Payroll (sim Payroll)", "Cloud bills (sim CloudBill)"),
        needs=("sim_invoice", "sim_payroll", "sim_cloud_bill"),
        defaults=_p(),
        compute=fi.costs,
        reference=MetricRef("Operating expense", "https://en.wikipedia.org/wiki/Operating_expense"),
        items_label="Cost records of the month",
        family="finance",
    ),
    MetricDef(
        id="fin.gross_margin",
        title="Gross margin",
        question="How much of the revenue is left after the cost of delivery?",
        definition="(Revenue − cloud − delivery payroll − subcontractors) / revenue for the reporting month. "
        "Delivery payroll is the gross pay of {opt.delivery_departments} with employer tax; subcontractors "
        "deliver client work, so their bills are cost of revenue (as in the case model).",
        how_calculated=(
            "Revenue of the reporting month (as in Revenue).",
            "Infrastructure = the cloud bill of the month.",
            "Delivery payroll = payroll lines of {opt.delivery_departments}"
            " × (1 + employer tax rate of the record).",
            "Subcontractors = bills of {opt.contractor_vendors} for the month.",
            "Margin = (revenue − infrastructure − delivery payroll − subcontractors − bills of cogs_vendors)"
            " / revenue × 100.",
            "Target {target}%: an assumption for IT services with heavy subcontracting"
            " (the history has no plan margin).",
        ),
        unit="percent",
        gen_unit="percent",
        direction="higher",
        view="kpi",
        sources=(INVOICES, "Payroll (sim Payroll)", "Cloud bills (sim CloudBill)", "People"),
        needs=("sim_invoice", "sim_payroll", "sim_cloud_bill", "people"),
        # target 30% (good ≥ 30, watch ≥ 20): an assumption, the history spec has no plan margin
        defaults=_p(
            30,
            30,
            20,
            delivery_departments=["Delivery", "Engineering", "Support"],
            cogs_vendors=[],
            contractor_vendors=["VN-01", "VN-02"],
        ),
        compute=fi.gross_margin,
        reference=MetricRef("Gross margin", "https://en.wikipedia.org/wiki/Gross_margin"),
        items_label="Cost records and invoices of the month",
        family="finance",
    ),
    MetricDef(
        id="fin.dso",
        title="Days sales outstanding",
        question="How long do clients take to pay?",
        definition="Open client receivables divided by the average daily billing of the last "
        "{window_days} days.",
        how_calculated=(
            "Open receivables: client invoices issued by the date and not paid at the date.",
            "Daily billing: client invoices issued in the last {window_days} days / {window_days}.",
            "DSO = open receivables / daily billing.",
        ),
        unit="days",
        gen_unit="days",
        direction="lower",
        view="kpi",
        sources=(INVOICES,),
        needs=("sim_invoice",),
        defaults=_p(60, 60, 75, 90),
        compute=fi.dso,
        reference=MetricRef("Days sales outstanding", "https://en.wikipedia.org/wiki/Days_sales_outstanding"),
        items_label="Open receivables, most overdue first",
        digits=1,
        family="finance",
    ),
    MetricDef(
        id="fin.taxes",
        title="Taxes paid · {window_days} days",
        question="How much do taxes take from cash?",
        definition="PAYE, National Insurance and corporation tax paid in the last {window_days} days; "
        "the payments "
        "due in the next {opt.due_ahead_days} days are a caveat.",
        how_calculated=(
            "Tax payment records known by the date.",
            "Sum the ones paid in the last {window_days} days; split by tax.",
            "Scheduled payments due in the next {opt.due_ahead_days} days are listed apart.",
        ),
        unit="money",
        gen_unit="usd",
        direction="context",
        view="kpi",
        sources=("Tax payments (sim TaxPayment)",),
        needs=("sim_tax_payment",),
        defaults=_p(window=90, due_ahead_days=30),
        compute=fi.taxes,
        reference=MetricRef("Tax expense", "https://en.wikipedia.org/wiki/Tax_expense"),
        items_label="Tax payments",
        family="finance",
    ),
    MetricDef(
        id="fin.budget_variance",
        title="Budget variance",
        question="Do we spend more than the budget?",
        definition="Costs of the last {opt.months} closed months against the budget for them "
        "({opt.annual_budget} USD a year, a parameter). Positive: over budget.",
        how_calculated=(
            "Costs of each of the last {opt.months} reporting months (as in Costs).",
            "Budget = annual_budget / 12 × months.",
            "Variance = (costs − budget) / budget × 100.",
        ),
        unit="percent",
        gen_unit="percent",
        direction="lower",
        view="kpi",
        sources=(INVOICES, "Payroll (sim Payroll)", "Cloud bills (sim CloudBill)", "Budget parameter"),
        needs=("sim_invoice", "sim_payroll", "sim_cloud_bill"),
        defaults=_p(0, 0, 5, months=3, annual_budget=11_200_000),
        compute=fi.budget_variance,
        reference=MetricRef("Variance analysis (accounting)", "https://en.wikipedia.org/wiki/Variance_analysis_(accounting)"),
        items_label="Cost records of the months",
        family="finance",
    ),
    # ------------------------------------------------------------------ clients and people
    MetricDef(
        id="rev.arr",
        title="ARR",
        question="How much recurring revenue do our contracts bring a year?",
        definition="Annual recurring revenue: the annual contract value in force on the date, "
        "summed over the client "
        "contracts that have started and not ended.",
        how_calculated=(
            "Client contracts known by the date (vendor agreements are left out).",
            "In force: start date on or before the date; no end date, or an end after it.",
            "ACV = the step of the contract's ACV schedule in force on the date; sum them.",
        ),
        unit="money",
        gen_unit="usd",
        direction="higher",
        view="kpi",
        sources=(CONTRACTS,),
        needs=("sim_contract",),
        defaults=_p(12_000_000, 12_000_000, 11_400_000),
        compute=gr.arr,
        reference=MetricRef("Annual recurring revenue", "https://en.wikipedia.org/wiki/Annual_recurring_revenue"),
        items_label="Contracts in force, largest first",
        family="clients",
    ),
    MetricDef(
        id="rev.churn",
        title="Churn (gross ARR)",
        question="Which share of our ARR do we lose a year?",
        definition="ARR lost to contract terminations and downgrades in the last {window_days} days, "
        "divided by "
        "the ARR at the start of that window; annualised when the history is shorter.",
        how_calculated=(
            "ARR at the window start: contracts in force then (as known then).",
            "Lost: the ACV before the end of each contract terminated in the window,"
            " and the ACV removed by downgrades.",
            "Churn = lost / ARR at the start × 100 (× 365 / days when the window is shorter than a year).",
            "Unknown with less than {opt.min_days} days of history.",
        ),
        unit="percent",
        gen_unit="percent",
        direction="lower",
        view="kpi",
        sources=(CONTRACTS, "Life events (sim LifeEvent)"),
        needs=("sim_contract",),
        defaults=_p(12, 12, 18, 365, min_days=90, annualise=True),
        compute=gr.churn,
        reference=MetricRef("Churn rate", "https://en.wikipedia.org/wiki/Churn_rate"),
        items_label="Contracts lost and downgraded",
        family="clients",
    ),
    MetricDef(
        id="cust.nps",
        title="NPS",
        question="Would our clients recommend us?",
        definition="Net promoter score of the latest client survey (option mode = {opt.mode}; "
        "mode window: the "
        "answers of the last {window_days} days): the share of promoters (score {opt.promoter_min}–10) minus "
        "the share of detractors (0–{opt.detractor_max}).",
        how_calculated=(
            "Survey answers (nps_response records) known by the date.",
            "Mode survey: the answers of the latest survey with an answer;"
            " mode window: the last {window_days} days.",
            "NPS = (promoters − detractors) / answers × 100; the breakdown gives every survey so far.",
            "Unknown when the history has no survey data: unknown is not zero.",
        ),
        unit="index",
        gen_unit="index",
        direction="higher",
        view="kpi",
        sources=("NPS survey answers (sim NpsResponse, v2)",),
        needs=("nps_responses",),
        defaults=_p(40, 40, 0, 90, mode="survey", promoter_min=9, detractor_max=6, min_responses=10),
        compute=gr.nps,
        reference=MetricRef("Net promoter score", "https://en.wikipedia.org/wiki/Net_promoter_score"),
        items_label="Survey answers, lowest first",
        digits=1,
        family="clients",
    ),
    MetricDef(
        id="rev.new_arr",
        title="New ARR · {window_days} days",
        question="How much new recurring revenue did we win?",
        definition="ACV of new client contracts plus the ACV added by upsell amendments, "
        "recorded in the last "
        "{window_days} days. Renewal price uplifts are not new ARR.",
        how_calculated=(
            "Contract versions known by the date and recorded in the window.",
            "New contract: its first ACV. Upsell amendment: the new ACV step − the previous one.",
            "Sum; each record links its won deal.",
        ),
        unit="money",
        gen_unit="usd",
        direction="higher",
        view="kpi",
        sources=(CONTRACTS, "Deals (sim Deal)"),
        needs=("sim_contract",),
        defaults=_p(750_000, 750_000, 375_000, 90),
        compute=gr.new_arr,
        reference=MetricRef("Annual recurring revenue", "https://en.wikipedia.org/wiki/Annual_recurring_revenue"),
        items_label="New contracts and upsells",
        family="clients",
    ),
    MetricDef(
        id="ppl.headcount",
        title="Headcount",
        question="How many people work for us?",
        definition="Employees on the date: started on or before it and not past their last day.",
        how_calculated=(
            "People of the company with their start and last day.",
            "Count the ones employed on the date; split by department.",
            "Joiners and leavers of the last {opt.recent_days} days are a caveat.",
        ),
        unit="count",
        gen_unit="count",
        direction="context",
        view="kpi",
        sources=("People directory", "Employee events (sim EmployeeEvent)"),
        needs=("people",),
        defaults=_p(20, recent_days=90),
        compute=gr.headcount,
        reference=MetricRef("VARIATOR", None, note="A count of the people employed on the date, from the HR records. No public standard: a plain count."),
        items_label="Employees",
        digits=0,
        family="people",
    ),
    # ------------------------------------------------------------------ delivery and operations
    MetricDef(
        id="ops.sla",
        title="SLA (availability)",
        question="Do we keep the availability we promise?",
        definition="Platform availability of the last {window_days} days from daily monitoring, "
        "weighted by the traffic share of each service, minus the company_model latency penalty; "
        "the contract target is 99.5%.",
        how_calculated=(
            "Daily monitoring rows (service, day) of the last {window_days} days.",
            "Platform availability of a day = sum(traffic weight × availability) of the services;"
            " mean over days.",
            "Penalty = mean over days of {opt.penalty_per_50ms} × max(platform p95 − {opt.slo_ms} ms, 0)"
            " / 50.",
            "SLA = availability − penalty, in percent. Unknown without daily monitoring.",
        ),
        unit="percent",
        gen_unit="percent",
        direction="higher",
        view="kpi",
        sources=("Daily monitoring (v2)", "Incidents (sim Incident)"),
        needs=("monitoring_daily",),
        defaults=_p(
            99.5,
            99.5,
            99.0,
            30,
            service_weights=dict(TRAFFIC),
            latency_penalty=True,
            slo_ms=250,
            penalty_per_50ms=0.002,
        ),
        compute=dl.sla,
        reference=MetricRef("Service-level agreement", "https://en.wikipedia.org/wiki/Service-level_agreement"),
        items_label="Incidents of the window",
        digits=3,
        family="operations",
    ),
    MetricDef(
        id="ops.latency",
        title="p95 latency",
        question="Is the platform fast enough?",
        definition="Platform 95th-percentile response time: "
        "the traffic-weighted p95 of the services each day, "
        "averaged over the last {window_days} days; the latency objective is {opt.slo_ms} ms.",
        how_calculated=(
            "Daily monitoring rows of the last {window_days} days.",
            "Platform p95 of a day = sum(traffic weight × p95_ms) of the services.",
            "Latency = mean over the days. Unknown without daily monitoring.",
        ),
        unit="ms",
        gen_unit="ms",
        direction="lower",
        view="kpi",
        sources=("Daily monitoring (v2)",),
        needs=("monitoring_daily",),
        # on track at the 180 ms target; above the 250 ms objective needs attention
        defaults=_p(180, 180, 250, 7, slo_ms=250, service_weights=dict(TRAFFIC)),
        compute=dl.latency,
        reference=MetricRef("Latency (engineering)", "https://en.wikipedia.org/wiki/Latency_(engineering)"),
        items_label="Incidents of the window",
        digits=1,
        family="operations",
    ),
    MetricDef(
        id="ops.incidents",
        title="Incidents · {window_days} days",
        question="How often does the platform break?",
        definition="Incidents that started in the last {window_days} days, per 30 days.",
        how_calculated=(
            "Incident records known by the date.",
            "Count the ones that started in the last {window_days} days; × 30 / {window_days}.",
            "Breakdown by severity; each incident links its declared cause (often a deploy).",
        ),
        unit="count",
        gen_unit="count",
        direction="lower",
        view="kpi",
        sources=("Incidents (sim Incident)",),
        needs=("sim_incident",),
        defaults=_p(4, 4, 6, 30),
        compute=dl.incidents,
        reference=MetricRef("Incident management (ITSM)", "https://en.wikipedia.org/wiki/Incident_management_(ITSM)"),
        items_label="Incidents, newest first",
        family="operations",
    ),
    MetricDef(
        id="ops.mttr",
        title="Time to restore (MTTR)",
        question="How fast do we restore the service?",
        definition="The {opt.stat} hours from the start of an incident to the restored service, "
        "over the incidents "
        "resolved in the last {window_days} days (DORA).",
        how_calculated=(
            "Incident records resolved in the last {window_days} days.",
            "Restore time = restore_minutes / 60.",
            "MTTR = {opt.stat} of the restore times; the sample size is shown.",
        ),
        unit="hours",
        gen_unit="hours",
        direction="lower",
        view="kpi",
        sources=("Incidents (sim Incident)",),
        needs=("sim_incident",),
        defaults=_p(2, 2, 4, 90, stat="mean", min_sample=5),
        compute=dl.mttr,
        reference=MetricRef("DORA metrics: failed deployment recovery time", "https://dora.dev/guides/dora-metrics/"),
        items_label="Resolved incidents, longest first",
        family="operations",
    ),
    MetricDef(
        id="ops.overdue_implementations",
        title="Late implementations · {window_days} days",
        question="Which client implementations are late?",
        definition="Client implementations that went live after their promised date in the last "
        "{window_days} days, plus open ones already older than the longest promise "
        "({opt.promise_bdays} business days for an "
        "onboarding, {opt.upsell_promise_bdays} for an upsell rollout).",
        how_calculated=(
            "Went live late: contract records of a completed implementation with go_live_late_days > 0.",
            "Open past the promise: {opt.project} epics not Done at the date (Jira transitions), "
            "with an age in "
            "business days above the longest promise of their kind.",
            "Value = both counts. The promised date of an open project is not in the records.",
        ),
        unit="count",
        gen_unit="count",
        direction="lower",
        view="list",
        sources=("Jira", "Client contracts (sim Contract)"),
        needs=("issues", "transitions"),
        defaults=_p(
            0,
            0,
            1,
            90,
            promise_bdays=50,
            upsell_promise_bdays=28,
            onboarding_prefix="Onboarding:",
            project="IMPL",
            issue_type="Epic",
        ),
        compute=dl.overdue_implementations,
        reference=MetricRef("VARIATOR", None, note="Implementations past their planned go-live date, from the project records. Specific to this company type."),
        items_label="Late and overdue implementations",
        digits=0,
        family="delivery",
    ),
    MetricDef(
        id="ops.team_load",
        title="Team load",
        question="Is the team over capacity?",
        definition="Assigned work / capacity of the engineers in the last finished sprint, "
        "from sprint planning. "
        "Above 105% the burnout rule raises attrition.",
        how_calculated=(
            "The last two-week sprint that ended by the date.",
            "Its load from the sprint planning record × 100.",
            "Records: the issues finished in the sprint and the people who started them.",
        ),
        unit="percent",
        gen_unit="percent",
        direction="lower",
        view="kpi",
        sources=("Sprint planning", "Jira"),
        needs=("sprint_load",),
        defaults=_p(95, 95, 105, baseline_sprints=6, projects=["PLAT", "IMPL"]),
        compute=dl.team_load,
        reference=MetricRef("Capacity utilization", "https://en.wikipedia.org/wiki/Capacity_utilization"),
        items_label="People and the work they finished in the sprint",
        digits=1,
        family="delivery",
    ),
    MetricDef(
        id="ops.throughput",
        title="Throughput · {window_days} days",
        question="How much work do we finish?",
        definition="Jira issues of {opt.projects} (no epics) moved to Done for the first time in the last "
        "{window_days} days.",
        how_calculated=(
            "Status history of the Jira issues known by the date.",
            "Count the first moves to Done in the window.",
            "Baseline: the average of the previous {opt.baseline_weeks} windows.",
        ),
        unit="count",
        gen_unit="count",
        direction="higher",
        view="kpi",
        sources=("Jira",),
        needs=("issues", "transitions"),
        defaults=_p(25, 25, 15, 7, projects=["PLAT", "IMPL"], baseline_weeks=8),
        compute=dl.throughput,
        reference=MetricRef("Throughput (business)", "https://en.wikipedia.org/wiki/Throughput_(business)"),
        items_label="Issues done",
        digits=0,
        family="delivery",
    ),
    MetricDef(
        id="ops.open_requisitions",
        title="Open requisitions",
        question="Which roles are we still hiring for?",
        definition="Requisitions that are not filled or cancelled at the date.",
        how_calculated=(
            "Requisition records known by the date.",
            "Count the ones whose status is not {opt.closed_statuses}.",
            "Each one links the decision that opened it.",
        ),
        unit="count",
        gen_unit="count",
        direction="lower",
        view="list",
        sources=("Requisitions (sim Requisition)", "Decisions (sim Decision)"),
        needs=("sim_requisition",),
        defaults=_p(0, 0, 2, closed_statuses=["filled", "cancelled", "closed"]),
        compute=dl.open_requisitions,
        reference=MetricRef("VARIATOR", None, note="Hiring requisitions that are open on the date, from the HR records. No public standard: a plain count."),
        items_label="Open requisitions",
        digits=0,
        family="people",
    ),
    MetricDef(
        id="ops.deploys",
        title="Deploys per week",
        question="How often do we ship to production?",
        definition="Production deploys of the last {window_days} days, per week (DORA deployment frequency).",
        how_calculated=(
            "Deploy records known by the date, environment {opt.environment}.",
            "Count the deploys of the last {window_days} days; × 7 / {window_days}.",
            "Rollbacks and incidents caused by a deploy are a caveat.",
        ),
        unit="count",
        gen_unit="count",
        direction="higher",
        view="kpi",
        sources=("Deploys (sim Deploy)",),
        needs=("sim_deploy",),
        defaults=_p(10, 10, 5, 28, environment="production"),
        compute=dl.deploys,
        reference=MetricRef("DORA metrics: deployment frequency", "https://dora.dev/guides/dora-metrics/"),
        items_label="Deploys, newest first",
        family="operations",
    ),
)

# The user roles: the MVP has one, the CEO, who sees every metric. The head of finance and the head of
# delivery stay people of the company (owners of levers), not users.
ROLES: dict[RoleId, dict[str, str]] = {
    "ceo": {"label": "CEO", "question": "Which lever gives the best year, and what can go wrong?"},
}

# The CEO brief: a short top row of key numbers, then the areas (a metric's area comes from its family).
TOP: tuple[str, ...] = ("rev.arr", "fin.cash", "fin.runway", "rev.churn", "ops.sla")
AREAS: tuple[dict[str, str], ...] = (
    {"id": "money", "title": "Money", "question": "Is there enough cash, and where does the money go?"},
    {"id": "clients", "title": "Clients and growth", "question": "Do we win, keep and please our clients?"},
    {
        "id": "delivery",
        "title": "Delivery and service",
        "question": "Does the platform run well, and does the team deliver on time?",
    },
)

BY_ID: dict[str, MetricDef] = {m.id: m for m in CATALOG}


def metrics_for(role: RoleId = "ceo") -> list[MetricDef]:
    """The metrics of a role: every metric, because the one MVP role (the CEO) covers all."""
    return list(CATALOG) if role in ROLES else []


def brief_layout(ids: list[str] | None = None) -> tuple[list[str], list[dict]]:
    """The top row and the areas of the CEO brief for the metric ids given (default: the catalogue).

    Every metric is in one place: in the top row, or in the area of its family (catalogue order).
    """
    have = [m.id for m in CATALOG] if ids is None else ids
    top = [i for i in TOP if i in have]
    areas = [{**a, "metrics": [i for i in have if i not in top and BY_ID[i].area == a["id"]]} for a in AREAS]
    return top, areas


def default_params() -> CatalogParams:
    return CatalogParams(metrics={m.id: m.defaults.model_copy(deep=True) for m in CATALOG})
