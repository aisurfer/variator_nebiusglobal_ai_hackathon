"""Case A "Best way to deliver" (delivery_best_way): who builds the new platform (delivery_model).

In-house team (8 people), the nearshore vendor Vistula Code Works on time and materials (10 people), or the offshore
vendor Kaveri Tech Services at a fixed price plus change requests (16 people). Meanings follow the hand-made page
docs/playground/it-delivery-simulator.html and the Nemotron instruction bundle (case 1). The generic page
has no RAID log and no codebase block (the hand-made page has both); they are left out.
"""

# ruff: noqa: E501 - data tables, one row per entry

from __future__ import annotations

from case_spec import CaseSpec

PAGE = {
    "title": "IT Delivery Simulator",
    "eyebrow": "VARIATOR · IT delivery · one platform",
    "lead": "Larchwood Systems delivers a new platform for a client: target go-live in month 9, a budget of $100k a "
    "month. Who builds it: an in-house team of 8, the nearshore vendor Vistula Code Works on time and materials "
    "(10 people), or the offshore vendor Kaveri Tech Services at a fixed price plus change requests (16 people)? "
    "Money is the business value over 24 months minus the delivery cost.",
    "change": [
        "Your preference: weights of money, time to go-live, management effort and months late; risk aversion.",
        "Scope, productivity, rates, ramp-up, rework, change requests, the fixed quote, the budget and the target go-live: a fixed value or a range (low, likely, high).",
        "Each fork has its own team size, rates, productivity, ramp-up, rework and management effort.",
        "Timeline: when the load-test environment work starts, and how many extra people to request and when.",
    ],
    "real": [
        "Each input has a real source: the backlog export, the vendor rate cards and quotes, the HR lead times, the budget.",
        "The numbers are a plausible example, not real data. Put in your own numbers to model your project.",
        "The chain matters more than the list: team throughput and the scope give the build time; the load-test environment can block the go-live; the value starts at go-live.",
        "Rework, change requests and vendor rotation are assumptions. Calibrate them with past projects.",
    ],
    "metrics": [
        "Outputs: money at month 24 (value minus cost), time to go-live, management effort, months late; score, P(best), feasible share, RAG.",
        "Chain values: throughput, work, build time, environment ready, go-live month, beta month, cost of the extra people, business value.",
        "Rules: a small team with a big scope reworks more; a monthly cost above the budget fails (in-house and nearshore); change requests, rework and a late go-live are flagged.",
        "Not on this page: the RAID log and the codebase metrics of the hand-made delivery page.",
    ],
    "labels": {
        "in_house": "In-house team",
        "nearshore_tm": "Nearshore T&M",
        "offshore_fixed": "Offshore fixed price",
        "money": "Money at month 24",
        "time": "Time to go-live",
        "resources": "Management effort",
        "delay_months": "Months late",
        "burn_over_budget": "Monthly cost above budget",
        "small_team_big_scope": "Small team, big scope",
        "change_requests": "Many change requests",
        "high_rework": "High rework",
        "late_go_live": "Go-live far beyond target",
    },  # fmt: skip
    # risk headline per criterion or check: the consequence for you (no emoji: the engine adds odds and evidence)
    "risks": {
        "money": "The business case can fail: value minus cost falls short",
        "time": "The go-live comes late and the value starts later",
        "resources": "Your managers can get overloaded",
        "delay_months": "You can miss the promised date: penalties and lost trust",
        "burn_over_budget": "The monthly cost can go over budget: the plan needs more money",
    },
    "chain": [
        "cap",
        "add_cap",
        "t_add",
        "work",
        "build_time",
        "env_ready",
        "duration",
        "beta_time",
        "add_cost",
        "tm_cost",
        "value",
    ],
}

PEOPLE = [
    ("LW-E03", "Jordan Patel", "Head of Delivery"),
    ("LW-E01", "Avery Collins", "Chief Executive Officer"),
    ("LW-E02", "Morgan Reyes", "Head of Finance"),
    ("LW-E04", "Hannah Okafor", "Project Manager"),
    ("LW-E06", "Daniel Kowalski", "Lead Backend Engineer"),
    ("LW-E22", "Victor Osei", "DevOps Engineer"),  # in the DevOps seat of Kwame Mensah (LW-E12, left before T)
]
SHORT = {
    "env_ready": "Environment ready", "duration": "Go-live month", "add_cap": "Extra throughput", "add_cost": "Cost of extra people",
    "work": "Work", "build_time": "Build time", "cap": "Team throughput", "value": "Business value",
    "t_add": "Extra people at full speed", "beta_time": "Beta month", "tm_cost": "Time-and-materials cost",
}  # fmt: skip
ITEMS = {
    "env_work": {
        "lane": "decisions", "owner": "LW-E22", "affects": ["env_ready", "duration"], "range": "env_lead", "source": "plan · DevOps estimate",
        "description": "Work on the load-test environment starts. The go-live waits for the environment and two weeks of load tests.",
    },
    "extra_people": {
        "lane": "decisions", "owner": "LW-E03", "affects": ["add_cap", "duration", "add_cost"],
        "value": {"variable": "add_fte", "label": "Extra people", "min": 0, "max": 8, "step": 1},
        "range": "hire_onboard", "source": "plan · HR and vendor terms",
        "description": "Request extra people. Hires start after 2-5 months; vendor people after 0.5-1.5 months at a 20% higher rate.",
    },
    "scope_change": {
        "lane": "clients_vendors", "owner": "LW-E04", "affects": ["work", "duration"], "shock": "scope_creep",
        "label": "The client adds 100 points of scope",
        "description": "The client adds 100 story points. It counts only when it comes before the planned go-live.",
    },
    "key_person_leaves": {
        "lane": "people", "owner": "LW-E03", "person": "LW-E06", "entity": "person.lw-e06", "affects": ["build_time", "duration"],
        "shock": "key_person_loss", "label": "Daniel Kowalski leaves",
        "description": "A key person of the team leaves: the rest of the work takes 14% longer from that month.",
    },
}  # fmt: skip
SHOCKS = {
    "key_person_loss": {"lane": "people", "owner": "LW-E03", "affects": ["cap", "duration"], "event": "key_person_leaves", "label": "A senior engineer of the in-house team leaves"},
    "vendor_turnover": {"lane": "clients_vendors", "owner": "LW-E03", "affects": ["cap", "duration"], "label": "The vendor rotates its team"},
    "scope_creep": {"lane": "clients_vendors", "owner": "LW-E04", "affects": ["work", "duration"], "event": "scope_change", "label": "The business adds features"},
}  # fmt: skip
BLOCKS = {
    "scope": ("Scope", ["scope_points", "change_share"]),
    "team": ("Team and rates", ["team_size", "productivity", "rate", "ramp_up_months", "mgmt_ratio", "hire_onboard"]),
    "vendor": ("Vendor terms", ["fixed_quote", "vendor_onboard", "add_rate_premium"]),
    "quality": ("Quality and tests", ["rework_share", "env_lead", "load_test_months"]),
    "money": ("Value and budget", ["value_per_month", "budget_per_month"]),
    "risk": ("Delivery risks", ["risk_delay_months"]),
    "lever": ("Levers", ["add_fte", "add_outsourced"]),
    "limits": ("Limits and thresholds", ["md_money", "md_time", "md_resources", "md_delay_months"]),
    "timeline": ("Timeline and events", ["target_months", "add_month", "env_start", "ev_scope_points", "ev_scope_month", "ev_kp", "ev_kp_month", "m_beta", "m_env", "g_money"]),
}  # fmt: skip

DELIVERY = CaseSpec(
    id="delivery_best_way",
    letter="A",
    title="Best way to deliver",
    company="Larchwood Systems",
    question="How should Larchwood Systems deliver the new platform: an in-house team, the nearshore vendor on time "
    "and materials, or the offshore vendor at a fixed price?",
    lead="Larchwood Systems delivers a new platform with a target go-live in month 9 and a budget of $100k a month. "
    "Which way gives the best result over 24 months: in-house, nearshore time and materials, or offshore fixed price?",
    owner="LW-E03",
    template="delivery_model",
    page=PAGE,
    status="ready",
    fork_date="2026-10-26",
    horizon=24,
    people=PEOPLE,
    priorities=[
        (
            "balanced",
            "Balanced",
            "The model defaults: money and time first, then months late and management effort.",
            None,
        ),
        (
            "money_first",
            "Money first",
            "Money at month 24 counts most; time and effort count less.",
            {"money": 2.0, "time": 0.5, "resources": 0.25, "delay_months": 0.5},
        ),
        (
            "speed_first",
            "Speed first",
            "Time to go-live and months late count most; money counts less.",
            {"money": 0.5, "time": 2.0, "resources": 0.25, "delay_months": 1.5},
        ),
        (
            "low_effort",
            "Low effort",
            "Management effort counts most: the team should run with little oversight.",
            {"money": 0.5, "time": 0.5, "resources": 2.0, "delay_months": 0.5},
        ),
    ],
    recompute={"in_house": None, "nearshore_tm": "structural", "offshore_fixed": "structural"},
    items=ITEMS,
    shocks=SHOCKS,
    short_labels=SHORT,
    chain_path=[["cap"], ["work"], ["build_time", "env_ready"], ["duration"], ["value"]],
    chain_higher={
        "cap": True,
        "add_cap": True,
        "t_add": False,
        "work": False,
        "build_time": False,
        "env_ready": False,
        "duration": False,
        "beta_time": False,
        "add_cost": False,
        "tm_cost": False,
        "value": True,
    },  # fmt: skip
    blocks=[(b, label) for b, (label, _v) in BLOCKS.items()],
    goals={
        "money": ("g_money", "Goal {usd}"),
        "time": ("target_months", "Target month {value:g}"),
        "delay_months": (0, "On time"),
    },
    risk_rows={
        "fragile": "budget_overrun",
        "burn_over_budget": "budget_overrun",
        "go_live": "go_live",
        "time": "go_live",
        "delay_months": "go_live",
        "money": "money_goal",
        "money_goal": "money_goal",
        "beta": "beta",
        "load_test_env": "load_test_env",
    },  # fmt: skip
    preview_metrics={
        "money": (
            "Money to date",
            "Business value earned minus the delivery cost paid to the end of each month, USD; month 24 is the money criterion.",
        ),
        "progress": ("Work done", "Share of the work done by the end of each month."),
        "spend": ("Spend to date", "Delivery cost paid to the end of each month, USD."),
    },
    calendar_rows={
        "go_live": ("Go-live late", "Chance that the go-live misses its target month."),
        "budget_overrun": (
            "Spend above the project budget",
            "Chance that the spend to date passes the project budget (the monthly budget times the target months) by each month.",
        ),
        "money_goal": ("Money goal missed", "Chance that the money at month 24 is below the goal."),
        "beta": ("Beta late", "Chance that half of the work is not done by the beta milestone."),
        "load_test_env": (
            "Load-test environment late",
            "Chance that the load-test environment is not ready by its milestone.",
        ),
    },
    reference_presets=("requirements_change", "fixed_deadline"),
    fork_icons={"in_house": "building-2", "nearshore_tm": "globe", "offshore_fixed": "file-signature"},
    fork_descriptions={
        "in_house": "Hire and run an internal team of 8",
        "nearshore_tm": "Vistula Code Works, nearshore, time and materials, 10 people",
        "offshore_fixed": "Kaveri Tech Services, offshore, fixed price plus change requests, 16 people",
    },
    hypothesis_icons={
        "requirements_change": "repeat",
        "priorities_shift": "shuffle",
        "key_people_leave": "user-minus",
        "tight_budget": "wallet",
        "fixed_deadline": "calendar-clock",
        "vendor_rates_rise": "trending-up",
    },  # fmt: skip
    variable_blocks={v: b for b, (_label, names) in BLOCKS.items() for v in names},
    sections={
        "world": {
            "summary": "World hypotheses as layers: requirements change, key people leave, a tight budget and more."
        },
        "month-time": {
            "summary": "Monthly fans per fork and the risk calendar: when the budget, a milestone, the go-live or the money goal can fail."
        },
        "month-why": {
            "summary": "The chain per fork from team throughput to the go-live month and the business value, the drivers and the rules."
        },
        "day": {
            "summary": "Three example scenarios (bad, typical, good): daily progress, spend, defects and events."
        },
    },
    section_help={
        "month-time": "A preview: monthly paths drawn to end at the results of the model at month 24. The fan shows the median and the 25-75% and 10-90% ranges. The risk calendar shows the chance, by each month, that the spend passes the project budget, a milestone or the go-live is late, or the money goal is missed.",
        "month-why": "The chain shows how the team and the scope set the go-live: throughput and work give the build time; the build time, the load-test environment and the delivery risks give the go-live month; the value starts at go-live. Values are means over all scenarios, per fork. The drivers are the inputs that move the money most.",
        "day": "A preview: three example scenarios of a fork, from the bad, the typical and the good part of the scenarios. Each shows daily progress, spend and defects for the first 60 working days, and the events.",
    },
    kpi_help={
        "feasible": (
            "Feasible",
            "Share of scenarios where no check rule fails (for example: the monthly cost above the budget).",
        ),
        "limits": (
            "RAG limits",
            "From the baseline fork (the in-house team): green = its median; red = its worst 10%. Flags make a colour worse; a failed check makes it red.",
        ),
        "drivers": (
            "Main drivers",
            "Inputs that move the money most for this fork.",
            "The correlation of each uncertain input with the money at month 24 over the scenarios. A bar to the right: higher values give more money; to the left: they cost money.",
        ),
        "weekNode": (
            "Week node",
            "One week of one group of paths: work done, spend to date and defects, and a short summary.",
        ),
        "dayExample": (
            "Example future",
            "A scenario of the model from the bad (10%), typical (50%) or good (90%) part of the money at month 24. Preview data.",
        ),
        "dayEvents": (
            "Events",
            "What happens on each day of the example: decisions, vendor and client events, payments, milestones and defects. A link opens the record in the knowledge base.",
        ),
    },
    control_help={
        "eventButtons": (
            "Events",
            "Place a certain event (the client adds scope, a key person leaves) on the timeline, then move it to its month.",
        ),
    },
)
